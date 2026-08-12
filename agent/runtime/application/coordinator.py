from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from agent.runtime.application.events import CommittedEventSink
from agent.runtime.domain.errors import (
    AttemptOwnershipLost,
    RuntimeFault,
    raise_if_ownership_lost,
)
from agent.runtime.domain.models import (
    EngineOutcome,
    EngineOutcomeKind,
    RunStatus,
)
from agent.runtime.ports.clock import Clock, RandomSource, SystemClock, SystemRandomSource
from agent.runtime.ports.engine import EngineAdapter, EngineRunRequest
from agent.runtime.ports.store import Claim, RuntimeStore
from agent.runtime.ports.tool import (
    RECONCILIATION_MARKER_KIND,
    ToolReconciliationExecutor,
    ToolReconciliationMarker,
)
from common.obs import get_logger, log_kv, use_trace_id
from common.trace import KIND_ENGINE, start_span

logger = get_logger("agent.runtime.coordinator")


class EngineRegistry:
    def __init__(self, adapters: Mapping[str, EngineAdapter]) -> None:
        self._adapters = dict(adapters)

    def get(self, name: str) -> EngineAdapter:
        try:
            return self._adapters[name]
        except KeyError as exc:
            raise RuntimeError(f"engine adapter is not registered: {name}") from exc

    @property
    def releases(self) -> dict[str, str]:
        return {name: adapter.release_fingerprint for name, adapter in self._adapters.items()}


