"""NativeLoopEngine：把自研循环接到统一的 ReasoningEngine 端口上。

职责收得很窄——组装工具面、取/存会话历史、把 genai Content 转成内部消息、
把循环产出的 StreamEvent 透出去。真正的"循环"在 loop.py。

会话历史由 Canonical Runtime 编译后通过 ``RunContext`` 传入。
Native Runtime 不持有进程级语义历史。
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator

from agent.engine.base import ReasoningEngine, RunContext
from agent.engine.loop_tools import LOOP_INSTRUCTION, TASK_PLAN_KEY, resolve_sub_agent_engine
from agent.engine.loop_tools.task_plan_tool import update_task_plan
from agent.engine.loop_tools.tool_search_tool import build_deferred_tools, tool_search
from agent.engine.native_loop.llm_client import NativeLlmClient, get_shared_client
from agent.engine.native_loop.loop import T_COMPLETED, LoopConfig, LoopState, NativeLoop
from agent.engine.native_loop.messages import Msg, ToolCall, Usage, content_to_msg
from agent.engine.native_loop.sub_agent import build_researcher_tool
from agent.engine.native_loop.tools import ToolRegistry, build_registry
from agent.runtime.adapters.brokered_tools import broker_native_registry
from agent.runtime.domain.models import EngineOutcome, EngineOutcomeKind, WorkingState
from agent.skills.stream_merge import merge_runner_events
from agent.stream.event_converters import StreamEvent
from common.obs import get_logger, log_kv

if TYPE_CHECKING:
    from agent.context import AgentContext

logger = get_logger("agent.native")

# 与 agent_loop 对齐：硬熔断 = 业务软收尾轮次 + 该余量，给 force-summary 留生效窗口。
_HARD_CAP_MARGIN = 2


@dataclass
class NativeRuntime:
    client: NativeLlmClient
    registry: ToolRegistry


def _collect_tools(ctx: "AgentContext", *, run_engine: str) -> list[Any]:
    """组装本引擎的工具面。

    必须与 `agent_loop`（见 agent/engine/agent_loop/agent_loop_engine.py 的
    build_loop_agent）**逐个一致**，否则两代引擎的对比就变成了工具面之争。
    ctx.tools 是两代共享的部分；下面 4 行是 loop 引擎专属的。
    """
    tools: list[Any] = list(ctx.tools)                     # 内置 + 检索 + 技能 + Claude SKILL + A2A
    tools.append(update_task_plan)                         # 计划即工具
    tools.append(tool_search)                              # 动态工具发现
    tools.extend(build_deferred_tools())                   # 延迟工具：translate / text_stats

    # 子代理委派：两种实现同名（researcher）、同描述、同参数（request），
    # 只是内核不同——ADK 版经 adk_bridge 自建子 Runner 执行。
    sub_engine = resolve_sub_agent_engine(ctx.settings.sub_agent_engine, run_engine)
    if sub_engine == "adk":
        from agent.engine.loop_tools.sub_agent_tool import build_sub_agent_tool  # noqa: PLC0415
        tools.append(build_sub_agent_tool(ctx.llm))
    else:
        tools.append(build_researcher_tool(ctx.chat))
    log_kv(logger, logging.INFO, "NativeLoop", "sub agent engine resolved",
           configured=ctx.settings.sub_agent_engine, resolved=sub_engine)
    return tools


class NativeLoopEngine(ReasoningEngine):
    def __init__(self, ctx: "AgentContext", *, engine: str) -> None:
        self._ctx = ctx
        self._runtime = NativeRuntime(
            client=get_shared_client(ctx.settings),
            registry=build_registry(_collect_tools(ctx, run_engine=engine)),
        )

    async def run_stream(self, rc: RunContext) -> AsyncIterator[StreamEvent]:
        runtime = self._runtime
        settings = rc.settings
        hard_cap = settings.max_loop_iters + _HARD_CAP_MARGIN

        # 历史是 Runtime 已提交 USER/ASSISTANT message 的临时投影。
        # 失败 partial delta 不在投影中，进程重启后仍可完整编译。
        messages = [content_to_msg(item) for item in rc.canonical_history]
        messages.append(content_to_msg(rc.user_message))
        initial_state, restored_phase = _restore_state(rc, messages)

        # Terminal commit may have lost the race with a process kill after the
        # COMPLETED checkpoint.  Seed the final semantic message without emitting
        # duplicate deltas, then let the adapter return its explicit outcome.
        if restored_phase == "COMPLETED" and initial_state.messages:
            final = initial_state.messages[-1]
            if final.role == "assistant" and not final.tool_calls:
                seed = getattr(rc.runtime_io, "seed_assistant_text", None)
                if seed is not None:
                    seed(str(final.content or ""))
                rc.engine_outcome = EngineOutcome(kind=EngineOutcomeKind.COMPLETED)
                return

        checkpoint_revision = rc.engine_checkpoint.revision if rc.engine_checkpoint else 0

        async def persist(state: LoopState, phase: str) -> None:
            nonlocal checkpoint_revision
            if rc.runtime_io is None:
                return
            plan = state.tool_state.get(TASK_PLAN_KEY)
            steps = plan.get("steps", []) if isinstance(plan, dict) else []
            current = plan.get("current", 1) if isinstance(plan, dict) else 1
            model_plan = [
                {
                    "step": index + 1,
                    "title": str(title),
                    "status": "done" if index + 1 < current else (
                        "running" if index + 1 == current else "planned"
                    ),
                }
                for index, title in enumerate(steps)
            ]
            saved = await rc.runtime_io.checkpoint(
                WorkingState(
                    goal=_user_text(rc),
                    model_plan=model_plan,
                    release_fingerprint=rc.release_fingerprint,
                ),
                expected_revision=checkpoint_revision,
                engine_state=_serialize_state(state, phase),
            )
            checkpoint_revision = saved.revision

        loop = NativeLoop(
            client=runtime.client,
            registry=broker_native_registry(runtime.registry, rc),
            system_instruction=LOOP_INSTRUCTION,
            # 摘要压缩复用已有的轻量单轮补全客户端（无需流式、无需工具）。
            chat=self._ctx.chat,
            checkpoint=persist,
            config=LoopConfig(
                max_iters=settings.max_loop_iters,
                hard_cap=hard_cap,
                max_tool_concurrency=settings.native_max_tool_concurrency,
                streaming_tool_exec=settings.native_streaming_tool_exec,
                tool_result_max_chars=settings.native_tool_result_max_chars,
                context_window_tokens=settings.context_window_tokens,
                compact_buffer_tokens=settings.compact_buffer_tokens,
                compact_preserve_units=settings.compact_preserve_units,
            ),
        )
        log_kv(logger, logging.INFO, "NativeLoop", "request",
               user=rc.user_id, session=rc.session_id,
               history=len(messages), max_iters=settings.max_loop_iters, hard_cap=hard_cap)

        # merge_runner_events 与 ADK 无关（签名是 AsyncIterator + 转换器），
        # 这里复用它来并发 drain 技能 UI 队列：技能/沙箱在一次工具调用内部推进来的
        # 展示帧因此能实时穿插在 text / tool_call 之间，而不是等工具整体返回才一次性出现。
        async for event in merge_runner_events(
            loop.run(messages, initial_state=initial_state), lambda e: [e],
        ):
            yield event

        if loop.stop_reason == T_COMPLETED:
            rc.engine_outcome = EngineOutcome(kind=EngineOutcomeKind.COMPLETED)
        else:
            # NativeLoop records a deterministic stop reason before emitting
            # its diagnostic error draft.  Keep that diagnostic non-authoritative
            # while returning an explicit terminal outcome to the Coordinator.
            rc.engine_outcome = EngineOutcome(
                kind=EngineOutcomeKind.TERMINAL_FAILURE,
                error_code=loop.stop_reason.upper() or "NATIVE_LOOP_ABORTED",
                message=f"native loop stopped: {loop.stop_reason or 'unknown'}",
            )

        # 不在本地写回历史；Coordinator 只会在成功 terminal 事务中提交
        # ASSISTANT_MESSAGE_COMMITTED，它才是下一轮的语义历史。


def _user_text(rc: RunContext) -> str:
    return " ".join(
        part.text for part in (rc.user_message.parts or []) if getattr(part, "text", None)
    ).strip()


def _serialize_state(state: LoopState, phase: str) -> dict[str, Any]:
    def message_payload(message: Msg) -> dict[str, Any]:
        content: Any = message.content
        if isinstance(content, list) and any(
            isinstance(item, dict) and item.get("type") == "image_url" for item in content
        ):
            # Binary input remains authoritative in Artifact CAS; never copy a
            # base64 image into runtime.db checkpoints.
            content = {"$runtime_current_input": True}
        rematerialize = (
            message.role == "tool"
            and message.name == "read_artifact"
            and len(str(content)) > 8 * 1024
        )
        if rematerialize:
            content = {"$runtime_artifact_result": True}
        return {
            "role": message.role,
            "content": content,
            "tool_calls": [asdict(call) for call in message.tool_calls or []],
            "tool_call_id": message.tool_call_id,
            "name": message.name,
            "is_error": message.is_error,
            "kind": message.kind,
            # The source Artifact + ToolExecution are already durable.  Omitting
            # a large materialized slice makes recovery replay that committed
            # read instead of copying the bytes into checkpoint JSON.
            "$rematerialize_tool_result": rematerialize,
        }

    return {
        "contract": "native-kernel-v1",
        "phase": phase,
        "iters": state.iters,
        "transition": state.transition,
        "attempted_reactive_compact": state.attempted_reactive_compact,
        "compact_failures": state.compact_failures,
        "compact_cooldown": state.compact_cooldown,
        "last_usage": asdict(state.last_usage) if state.last_usage else None,
        "tool_state": state.tool_state,
        "messages": [message_payload(message) for message in state.messages],
    }


def _restore_state(
    rc: RunContext, fallback_messages: list[Msg],
) -> tuple[LoopState, str | None]:
    checkpoint = rc.engine_checkpoint
    raw = checkpoint.engine_state if checkpoint is not None else None
    if not isinstance(raw, dict) or raw.get("contract") != "native-kernel-v1":
        return LoopState(messages=fallback_messages), None

    current_input = content_to_msg(rc.user_message).content
    restored: list[Msg] = []
    for item in raw.get("messages") or []:
        if item.get("$rematerialize_tool_result"):
            continue
        content = item.get("content")
        if isinstance(content, dict) and content.get("$runtime_current_input"):
            content = current_input
        restored.append(Msg(
            role=str(item.get("role", "user")),
            content=content,
            tool_calls=[ToolCall(**call) for call in item.get("tool_calls") or []] or None,
            tool_call_id=item.get("tool_call_id"),
            name=item.get("name"),
            is_error=bool(item.get("is_error", False)),
            kind=str(item.get("kind", "normal")),
        ))
    usage = Usage(**raw["last_usage"]) if isinstance(raw.get("last_usage"), dict) else None
    state = LoopState(
        messages=restored or fallback_messages,
        iters=int(raw.get("iters", 0)),
        transition=raw.get("transition"),
        attempted_reactive_compact=int(raw.get("attempted_reactive_compact", 0)),
        compact_failures=int(raw.get("compact_failures", 0)),
        compact_cooldown=int(raw.get("compact_cooldown", 0)),
        last_usage=usage,
        tool_state=dict(raw.get("tool_state") or {}),
    )
    phase = str(raw.get("phase") or "")
    if phase == "MODEL_REQUEST":
        # Re-run the interrupted model slot with identical turn/call ordinals.
        state.iters = max(0, state.iters - 1)
    return state, phase or None
