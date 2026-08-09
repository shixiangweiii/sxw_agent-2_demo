"""★ 自研 Tool-Use 循环主体（对应 CC `query.ts:241 queryLoop()`）。

与 `agent_loop` 最本质的区别：**这里真的有一个 `while`**。模型调用、工具调度、
续推与终止判定全部由本文件拥有，不再借道任何 Agent 框架的流程引擎。

复刻自 CC 的关键不变量：
- 退出信号是"本轮模型有没有发起 tool_call"，**不是 `stop_reason`**
  （CC query.ts:553 明确注明 stop_reason 不可靠，Qwen 上同样成立）；
- 状态就是一个扁平 messages 数组，续推靠 `state = 新状态; continue`；
- 每个 continue 点带**命名 transition**，纯为可观测（`[LoopControl]` 日志）；
- 恢复优先于失败：上下文超长不是终止条件，而是"压缩后重来一轮"。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from agent.engine.loop_tools import (
    FORCE_SUMMARY_REMINDER,
    PLAN_CONTINUATION_REMINDER,
    TASK_PLAN_KEY,
    has_open_steps,
)
from agent.engine.native_loop import compact, executor
from agent.engine.native_loop.llm_client import (
    ContextOverflowError,
    NativeLlmClient,
    NativeLlmError,
    TextDelta,
    ToolCallReady,
    TurnEnd,
)
from agent.engine.native_loop.messages import (
    Msg,
    ToolCall,
    Usage,
    apply_tool_result_budget,
    clone,
    messages_after_boundary,
)
from agent.engine.native_loop.tools import ToolRegistry
from agent.llm.chat import AgentChatClient
from agent.stream.event_converters import StreamEvent
from common.obs import get_logger, log_kv
from common.trace import KIND_COMPACT, KIND_TURN, current_span, start_span

logger = get_logger("agent.native")

PLAN_TOOL = "update_task_plan"

# transition 名称：每个 continue / return 点都有一个，只为把"循环为什么又转了一圈"
# 变成可检索的日志。对应 CC `State.transition`。
T_NEXT_TURN = "next_turn"
T_FORCE_SUMMARY = "force_summary"
T_HARD_CAP = "hard_cap"
T_COMPLETED = "completed"
T_MODEL_ERROR = "model_error"
T_ABORTED = "aborted"
T_REACTIVE_COMPACT = "reactive_compact_retry"


@dataclass
class LoopConfig:
    max_iters: int                      # 软收尾轮次（到达即 force-summary 劝停）
    hard_cap: int                       # 硬熔断轮次（= max_iters + 余量）
    max_tool_concurrency: int
    streaming_tool_exec: bool           # 安全阀：关掉即"流完再统一跑工具"
    tool_result_max_chars: int
    context_window_tokens: int          # 上下文压缩：有效窗口
    compact_buffer_tokens: int          # 上下文压缩：预留 buffer（阈值 = 窗口 − buffer）
    compact_preserve_units: int         # 压缩后原样保留的尾部原子单元数
    # 反应式压缩最多尝试几次。CC 是单次守卫；这里允许配置，但默认同样是 1 次，
    # 目的是防"压缩 → 仍超长 → 再压缩"的死循环。
    max_reactive_compacts: int = 1


# 压缩失败后跳过多少轮再重试。用冷却而不是"永久关闭"：一次偶发失败
# （比如摘要模型抖动）不该让长会话在剩余时间里彻底失去压缩能力。
_COMPACT_COOLDOWN_TURNS = 3


@dataclass
class LoopState:
    """跨轮可变状态。每个 continue 点整体替换，避免散落的字段赋值。"""

    messages: list[Msg]
    iters: int = 0
    transition: Optional[str] = None
    # 上下文压缩守卫：反应式压缩次数上限 + 压缩失败后的冷却轮次
    attempted_reactive_compact: int = 0
    compact_failures: int = 0            # 仅用于可观测
    compact_cooldown: int = 0            # >0 时跳过主动压缩，每轮递减
    last_usage: Optional[Usage] = None
    tool_state: dict[str, Any] = field(default_factory=dict)


class NativeLoop:
    """一个请求一个实例（持有本轮的迭代计数与工具状态）。"""

    def __init__(
        self,
        *,
        client: NativeLlmClient,
        registry: ToolRegistry,
        system_instruction: str,
        config: LoopConfig,
        chat: Optional[AgentChatClient] = None,
        checkpoint: Callable[[LoopState, str], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._registry = registry
        self._system = system_instruction
        self._config = config
        # 摘要走轻量单轮补全客户端；为 None 时压缩整体禁用（降级为只做体积治理）。
        self._chat = chat
        self._checkpoint_hook = checkpoint
        self._invocation_id = f"invocation_{uuid.uuid4().hex}"
        # run() 结束后由调用方取回：完整历史 + 收口原因（transition 名）。
        # stop_reason 让嵌套场景（如 Claude SKILL 子 Runner）能区分
        # "正常收口" 与 "撞上熔断"，而不必去匹配错误文案。
        self.final_messages: list[Msg] = []
        self.stop_reason: str = ""
        # 每轮请求都会带上、但不在 state.messages 里的固定开销：system 指令 +
        # 全部工具的 JSON Schema。当前工具面下就有约 1.8k token，不计入会系统性低估
        # 上下文规模，让压缩阈值形同虚设。工具面启动后不变，故只算一次。
        self._fixed_overhead_chars = len(system_instruction) + len(
            json.dumps(registry.wire_declarations(), ensure_ascii=False),
        )

    async def run(
        self,
        messages: list[Msg],
        *,
        initial_state: LoopState | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """驱动循环，产出统一 StreamEvent 序列。结束时 ``final_messages`` 可取回完整历史。"""
        state = initial_state or LoopState(messages=messages)
        cfg = self._config
        log_kv(logger, logging.INFO, "NativeLoop", "start",
               max_iters=cfg.max_iters, hard_cap=cfg.hard_cap,
               streaming_tool_exec=cfg.streaming_tool_exec, tools=len(self._registry))

        # 若进程在完整 ToolCall batch 落盘后消失，先恢复缺失的
        # ToolResult；稳定 logical slot 会让 Broker 复用已提交结果。
        async for event in self._resume_pending_tools(state):
            yield event

        while True:
            state.iters += 1

            # ── 硬熔断：软收尾没劝住时的最后一道闸 ───────────────────────
            if state.iters > cfg.hard_cap:
                log_kv(logger, logging.ERROR, "LoopControl", "hard cap exceeded",
                       iter=state.iters, hard_cap=cfg.hard_cap, transition=T_HARD_CAP)
                for ev in self._fail(
                    state, T_HARD_CAP,
                    f"已达框架硬熔断上限（{cfg.hard_cap} 轮），本轮对话中止。",
                ):
                    yield ev
                return

            # ★ 一轮 = 一个 turn span。必须用 `with` 而不是手动 open/close：
            #   span 父子关系走 contextvar，而流式提前投递的工具是在本作用域里
            #   `asyncio.create_task` 出去的（context 在创建那一刻复制）——
            #   只有本轮 turn 正躺在 contextvar 里，那些工具 span 才会挂到正确的轮次下。
            #   这也是 native_loop 的 turn 边界**真实**、而 ADK 侧只能靠回调推断的原因。
            with start_span("native.turn", KIND_TURN, iter=state.iters) as turn_span:
                # ── 主动压缩：估算逼近窗口上限就先摘要，别等真的 413 ─────────
                await self._maybe_proactive_compact(state)
                # 持久化模型 I/O 前的边界；半个 stream 失败时从此重放。
                await self._checkpoint(state, "MODEL_REQUEST")

                request_messages = self._build_request(state)
                turn_span.set(request_messages=len(request_messages))

                # ── 调模型：边流边产 text，同时累积 tool_call ────────────────
                text_parts: list[str] = []
                ready_calls: list[ToolCall] = []
                early_tasks: list[tuple[ToolCall, asyncio.Task[executor.ToolOutcome]]] = []
                deferred_calls: list[ToolCall] = []
                early_allowed = cfg.streaming_tool_exec
                finish_reason: Optional[str] = None

                try:
                    async for item in self._client.stream(
                        messages=request_messages,
                        tools=self._registry.wire_declarations() or None,
                        allow_early_tool_dispatch=cfg.streaming_tool_exec,
                    ):
                        if isinstance(item, TextDelta):
                            text_parts.append(item.text)
                            yield StreamEvent("text", {"delta": item.text})
                        elif isinstance(item, ToolCallReady):
                            # Runtime identity is derived from the durable model activity slot,
                            # not from a provider-generated function_call_id.  A whole-attempt
                            # replay therefore lands on the same ToolExecution and mismatches
                            # fail closed in the Broker.
                            item.call.logical_key = (
                                f"native:turn:{state.iters - 1}:call:{len(ready_calls)}"
                            )
                            ready_calls.append(item.call)
                            for ev in self._call_events(item.call):
                                yield ev
                            # 流式工具执行：只要目前为止的调用全是并发安全的，就立刻开跑。
                            # 一旦出现非安全工具，之后的一律推迟到流结束后按批次串行执行——
                            # 这样恰好保住 CC `partitionToolCalls` 的"并发前缀 + 顺序其余"语义。
                            if early_allowed and self._is_concurrency_safe(item.call):
                                turn_span.incr("early_dispatched")
                                early_tasks.append((item.call, asyncio.create_task(
                                    self._execute(item.call, state),
                                )))
                            else:
                                early_allowed = False
                                deferred_calls.append(item.call)
                        elif isinstance(item, TurnEnd):
                            finish_reason = item.finish_reason
                            if item.usage is not None:
                                state.last_usage = item.usage
                except ContextOverflowError as exc:
                    # ★ 恢复优先于失败：上下文超长不是终止条件，而是"压缩后重来一轮"。
                    await self._cancel_tasks(early_tasks)
                    recovered = await self._reactive_compact(
                        state, already_emitted=bool(text_parts))
                    if recovered:
                        state.iters -= 1    # 这一轮没真正跑成，不计入软收尾预算
                        state.transition = T_REACTIVE_COMPACT
                        turn_span.set(transition=T_REACTIVE_COMPACT, recovered=True)
                        continue
                    log_kv(logger, logging.ERROR, "LoopControl",
                           "context overflow, unrecoverable",
                           iter=state.iters, transition=T_MODEL_ERROR)
                    turn_span.set(transition=T_MODEL_ERROR, failure="context_overflow")
                    for ev in self._fail(state, T_MODEL_ERROR, str(exc)):
                        yield ev
                    return
                except NativeLlmError as exc:
                    await self._cancel_tasks(early_tasks)
                    log_kv(logger, logging.ERROR, "LoopControl", "model error",
                           iter=state.iters, kind=exc.kind, transition=T_MODEL_ERROR)
                    turn_span.set(transition=T_MODEL_ERROR, failure="model_error",
                                  error_kind=exc.kind)
                    for ev in self._fail(state, T_MODEL_ERROR, str(exc)):
                        yield ev
                    return
                except BaseException:
                    # 取消 / GeneratorExit 兜底，必须排在具体异常之后。用 BaseException
                    # 是因为消费方 `aclose()` 抛来的 GeneratorExit 既不是 Exception
                    # 也不是 CancelledError，窄捕获会漏掉，让已提前投递的工具变成游离 task。
                    await self._cancel_tasks(early_tasks)
                    raise

                turn_span.set(text_chars=sum(len(t) for t in text_parts),
                              tool_calls=[c.name for c in ready_calls] or None,
                              finish_reason=finish_reason)

                # 模型本轮产出固化进历史：正文 + 工具调用同属一条 assistant 消息。
                state.messages.append(Msg(
                    role="assistant",
                    content="".join(text_parts) or None,
                    tool_calls=list(ready_calls) or None,
                ))

                # ── 唯一的退出判定 ──────────────────────────────────────────
                # 依据是"本轮有没有发起工具调用"，不看 finish_reason：
                # 后者在多数 OpenAI 兼容实现上并不可靠（CC 源码同注）。
                if not ready_calls:
                    await self._checkpoint(state, "COMPLETED")
                    log_kv(logger, logging.INFO, "LoopControl", "no tool calls, turn complete",
                           iter=state.iters, finish_reason=finish_reason,
                           transition=T_COMPLETED)
                    turn_span.set(transition=T_COMPLETED)
                    for ev in self._complete(state, finish_reason):
                        yield ev
                    return

                # Side-effect/UNKNOWN 工具必须等完整 ToolCall batch 落盘。
                try:
                    await self._checkpoint(state, "TOOL_BATCH_COMMITTED")
                except BaseException:
                    await self._cancel_tasks(early_tasks)
                    raise

                # ── 收集工具结果：先回收已提前开跑的，再按批次跑其余 ─────────
                try:
                    for call, task in early_tasks:
                        outcome = await task
                        for ev in self._result_events(outcome):
                            yield ev
                        state.messages.append(outcome.message)
                        await self._checkpoint(state, "TOOL_RESULT_COMMITTED")

                    async for outcome in executor.run_calls(
                        deferred_calls,
                        self._registry,
                        invocation_id=self._invocation_id,
                        state=state.tool_state,
                        max_concurrency=cfg.max_tool_concurrency,
                    ):
                        for ev in self._result_events(outcome):
                            yield ev
                        state.messages.append(outcome.message)
                        await self._checkpoint(state, "TOOL_RESULT_COMMITTED")
                except BaseException:
                    # 用 BaseException 而不是 CancelledError：消费方若走 `aclose()`，
                    # 生成器在 yield 处收到的是 GeneratorExit（它不是 Exception 的子类，
                    # 也不是 CancelledError），窄捕获会让提前投递的工具任务变成游离 task
                    # 继续跑——那可能是技能沙箱子进程或 skill-center 的 HTTP 调用。
                    # 取消时也必须补齐 tool_result 配对，否则这段历史下次进模型会被判 400。
                    await self._cancel_tasks(early_tasks)
                    self._fill_missing_results(state, ready_calls)
                    log_kv(logger, logging.WARNING, "LoopControl", "cancelled during tools",
                           iter=state.iters, transition=T_ABORTED)
                    turn_span.set(transition=T_ABORTED)
                    raise

                state.transition = T_NEXT_TURN
                await self._checkpoint(state, "NEXT_TURN")
                turn_span.set(transition=T_NEXT_TURN)
                log_kv(logger, logging.INFO, "LoopControl", "next turn",
                       iter=state.iters, tool_calls=len(ready_calls), transition=T_NEXT_TURN)

    # ── 收口 ───────────────────────────────────────────────────────────────
    # 引擎出口仅记录内部 stop reason。Run terminal 由 Coordinator 独占裁决；
    # 不再发 done，也不把生成器 EOF 当成对外完成协议。

    def _finish(self, state: LoopState, reason: str) -> None:
        state.transition = reason
        self.final_messages = state.messages
        self.stop_reason = reason

    def _fail(self, state: LoopState, reason: str, message: str) -> list[StreamEvent]:
        self._finish(state, reason)
        return [StreamEvent("error", {"message": message, "reason": reason})]

    def _complete(self, state: LoopState, finish_reason: Optional[str]) -> list[StreamEvent]:
        self._finish(state, T_COMPLETED)
        return []

    # ── 上下文压缩 ─────────────────────────────────────────────────────────

    async def _maybe_proactive_compact(self, state: LoopState) -> None:
        """估算上下文逼近窗口上限时先摘要，避免真的撞上 413。"""
        if self._chat is None:
            return
        if state.compact_cooldown > 0:
            state.compact_cooldown -= 1
            return
        decision = compact.decide(
            state.messages, state.last_usage,
            context_window_tokens=self._config.context_window_tokens,
            buffer_tokens=self._config.compact_buffer_tokens,
            fixed_overhead_chars=self._fixed_overhead_chars,
        )
        if not decision.should:
            return
        log_kv(logger, logging.INFO, "Compact", "threshold reached",
               tokens=decision.tokens, threshold=decision.threshold,
               # 如实标注：上游不返回 usage 时这是字符估算值，不是精确 token 计数。
               estimated=decision.estimated, trigger="proactive")
        with start_span("native.compact", KIND_COMPACT, trigger="proactive",
                        tokens=decision.tokens, threshold=decision.threshold,
                        estimated=decision.estimated,
                        messages_before=len(state.messages)) as span:
            compacted = await compact.compact(
                state.messages, self._chat,
                preserve_units=self._config.compact_preserve_units,
                trigger="proactive",
            )
            if compacted is None:
                span.set(ok=False).set_status("error")
                self._enter_compact_cooldown(state, "proactive")
                return
            span.set(ok=True, messages_after=len(compacted))
            self._adopt_compacted(state, compacted)

    async def _reactive_compact(self, state: LoopState, *, already_emitted: bool) -> bool:
        """模型报上下文超长后的恢复：压缩历史并让调用方重来一轮。

        ``already_emitted`` 是防重复输出的闸：正文一旦已经流给了前端，
        重来一轮会让用户看到两遍内容，此时宁可如实报错。
        （实践中超长在请求发出阶段就被判定，走不到这一支。）
        """
        if self._chat is None or already_emitted:
            return False
        if state.attempted_reactive_compact >= self._config.max_reactive_compacts:
            log_kv(logger, logging.WARNING, "Compact", "reactive budget exhausted",
                   attempts=state.attempted_reactive_compact)
            return False
        state.attempted_reactive_compact += 1
        with start_span("native.compact", KIND_COMPACT, trigger="reactive",
                        attempt=state.attempted_reactive_compact,
                        messages_before=len(state.messages)) as span:
            compacted = await compact.compact(
                state.messages, self._chat,
                preserve_units=self._config.compact_preserve_units,
                trigger="reactive",
            )
            if compacted is None:
                span.set(ok=False).set_status("error")
                self._enter_compact_cooldown(state, "reactive")
                return False
            span.set(ok=True, messages_after=len(compacted))
            self._adopt_compacted(state, compacted)
        log_kv(logger, logging.WARNING, "LoopControl", "recovered from context overflow, retrying",
               attempt=state.attempted_reactive_compact, transition=T_REACTIVE_COMPACT)
        return True

    @staticmethod
    def _adopt_compacted(state: LoopState, compacted: list[Msg]) -> None:
        """采用压缩结果。

        ★ 必须同时作废 ``last_usage``：它记的是**压缩前**那次请求的 prompt_tokens，
        而 `compact.estimate_tokens` 取 usage 与字符估算的较大者。压缩已经把字符数打下去了，
        旧 usage 却会把估算值顶回原位，导致下一轮立刻触发一次冗余的二次压缩
        （多花一次摘要调用、且摘要被二次摘要，早期信息经两轮有损压缩）。
        置空后由下一次模型调用的 TurnEnd 立刻填回真值。
        """
        state.messages = compacted
        state.last_usage = None

    @staticmethod
    def _enter_compact_cooldown(state: LoopState, trigger: str) -> None:
        state.compact_failures += 1
        state.compact_cooldown = _COMPACT_COOLDOWN_TURNS
        log_kv(logger, logging.WARNING, "Compact", "compaction failed, cooling down",
               trigger=trigger, failures=state.compact_failures,
               cooldown_turns=_COMPACT_COOLDOWN_TURNS)

    # ── 请求组装 ───────────────────────────────────────────────────────────

    def _build_request(self, state: LoopState) -> list[Msg]:
        """组装本次模型请求的消息视图。

        关键点：返回的是**副本**。体积治理与临时系统提醒都只作用于本次请求，
        不写回会话历史——否则这些提醒会跨轮累积，越滚越多。
        """
        live = clone(messages_after_boundary(state.messages))
        truncated = apply_tool_result_budget(live, self._config.tool_result_max_chars)
        if truncated:
            log_kv(logger, logging.INFO, "ToolResultBudget", "oversized results truncated",
                   count=truncated, max_chars=self._config.tool_result_max_chars)

        request = [Msg(role="system", content=self._system), *live]

        # 计划续推：模型上一轮登记的计划若还有未完成步骤，提醒它继续推进。
        # `iters > 1` 是为了避开刚登记计划的那一轮——那时提醒"继续推进"没有意义。
        # current_span() 此刻就是本轮的 turn span（_build_request 在 with 块内被调用）。
        # 字段名与 ADK 侧 LoopController 透出的那两个一致，归因规则才能三代通用。
        turn_span = current_span()
        plan = state.tool_state.get(TASK_PLAN_KEY)
        if state.iters > 1 and has_open_steps(plan):
            if turn_span is not None:
                turn_span.set(plan_continuation=True)
            request.append(Msg(role="user", content=PLAN_CONTINUATION_REMINDER))

        # force-summary 软收尾：到达业务轮次上限，用一条系统消息劝停。
        # 这是软控制——模型还需要至少再转一轮才能把最终答案写出来，
        # 所以硬熔断必须留出余量（见 LoopConfig.hard_cap）。
        if state.iters >= self._config.max_iters:
            if turn_span is not None:
                turn_span.set(forced_summary=True)
            log_kv(logger, logging.WARNING, "LoopControl", "max iters reached, force summary",
                   iter=state.iters, max=self._config.max_iters, transition=T_FORCE_SUMMARY)
            request.append(Msg(role="user", content=FORCE_SUMMARY_REMINDER))

        return request

    # ── 工具执行 ───────────────────────────────────────────────────────────

    async def _checkpoint(self, state: LoopState, phase: str) -> None:
        if self._checkpoint_hook is not None:
            await self._checkpoint_hook(state, phase)

    async def _resume_pending_tools(self, state: LoopState) -> AsyncIterator[StreamEvent]:
        """恢复最近一个已落盘 batch 中尚未配对的 ToolResult。"""
        assistant_index: int | None = None
        for index in range(len(state.messages) - 1, -1, -1):
            message = state.messages[index]
            if message.role == "assistant" and message.tool_calls:
                assistant_index = index
                break
            if message.role != "tool":
                break
        if assistant_index is None:
            return
        calls = state.messages[assistant_index].tool_calls or []
        answered = {
            message.tool_call_id
            for message in state.messages[assistant_index + 1:]
            if message.role == "tool"
        }
        pending = [call for call in calls if call.id not in answered]
        if not pending:
            return
        async for outcome in executor.run_calls(
            pending,
            self._registry,
            invocation_id=self._invocation_id,
            state=state.tool_state,
            max_concurrency=self._config.max_tool_concurrency,
        ):
            for event in self._result_events(outcome):
                yield event
            state.messages.append(outcome.message)
            await self._checkpoint(state, "TOOL_RESULT_COMMITTED")
        state.transition = T_NEXT_TURN
        await self._checkpoint(state, "NEXT_TURN")

    def _is_concurrency_safe(self, call: ToolCall) -> bool:
        spec = self._registry.get(call.name)
        return bool(spec and spec.concurrency_safe and not spec.exclusive_resources)

    async def _execute(self, call: ToolCall, state: LoopState) -> executor.ToolOutcome:
        return await executor.execute_one(
            call, self._registry,
            invocation_id=self._invocation_id, state=state.tool_state,
        )

    @staticmethod
    async def _cancel_tasks(
        tasks: list[tuple[ToolCall, asyncio.Task[executor.ToolOutcome]]],
    ) -> None:
        for _, task in tasks:
            task.cancel()
        for _, task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 清理阶段不再传播
                pass

    def _fill_missing_results(self, state: LoopState, calls: list[ToolCall]) -> None:
        """给还没有配对结果的 tool_call 补合成结果（CC 的 yieldMissingToolResultBlocks）。"""
        answered = {m.tool_call_id for m in state.messages if m.role == "tool"}
        for call in calls:
            if call.id in answered:
                continue
            state.messages.append(executor.cancelled_outcome(call).message)

    # ── 事件翻译 ───────────────────────────────────────────────────────────

    def _call_events(self, call: ToolCall) -> list[StreamEvent]:
        """tool_call → Engine projection; durable Broker facts remain authoritative."""
        if call.name == PLAN_TOOL:
            # The post-result native checkpoint atomically persists WorkingState
            # and MODEL_PLAN_UPDATED.  Do not publish request-local plan state.
            return []
        args, parse_error = executor.parse_arguments(call)
        broker_owned = self._registry.get(call.name) is not None and parse_error is None
        return [StreamEvent("tool_call", {
            "id": call.id,
            "name": call.name,
            "args": args or {},
        }, authority="broker" if broker_owned else "engine")]

    def _result_events(self, outcome: executor.ToolOutcome) -> list[StreamEvent]:
        # 计划工具的返回不给用户看：它的信息已经由 plan_step 表达过了。
        if outcome.call.name == PLAN_TOOL:
            return []
        _, parse_error = executor.parse_arguments(outcome.call)
        broker_owned = (
            self._registry.get(outcome.call.name) is not None
            and parse_error is None
        )
        return [StreamEvent("tool_result", {
            "id": outcome.call.id,
            "name": outcome.call.name,
            "response": executor.json_safe(outcome.response),
        }, authority="broker" if broker_owned else "engine")]