class RunCoordinator:
    def __init__(
        self,
        store: RuntimeStore,
        registry: EngineRegistry,
        *,
        clock: Clock | None = None,
        random_source: RandomSource | None = None,
        event_flush_ms: int = 100,
        event_flush_bytes: int = 2048,
        max_model_attempts: int = 2,
        tool_reconciler: ToolReconciliationExecutor | None = None,
        tool_broker: Any | None = None,
        max_skill_event_bytes: int = 64 * 1024,
        max_skill_events: int = 2000,
        max_skill_event_total_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self.store = store
        self.registry = registry
        self.clock = clock or SystemClock()
        self.random = random_source or SystemRandomSource()
        self.event_flush_ms = event_flush_ms
        self.event_flush_bytes = event_flush_bytes
        self.max_model_attempts = max_model_attempts
        self.tool_reconciler = tool_reconciler
        self.tool_broker = tool_broker
        self.max_skill_event_bytes = max_skill_event_bytes
        self.max_skill_events = max_skill_events
        self.max_skill_event_total_bytes = max_skill_event_total_bytes

    async def execute_claim(self, claim: Claim, *, worker_id: str) -> RunStatus:
        """恢复本 Run 的诊断 trace_id，再执行。

        Worker 进程没有 `TraceMiddleware`，contextvar 也跨不过 API→DB→Worker 这道
        交接。不在这里恢复，`get_trace_id()` 就恒为默认值 "-"：全部 span 挤进同一条
        轨迹、`GET /api/v1/traces/{trace_id}` 永远 404、内存里那条记录还会无限增长。
        回落到 run_id 是为了保证**每个 Run 始终有一个唯一且可查的轨迹键**，
        哪怕调用方没带 x-trace-id。
        """
        with use_trace_id(claim.run.trace_id or claim.run.envelope.run_id):
            try:
                return await self._execute_claim(claim, worker_id=worker_id)
            except RuntimeFault as exc:
                raise_if_ownership_lost(exc)
                raise

    async def _execute_claim(self, claim: Claim, *, worker_id: str) -> RunStatus:
        # 第 1 步：把 Activity 从 CLAIMED 推进到 RUNNING。这一步带 fencing token 做 CAS，
        # 如果租约已被别的 Worker 抢走会抛 STALE_FENCING_TOKEN，从而在做任何实质工作
        # 之前就阻止陈旧 Worker 继续往下跑。
        activity = await self.store.mark_activity_running( # 标记为activity执行中
            claim.activity.activity_id,
            worker_id=worker_id,
            fencing_token=claim.activity.fencing_token,
            now_ms=self.clock.now_ms(),
        )
        # 第 2 步：重新读一次 Run。claim_next 之后到这里之间，Run 可能已被 cancel，
        # 所以不能复用 claim.run 里那份快照，必须拿库里的最新状态做判断。
        run = await self.store.get_run(claim.run.envelope.run_id)
        # 第 3 步：解析 Activity 的 resume_payload：判断这是不是一次"仅做协调"的特殊任务。
        # parse_exact 采用 strict + extra=forbid，字段多一个少一个都会返回 None。
        marker = ToolReconciliationMarker.parse_exact(activity.resume_payload)
        # 分支 1：payload 自称是协调标记（kind 匹配）却解析失败，说明持久化数据已损坏。
        # 这属于所有权污染，必须立即失败；绝不能让它落进引擎执行，否则会把一个只允许
        # 查询的任务变成真正的引擎/工具重放。
        if (
            marker is None
            and isinstance(activity.resume_payload, dict)
            and activity.resume_payload.get("kind") == RECONCILIATION_MARKER_KIND
        ):
            # A malformed durable marker is ownership corruption.  In
            # particular, never let it fall through into Engine execution.
            raise RuntimeFault(
                "TOOL_RECONCILIATION_MISMATCH",
                "claimed Activity carries a malformed reconcile-only marker",
                409,
            )
        # 分支 2：普通 Activity，但 Run 已经被请求取消。直接用刚拿到的新 fencing token
        # 结束 Run，不再加载 checkpoint/历史/引擎——省掉一整轮无意义的模型调用。
        if marker is None and run.status is RunStatus.CANCEL_REQUESTED:
            # Cancellation may win after claim_next but before execute_claim.
            # The fresh Activity fence is sufficient to settle cancellation;
            # no Engine registry/checkpoint/history/adapter work is authorized.
            # 取消与超时可能同时成立。先算清谁赢：deadline 已过就记 TIMED_OUT，
            # 否则才是 CANCELLED，避免把超时误报成用户取消。
            deadline_won = self.clock.now_ms() >= run.envelope.deadline_at
            terminal = await self.store.finalize_failure(
                run_id=run.envelope.run_id,
                activity_id=activity.activity_id,
                fencing_token=activity.fencing_token,
                code="DEADLINE_EXCEEDED" if deadline_won else "CANCELLED",
                message=(
                    "Run deadline elapsed before cancellation settlement"
                    if deadline_won
                    else "cancel won before Engine Adapter execution"
                ),
                terminal_status=(
                    RunStatus.TIMED_OUT if deadline_won else RunStatus.CANCELLED
                ),
                now_ms=self.clock.now_ms(),
            )
            return terminal.status
        # 分支 3：这是一个经操作员授权的"仅做协调" Activity。故意在注册表查询、
        # checkpoint 加载和历史编译之前处理；只能调用查询 hook，绝不能重新派发
        # 原始工具或引擎。
        # “仅做协调”（reconcile-only）指的是：这次被 Worker 领取的 Activity 不是来跑引擎的，而是只被授权去“查一下某次工具副作用到底成没成”，
        # 查完就裁决 ToolExecution / 父 Activity / Run 的状态，全程不碰 LLM、不重新调用原始工具。
        if marker is not None:
            # This branch intentionally precedes registry lookup, checkpoint
            # loading and history compilation.  Operator authorization permits
            # a query hook only; it can never redispatch the Engine/original Tool.
            # 子分支 3a：协调查询本身也受 Run 全局 deadline 约束。已超时就不再向
            # 下游发查询，直接判 TIMED_OUT。
            if self.clock.now_ms() >= run.envelope.deadline_at:
                terminal = await self.store.finalize_failure(
                    run_id=run.envelope.run_id,
                    activity_id=activity.activity_id,
                    fencing_token=activity.fencing_token,
                    code="DEADLINE_EXCEEDED",
                    message="Run deadline elapsed before reconcile-only query",
                    terminal_status=RunStatus.TIMED_OUT,
                    now_ms=self.clock.now_ms(),
                )
                return terminal.status
            # 子分支 3b：当前 Worker 没装 ToolReconciliationExecutor，无法执行查询。
            # 报 503 让 Activity 交回去重派，而不是把 Run 判失败——能力缺失是 Worker
            # 侧问题，不该影响 Run 的裁决。
            if self.tool_reconciler is None:
                raise RuntimeFault(
                    "TOOL_RECONCILER_UNAVAILABLE",
                    "reconcile-only Activity has no Worker ToolReconciliationExecutor",
                    503,
                )
            # 只读查询：调用下游注册的 reconcile hook 去问"那次副作用到底成没成"。
            # 契约上禁止它重新派发原始工具。
            await self.tool_reconciler.reconcile_only(
                tool_execution_id=marker.tool_execution_id,
                parent_activity_id=activity.activity_id,
                fencing_token=activity.fencing_token,
                expected_effect_revision=marker.expected_effect_revision,
                deadline_at_ms=run.envelope.deadline_at,
            )
            # 在单个短事务里裁决查询结果：校验 marker/signal/effect revision 完全对得上，
            # 然后推进 ToolExecution、父 Activity 和 Run 状态。这里返回的状态就是本次
            # attempt 的最终结果，全程不碰引擎。
            settled = await self.store.settle_reconciliation_query(
                run_id=run.envelope.run_id,
                activity_id=activity.activity_id,
                fencing_token=activity.fencing_token,
                tool_execution_id=marker.tool_execution_id,
                signal_id=marker.signal_id,
                expected_effect_revision=marker.expected_effect_revision,
                now_ms=self.clock.now_ms(),
            )
            return settled.status
        # 以下进入正常的引擎执行路径。
        # 第 4 步：按 Run 声明的 engine 名字取出适配器。
        adapter = self.registry.get(run.envelope.engine)
        # Exact-release claim SQL makes this branch unreachable during normal
        # operation.  Keep a defence against registry/store corruption, but do
        # not turn a Worker ownership violation into a Run terminal fact.
        if adapter.release_fingerprint != run.envelope.release_fingerprint:
            raise RuntimeFault(
                "CLAIM_RELEASE_MISMATCH",
                "claimed Run does not match the executing Worker release",
                500,
                {
                    "run_id": run.envelope.run_id,
                    "engine": run.envelope.engine,
                    "run_release": run.envelope.release_fingerprint,
                    "worker_release": adapter.release_fingerprint,
                },
            )
        # 拉出最新一份 checkpoint（可能为 None，说明是首次执行而不是恢复）。
        checkpoint = await self.store.latest_checkpoint(run.envelope.run_id)
        # 分支 5：认领和恢复准备可能消耗了不少时间，所以在真正进引擎前再校一次 deadline。
        # 宁可在这里直接超时结束，也不要发起一个注定超时的模型调用。
        if self.clock.now_ms() >= run.envelope.deadline_at:
            terminal = await self.store.finalize_failure(
                run_id=run.envelope.run_id,
                activity_id=activity.activity_id,
                fencing_token=activity.fencing_token,
                code="DEADLINE_EXCEEDED",
                message="Run deadline elapsed before engine execution",
                terminal_status=RunStatus.TIMED_OUT,
                now_ms=self.clock.now_ms(),
            )
            return terminal.status

        # 第 5 步：从已提交的 Canonical Events 编译历史。注意历史的唯一事实源是事件表，
        # 不是上一次 attempt 的进程内 session；失败的 partial delta 不会被编入。
        history = await self.store.compile_history(run.envelope.run_id)
        # 组装交给引擎的请求。fencing_token 一路传到引擎内部，使引擎的每一次写
        # （事件/checkpoint/工具）都带着同一把所有权凭证。
        request = EngineRunRequest(
            envelope=run.envelope,
            activity_id=activity.activity_id,
            fencing_token=activity.fencing_token,
            attempt=activity.attempt,
            input_text=run.input_text,
            history=tuple(history),
            checkpoint=checkpoint,
            resume_payload=activity.resume_payload,
        )
        # 第 6 步：创建 RuntimeIO。它是三代引擎唯一的对外出口：引擎只能通过它写
        # 事件/checkpoint，并且写入按 flush_ms/flush_bytes 聚合后先 commit 再对 SSE 可见。
        io = CommittedEventSink(
            self.store,
            run_id=run.envelope.run_id,
            activity_id=activity.activity_id,
            fencing_token=activity.fencing_token,
            deadline_at_ms=run.envelope.deadline_at,
            flush_ms=self.event_flush_ms,
            flush_bytes=self.event_flush_bytes,
            clock=self.clock,
            tool_broker=self.tool_broker,
            max_skill_event_bytes=self.max_skill_event_bytes,
            max_skill_events=self.max_skill_events,
            max_skill_event_total_bytes=self.max_skill_event_total_bytes,
        )

        outcome: EngineOutcome
        # 第 7 步：整个引擎执行包在一个诊断 span 里，方便三代引擎用同一套指标对比。
        with start_span(
            "runtime.engine_attempt", KIND_ENGINE,
            run_id=run.envelope.run_id,
            activity_id=activity.activity_id,
            engine=run.envelope.engine,
            attempt=activity.attempt,
            fencing_token=activity.fencing_token,
            release_fingerprint=run.envelope.release_fingerprint,
        ) as span:
            # 引擎无关的事件旁路：Sink 是三代引擎唯一共同出口，挂在这里产出的
            # TTFT / event_counts / 工具与引用事件时间线对三代天然对等。
            io.attach_trace_span(span)
            try:
                # 这一行是三代引擎执行的统一调用点，"正常路径"（引擎顺利跑完、outcome.kind=COMPLETED、无未决工具效果、无取消/超时）确实是最常见的分支，
                # 但代码结构上它同时也是异常、取消、超时、HITL 等所有分支共同的起点——Coordinator 故意不信任这里返回的 outcome 是最终事实,
                # 后面还要用 DB 里的权威状（cancel_requested、deadline、unresolved tool effects）逐层覆盖它

                outcome = await adapter.execute(request, io)
            # 网络/超时/IO 类异常视为可重试：这类错误通常是瞬时性的，
            # 交给下面的重试分支安排退避重试。
            except asyncio.CancelledError:
                await io.abort()
                raise
            except AttemptOwnershipLost:
                await io.abort()
                raise
            except RuntimeFault as exc:
                raise_if_ownership_lost(exc)
                span.set_status("error").set(error=exc.code)
                outcome = EngineOutcome(
                    kind=EngineOutcomeKind.TERMINAL_FAILURE,
                    error_code=exc.code,
                    message=exc.message,
                )
            except (TimeoutError, ConnectionError, OSError) as exc:
                span.set_status("error").set(error=type(exc).__name__)
                outcome = EngineOutcome(
                    kind=EngineOutcomeKind.RETRYABLE_FAILURE,
                    error_code=type(exc).__name__, message=str(exc),
                )
            # 其余异常视为适配器违反契约，直接终态失败。关键点是：绝不能因为
            # 生成器提前退出就把它当成"正常结束"。
            except Exception as exc:  # adapter contract violation is terminal, never EOF-success
                span.set_status("error").set(error=type(exc).__name__)
                outcome = EngineOutcome(
                    kind=EngineOutcomeKind.TERMINAL_FAILURE,
                    error_code="ENGINE_ADAPTER_ERROR", message=str(exc),
                )
            try:
                # 关闭 Sink：flush 掉最后一批聚合中的 delta。
                await io.close()
            except Exception as exc:
                # 事件提交失败是不可吞的：它意味着已产出的输出没能落库。
                # 直接向上抛，不让后面的终态提交建立在不完整的事件流上。
                span.set_status("error").set(event_commit_error=type(exc).__name__)
                raise
            # `outcome.kind` 是新架构下"为什么结束"的唯一对等字段，沿用旧的
            # finish_reason 命名，让历史评测报告与 trace_signals 规则保持可比。
            span.set(
                outcome=outcome.kind,
                finish_reason=outcome.kind,
                output_chars=len(io.assistant_text),
                **io.trace_rollup(),
            )
            # 只有真的产出了文本才往 span 里写 answer payload。
            if io.assistant_text:
                span.set_payload("answer", io.assistant_text)

        # 第 8 步：三道串行的 outcome 修正。顺序有意义——后面的优先级更高，
        # 依次覆盖前面的结论，最终只留下一个权威 outcome。
        # 分支 6：引擎内部报告了错误事件，但 outcome 却说是 COMPLETED。
        # 这种矛盾按失败处理，不能假装成功。
        # An engine-internal error event is diagnostic only, but a contradictory
        # COMPLETED outcome fails closed instead of pretending success.
        if io.engine_error is not None and outcome.kind is EngineOutcomeKind.COMPLETED:
            outcome = EngineOutcome(
                kind=EngineOutcomeKind.TERMINAL_FAILURE,
                error_code=str(io.engine_error.get("reason", "ENGINE_REPORTED_ERROR")),
                message=str(io.engine_error.get("message", "engine reported an error")),
            )
        # 分支 7：引擎执行过程中 Run 被请求取消，把 outcome 强制改为 CANCELLED。
        # 注意这里不看引擎自己怎么说，取消的权威在库里。
        if await self.store.is_cancel_requested(run.envelope.run_id):
            outcome = EngineOutcome(kind=EngineOutcomeKind.CANCELLED)
        # 分支 8：引擎执行过程中截止时间已到，把 outcome 强制改为超时失败。
        # 超时排在取消之后，因此两者同时成立时以超时为准。
        if self.clock.now_ms() >= run.envelope.deadline_at:
            outcome = EngineOutcome(
                kind=EngineOutcomeKind.TERMINAL_FAILURE,
                error_code="DEADLINE_EXCEEDED",
                message="Run deadline elapsed during engine execution",
            )

        # 第 9 步：根据最终 outcome 提交归属。这是整个方法唯一能写 Run 终态的地方，
        # 下面四条分支互斥，每条都必须给 final 赋值。
        # 分支 9：引擎正常完成。但若还有未决的外部工具效果，不能让模型回答直接
        # 抹掉不确定性，必须进入人工协调边界。
        if outcome.kind is EngineOutcomeKind.COMPLETED:
            # 成功终态前的最后一道门禁：查一下本 Run 是否还有 DISPATCHED/UNKNOWN 等
            # 未落定的副作用。
            unresolved = await self.store.unresolved_tool_execution_ids(run.envelope.run_id)
            # 子分支 9a：存在未决效果。不判成功也不判失败，而是进 WAITING_INPUT
            # 把 worker slot 释放出去，并在 pending_input 里显式列出需要人工协调的 ID。
            if unresolved:
                # A model answer cannot erase an uncertain external effect. Release
                # the worker slot and expose an explicit manual-reconcile boundary.
                final = await self.store.wait_for_input(
                    run_id=run.envelope.run_id,
                    activity_id=activity.activity_id,
                    fencing_token=activity.fencing_token,
                    pending_input={
                        "type": "TOOL_RECONCILIATION_REQUIRED",
                        "unresolved_tool_execution_ids": unresolved,
                    },
                    now_ms=self.clock.now_ms(),
                )
            # 子分支 9b：没有未决效果，可以正常提交成功终态。
            # final assistant 文本 + citation + 成功终态在同一个事务里落库。
            else:
                final_assistant = io.final_assistant
                assistant_text = (
                    final_assistant[0] if final_assistant is not None else io.assistant_text
                )
                final = await self.store.finalize_success(
                    run_id=run.envelope.run_id,
                    activity_id=activity.activity_id,
                    fencing_token=activity.fencing_token,
                    assistant_text=assistant_text,
                    # SQLite finalize derives citations from committed EvidenceSet indexes;
                    # request-local engine emissions cannot influence durable authority.
                    citations=[],
                    now_ms=self.clock.now_ms(),
                    message_id=(
                        final_assistant[1] if final_assistant is not None else None
                    ),
                    generation_id=(
                        final_assistant[2] if final_assistant is not None else None
                    ),
                )
        # 分支 10：引擎主动请求等待外部输入（如 HITL 信号）。这不是终态，
        # Run 置为 WAITING_INPUT 后释放 worker slot，等 signal 到了再重新派发。
        elif outcome.kind is EngineOutcomeKind.WAITING_INPUT:
            final = await self.store.wait_for_input(
                run_id=run.envelope.run_id,
                activity_id=activity.activity_id,
                fencing_token=activity.fencing_token,
                # 引擎没给具体等待原因时，兜底成一个通用输入边界。
                pending_input=outcome.pending_input or {"type": "GENERIC_INPUT"},
                now_ms=self.clock.now_ms(),
            )
        # 分支 11：可重试失败且未达最大尝试次数。注意 attempt 这个条件必须和
        # RETRYABLE_FAILURE 同时成立，否则会落到分支 12 直接判失败。
        elif outcome.kind is EngineOutcomeKind.RETRYABLE_FAILURE and activity.attempt < self.max_model_attempts:
            # 优先尊重引擎给的 retry_after（例如上游 429 带的退避时间），
            # 没给才用本地指数退避 + 拖动。
            delay_ms = outcome.retry_after_ms or self._retry_delay_ms(activity.attempt)
            await self.store.schedule_retry(
                run_id=run.envelope.run_id,
                activity_id=activity.activity_id,
                fencing_token=activity.fencing_token,
                fire_at=self.clock.now_ms() + delay_ms,
                error={"code": outcome.error_code, "message": outcome.message,
                       "retryable": True, "attempt": activity.attempt},
                now_ms=self.clock.now_ms(),
            )
            # 重试不是终态，Run 仍然活着；重读一下拿到 schedule_retry 写完后的状态。
            final = await self.store.get_run(run.envelope.run_id)
        # 分支 12：其他情况（终端失败、取消、重试次数耗尽等）提交计划终态。
        # Store 会在同一写事务里再次检查 ToolEffect：普通 FAILED 若仍有
        # unresolved effect，不会越过账本写终态，而是保存 sticky pending
        # terminal 并进入严格人工协调；最后一个 effect 被处置后才提交原 FAILED。
        else:
            # 先按 outcome 种类定终态：取消→CANCELLED，其余→FAILED。
            status = RunStatus.CANCELLED if outcome.kind is EngineOutcomeKind.CANCELLED else RunStatus.FAILED
            # 子分支 12a：错误码是 DEADLINE_EXCEEDED 时，终态统一映射为 TIMED_OUT。
            # 超时在对外语义上是一个独立结果，不能混入普通 FAILED。
            if outcome.error_code == "DEADLINE_EXCEEDED":
                status = RunStatus.TIMED_OUT
            final = await self.store.finalize_failure(
                run_id=run.envelope.run_id,
                activity_id=activity.activity_id,
                fencing_token=activity.fencing_token,
                code=outcome.error_code or outcome.kind,
                message=outcome.message or outcome.kind,
                terminal_status=status,
                now_ms=self.clock.now_ms(),
            )
        # 第 10 步：记一条本次 attempt 的结算日志，并把库里的权威状态返回给 Worker。
        # 返回值不一定是终态（如重试/等待输入），Worker 据此决定后续调度。
        log_kv(
            logger, logging.INFO, "Runtime", "attempt settled",
            run_id=run.envelope.run_id, activity_id=activity.activity_id,
            engine=run.envelope.engine, status=final.status,
        )
        return final.status

    def _retry_delay_ms(self, attempt: int) -> int:
        base = min(30_000, 1000 * (2 ** max(0, attempt - 1)))
        return max(1, int(base * self.random.uniform(0.8, 1.2)))
