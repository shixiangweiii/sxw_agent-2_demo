from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from agent.runtime.application.events import CommittedEventSink
from agent.runtime.application.release_compatibility import ReleaseCompatibilityRegistry
from agent.runtime.domain.errors import RuntimeFault
from agent.runtime.domain.models import (
    SCHEMA_VERSION,
    EngineOutcome,
    EngineOutcomeKind,
    RunStatus,
)
from agent.runtime.ports.clock import Clock, RandomSource, SystemClock, SystemRandomSource
from agent.runtime.ports.engine import EngineAdapter, EngineRunRequest
from agent.runtime.ports.release_compatibility import (
    CheckpointUpgradeKey,
    CheckpointUpgradeRequest,
    CheckpointUpgradeResult,
)
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
        release_compatibility: ReleaseCompatibilityRegistry | None = None,
        tool_reconciler: ToolReconciliationExecutor | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.clock = clock or SystemClock()
        self.random = random_source or SystemRandomSource()
        self.event_flush_ms = event_flush_ms
        self.event_flush_bytes = event_flush_bytes
        self.max_model_attempts = max_model_attempts
        self.release_compatibility = release_compatibility or ReleaseCompatibilityRegistry()
        self.tool_reconciler = tool_reconciler

    async def execute_claim(self, claim: Claim, *, worker_id: str) -> RunStatus:
        """恢复本 Run 的诊断 trace_id，再执行。

        Worker 进程没有 `TraceMiddleware`，contextvar 也跨不过 API→DB→Worker 这道
        交接。不在这里恢复，`get_trace_id()` 就恒为默认值 "-"：全部 span 挤进同一条
        轨迹、`GET /api/v1/traces/{trace_id}` 永远 404、内存里那条记录还会无限增长。
        回落到 run_id 是为了保证**每个 Run 始终有一个唯一且可查的轨迹键**，
        哪怕调用方没带 x-trace-id。
        """
        with use_trace_id(claim.run.trace_id or claim.run.envelope.run_id):
            return await self._execute_claim(claim, worker_id=worker_id)

    async def _execute_claim(self, claim: Claim, *, worker_id: str) -> RunStatus:
        activity = await self.store.mark_activity_running(
            claim.activity.activity_id,
            worker_id=worker_id,
            fencing_token=claim.activity.fencing_token,
            now_ms=self.clock.now_ms(),
        )
        run = await self.store.get_run(claim.run.envelope.run_id)
        marker = ToolReconciliationMarker.parse_exact(activity.resume_payload)
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
        if marker is None and run.status is RunStatus.CANCEL_REQUESTED:
            # Cancellation may win after claim_next but before execute_claim.
            # The fresh Activity fence is sufficient to settle cancellation;
            # no Engine registry/checkpoint/history/adapter work is authorized.
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
        if marker is not None:
            # This branch intentionally precedes registry lookup, checkpoint
            # loading and history compilation.  Operator authorization permits
            # a query hook only; it can never redispatch the Engine/original Tool.
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
            if self.tool_reconciler is None:
                raise RuntimeFault(
                    "TOOL_RECONCILER_UNAVAILABLE",
                    "reconcile-only Activity has no Worker ToolReconciliationExecutor",
                    503,
                )
            await self.tool_reconciler.reconcile_only(
                tool_execution_id=marker.tool_execution_id,
                parent_activity_id=activity.activity_id,
                fencing_token=activity.fencing_token,
                expected_effect_revision=marker.expected_effect_revision,
                deadline_at_ms=run.envelope.deadline_at,
            )
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
        adapter = self.registry.get(run.envelope.engine)
        checkpoint = await self.store.latest_checkpoint(run.envelope.run_id)
        if adapter.release_fingerprint != run.envelope.release_fingerprint:
            incompatibility: str | None = None
            if checkpoint is None:
                incompatibility = "release mismatch has no committed checkpoint to upgrade"
            elif checkpoint.release_fingerprint != run.envelope.release_fingerprint:
                incompatibility = "latest checkpoint release does not match the Run effective release"
            else:
                key = CheckpointUpgradeKey(
                    engine=run.envelope.engine,
                    from_release_fingerprint=run.envelope.release_fingerprint,
                    from_schema_version=checkpoint.schema_version,
                    to_release_fingerprint=adapter.release_fingerprint,
                    to_schema_version=SCHEMA_VERSION,
                )
                upgrader = self.release_compatibility.get(key)
                if upgrader is None:
                    incompatibility = "no exact checkpoint upgrader is registered"
                else:
                    try:
                        # Explicitly outside every Store transaction.  The port is
                        # synchronous to exclude awaitable model/tool/network work.
                        upgraded = upgrader.upgrade(CheckpointUpgradeRequest(
                            key=key,
                            checkpoint=checkpoint.model_copy(deep=True),
                        ))
                        if not isinstance(upgraded, CheckpointUpgradeResult):
                            raise TypeError("upgrader returned an invalid result type")
                    except Exception as exc:
                        incompatibility = f"checkpoint upgrader failed closed: {type(exc).__name__}"
                    else:
                        try:
                            checkpoint = await self.store.publish_checkpoint_upgrade(
                                run_id=run.envelope.run_id,
                                activity_id=activity.activity_id,
                                fencing_token=activity.fencing_token,
                                source_checkpoint_id=checkpoint.checkpoint_id,
                                expected_revision=checkpoint.revision,
                                from_release_fingerprint=key.from_release_fingerprint,
                                from_schema_version=key.from_schema_version,
                                to_release_fingerprint=key.to_release_fingerprint,
                                to_schema_version=key.to_schema_version,
                                working_state=upgraded.working_state,
                                engine_state=upgraded.engine_state,
                                engine_state_ref=upgraded.engine_state_ref,
                                now_ms=self.clock.now_ms(),
                            )
                        except RuntimeFault as exc:
                            # A lost fence or checkpoint CAS is ownership loss, not
                            # evidence that this Run is incompatible.  The stale
                            # worker must never be allowed to terminalize it.
                            if exc.code in {
                                "STALE_FENCING_TOKEN",
                                "CHECKPOINT_REVISION_CONFLICT",
                            }:
                                raise
                            incompatibility = (
                                f"checkpoint upgrade publication failed closed: {exc.code}"
                            )
                        else:
                            run = await self.store.get_run(run.envelope.run_id)
            if incompatibility is not None:
                terminal = await self.store.finalize_failure(
                    run_id=run.envelope.run_id,
                    activity_id=activity.activity_id,
                    fencing_token=activity.fencing_token,
                    code="INCOMPATIBLE_RELEASE",
                    message=(
                        f"Run release {run.envelope.release_fingerprint} cannot be resumed by "
                        f"worker release {adapter.release_fingerprint}: {incompatibility}"
                    ),
                    terminal_status=RunStatus.INCOMPATIBLE_RELEASE,
                    now_ms=self.clock.now_ms(),
                )
                return terminal.status
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

        history = await self.store.compile_history(run.envelope.run_id)
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
        io = CommittedEventSink(
            self.store,
            run_id=run.envelope.run_id,
            activity_id=activity.activity_id,
            fencing_token=activity.fencing_token,
            deadline_at_ms=run.envelope.deadline_at,
            flush_ms=self.event_flush_ms,
            flush_bytes=self.event_flush_bytes,
            clock=self.clock,
        )

        outcome: EngineOutcome
        with start_span(
            "runtime.engine_attempt", KIND_ENGINE,
            run_id=run.envelope.run_id,
            activity_id=activity.activity_id,
            engine=run.envelope.engine,
            attempt=activity.attempt,
            fencing_token=activity.fencing_token,
        ) as span:
            try:
                outcome = await adapter.execute(request, io)
            except (TimeoutError, ConnectionError, OSError) as exc:
                span.set_status("error").set(error=type(exc).__name__)
                outcome = EngineOutcome(
                    kind=EngineOutcomeKind.RETRYABLE_FAILURE,
                    error_code=type(exc).__name__, message=str(exc),
                )
            except Exception as exc:  # adapter contract violation is terminal, never EOF-success
                span.set_status("error").set(error=type(exc).__name__)
                outcome = EngineOutcome(
                    kind=EngineOutcomeKind.TERMINAL_FAILURE,
                    error_code="ENGINE_ADAPTER_ERROR", message=str(exc),
                )
            try:
                await io.close()
            except Exception as exc:
                span.set_status("error").set(event_commit_error=type(exc).__name__)
                raise
            span.set(outcome=outcome.kind, output_chars=len(io.assistant_text))

        # An engine-internal error event is diagnostic only, but a contradictory
        # COMPLETED outcome fails closed instead of pretending success.
        if io.engine_error is not None and outcome.kind is EngineOutcomeKind.COMPLETED:
            outcome = EngineOutcome(
                kind=EngineOutcomeKind.TERMINAL_FAILURE,
                error_code=str(io.engine_error.get("reason", "ENGINE_REPORTED_ERROR")),
                message=str(io.engine_error.get("message", "engine reported an error")),
            )
        if await self.store.is_cancel_requested(run.envelope.run_id):
            outcome = EngineOutcome(kind=EngineOutcomeKind.CANCELLED)
        if self.clock.now_ms() >= run.envelope.deadline_at:
            outcome = EngineOutcome(
                kind=EngineOutcomeKind.TERMINAL_FAILURE,
                error_code="DEADLINE_EXCEEDED",
                message="Run deadline elapsed during engine execution",
            )

        if outcome.kind is EngineOutcomeKind.COMPLETED:
            unresolved = await self.store.unresolved_tool_execution_ids(run.envelope.run_id)
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
            else:
                final = await self.store.finalize_success(
                    run_id=run.envelope.run_id,
                    activity_id=activity.activity_id,
                    fencing_token=activity.fencing_token,
                    assistant_text=io.assistant_text,
                    # SQLite finalize derives citations from committed EvidenceSet indexes;
                    # request-local engine emissions cannot influence durable authority.
                    citations=[],
                    now_ms=self.clock.now_ms(),
                )
        elif outcome.kind is EngineOutcomeKind.WAITING_INPUT:
            final = await self.store.wait_for_input(
                run_id=run.envelope.run_id,
                activity_id=activity.activity_id,
                fencing_token=activity.fencing_token,
                pending_input=outcome.pending_input or {"type": "GENERIC_INPUT"},
                now_ms=self.clock.now_ms(),
            )
        elif outcome.kind is EngineOutcomeKind.RETRYABLE_FAILURE and activity.attempt < self.max_model_attempts:
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
            final = await self.store.get_run(run.envelope.run_id)
        else:
            status = RunStatus.CANCELLED if outcome.kind is EngineOutcomeKind.CANCELLED else RunStatus.FAILED
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
        log_kv(
            logger, logging.INFO, "Runtime", "attempt settled",
            run_id=run.envelope.run_id, activity_id=activity.activity_id,
            engine=run.envelope.engine, status=final.status,
        )
        return final.status

    def _retry_delay_ms(self, attempt: int) -> int:
        base = min(30_000, 1000 * (2 ** max(0, attempt - 1)))
        return max(1, int(base * self.random.uniform(0.8, 1.2)))
