"""AgentInvocationPlugin：ADK BasePlugin。

聚合五件生产加固（对应原项目 agent_invocation_plugin）：
- before_tool_callback → ToolArgsGuard：解析 sentinel 在真实工具分发前短路；
- on_tool_error_callback → ToolErrorFeedback：工具异常封装为 function_response 喂回，不中断 turn；
- before_model_callback → 委托 LoopController 做续推 / 预算 / force-summary；
- before/after_tool_callback → 工具调用可观测；
- **结构化轨迹埋点**（turn / llm / tool span）。

★ 轨迹埋点为什么落在这里：本插件**同时被 agent_loop 与 plan_execute 挂载**
（见 agent_loop_engine.py 与 plan_execute/execution_planner.py），所以一处埋点让
两代 ADK 引擎拿到与自研 native_loop 近乎对等的轨迹粒度。可观测性不对等会直接污染
引擎对比——那是"谁埋点更深"之争，不是"循环归谁驱动"之争。

⚠️ **诚实边界**：ADK 的插件回调是一串**离散的点**，turn 的边界只能靠
"下一次 before_model / run 结束"推断，因此 `adk.turn` 的 duration 略有失真；
native_loop 的 turn span 是循环体内真实的一圈。对比时间时须知道这个差别。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from google.adk.plugins.base_plugin import BasePlugin

from agent.engine.agent_loop.loop_processor import LoopController
from agent.plugins.tool_args_guard_plugin import build_tool_args_parse_error_response
from common.obs import get_logger, log_kv
from common.trace import (
    KIND_LLM,
    KIND_TOOL,
    KIND_TURN,
    STATUS_ERROR,
    close_span,
    open_span,
    pop_span,
    push_span,
)

if TYPE_CHECKING:
    from agent.runtime.adapters.brokered_tools import AdkToolBatch

logger = get_logger("agent.plugin")


class AgentInvocationPlugin(BasePlugin):
    def __init__(
        self,
        controller: Optional[LoopController] = None,
        *,
        tool_batch: "AdkToolBatch | None" = None,
    ) -> None:
        super().__init__(name="agent_invocation")
        # controller=None：仅启用 ToolErrorFeedback / 可观测（供 Plan-Execute 复用，
        # 不引入 Agent-Loop 的续推 / force-summary 语义）。
        self._ctrl = controller
        self._tool_batch = tool_batch
        # 轨迹状态。插件实例是**每请求一份**（两个引擎都在 run_stream/execute 里现构造），
        # 所以这些字段不会跨请求串味。
        self._turn_span: Any = None
        self._llm_span: Any = None
        self._turn_index = 0
        # 工具可以并行执行，故按 function_call_id 配对，不能用单个字段。
        self._tool_spans: dict[str, Any] = {}
        self._tool_tokens: dict[str, Any] = {}

    # ── 模型调用 ───────────────────────────────────────────────────────────

    # ADK 在每次调模型前回调这里（每轮循环一次），此时 llm_request 已组装好但还没发出去，
    # 是唯一能"改写本次请求"的时机——续推提醒、历史裁剪、force-summary 都必须在这里做。
    async def before_model_callback(self, *, callback_context: Any, llm_request: Any) -> None:
        if self._ctrl is not None:
            self._ctrl.before_model(callback_context, llm_request)
        # 埋点放在 controller 之后：这样记下的 contents 是**模型真正看到的那一份**，
        # 已经含续推提醒 / force-summary / 预算裁剪的结果。
        # 「为什么这一轮没去检索」只能靠这份输入回答——它是本模块最核心的诊断信号。
        self._begin_turn(llm_request)
        return None

    async def after_model_callback(self, *, callback_context: Any, llm_response: Any) -> None:
        # ⚠️ 本回调在 ADK 的流式循环**内部**触发（base_llm_flow.py 的
        # `async for llm_response in agen`），SSE 模式下每个增量块都会来一次。
        # 不过滤就会一轮产出几十个 llm span。收口只认"聚合响应"：
        # partial 为假、或带 usage —— 两者任一成立即本轮模型输出结束。
        if getattr(llm_response, "partial", False):
            return None
        if self._tool_batch is not None:
            # ADK 2.6.2 awaits this aggregate callback before scheduling any
            # function task.  The callback therefore forms the durable batch
            # boundary: flush text, atomically PREPARE every call, then return.
            await self._tool_batch.prepare_model_response(
                llm_response,
                turn_ordinal=max(0, self._turn_index - 1),
            )
        usage = getattr(llm_response, "usage_metadata", None)
        self._end_llm_span(llm_response, usage)
        return None

    async def on_model_error_callback(
        self, *, callback_context: Any, llm_request: Any, error: Exception,
    ) -> None:
        close_span(self._llm_span, STATUS_ERROR,
                   error_type=type(error).__name__, error=str(error)[:500])
        self._llm_span = None
        return None

    # ── 工具调用 ───────────────────────────────────────────────────────────

    # ADK 在真正执行工具函数之前回调这里。
    # 返回 None = 放行；返回 dict = **短路**，该 dict 直接作为工具结果，真实工具不会被执行。
    async def before_tool_callback(
        self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any,
    ) -> Optional[dict[str, Any]]:
        name = getattr(tool, "name", "?")
        key = self._call_key(tool_context)
        span = open_span(f"tool.{name}", KIND_TOOL, parent=self._turn_span, tool=name)
        span.set_payload("args", tool_args)
        self._tool_spans[key] = span
        # 把工具 span 设为当前，让工具函数内部新建的 span（典型：knowledge_search 的
        # retrieval span）挂到它下面，而不是浮到 root。这样 ADK 侧与 native_loop 侧的
        # 树形完全同构——人读两代轨迹时才能直接对照。
        # ADK 的 before/after_tool 在同一个协程里（functions.py 的 Step 1 与 Step 4），
        # 并行工具各自一个 task，故 push/pop 天然同 context 配对。
        self._tool_tokens[key] = push_span(span)

        # 三类工具失败中的第一类：模型生成的参数根本不是合法对象（顶层数组/标量/坏 JSON）。
        # 这类参数在更上游已被 LiteLlm 层换成 sentinel（见 llm/tool_args_normalizer.py），
        # 这里识别到就直接返回结构化错误，避免拿着垃圾参数去调真实工具。
        parse_error = build_tool_args_parse_error_response(tool, tool_args)
        if parse_error is not None:
            # 短路不影响配对：ADK 的 after_tool_callback 是无条件执行的
            # （functions.py Step 4 在 Step 3 之外），span 仍会在那里收口。
            span.set(short_circuited=True, failure="args_parse_error")
            return parse_error
        log_kv(logger, logging.INFO, "ToolCall", "invoke", tool=name)
        return None

    async def after_tool_callback(
        self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any, result: Any,
    ) -> Optional[dict[str, Any]]:
        name = getattr(tool, "name", "?")
        key = self._call_key(tool_context)
        pop_span(self._tool_tokens.pop(key, None))
        span = self._tool_spans.pop(key, None)
        if span is not None:
            span.set_payload("result", result)
            # 已在 on_tool_error 标过 error 的不要覆盖回 ok。
            close_span(span, getattr(span, "status", "ok"))
        log_kv(logger, logging.INFO, "ToolCall", "done", tool=name)
        return None

    # ★ 整个可靠性设计里最关键的一个回调：工具抛出未捕获异常时被调用。
    # 返回一个 dict，ADK 就会把它当作正常的 function_response 回灌给模型，
    # 于是循环可以继续转下去；如果不实现这个回调，异常会一路上抛、直接打断整轮对话。
    # 这就是"三类工具失败都反馈给模型续推，不中断 turn"里的第三类（框架级异常）。
    async def on_tool_error_callback(
        self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any, error: Exception,
    ) -> Optional[dict[str, Any]]:
        # 只打标记不收口：ADK 会继续走到 after_tool_callback，由那里统一关闭，
        # 避免同一个 span 被 emit 两次。
        span = self._tool_spans.get(self._call_key(tool_context))
        if span is not None:
            span.set_status(STATUS_ERROR)
            span.set(failure="tool_raised", error_type=type(error).__name__,
                     error=str(error)[:500])
        log_kv(logger, logging.WARNING, "ToolErrorFeedback", "tool raised, feeding back",
               tool=getattr(tool, "name", "?"), error=type(error).__name__)
        return {
            "error": f"{type(error).__name__}: {error}",
            "hint": "工具执行失败，请根据错误调整参数后重试，或改用其它方式；不要中断对话。",
        }

    # ── 收口 ───────────────────────────────────────────────────────────────

    async def after_run_callback(self, *, invocation_context: Any) -> None:
        """兜底关闭悬空 span。

        必需而非可选：模型流被取消、或最后一轮没等到聚合响应时，
        turn/llm span 不会有自然的收口点，缺了这里就会整段丢失。
        """
        self._end_llm_span(None, None)
        close_span(self._turn_span)
        self._turn_span = None
        for key, span in self._tool_spans.items():
            pop_span(self._tool_tokens.pop(key, None))
            close_span(span, "cancelled")
        self._tool_spans.clear()
        self._tool_tokens.clear()
        return None

    # ── 内部 ───────────────────────────────────────────────────────────────

    @staticmethod
    def _call_key(tool_context: Any) -> str:
        return str(getattr(tool_context, "function_call_id", None) or id(tool_context))

    def _begin_turn(self, llm_request: Any) -> None:
        # 上一轮的 span 在这里收口——这正是"turn 边界靠推断"的由来。
        self._end_llm_span(None, None)
        close_span(self._turn_span)

        self._turn_index += 1
        self._turn_span = open_span("adk.turn", KIND_TURN, iter=self._turn_index)
        # 循环控制信号：与 native_loop 的 turn span 用同名字段，
        # 让"为什么收尾"这类归因规则对三代引擎通用。
        if self._ctrl is not None:
            self._turn_span.set(forced_summary=self._ctrl.forced_summary or None,
                                plan_continuation=self._ctrl.plan_continuation or None)

        config = getattr(llm_request, "config", None)
        tools = sorted((getattr(llm_request, "tools_dict", None) or {}).keys())
        self._llm_span = open_span(
            "adk.llm", KIND_LLM, parent=self._turn_span,
            model=getattr(llm_request, "model", None),
            tool_count=len(tools),
            # 只记工具**名字**不记完整 schema：schema 每轮都一样（工具面启动后不变），
            # 逐轮全量落盘就是纯重复；而路由分析真正需要的是"模型这轮能选哪些工具"。
            tools=tools or None,
        )
        # ★ G2 的落点：模型这一轮真正看到的输入。
        self._llm_span.set_payload("contents", getattr(llm_request, "contents", None))
        if config is not None:
            self._llm_span.set_payload(
                "system_instruction", getattr(config, "system_instruction", None),
            )

    def _end_llm_span(self, llm_response: Any, usage: Any) -> None:
        span = self._llm_span
        if span is None:
            return
        self._llm_span = None
        attrs: dict[str, Any] = {}
        if llm_response is not None:
            attrs["finish_reason"] = _as_text(getattr(llm_response, "finish_reason", None))
            attrs["error_code"] = _as_text(getattr(llm_response, "error_code", None))
            text, calls = _summarize_content(getattr(llm_response, "content", None))
            attrs["text_chars"] = len(text)
            attrs["function_calls"] = calls or None
            if text:
                span.set_payload("text", text)
        if usage is not None:
            attrs["prompt_tokens"] = getattr(usage, "prompt_token_count", None)
            attrs["completion_tokens"] = getattr(usage, "candidates_token_count", None)
            attrs["total_tokens"] = getattr(usage, "total_token_count", None)
        close_span(span, getattr(span, "status", "ok"), **attrs)


def _as_text(value: Any) -> Optional[str]:
    """枚举 / 对象统一成可读字符串（finish_reason 在 ADK 里是枚举）。"""
    if value is None:
        return None
    return getattr(value, "name", None) or getattr(value, "value", None) or str(value)


def _summarize_content(content: Any) -> tuple[str, list[str]]:
    """从模型响应里取正文与本轮发起的工具名（路由分析的原料）。"""
    texts: list[str] = []
    calls: list[str] = []
    for part in getattr(content, "parts", None) or []:
        text = getattr(part, "text", None)
        if text:
            texts.append(text)
        fc = getattr(part, "function_call", None)
        if fc is not None:
            calls.append(getattr(fc, "name", "?"))
    return "".join(texts), calls
