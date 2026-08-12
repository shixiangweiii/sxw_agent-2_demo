"""★ 自研 Tool-Use 循环主体（对应 CC `query.ts:241 queryLoop()`）。

与 `agent_loop` 最本质的区别：**这里真的有一个 `while`**。模型调用、工具调度、
续推与终止判定全部由本文件拥有，不再借道任何 Agent 框架的流程引擎。

借鉴 CC 的扁平循环结构，同时在 Runtime 生产边界收紧协议：
- 只接受 provider 显式 stop / tool_calls finish marker，并核对它与本轮完整
  ToolCall batch 一致；静默 EOF、矛盾 marker 均失败关闭；
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
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, Optional

from jsonschema import Draft202012Validator

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

EarlyToolDispatchMode = Literal[
    "off",
    "experimental_heuristic",
    "provider_block_complete",
]


@dataclass
class LoopConfig:
    max_iters: int                      # 软收尾轮次（到达即 force-summary 劝停）
    hard_cap: int                       # 硬熔断轮次（= max_iters + 余量）
    max_tool_concurrency: int
    early_tool_dispatch: EarlyToolDispatchMode
    tool_result_max_chars: int
    context_window_tokens: int          # 上下文压缩：有效窗口
    compact_buffer_tokens: int          # 上下文压缩：预留 buffer（阈值 = 窗口 − buffer）
    compact_preserve_units: int         # 压缩后原样保留的尾部原子单元数
    max_tool_calls_per_turn: int = 64
    max_tool_calls_per_run: int = 256
    max_tool_argument_bytes: int = 64 * 1024
    max_tool_batch_argument_bytes: int = 256 * 1024
    max_model_output_bytes: int = 1024 * 1024
    # 反应式压缩最多尝试几次。CC 是单次守卫；这里允许配置，但默认同样是 1 次，
    # 目的是防"压缩 → 仍超长 → 再压缩"的死循环。
    max_reactive_compacts: int = 1


# 压缩失败后跳过多少轮再重试。用冷却而不是"永久关闭"：一次偶发失败
# （比如摘要模型抖动）不该让长会话在剩余时间里彻底失去压缩能力。
_COMPACT_COOLDOWN_TURNS = 3


class _EarlyToolScheduler:
    """Experimental early dispatch with fixed workers and bounded admission.

    The default ``off`` path never constructs this object.  Experimental mode
    therefore cannot create one task per streamed call or grow an unbounded
    pending list; backpressure reaches the provider iterator through submit().
    """

    def __init__(
        self,
        *,
        execute: ExecuteToolHook,
        state: LoopState,
        workers: int,
        queue_size: int,
    ) -> None:
        self._execute = execute
        self._state = state
        self._queue: asyncio.Queue[
            tuple[ToolCall, asyncio.Future[executor.ToolOutcome]] | None
        ] = asyncio.Queue(maxsize=max(1, queue_size))
        self._workers = [
            asyncio.create_task(self._worker(), name=f"native-early-tool-{index}")
            for index in range(max(1, workers))
        ]
        self._futures: list[asyncio.Future[executor.ToolOutcome]] = []
        self._sealed = False

    async def submit(
        self, call: ToolCall,
    ) -> asyncio.Future[executor.ToolOutcome]:
        if self._sealed:
            raise RuntimeError("early scheduler is sealed")
        future: asyncio.Future[executor.ToolOutcome] = (
            asyncio.get_running_loop().create_future()
        )
        self._futures.append(future)
        await self._queue.put((call, future))
        return future

    async def seal(self) -> None:
        if self._sealed:
            return
        self._sealed = True
        for _ in self._workers:
            await self._queue.put(None)

    async def join(self) -> None:
        await self.seal()
        await asyncio.gather(*self._workers)

    async def cancel(self) -> None:
        for future in self._futures:
            if not future.done():
                future.cancel()
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        # Consume any completed exception so loop teardown never leaves
        # "Future exception was never retrieved" diagnostics behind.
        await asyncio.gather(*self._futures, return_exceptions=True)

    async def _worker(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                call, future = item
                if future.cancelled():
                    continue
                try:
                    result = await self._execute(call, self._state)
                except asyncio.CancelledError:
                    if not future.done():
                        future.cancel()
                    raise
                except BaseException as exc:
                    if not future.done():
                        future.set_exception(exc)
                else:
                    if not future.done():
                        future.set_result(result)
            finally:
                self._queue.task_done()


@dataclass
class LoopState:
    """跨轮可变状态。每个 continue 点整体替换，避免散落的字段赋值。"""

    messages: list[Msg]
    iters: int = 0
    # model_call_count 在发起请求前 checkpoint 预留，恢复不退款；因此进程崩溃
    # 不能绕过硬上限。iters 仍表示语义 turn slot，可在 MODEL_REQUEST 恢复时回退。
    model_call_count: int = 0
    tool_call_count: int = 0
    transition: Optional[str] = None
    # 上下文压缩守卫：反应式压缩次数上限 + 压缩失败后的冷却轮次
    attempted_reactive_compact: int = 0
    compact_failures: int = 0            # 仅用于可观测
    compact_cooldown: int = 0            # >0 时跳过主动压缩，每轮递减
    last_usage: Optional[Usage] = None
    tool_state: dict[str, Any] = field(default_factory=dict)
    generation_counter: int = 0
    current_message_id: str = "uninitialized"
    current_generation_id: str = "uninitialized"
    supersedes_generation_id: str | None = None
    generation_reason: str = "initial"
    final_text: str | None = None
    final_message_id: str | None = None
    final_generation_id: str | None = None
    resume_from_model_request: bool = False
    restored_phase: str | None = None


CheckpointHook = Callable[
    [LoopState, str, tuple[StreamEvent, ...]],
    Awaitable[None],
]
PrepareToolBatchHook = Callable[[list[ToolCall], LoopState], Awaitable[None]]
RunToolCallsHook = Callable[
    [list[ToolCall], LoopState, int],
    AsyncIterator[executor.ToolOutcome],
]
ExecuteToolHook = Callable[[ToolCall, LoopState], Awaitable[executor.ToolOutcome]]
ControlProbeHook = Callable[[], Awaitable[None]]


class NativeRunCancelled(Exception):
    """The durable Runtime observed cancellation at a safe Native boundary."""


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
        checkpoint: CheckpointHook | None = None,
        prepare_tool_batch: PrepareToolBatchHook | None = None,
        run_tool_calls: RunToolCallsHook | None = None,
        execute_tool: ExecuteToolHook | None = None,
        control_probe: ControlProbeHook | None = None,
        message_id_factory: Callable[[int], str] | None = None,
    ) -> None:
        self._client = client
        self._registry = registry
        self._system = system_instruction
        self._config = config
        # 摘要走轻量单轮补全客户端；为 None 时压缩整体禁用（降级为只做体积治理）。
        self._chat = chat
        self._checkpoint_hook = checkpoint
        self._prepare_tool_batch_hook = prepare_tool_batch
        self._run_tool_calls_hook = run_tool_calls
        self._execute_tool_hook = execute_tool
        self._control_probe = control_probe
        self._message_id_factory = message_id_factory or (
            lambda ordinal: f"native-message-{ordinal}"
        )
        self._invocation_id = f"invocation_{uuid.uuid4().hex}"
        # run() 结束后由调用方取回：完整历史 + 收口原因（transition 名）。
        # stop_reason 让嵌套场景（如 Claude SKILL 子 Runner）能区分
        # "正常收口" 与 "撞上熔断"，而不必去匹配错误文案。
        self.final_messages: list[Msg] = []
        self.stop_reason: str = ""
        self.error_code: str | None = None
        # 每轮请求都会带上、但不在 state.messages 里的固定开销：system 指令 +
        # 全部工具的 JSON Schema。当前工具面下就有约 1.8k token，不计入会系统性低估
        # 上下文规模，让压缩阈值形同虚设。工具面启动后不变，故只算一次。
        self._fixed_overhead_chars = len(system_instruction) + len(
            json.dumps(registry.wire_declarations(), ensure_ascii=False),
        )

    async def run(
        self,
        messages: list[Msg] | None = None,
        *,
        initial_state: LoopState | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """驱动循环，产出统一 StreamEvent 序列。结束时 ``final_messages`` 可取回完整历史。"""
        if (messages is None) == (initial_state is None):
            raise ValueError("exactly one of messages or initial_state is required")
        state = (
            initial_state
            if initial_state is not None
            else LoopState(messages=messages or [])
        )
        cfg = self._config
        log_kv(logger, logging.INFO, "NativeLoop", "start",
               max_iters=cfg.max_iters, hard_cap=cfg.hard_cap,
               early_tool_dispatch=cfg.early_tool_dispatch, tools=len(self._registry))

        if cfg.early_tool_dispatch == "provider_block_complete":
            # The OpenAI-compatible adapter has no explicit block-complete
            # signal. Worker startup normally rejects this configuration; keep
            # the kernel guard so non-Worker callers cannot silently downgrade.
            raise RuntimeError("EARLY_DISPATCH_CAPABILITY_UNAVAILABLE")

        # Cancellation/deadline is checked before any recovery dispatch or
        # model reservation.  The reusable kernel has no Runtime dependency;
        # production injects this narrow awaited probe.
        await self._probe_control()

        # 若进程在完整 ToolCall batch 落盘后消失，先恢复缺失的
        # ToolResult；稳定 logical slot 会让 Broker 复用已提交结果。
        async for event in self._resume_pending_tools(state):
            yield event

        while True:
            await self._probe_control()
            state.iters += 1

            # ── 硬熔断：模型请求在 checkpoint 中预留，崩溃不退款 ─────────
            if state.model_call_count >= cfg.hard_cap:
                log_kv(logger, logging.ERROR, "LoopControl", "hard cap exceeded",
                       calls=state.model_call_count, hard_cap=cfg.hard_cap,
                       transition=T_HARD_CAP)
                for ev in self._fail(
                    state, T_HARD_CAP,
                    f"已达框架硬熔断上限（{cfg.hard_cap} 轮），本轮对话中止。",
                    code="RUN_MODEL_CALL_LIMIT",
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
                # generation start 与 checkpoint 在 Runtime 的同一短事务中
                # 提交，之后才允许发起网络请求。
                prior_generation = (
                    state.current_generation_id
                    if state.current_generation_id != "uninitialized"
                    else None
                )
                state.model_call_count += 1
                state.generation_counter += 1
                state.current_message_id = self._message_id_factory(state.iters - 1)
                state.supersedes_generation_id = prior_generation
                if state.resume_from_model_request:
                    state.generation_reason = "recovery"
                elif state.transition == T_REACTIVE_COMPACT:
                    state.generation_reason = "reactive_compact"
                elif prior_generation is None:
                    state.generation_reason = "initial"
                else:
                    state.generation_reason = "next_turn"
                state.current_generation_id = f"gen_{uuid.uuid4().hex}"
                state.final_text = None
                state.final_message_id = None
                state.final_generation_id = None
                await self._checkpoint(
                    state,
                    "MODEL_REQUEST",
                    events=(StreamEvent("output_generation_started", {
                        "message_id": state.current_message_id,
                        "generation_id": state.current_generation_id,
                        "supersedes_generation_id": state.supersedes_generation_id,
                        "reason": state.generation_reason,
                    }),),
                )
                state.resume_from_model_request = False

                # 组装本次模型请求的消息视图，这里控制LLM能看到的东西，LLM负责决策
                # _build_request 是 LLM 的"眼睛"——Runtime 通过它控制视野（历史范围、体积上限、临时提醒），但决策权始终在 LLM；
                # 这是 native_loop 区别于 ADK 框架流程引擎的核心：自研 while 循环 + LLM 自主 tool_call，框架只做视野治理与副作用持久化。
                request_messages = self._build_request(state)
                turn_span.set(request_messages=len(request_messages))

                # ── 调模型：边流边产 text，同时累积 tool_call ────────────────
                text_parts: list[str] = []
                ready_calls: list[ToolCall] = [] # ready_calls 在循环外声明，所以整个流迭代期间共享同一个列表，每次 append 都在同一个列表上累积
                early_scheduler = (
                    _EarlyToolScheduler(
                        execute=self._execute,
                        state=state,
                        workers=cfg.max_tool_concurrency,
                        queue_size=cfg.max_tool_calls_per_turn,
                    )
                    if cfg.early_tool_dispatch == "experimental_heuristic"
                    else None
                )
                early_results: list[tuple[ToolCall, asyncio.Future[executor.ToolOutcome]]] = []
                deferred_calls: list[ToolCall] = []
                early_allowed = early_scheduler is not None
                finish_reason: Optional[str] = None
                model_output_bytes = 0
                saw_turn_end = False

                try:
                    async for item in self._client.stream(
                        messages=request_messages,
                        tools=self._registry.wire_declarations() or None, # 把 ToolRegistry 里所有注册工具转换成 OpenAI tools 请求体格式的 JSON Schema 列表——告诉 LLM"你可以调用这些工具，签名是这样"。
                        allow_early_tool_dispatch=(
                            cfg.early_tool_dispatch == "experimental_heuristic"
                        ),
                        max_tool_calls=cfg.max_tool_calls_per_turn,
                        max_tool_argument_bytes=cfg.max_tool_argument_bytes,
                        max_tool_batch_argument_bytes=cfg.max_tool_batch_argument_bytes,
                    ):
                        if saw_turn_end:
                            raise NativeLlmError(
                                "provider emitted data after the finish marker",
                                "MODEL_PROTOCOL_INVALID",
                            )
                        if isinstance(item, TextDelta):
                            model_output_bytes += len(item.text.encode("utf-8"))
                            if model_output_bytes > cfg.max_model_output_bytes:
                                raise NativeLlmError(
                                    "model generation exceeded the configured byte limit",
                                    "MODEL_OUTPUT_LIMIT_EXCEEDED",
                                )
                            text_parts.append(item.text)
                            yield StreamEvent("text", {
                                "delta": item.text,
                                "message_id": state.current_message_id,
                                "generation_id": state.current_generation_id,
                            })
                        elif isinstance(item, ToolCallReady):
                            # Runtime identity is derived from the durable model activity slot,
                            # not from a provider-generated function_call_id.  A whole-attempt
                            # replay therefore lands on the same ToolExecution and mismatches
                            # fail closed in the Broker.
                            item.call.logical_key = (
                                f"native:turn:{state.iters - 1}:call:{len(ready_calls)}"
                            )
                            self._check_call_limits(state, ready_calls, item.call)
                            ready_calls.append(item.call) #  CC 的 streaming tool dispatch（流式工具提前派发） 语义， 虽然每次只追加一个，但因为整个流的多个 ToolCallReady 都走同一分支，流结束时 ready_calls 自然就是本轮所有调用的集合。
                            # 流式工具执行：只要目前为止的调用全是并发安全的，就立刻开跑。
                            # 一旦出现非安全工具，之后的一律推迟到流结束后按批次串行执行——
                            # 这样恰好保住 CC `partitionToolCalls` 的"并发前缀 + 顺序其余"语义。
                            if (
                                early_allowed
                                and self._is_concurrency_safe(item.call)
                                and self._arguments_valid(item.call)
                            ):
                                turn_span.incr("early_dispatched")
                                assert early_scheduler is not None
                                early_results.append((
                                    item.call,
                                    await early_scheduler.submit(item.call),
                                ))
                            else:
                                early_allowed = False
                                deferred_calls.append(item.call)
                        elif isinstance(item, TurnEnd):
                            if saw_turn_end:
                                raise NativeLlmError(
                                    "provider emitted more than one turn terminator",
                                    "MODEL_PROTOCOL_INVALID",
                                )
                            saw_turn_end = True
                            finish_reason = item.finish_reason
                            if item.usage is not None:
                                state.last_usage = item.usage
                except ContextOverflowError as exc:
                    # ★ 恢复优先于失败：上下文超长不是终止条件，而是"压缩后重来一轮"。
                    await self._cancel_early(early_scheduler, early_results)
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
                    for ev in self._fail(
                        state, T_MODEL_ERROR, str(exc), code="CONTEXT_OVERFLOW",
                    ):
                        yield ev
                    return
                except NativeLlmError as exc:
                    await self._cancel_early(early_scheduler, early_results)
                    log_kv(logger, logging.ERROR, "LoopControl", "model error",
                           iter=state.iters, kind=exc.kind, transition=T_MODEL_ERROR)
                    turn_span.set(transition=T_MODEL_ERROR, failure="model_error",
                                  error_kind=exc.kind)
                    for ev in self._fail(state, T_MODEL_ERROR, str(exc), code=exc.kind):
                        yield ev
                    return
                except BaseException:
                    # 取消 / GeneratorExit 兜底，必须排在具体异常之后。用 BaseException
                    # 是因为消费方 `aclose()` 抛来的 GeneratorExit 既不是 Exception
                    # 也不是 CancelledError，窄捕获会漏掉，让已提前投递的工具变成游离 task。
                    await self._cancel_early(early_scheduler, early_results)
                    raise

                if early_scheduler is not None:
                    await early_scheduler.seal()

                if not saw_turn_end or finish_reason is None:
                    await self._cancel_early(early_scheduler, early_results)
                    for ev in self._fail(
                        state,
                        T_MODEL_ERROR,
                        "provider stream ended without an explicit finish marker",
                        code="MODEL_STREAM_INCOMPLETE",
                    ):
                        yield ev
                    return

                turn_span.set(text_chars=sum(len(t) for t in text_parts),
                              tool_calls=[c.name for c in ready_calls] or None,
                              finish_reason=finish_reason)

                # 模型本轮产出固化进历史：正文 + 工具调用同属一条 assistant 消息。
                state.messages.append(Msg(
                    role="assistant",
                    content="".join(text_parts) or None,
                    tool_calls=list(ready_calls) or None,
                ))

                protocol_error = self._validate_finish_reason(
                    finish_reason,
                    text="".join(text_parts),
                    calls=ready_calls,
                )
                if protocol_error is not None:
                    await self._cancel_early(early_scheduler, early_results)
                    for ev in self._fail(
                        state,
                        T_MODEL_ERROR,
                        protocol_error[1],
                        code=protocol_error[0],
                    ):
                        yield ev
                    return

                state.tool_call_count += len(ready_calls)
                invalid = self._validate_tool_batch(ready_calls)
                if invalid:
                    await self._cancel_early(early_scheduler, early_results)
                    synthetic_events: list[StreamEvent] = []
                    for index, call in enumerate(ready_calls):
                        code, message = invalid.get(
                            index,
                            ("TOOL_BATCH_REJECTED", "another call made the batch invalid"),
                        )
                        outcome = executor.rejected_outcome(call, code=code, message=message)
                        state.messages.append(outcome.message)
                        synthetic_events.extend(self._synthetic_events(outcome, code=code))
                    state.transition = T_NEXT_TURN
                    await self._checkpoint(
                        state,
                        "NEXT_TURN",
                        events=tuple(synthetic_events),
                    )
                    continue

                await self._checkpoint(state, "MODEL_RESPONSE_COMMITTED")

                # ── 唯一的退出判定 ──────────────────────────────────────────
                if not ready_calls:
                    final_text = "".join(text_parts)
                    state.final_text = final_text
                    state.final_message_id = state.current_message_id
                    state.final_generation_id = state.current_generation_id
                    state.transition = T_COMPLETED
                    await self._checkpoint(state, "COMPLETED")
                    log_kv(logger, logging.INFO, "LoopControl", "no tool calls, turn complete",
                           iter=state.iters, finish_reason=finish_reason,
                           transition=T_COMPLETED)
                    turn_span.set(transition=T_COMPLETED)
                    for ev in self._complete(state):
                        yield ev
                    return

                # Broker atomically PREPAREs the complete stable-slot batch only
                # after MODEL_RESPONSE_COMMITTED, and no external dispatch is
                # reachable before TOOL_BATCH_COMMITTED.
                await self._prepare_tool_batch(ready_calls, state)

                # Side-effect/UNKNOWN 工具必须等完整 ToolCall batch 落盘。
                try:
                    await self._checkpoint(state, "TOOL_BATCH_COMMITTED")
                except BaseException:
                    await self._cancel_early(early_scheduler, early_results)
                    raise

                # ── 收集工具结果：先回收已提前开跑的，再按批次跑其余 ─────────
                try:
                    for call, future in early_results:
                        outcome = await future
                        for ev in self._result_events(outcome):
                            yield ev
                        state.messages.append(outcome.message)
                        await self._checkpoint(state, "TOOL_RESULT_COMMITTED")

                    async for outcome in self._run_calls(deferred_calls, state):
                        for ev in self._result_events(outcome):
                            yield ev
                        state.messages.append(outcome.message)
                        await self._checkpoint(state, "TOOL_RESULT_COMMITTED")
                    if early_scheduler is not None:
                        await early_scheduler.join()
                except BaseException:
                    # 用 BaseException 而不是 CancelledError：消费方若走 `aclose()`，
                    # 生成器在 yield 处收到的是 GeneratorExit（它不是 Exception 的子类，
                    # 也不是 CancelledError），窄捕获会让提前投递的工具任务变成游离 task
                    # 继续跑——那可能是技能沙箱子进程或 skill-center 的 HTTP 调用。
                    # 取消时也必须补齐 tool_result 配对，否则这段历史下次进模型会被判 400。
                    await self._cancel_early(early_scheduler, early_results)
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

    def _fail(
        self,
        state: LoopState,
        reason: str,
        message: str,
        *,
        code: str | None = None,
    ) -> list[StreamEvent]:
        self._finish(state, reason)
        self.error_code = code or reason.upper()
        return [StreamEvent("error", {
            "message": message,
            "reason": reason,
            "code": self.error_code,
        })]

    def _complete(self, state: LoopState) -> list[StreamEvent]:
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
        # compact() already replaces the discarded prefix.  There is no
        # append-only historical format or compatibility boundary to scan.
        live = clone(state.messages)
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

    async def _checkpoint(
        self,
        state: LoopState,
        phase: str,
        *,
        events: tuple[StreamEvent, ...] = (),
    ) -> None:
        if self._checkpoint_hook is not None:
            await self._checkpoint_hook(state, phase, events)

    async def _probe_control(self) -> None:
        if self._control_probe is not None:
            await self._control_probe()

    async def _prepare_tool_batch(
        self, calls: list[ToolCall], state: LoopState,
    ) -> None:
        if self._prepare_tool_batch_hook is not None:
            await self._prepare_tool_batch_hook(calls, state)

    async def _run_calls(
        self, calls: list[ToolCall], state: LoopState,
    ) -> AsyncIterator[executor.ToolOutcome]:
        if not calls:
            return
        if self._run_tool_calls_hook is not None:
            async for outcome in self._run_tool_calls_hook(
                calls, state, self._config.max_tool_concurrency,
            ):
                yield outcome
            return
        async for outcome in executor.run_calls(
            calls,
            self._registry,
            invocation_id=self._invocation_id,
            state=state.tool_state,
            max_concurrency=self._config.max_tool_concurrency,
        ):
            yield outcome

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
            # A crash may land after the final per-result checkpoint but before
            # the explicit turn boundary.  Re-establish NEXT_TURN durably
            # before reserving another model request; do not silently skip a
            # phase merely because all ledger results are already paired.
            if state.restored_phase == "TOOL_RESULT_COMMITTED":
                state.transition = T_NEXT_TURN
                await self._checkpoint(state, "NEXT_TURN")
            return
        # Idempotent full-batch prepare rebuilds attempt-local correlation after
        # restart and re-validates every stable slot before any re-dispatch.
        await self._prepare_tool_batch(calls, state)
        if state.restored_phase == "MODEL_RESPONSE_COMMITTED":
            await self._checkpoint(state, "TOOL_BATCH_COMMITTED")
        async for outcome in self._run_calls(pending, state):
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
        if self._execute_tool_hook is not None:
            return await self._execute_tool_hook(call, state)
        return await executor.execute_one(
            call, self._registry,
            invocation_id=self._invocation_id, state=state.tool_state,
        )

    @staticmethod
    async def _cancel_early(
        scheduler: _EarlyToolScheduler | None,
        _results: list[tuple[ToolCall, asyncio.Future[executor.ToolOutcome]]],
    ) -> None:
        if scheduler is not None:
            await scheduler.cancel()

    def _check_call_limits(
        self,
        state: LoopState,
        current: list[ToolCall],
        call: ToolCall,
    ) -> None:
        cfg = self._config
        if len(current) + 1 > cfg.max_tool_calls_per_turn:
            raise NativeLlmError(
                "model exceeded the per-turn ToolCall limit",
                "TOOL_CALL_LIMIT_EXCEEDED",
            )
        if state.tool_call_count + len(current) + 1 > cfg.max_tool_calls_per_run:
            raise NativeLlmError(
                "model exceeded the per-Run ToolCall limit",
                "TOOL_CALL_LIMIT_EXCEEDED",
            )
        argument_bytes = len((call.arguments or "").encode("utf-8"))
        if argument_bytes > cfg.max_tool_argument_bytes:
            raise NativeLlmError(
                "one ToolCall argument exceeded the configured byte limit",
                "TOOL_ARGUMENTS_TOO_LARGE",
            )
        batch_bytes = argument_bytes + sum(
            len((existing.arguments or "").encode("utf-8")) for existing in current
        )
        if batch_bytes > cfg.max_tool_batch_argument_bytes:
            raise NativeLlmError(
                "ToolCall batch arguments exceeded the configured byte limit",
                "TOOL_BATCH_TOO_LARGE",
            )

    def _arguments_valid(self, call: ToolCall) -> bool:
        spec = self._registry.get(call.name)
        if spec is None:
            return False
        arguments, error = executor.parse_arguments(call)
        if error is not None or arguments is None:
            return False
        return not list(Draft202012Validator(spec.parameters).iter_errors(arguments))

    def _validate_tool_batch(
        self, calls: list[ToolCall],
    ) -> dict[int, tuple[str, str]]:
        invalid: dict[int, tuple[str, str]] = {}
        for index, call in enumerate(calls):
            spec = self._registry.get(call.name)
            if spec is None:
                invalid[index] = (
                    "TOOL_NOT_FOUND",
                    f"tool is not present in the frozen catalog: {call.name}",
                )
                continue
            arguments, parse_error = executor.parse_arguments(call)
            if parse_error is not None or arguments is None:
                invalid[index] = (
                    "TOOL_ARGUMENTS_INVALID",
                    "tool arguments are not a JSON object",
                )
                continue
            errors = sorted(
                Draft202012Validator(spec.parameters).iter_errors(arguments),
                key=lambda item: list(item.path),
            )
            if errors:
                invalid[index] = (
                    "TOOL_ARGUMENTS_INVALID",
                    errors[0].message[:1000],
                )
        return invalid

    @staticmethod
    def _validate_finish_reason(
        finish_reason: str,
        *,
        text: str,
        calls: list[ToolCall],
    ) -> tuple[str, str] | None:
        if finish_reason == "stop":
            if calls:
                return (
                    "MODEL_PROTOCOL_INVALID",
                    "finish_reason=stop cannot carry ToolCalls",
                )
            if not text.strip():
                return (
                    "MODEL_EMPTY_FINAL_RESPONSE",
                    "final assistant response is empty",
                )
            return None
        if finish_reason == "tool_calls":
            if not calls:
                return (
                    "MODEL_PROTOCOL_INVALID",
                    "finish_reason=tool_calls requires a complete ToolCall batch",
                )
            call_ids = [call.id for call in calls]
            if any(not call_id for call_id in call_ids) or len(call_ids) != len(set(call_ids)):
                return (
                    "MODEL_PROTOCOL_INVALID",
                    "ToolCall ids must be present and unique within one model response",
                )
            return None
        return (
            "MODEL_PROTOCOL_INVALID",
            f"unsupported finish_reason: {finish_reason}",
        )

    # ── 事件翻译 ───────────────────────────────────────────────────────────

    def _result_events(self, outcome: executor.ToolOutcome) -> list[StreamEvent]:
        # 计划工具的返回不给用户看：它的信息已经由 plan_step 表达过了。
        if outcome.call.name == PLAN_TOOL or not outcome.project_event:
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

    @staticmethod
    def _synthetic_events(
        outcome: executor.ToolOutcome,
        *,
        code: str,
    ) -> list[StreamEvent]:
        call = outcome.call
        args, _ = executor.parse_arguments(call)
        return [
            StreamEvent("tool_call", {
                "id": call.id,
                "name": call.name,
                "args": args or {},
                "not_dispatched": True,
            }, authority="engine"),
            StreamEvent("tool_result", {
                "id": call.id,
                "name": call.name,
                "response": executor.json_safe(outcome.response),
                "error_code": code,
                "not_dispatched": True,
            }, authority="engine"),
        ]
