"""工具执行：把模型发起的 tool_call 变成配对的 tool_result。

本模块守住 CC 循环里最硬的一条不变量（`toolExecution.ts`）：
**每一个 tool_call 都必须产出配对的 tool_result，任何异常都不许逃逸出去。**
少一条配对，下一轮请求就会被上游直接判 400；抛出异常，整轮对话就断了。

四条合成路径（对应 CC 的 `runToolUse` 四个分支）：
1. 工具不存在        → is_error 结果，告诉模型换一个工具；
2. 参数不是合法对象  → is_error 结果，让模型自己重新生成参数；
3. 工具抛出异常      → is_error 结果，喂回错误信息让模型改参数或换路（不中断 turn）；
4. 请求被取消        → 合成取消结果，保证悬空的 tool_call 也有配对。

错误措辞刻意与 ADK 侧 ``AgentInvocationPlugin.on_tool_error_callback`` /
``build_tool_args_parse_error_response`` 保持一致，让两代引擎喂给模型的
错误词汇相同——否则模型的恢复行为会因引擎而异，对比就不干净了。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

from agent.engine.native_loop.messages import Msg, ToolCall
from agent.engine.native_loop.tools import (
    NativeToolContext,
    ToolRegistry,
    ToolSpec,
    call_tool,
)
from common.obs import get_logger, log_kv
from common.trace import KIND_TOOL, start_span

logger = get_logger("agent.native")

_TOOL_ERROR_HINT = "工具执行失败，请根据错误调整参数后重试，或改用其它方式；不要中断对话。"
_ARGS_ERROR = "ToolArgumentsParseError: 模型生成的工具参数不是可执行的对象。"
_ARGS_ERROR_HINT = "请按工具声明重新生成完整的对象参数后重试；不要中断对话。"
_CANCELLED = "调用已被取消。"


@dataclass
class ToolOutcome:
    """一次工具调用的最终产物：喂回模型的消息 + 给前端的事件载荷。"""

    call: ToolCall
    message: Msg                    # role=tool，进入下一轮模型请求
    response: Any                   # 原始对象，供 SSE tool_result 事件与 citation 识别
    ok: bool


def _result_message(call: ToolCall, response: Any, *, ok: bool) -> Msg:
    """把工具返回值序列化进 role=tool 消息。

    模型侧只能读字符串，所以统一 JSON 序列化；``response`` 原始对象仍单独保留，
    前端和 citation_injector 需要结构化字段（如 ``response.hits``）。
    """
    return Msg(
        role="tool",
        content=_to_text(response),
        tool_call_id=call.id,
        name=call.name,
        is_error=not ok,
    )


def _to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def json_safe(value: Any) -> Any:
    """SSE 载荷必须可 JSON 序列化，不行就退化成字符串（与 event_converters 一致）。"""
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        return str(value)


def _error_outcome(call: ToolCall, error: str, hint: str) -> ToolOutcome:
    response = {"error": error, "hint": hint}
    return ToolOutcome(call=call, message=_result_message(call, response, ok=False),
                       response=response, ok=False)


def parse_arguments(call: ToolCall) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """解析 tool_call 参数。返回 (参数, 错误原因)，二者恰有一个非 None。

    参数解析在自研循环里归我们自己——这正是不再需要 ADK 那套
    ``tool_args_normalizer`` monkeypatch 的原因：解析失败不是异常，
    而是一条可以喂回模型让它自己改的普通信息。
    """
    raw = (call.arguments or "").strip()
    if not raw:
        return {}, None                      # 无参工具：模型给空串是正常的
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        return None, type(exc).__name__
    if not isinstance(parsed, dict):
        # 顶层数组/标量无法猜出模型本意，一律退回让模型重新生成。
        return None, "NonObjectToolArguments"
    return parsed, None


async def execute_one(
    call: ToolCall,
    registry: ToolRegistry,
    *,
    invocation_id: str,
    state: dict[str, Any],
) -> ToolOutcome:
    """执行单个工具调用。**本函数永不抛异常**（CancelledError 除外，由上层处理）。"""
    # span 字段与 ADK 侧 `tool.<name>` 逐个对齐（tool / failure / error_type），
    # 否则评测的失败归因规则得为两代引擎各写一套。
    with start_span(f"tool.{call.name}", KIND_TOOL,
                    tool=call.name, call_id=call.id or None) as span:
        spec = registry.get(call.name)
        if spec is None:
            log_kv(logger, logging.WARNING, "ToolCall", "no such tool", tool=call.name)
            span.set(failure="no_such_tool").set_status("error")
            return _error_outcome(
                call,
                f"NoSuchTool: 不存在名为 {call.name} 的工具。",
                "请从可用工具列表中重新选择；不要中断对话。",
            )

        args, parse_error = parse_arguments(call)
        if args is None:
            log_kv(logger, logging.WARNING, "ToolArgumentsParseError",
                   "invalid arguments blocked before dispatch",
                   tool=call.name, error=parse_error)
            span.set(failure="args_parse_error", error_type=parse_error).set_status("error")
            span.set_payload("raw_arguments", call.arguments)
            return _error_outcome(call, _ARGS_ERROR, _ARGS_ERROR_HINT)

        span.set(concurrency_safe=spec.concurrency_safe)
        span.set_payload("args", args)
        ctx = NativeToolContext(
            function_call_id=call.id or f"call_{uuid.uuid4().hex}",
            invocation_id=invocation_id,
            state=state,
        )
        log_kv(logger, logging.INFO, "ToolCall", "invoke", tool=call.name, call_id=call.id)
        try:
            response = await call_tool(spec, args, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 这正是"工具异常喂回、不中断 turn"的落点
            log_kv(logger, logging.WARNING, "ToolErrorFeedback", "tool raised, feeding back",
                   tool=call.name, error=type(exc).__name__)
            span.set(failure="tool_raised", error_type=type(exc).__name__,
                     error=str(exc)[:500]).set_status("error")
            return _error_outcome(call, f"{type(exc).__name__}: {exc}", _TOOL_ERROR_HINT)

        log_kv(logger, logging.INFO, "ToolCall", "done", tool=call.name, call_id=call.id)
        span.set_payload("result", response)
        return ToolOutcome(call=call, message=_result_message(call, response, ok=True),
                           response=response, ok=True)


def cancelled_outcome(call: ToolCall) -> ToolOutcome:
    """给未及执行的 tool_call 补一个取消结果，保证配对完整。"""
    response = {"error": _CANCELLED, "cancelled": True}
    return ToolOutcome(call=call, message=_result_message(call, response, ok=False),
                       response=response, ok=False)


# ── 分批调度 ───────────────────────────────────────────────────────────────
# 对应 CC 的 `partitionToolCalls`：连续的只读工具并成一个并发批次，
# 其余每个各自成一个串行批次。批次之间严格保序——并发只发生在批次内部，
# 所以"先查资料再写状态"这种顺序依赖不会被打乱。

@dataclass
class Batch:
    concurrent: bool
    calls: list[ToolCall]


def partition(calls: list[ToolCall], registry: ToolRegistry) -> list[Batch]:
    batches: list[Batch] = []
    for call in calls:
        spec: Optional[ToolSpec] = registry.get(call.name)
        # 工具不存在时按"不安全"处理：它会走合成错误路径，不该混进并发批次。
        safe = bool(spec and spec.concurrency_safe and not spec.exclusive_resources)
        if safe and batches and batches[-1].concurrent:
            batches[-1].calls.append(call)
        else:
            batches.append(Batch(concurrent=safe, calls=[call]))
    return batches


async def run_calls(
    calls: list[ToolCall],
    registry: ToolRegistry,
    *,
    invocation_id: str,
    state: dict[str, Any],
    max_concurrency: int,
) -> AsyncIterator[ToolOutcome]:
    """按批次执行工具调用，边完成边产出。"""
    for batch in partition(calls, registry):
        if not batch.concurrent or len(batch.calls) == 1:
            for call in batch.calls:
                yield await execute_one(call, registry, invocation_id=invocation_id, state=state)
            continue

        semaphore = asyncio.Semaphore(max(1, max_concurrency))

        async def guarded(call: ToolCall) -> ToolOutcome:
            async with semaphore:
                return await execute_one(
                    call, registry, invocation_id=invocation_id, state=state,
                )

        log_kv(logger, logging.INFO, "ToolCall", "concurrent batch",
               count=len(batch.calls), tools=[c.name for c in batch.calls])
        tasks = [asyncio.create_task(guarded(call)) for call in batch.calls]
        try:
            # 按调用顺序回收结果：并发只为省时间，产出顺序仍与模型给的顺序一致，
            # 这样前端看到的 tool_result 次序是稳定可预期的。
            for task in tasks:
                yield await task
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            raise
