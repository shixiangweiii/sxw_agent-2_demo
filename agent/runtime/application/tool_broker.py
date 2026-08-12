from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence

from pydantic import ValidationError

from agent.runtime.application.tool_outputs import ToolResultAdapter, plain_json_output
from agent.runtime.domain.artifact import MAX_HTTP_RANGE_BYTES, ArtifactPurpose
from agent.runtime.domain.errors import (
    AttemptOwnershipLost,
    RuntimeFault,
    raise_if_ownership_lost,
)
from agent.runtime.domain.models import (
    ToolEffectClass,
    EvidenceSet,
    ToolExecutionOutput,
    ToolManifest,
    ToolResultEnvelope,
    ToolResultStatus,
    canonical_json,
    sha256_json,
)
from agent.runtime.ports.artifact import ArtifactStore
from agent.runtime.ports.clock import Clock, SystemClock
from agent.runtime.ports.store import ToolExecutionPreparation

ToolExecutor = Callable[[dict[str, Any], "ToolCallContext"], Any | Awaitable[Any]]
ReconcileHook = Callable[["ToolCallContext"], ToolResultEnvelope | None | Awaitable[ToolResultEnvelope | None]]


@dataclass(frozen=True)
class ToolCallContext:
    run_id: str
    parent_activity_id: str
    tool_activity_id: str
    tool_execution_id: str
    idempotency_key: str
    deadline_at_ms: int
    attempt: int
    clock: Clock
    prior_result_ref: str | None = None
    prior_external_object_id: str | None = None
    prior_preview: Any | None = None
    prior_error_code: str | None = None
    prior_error_message: str | None = None

    @property
    def remaining_ms(self) -> int:
        return max(0, self.deadline_at_ms - self.clock.now_ms())


@dataclass(frozen=True)
class _RegisteredTool:
    manifest: ToolManifest
    executor: ToolExecutor
    result_adapter: ToolResultAdapter
    reconcile: ReconcileHook | None = None


@dataclass(frozen=True)
class ToolBatchCall:
    logical_key: str
    tool_name: str
    arguments: dict[str, Any]
    framework_call_id: str | None = None
    manifest_override: ToolManifest | None = None


@dataclass(frozen=True)
class PreparedToolExecution:
    tool_execution_id: str
    logical_key: str
    tool_name: str
    request_digest: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class _SettlementAbort:
    kind: str
    code: str
    message: str
    http_status: int = 409
    details: dict[str, Any] | None = None

    @classmethod
    def from_error(cls, error: BaseException) -> "_SettlementAbort":
        if isinstance(error, RuntimeFault):
            return cls(
                "runtime",
                error.code,
                error.message,
                error.http_status,
                error.details,
            )
        if isinstance(error, AttemptOwnershipLost):
            return cls("ownership", error.code, str(error))
        if isinstance(error, (asyncio.CancelledError, GeneratorExit)):
            return cls("cancelled", "TOOL_BATCH_CANCELLED", "tool batch was cancelled")
        return cls(
            "runtime",
            "TOOL_BATCH_SETTLEMENT_ABORTED",
            f"tool batch settlement aborted after {type(error).__name__}",
            500,
        )

    def raise_error(self) -> None:
        if self.kind == "cancelled":
            raise asyncio.CancelledError(self.message)
        if self.kind == "ownership":
            raise AttemptOwnershipLost(self.code, self.message)
        raise RuntimeFault(
            self.code,
            self.message,
            self.http_status,
            dict(self.details) if self.details is not None else None,
        )


class ToolSettlementOrder:
    """One-attempt gate for ordered durable settlement of a concurrent batch.

    Executor work and durable ``DISPATCHED`` transitions remain concurrent.
    Each call waits only when it reaches its first ToolResult settlement.  A
    control failure aborts every still-waiting ordinal synchronously, so task
    cancellation or ownership loss cannot leave a waiter orphaned.
    """

    def __init__(self, size: int) -> None:
        if size <= 0:
            raise ValueError("ToolSettlementOrder size must be positive")
        loop = asyncio.get_running_loop()
        self._signals: list[asyncio.Future[_SettlementAbort | None]] = [
            loop.create_future() for _ in range(size)
        ]
        self._signals[0].set_result(None)
        self._next = 0
        self._abort: _SettlementAbort | None = None

    def turn(self, ordinal: int) -> "ToolSettlementTurn":
        if ordinal < 0 or ordinal >= len(self._signals):
            raise ValueError("tool settlement ordinal is outside the batch")
        return ToolSettlementTurn(self, ordinal)

    async def _wait(self, ordinal: int) -> None:
        if self._abort is not None:
            self._abort.raise_error()
        signal = await asyncio.shield(self._signals[ordinal])
        if self._abort is not None:
            self._abort.raise_error()
        if signal is not None:
            signal.raise_error()

    def _complete(self, ordinal: int) -> None:
        if self._abort is not None:
            return
        if ordinal != self._next:
            raise RuntimeError("tool settlement turn completed out of order")
        self._next += 1
        if self._next < len(self._signals):
            signal = self._signals[self._next]
            if not signal.done():
                signal.set_result(None)

    def _abort_all(self, error: BaseException) -> None:
        if self._abort is not None:
            return
        self._abort = _SettlementAbort.from_error(error)
        for signal in self._signals:
            if not signal.done():
                signal.set_result(self._abort)


class ToolSettlementTurn:
    """Single ordinal token handed to one ``execute_prepared`` invocation."""

    def __init__(self, order: ToolSettlementOrder, ordinal: int) -> None:
        self._order = order
        self._ordinal = ordinal
        self._acquired = False
        self._finished = False

    async def acquire(self) -> None:
        if self._finished:
            raise RuntimeError("tool settlement turn is already finished")
        if not self._acquired:
            await self._order._wait(self._ordinal)
            self._acquired = True

    def complete(self) -> None:
        if self._finished:
            return
        if not self._acquired:
            raise RuntimeError("tool settlement turn completed before acquire")
        self._order._complete(self._ordinal)
        self._finished = True

    def abort(self, error: BaseException) -> None:
        if self._finished:
            return
        self._finished = True
        self._order._abort_all(error)


class ToolBroker:
    """Effect-aware durable tool dispatch protocol.

    The Store commits PREPARED + TOOL_CALL before this class invokes external
    code.  Blob writes happen outside SQLite; metadata, result ref and
    TOOL_RESULT are then committed together by ``settle_tool_execution``.
    """

    def __init__(
        self,
        store: Any,
        artifact_store: ArtifactStore,
        *,
        clock: Clock | None = None,
        inline_result_max_bytes: int = 8192,
    ) -> None:
        self.store = store
        self.artifact_store = artifact_store
        self.clock = clock or SystemClock()
        self.inline_result_max_bytes = inline_result_max_bytes
        self._tools: dict[str, _RegisteredTool] = {}

    def register(
        self,
        manifest: ToolManifest,
        executor: ToolExecutor,
        *,
        result_adapter: ToolResultAdapter = plain_json_output,
        reconcile: ReconcileHook | None = None,
    ) -> None:
        if manifest.effect_class is ToolEffectClass.IDEMPOTENT_EFFECT and not manifest.supports_idempotency:
            raise ValueError(f"{manifest.name}: IDEMPOTENT_EFFECT requires supports_idempotency")
        if manifest.supports_reconcile and reconcile is None:
            raise ValueError(f"{manifest.name}: supports_reconcile requires a hook")
        if manifest.name in self._tools:
            raise ValueError(f"duplicate tool manifest: {manifest.name}")
        self._tools[manifest.name] = _RegisteredTool(
            manifest, executor, result_adapter, reconcile,
        )

    async def prepare_batch(
        self,
        *,
        run_id: str,
        parent_activity_id: str,
        fencing_token: int,
        calls: Sequence[ToolBatchCall],
    ) -> tuple[PreparedToolExecution, ...]:
        """Atomically freeze every stable ToolCall slot before dispatch."""

        preparations: list[ToolExecutionPreparation] = []
        resolved: list[tuple[ToolBatchCall, ToolManifest, str]] = []
        for call in calls:
            manifest = call.manifest_override
            if manifest is None:
                manifest = self._resolve_tool(call.tool_name).manifest
            elif manifest.name != call.tool_name:
                raise ValueError("manifest_override name disagrees with tool_name")
            request_digest = sha256_json(call.arguments)
            preparations.append(ToolExecutionPreparation(
                logical_key=call.logical_key,
                tool_name=call.tool_name,
                release_digest=manifest.release_digest,
                effect_class=manifest.effect_class.value,
                request_digest=request_digest,
                request=call.arguments,
                supports_reconcile=manifest.supports_reconcile,
                framework_call_id=call.framework_call_id,
            ))
            resolved.append((call, manifest, request_digest))
        rows = await self.store.prepare_tool_execution_batch(
            run_id=run_id,
            parent_activity_id=parent_activity_id,
            fencing_token=fencing_token,
            preparations=preparations,
            now_ms=self.clock.now_ms(),
        )
        return tuple(
            PreparedToolExecution(
                tool_execution_id=str(row["tool_execution_id"]),
                logical_key=call.logical_key,
                tool_name=call.tool_name,
                request_digest=request_digest,
                arguments=dict(call.arguments),
            )
            for (call, _manifest, request_digest), row in zip(resolved, rows, strict=True)
        )

    async def execute_prepared(
        self,
        *,
        prepared: PreparedToolExecution,
        parent_activity_id: str,
        fencing_token: int,
        deadline_at_ms: int,
        executor_override: ToolExecutor | None = None,
        result_adapter_override: ToolResultAdapter | None = None,
        manifest_override: ToolManifest | None = None,
        reconcile_override: ReconcileHook | None = None,
        settlement_turn: ToolSettlementTurn | None = None,
    ) -> ToolResultEnvelope:
        """Execute one already-prepared slot, optionally settling by batch ordinal."""

        try:
            tool = self._resolve_tool(
                prepared.tool_name,
                manifest_override=manifest_override,
                executor_override=executor_override,
                result_adapter_override=result_adapter_override,
                reconcile_override=reconcile_override,
            )
            execution = await self.store.get_tool_execution(prepared.tool_execution_id)
            if (
                execution["logical_key"] != prepared.logical_key
                or execution["tool_name"] != prepared.tool_name
                or execution["request_digest"] != prepared.request_digest
                or execution["release_digest"] != tool.manifest.release_digest
            ):
                raise RuntimeFault(
                    "TOOL_REPLAY_MISMATCH",
                    "prepared ToolExecution no longer matches its frozen request",
                    409,
                    {"tool_execution_id": prepared.tool_execution_id},
                )
            result = await self._execute_ledger(
                tool=tool,
                execution=execution,
                arguments=prepared.arguments,
                parent_activity_id=parent_activity_id,
                fencing_token=fencing_token,
                deadline_at_ms=deadline_at_ms,
                settlement_turn=settlement_turn,
            )
            if settlement_turn is not None:
                await settlement_turn.acquire()
                settlement_turn.complete()
            return result
        except BaseException as exc:
            if settlement_turn is not None:
                settlement_turn.abort(exc)
            raise

    async def materialize_committed_result(
        self,
        *,
        tool_execution_id: str,
        parent_activity_id: str,
        deadline_at_ms: int,
        manifest_override: ToolManifest | None = None,
        executor_override: ToolExecutor | None = None,
        result_adapter_override: ToolResultAdapter | None = None,
    ) -> ToolResultEnvelope:
        """Project a committed ledger result from its authoritative payload.

        ``read_artifact`` is intentionally re-executed as a bounded read.  For
        every other tool, a Broker-created large-result Artifact stores the
        complete ToolResult envelope and is re-materialized directly from CAS;
        the ledger preview is only a projection and must never become recovery
        input.
        """

        execution = await self.store.get_tool_execution(tool_execution_id)
        if execution["effect_status"] != "COMMITTED":
            raise RuntimeFault(
                "TOOL_RESULT_NOT_COMMITTED",
                "ToolExecution has no committed result to materialize",
                409,
                {"tool_execution_id": tool_execution_id},
            )
        tool_name = str(execution["tool_name"])
        if tool_name != "read_artifact":
            ledger_result = _tool_result_from_ledger(execution)
            document = _tool_result_document(execution)
            full_result_ref = document.get("full_result_ref")
            if full_result_ref is None:
                return ledger_result
            if (
                not isinstance(full_result_ref, str)
                or full_result_ref != ledger_result.result_ref
                or full_result_ref != execution.get("result_ref")
            ):
                raise RuntimeFault(
                    "TOOL_RESULT_LEDGER_MISMATCH",
                    "full ToolResult Artifact reference disagrees with its ledger",
                    500,
                    {"tool_execution_id": tool_execution_id},
                )
            return await self._materialize_result_artifact(
                execution=execution,
                ledger_result=ledger_result,
                artifact_id=full_result_ref,
                deadline_at_ms=deadline_at_ms,
            )
        tool = self._resolve_tool(
            tool_name,
            manifest_override=manifest_override,
            executor_override=executor_override,
            result_adapter_override=result_adapter_override,
        )
        arguments = json.loads(execution["request_json"])
        if tool.manifest.result_policy == "ARTIFACT_BOUNDED_READ":
            return await self._materialize_committed(
                tool, execution, arguments, parent_activity_id, deadline_at_ms,
            )
        return _tool_result_from_ledger(execution)

    async def _materialize_result_artifact(
        self,
        *,
        execution: dict[str, Any],
        ledger_result: ToolResultEnvelope,
        artifact_id: str,
        deadline_at_ms: int,
    ) -> ToolResultEnvelope:
        metadata = await self.store.get_artifact_metadata(artifact_id)
        if metadata.get("media_type") != "application/json":
            raise RuntimeFault(
                "TOOL_RESULT_CONTRACT_INVALID",
                "full ToolResult Artifact must use application/json",
                500,
                {"tool_execution_id": execution["tool_execution_id"]},
            )
        try:
            total_size = int(metadata["size_bytes"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeFault(
                "TOOL_RESULT_CONTRACT_INVALID",
                "full ToolResult Artifact metadata has an invalid size",
                500,
                {"tool_execution_id": execution["tool_execution_id"]},
            ) from exc
        if total_size <= 0:
            raise RuntimeFault(
                "TOOL_RESULT_CONTRACT_INVALID",
                "full ToolResult Artifact cannot be empty",
                500,
                {"tool_execution_id": execution["tool_execution_id"]},
            )

        chunks: list[bytes] = []
        offset = 0
        while offset < total_size:
            if self.clock.now_ms() >= deadline_at_ms:
                raise RuntimeFault(
                    "TOOL_MATERIALIZATION_DEADLINE_EXPIRED",
                    "Run deadline elapsed while materializing ToolResult Artifact",
                    409,
                    {"tool_execution_id": execution["tool_execution_id"]},
                )
            end = min(total_size, offset + MAX_HTTP_RANGE_BYTES)
            part = await self.artifact_store.read_range(
                artifact_id,
                start=offset,
                end_exclusive=end,
            )
            if (
                part.total_size != total_size
                or part.start != offset
                or part.end_exclusive != end
            ):
                raise RuntimeFault(
                    "TOOL_RESULT_CONTRACT_INVALID",
                    "full ToolResult Artifact range disagrees with metadata",
                    500,
                    {"tool_execution_id": execution["tool_execution_id"]},
                )
            chunks.append(part.data)
            offset = end

        try:
            raw = b"".join(chunks).decode("utf-8")
            value = json.loads(raw, parse_constant=_reject_json_constant)
            if not isinstance(value, dict):
                raise ValueError("ToolResult Artifact root must be an object")
            materialized = ToolResultEnvelope.model_validate(value)
        except (UnicodeDecodeError, ValueError, ValidationError) as exc:
            raise RuntimeFault(
                "TOOL_RESULT_CONTRACT_INVALID",
                "full ToolResult Artifact does not contain a valid current envelope",
                500,
                {"tool_execution_id": execution["tool_execution_id"]},
            ) from exc

        if materialized.result_ref is not None:
            raise RuntimeFault(
                "TOOL_RESULT_CONTRACT_INVALID",
                "full ToolResult Artifact must not recursively reference itself",
                500,
                {"tool_execution_id": execution["tool_execution_id"]},
            )
        for field_name in (
            "status",
            "error_code",
            "error_message",
            "external_object_id",
            "pending_input",
        ):
            if getattr(materialized, field_name) != getattr(ledger_result, field_name):
                raise RuntimeFault(
                    "TOOL_RESULT_LEDGER_MISMATCH",
                    f"full ToolResult Artifact {field_name} disagrees with its ledger",
                    500,
                    {"tool_execution_id": execution["tool_execution_id"]},
                )
        return materialized.model_copy(update={"result_ref": artifact_id})

    async def reconcile_only(
        self,
        *,
        tool_execution_id: str,
        parent_activity_id: str,
        fencing_token: int,
        expected_effect_revision: int,
        deadline_at_ms: int,
    ) -> dict[str, Any]:
        """Run only the persisted ToolExecution's query hook after cancel won.

        The Tool Activity is first moved onto its reconcile-only boundary.  A
        missing hook or release mismatch is then durably returned to manual;
        the original executor is never reachable from this method.
        """
        execution = await self.store.get_tool_execution(tool_execution_id)
        if (
            execution["effect_status"] != "RECONCILING"
            or int(execution["revision"]) != expected_effect_revision
        ):
            raise RuntimeFault(
                "TOOL_RECONCILIATION_MISMATCH",
                "reconcile-only marker does not match the frozen ToolEffect revision",
                409,
                {"tool_execution_id": tool_execution_id,
                 "effect_status": execution["effect_status"],
                 "expected_effect_revision": expected_effect_revision,
                 "actual_effect_revision": execution["revision"]},
            )
        execution = await self.store.mark_tool_reconciling(
            tool_execution_id=tool_execution_id,
            parent_activity_id=parent_activity_id,
            fencing_token=fencing_token,
            now_ms=self.clock.now_ms(),
        )
        tool = self._tools.get(str(execution["tool_name"]))
        if tool is None or tool.reconcile is None or not tool.manifest.supports_reconcile:
            await self._require_manual(
                execution,
                parent_activity_id,
                fencing_token,
                code="TOOL_RECONCILE_HOOK_UNAVAILABLE",
                message="the frozen ToolExecution has no registered reconcile hook",
            )
            return await self.store.get_tool_execution(tool_execution_id)
        if tool.manifest.release_digest != execution["release_digest"]:
            await self._require_manual(
                execution,
                parent_activity_id,
                fencing_token,
                code="TOOL_RECONCILE_RELEASE_MISMATCH",
                message="registered reconcile hook does not match the frozen Tool release",
            )
            return await self.store.get_tool_execution(tool_execution_id)
        if not bool(execution.get("supports_reconcile")):
            await self._require_manual(
                execution,
                parent_activity_id,
                fencing_token,
                code="TOOL_RECONCILE_UNSUPPORTED",
                message="the frozen ToolExecution did not declare reconcile capability",
            )
            return await self.store.get_tool_execution(tool_execution_id)

        recovered = await self._resolve_uncertain(
            tool,
            execution,
            parent_activity_id,
            fencing_token,
            deadline_at_ms,
        )
        execution = await self.store.get_tool_execution(tool_execution_id)
        if recovered is not None and recovered.status is ToolResultStatus.FAILURE:
            await self.store.settle_tool_execution(
                tool_execution_id=tool_execution_id,
                parent_activity_id=parent_activity_id,
                fencing_token=fencing_token,
                effect_status="FAILED",
                result=recovered.model_dump(mode="json"),
                result_ref=recovered.result_ref,
                error={
                    "code": recovered.error_code or "TOOL_RECONCILE_FAILED",
                    "message": recovered.error_message
                    or "external reconciliation confirmed failure",
                },
                external_object_id=None,
                now_ms=self.clock.now_ms(),
            )
        elif recovered is not None and recovered.status in {
            ToolResultStatus.SUCCESS,
            ToolResultStatus.NO_OUTPUT,
        }:
            await self._commit_result(
                execution,
                parent_activity_id,
                fencing_token,
                recovered,
                return_full=False,
            )
        elif recovered is not None and recovered.status is ToolResultStatus.UNKNOWN:
            await self.store.settle_tool_execution(
                tool_execution_id=tool_execution_id,
                parent_activity_id=parent_activity_id,
                fencing_token=fencing_token,
                effect_status="MANUAL_REQUIRED",
                result=recovered.model_dump(mode="json"),
                result_ref=recovered.result_ref,
                error={
                    "code": recovered.error_code,
                    "message": recovered.error_message,
                },
                external_object_id=recovered.external_object_id,
                now_ms=self.clock.now_ms(),
            )
        else:
            await self._require_manual(
                execution,
                parent_activity_id,
                fencing_token,
                code="TOOL_RECONCILE_INCONCLUSIVE",
                message="reconcile hook failed or returned no conclusive result",
            )
        return await self.store.get_tool_execution(tool_execution_id)

    async def execute(
        self,
        *,
        run_id: str,
        parent_activity_id: str,
        fencing_token: int,
        logical_key: str,
        tool_name: str,
        arguments: dict[str, Any],
        deadline_at_ms: int,
        manifest_override: ToolManifest | None = None,
        executor_override: ToolExecutor | None = None,
        result_adapter_override: ToolResultAdapter | None = None,
        reconcile_override: ReconcileHook | None = None,
        settlement_turn: ToolSettlementTurn | None = None,
    ) -> ToolResultEnvelope:
        """ADK single-call wrapper over prepare+execute_prepared."""

        tool = self._resolve_tool(
            tool_name,
            manifest_override=manifest_override,
            executor_override=executor_override,
            result_adapter_override=result_adapter_override,
            reconcile_override=reconcile_override,
        )
        prepared = await self.prepare_batch(
            run_id=run_id,
            parent_activity_id=parent_activity_id,
            fencing_token=fencing_token,
            calls=(ToolBatchCall(
                logical_key=logical_key,
                tool_name=tool_name,
                arguments=arguments,
                manifest_override=tool.manifest,
            ),),
        )
        return await self.execute_prepared(
            prepared=prepared[0],
            parent_activity_id=parent_activity_id,
            fencing_token=fencing_token,
            deadline_at_ms=deadline_at_ms,
            manifest_override=tool.manifest,
            executor_override=tool.executor,
            result_adapter_override=tool.result_adapter,
            reconcile_override=tool.reconcile,
            settlement_turn=settlement_turn,
        )

    def _resolve_tool(
        self,
        tool_name: str,
        *,
        manifest_override: ToolManifest | None = None,
        executor_override: ToolExecutor | None = None,
        result_adapter_override: ToolResultAdapter | None = None,
        reconcile_override: ReconcileHook | None = None,
    ) -> _RegisteredTool:
        if manifest_override is not None or executor_override is not None:
            if manifest_override is None or executor_override is None:
                raise ValueError("manifest_override and executor_override must be supplied together")
            if manifest_override.name != tool_name:
                raise ValueError("manifest_override name disagrees with tool_name")
            return _RegisteredTool(
                manifest_override,
                executor_override,
                result_adapter_override or plain_json_output,
                reconcile_override,
            )
        try:
            return self._tools[tool_name]
        except KeyError as exc:
            raise RuntimeFault(
                "TOOL_NOT_REGISTERED", f"tool is not registered: {tool_name}",
            ) from exc

    async def _execute_ledger(
        self,
        *,
        tool: _RegisteredTool,
        execution: dict[str, Any],
        arguments: dict[str, Any],
        parent_activity_id: str,
        fencing_token: int,
        deadline_at_ms: int,
        settlement_turn: ToolSettlementTurn | None,
    ) -> ToolResultEnvelope:
        manifest = tool.manifest
        run_id = str(execution["run_id"])

        async def settlement_boundary() -> None:
            if settlement_turn is not None:
                await settlement_turn.acquire()

        frozen_effect_class = ToolEffectClass(execution["effect_class"])
        safe_replay = (
            frozen_effect_class is ToolEffectClass.READ_ONLY
            or (
                frozen_effect_class is ToolEffectClass.IDEMPOTENT_EFFECT
                and manifest.supports_idempotency
                and bool(execution["idempotency_key"])
            )
        )
        while True:
            status = str(execution["effect_status"])
            if status == "COMMITTED":
                if manifest.result_policy == "ARTIFACT_BOUNDED_READ":
                    result = await self._materialize_committed(
                        tool, execution, arguments, parent_activity_id, deadline_at_ms,
                    )
                    await settlement_boundary()
                    return result
                await settlement_boundary()
                return _tool_result_from_ledger(execution)
            if status == "MANUAL_REQUIRED":
                await settlement_boundary()
                if execution.get("result_json"):
                    return _tool_result_from_ledger(execution)
                return ToolResultEnvelope(
                    status=ToolResultStatus.UNKNOWN,
                    error_code="TOOL_EFFECT_UNKNOWN",
                    error_message="manual reconciliation is required",
                )
            if status == "FAILED" and (
                execution.get("reconcile_state") == "MANUAL_FAILED"
                or not safe_replay
                or int(execution["attempt"]) >= manifest.max_attempts
            ):
                await settlement_boundary()
                return _tool_result_from_ledger(execution)

            if status in {"DISPATCHED", "UNKNOWN", "RECONCILING"}:
                recovered = await self._resolve_uncertain(
                    tool,
                    execution,
                    parent_activity_id,
                    fencing_token,
                    deadline_at_ms,
                )
                execution = await self.store.get_tool_execution(
                    execution["tool_execution_id"]
                )
                if recovered is not None and recovered.status is ToolResultStatus.FAILURE:
                    await settlement_boundary()
                    await self.store.settle_tool_execution(
                        tool_execution_id=execution["tool_execution_id"],
                        parent_activity_id=parent_activity_id,
                        fencing_token=fencing_token,
                        effect_status="FAILED",
                        result=recovered.model_dump(mode="json"),
                        result_ref=recovered.result_ref,
                        error={
                            "code": recovered.error_code or "TOOL_RECONCILE_FAILED",
                            "message": recovered.error_message
                            or "external reconciliation confirmed failure",
                        },
                        external_object_id=None,
                        now_ms=self.clock.now_ms(),
                    )
                    return recovered
                if recovered is not None and recovered.status in {
                    ToolResultStatus.SUCCESS,
                    ToolResultStatus.NO_OUTPUT,
                }:
                    await settlement_boundary()
                    return await self._commit_result(
                        execution, parent_activity_id, fencing_token, recovered,
                        return_full=manifest.result_policy == "ARTIFACT_BOUNDED_READ",
                    )
                if recovered is not None and recovered.status is ToolResultStatus.UNKNOWN:
                    await settlement_boundary()
                    execution = await self.store.settle_tool_execution(
                        tool_execution_id=execution["tool_execution_id"],
                        parent_activity_id=parent_activity_id,
                        fencing_token=fencing_token,
                        effect_status="UNKNOWN",
                        result=recovered.model_dump(mode="json"),
                        result_ref=recovered.result_ref,
                        error={
                            "code": recovered.error_code,
                            "message": recovered.error_message,
                        },
                        external_object_id=recovered.external_object_id,
                        now_ms=self.clock.now_ms(),
                    )
                if not safe_replay or int(execution["attempt"]) >= manifest.max_attempts:
                    await settlement_boundary()
                    return await self._require_manual(
                        execution, parent_activity_id, fencing_token,
                    )

            if int(execution["attempt"]) >= manifest.max_attempts:
                await settlement_boundary()
                if execution.get("result_json"):
                    return _tool_result_from_ledger(execution)
                return await self._require_manual(
                    execution, parent_activity_id, fencing_token,
                )

            execution = await self.store.mark_tool_dispatched(
                tool_execution_id=execution["tool_execution_id"],
                parent_activity_id=parent_activity_id,
                fencing_token=fencing_token,
                now_ms=self.clock.now_ms(),
            )
            remaining_ms = deadline_at_ms - self.clock.now_ms()
            if remaining_ms <= 0:
                raise RuntimeFault(
                    "TOOL_DISPATCH_DEADLINE_EXPIRED",
                    "Run deadline elapsed after durable dispatch and before executor entry",
                    409,
                    {"tool_execution_id": execution["tool_execution_id"]},
                )
            ctx = ToolCallContext(
                run_id=run_id,
                parent_activity_id=parent_activity_id,
                tool_activity_id=execution["activity_id"],
                tool_execution_id=execution["tool_execution_id"],
                idempotency_key=execution["idempotency_key"],
                deadline_at_ms=deadline_at_ms,
                attempt=execution["attempt"],
                clock=self.clock,
                **_prior_context(execution),
            )
            timeout = min(
                manifest.timeout_seconds,
                remaining_ms / 1000,
            )
            try:
                async with asyncio.timeout(timeout):
                    value = tool.executor(arguments, ctx)
                    if inspect.isawaitable(value):
                        value = await value
            except AttemptOwnershipLost:
                # Attempt ownership is Runtime control flow, not a Tool result.
                # In particular, do not mutate the already-dispatched ledger:
                # lease recovery under the new fence is the only authority
                # allowed to classify that uncertain effect.
                raise
            except TimeoutError as exc:
                await settlement_boundary()
                result, execution = await self._settle_dispatch_failure(
                    tool, execution, parent_activity_id, fencing_token,
                    code="TOOL_TIMEOUT", message=str(exc) or "tool timed out",
                )
            except RuntimeFault as exc:
                # A RuntimeFault raised by the executor is still an execution
                # outcome once DISPATCHED is durable.  First exclude ownership
                # loss, then settle effect-aware (READ_ONLY=FAILED, every
                # effectful class=UNKNOWN) before preserving the Runtime code.
                raise_if_ownership_lost(exc)
                await settlement_boundary()
                await self._settle_dispatch_failure(
                    tool,
                    execution,
                    parent_activity_id,
                    fencing_token,
                    code=exc.code,
                    message=exc.message,
                )
                raise
            except ValidationError as exc:
                if not _is_tool_output_validation_error(exc):
                    await settlement_boundary()
                    result, execution = await self._settle_dispatch_failure(
                        tool,
                        execution,
                        parent_activity_id,
                        fencing_token,
                        code=type(exc).__name__,
                        message=str(exc),
                    )
                else:
                    fault = RuntimeFault(
                        "TOOL_RESULT_CONTRACT_INVALID",
                        "tool returned a non-JSON-safe typed result",
                        500,
                        {"tool_name": tool.manifest.name},
                    )
                    await settlement_boundary()
                    await self._settle_dispatch_failure(
                        tool,
                        execution,
                        parent_activity_id,
                        fencing_token,
                        code=fault.code,
                        message=fault.message,
                    )
                    raise fault from exc
            except Exception as exc:  # after dispatch, side-effect failures are conservative
                await settlement_boundary()
                result, execution = await self._settle_dispatch_failure(
                    tool, execution, parent_activity_id, fencing_token,
                    code=type(exc).__name__, message=str(exc),
                )
            else:
                try:
                    output = self._adapt_execution_output(tool, value, execution)
                except RuntimeFault as exc:
                    raise_if_ownership_lost(exc)
                    await settlement_boundary()
                    await self._settle_dispatch_failure(
                        tool,
                        execution,
                        parent_activity_id,
                        fencing_token,
                        code=exc.code,
                        message=exc.message,
                    )
                    raise
                result = output.result
                if result.status not in {
                    ToolResultStatus.FAILURE,
                    ToolResultStatus.UNKNOWN,
                }:
                    # Keep Store/Artifact commit failures outside the executor exception
                    # classifier: a stale fence or SQLite failure is not a Tool failure.
                    await settlement_boundary()
                    return await self._commit_result(
                        execution,
                        parent_activity_id,
                        fencing_token,
                        result,
                        evidence=output.evidence,
                        return_full=manifest.result_policy == "ARTIFACT_BOUNDED_READ",
                    )
                await settlement_boundary()
                result, execution = await self._settle_dispatch_failure(
                    tool,
                    execution,
                    parent_activity_id,
                    fencing_token,
                    code=result.error_code or result.status.value,
                    message=result.error_message or "tool reported an unsuccessful result",
                    reported_result=result,
                )

            if safe_replay and int(execution["attempt"]) < manifest.max_attempts:
                continue
            if result.status is ToolResultStatus.UNKNOWN:
                await settlement_boundary()
                return await self._require_manual(
                    execution, parent_activity_id, fencing_token,
                )
            await settlement_boundary()
            return result

    async def _materialize_committed(
        self,
        tool: _RegisteredTool,
        execution: dict[str, Any],
        arguments: dict[str, Any],
        parent_activity_id: str,
        deadline_at_ms: int,
    ) -> ToolResultEnvelope:
        """Re-read bounded Artifact bytes without creating another dispatch fact."""
        context = ToolCallContext(
            run_id=execution["run_id"],
            parent_activity_id=parent_activity_id,
            tool_activity_id=execution["activity_id"],
            tool_execution_id=execution["tool_execution_id"],
            idempotency_key=execution["idempotency_key"],
            deadline_at_ms=deadline_at_ms,
            attempt=execution["attempt"],
            clock=self.clock,
            **_prior_context(execution),
        )
        timeout = min(
            tool.manifest.timeout_seconds,
            max(0.001, (deadline_at_ms - self.clock.now_ms()) / 1000),
        )
        async with asyncio.timeout(timeout):
            value = tool.executor(arguments, context)
            if inspect.isawaitable(value):
                value = await value
        output = self._adapt_execution_output(tool, value, execution)
        if output.evidence is not None:
            raise RuntimeFault(
                "EVIDENCE_CONTRACT_INVALID",
                "bounded Artifact materialization cannot produce EvidenceSet",
                500,
            )
        return output.result

    def _adapt_execution_output(
        self,
        tool: _RegisteredTool,
        value: Any,
        execution: dict[str, Any],
    ) -> ToolExecutionOutput:
        try:
            output = tool.result_adapter(value)
        except RuntimeFault:
            raise
        except Exception as exc:
            raise RuntimeFault(
                "TOOL_RESULT_CONTRACT_INVALID",
                "tool result adapter rejected its protocol value",
                500,
                {"tool_name": tool.manifest.name, "reason": str(exc)},
            ) from exc
        if not isinstance(output, ToolExecutionOutput):
            raise RuntimeFault(
                "TOOL_RESULT_CONTRACT_INVALID",
                "tool result adapter must return ToolExecutionOutput",
                500,
                {"tool_name": tool.manifest.name},
            )
        try:
            output = ToolExecutionOutput.model_validate(output.model_dump(mode="python"))
        except Exception as exc:
            raise RuntimeFault(
                "TOOL_RESULT_CONTRACT_INVALID",
                "tool result is not strict JSON-safe ToolExecutionOutput",
                500,
                {"tool_name": tool.manifest.name},
            ) from exc
        self._validate_evidence_identity(output, execution)
        return output

    @staticmethod
    def _validate_evidence_identity(
        output: ToolExecutionOutput,
        execution: dict[str, Any],
    ) -> None:
        evidence = output.evidence
        if evidence is None and execution["tool_name"] == "knowledge_search":
            raise RuntimeFault(
                "EVIDENCE_CONTRACT_INVALID",
                "knowledge_search SUCCESS requires a current EvidenceSet",
                500,
                {"tool_execution_id": execution["tool_execution_id"]},
            )
        if evidence is None:
            return
        if execution["tool_name"] != "knowledge_search":
            raise RuntimeFault(
                "EVIDENCE_CONTRACT_INVALID",
                "only knowledge_search may produce EvidenceSet",
                500,
            )
        expected = {
            "run_id": str(execution["run_id"]),
            "activity_id": str(execution["activity_id"]),
            "tool_execution_id": str(execution["tool_execution_id"]),
        }
        actual = {
            "run_id": evidence.run_id,
            "activity_id": evidence.activity_id,
            "tool_execution_id": evidence.tool_execution_id,
        }
        if actual != expected:
            raise RuntimeFault(
                "EVIDENCE_CONTRACT_INVALID",
                "EvidenceSet identity disagrees with ToolExecution authority",
                500,
                {"expected": expected, "actual": actual},
            )

    async def _require_manual(
        self,
        execution: dict[str, Any],
        parent_activity_id: str,
        fencing_token: int,
        *,
        code: str = "TOOL_EFFECT_UNKNOWN",
        message: str = "external effect could not be confirmed; manual reconciliation required",
    ) -> ToolResultEnvelope:
        if execution["effect_status"] == "MANUAL_REQUIRED":
            if execution.get("result_json"):
                return _tool_result_from_ledger(execution)
        if execution["effect_status"] == "DISPATCHED":
            unknown = (
                _tool_result_from_ledger(execution).model_copy(update={
                    "status": ToolResultStatus.UNKNOWN,
                    "error_code": "TOOL_ACK_LOST",
                    "error_message": "dispatch outcome was not committed before recovery",
                })
                if execution.get("result_json")
                else ToolResultEnvelope(
                    status=ToolResultStatus.UNKNOWN,
                    error_code="TOOL_ACK_LOST",
                    error_message="dispatch outcome was not committed before recovery",
                )
            )
            execution = await self.store.settle_tool_execution(
                tool_execution_id=execution["tool_execution_id"],
                parent_activity_id=parent_activity_id,
                fencing_token=fencing_token,
                effect_status="UNKNOWN",
                result=unknown.model_dump(mode="json"),
                result_ref=unknown.result_ref,
                error={"code": unknown.error_code, "message": unknown.error_message},
                external_object_id=unknown.external_object_id,
                now_ms=self.clock.now_ms(),
            )
        prior = (
            _tool_result_from_ledger(execution)
            if execution.get("result_json")
            else ToolResultEnvelope(
                status=ToolResultStatus.UNKNOWN,
                error_code=code,
                error_message=message,
            )
        )
        manual = prior.model_copy(update={
            "status": ToolResultStatus.UNKNOWN,
            "error_code": _bounded_error_code(code, "TOOL_EFFECT_UNKNOWN"),
            "error_message": _bounded_error_message(
                message, "manual reconciliation is required",
            ),
        })
        settled_execution = await self.store.settle_tool_execution(
            tool_execution_id=execution["tool_execution_id"],
            parent_activity_id=parent_activity_id,
            fencing_token=fencing_token,
            effect_status="MANUAL_REQUIRED",
            result=manual.model_dump(mode="json"), result_ref=manual.result_ref,
            error={"code": manual.error_code, "message": manual.error_message},
            external_object_id=manual.external_object_id, now_ms=self.clock.now_ms(),
        )
        return _tool_result_from_ledger(settled_execution)

    async def _resolve_uncertain(
        self,
        tool: _RegisteredTool,
        execution: dict[str, Any],
        parent_activity_id: str,
        fencing_token: int,
        deadline_at_ms: int,
    ) -> ToolResultEnvelope | None:
        if (
            execution["effect_status"] == "MANUAL_REQUIRED"
            or tool.reconcile is None
            or self.clock.now_ms() >= deadline_at_ms
        ):
            return None
        execution = await self.store.mark_tool_reconciling(
            tool_execution_id=execution["tool_execution_id"],
            parent_activity_id=parent_activity_id,
            fencing_token=fencing_token,
            now_ms=self.clock.now_ms(),
        )
        remaining_ms = deadline_at_ms - self.clock.now_ms()
        if remaining_ms <= 0:
            return None
        ctx = ToolCallContext(
            run_id=execution["run_id"], parent_activity_id=parent_activity_id,
            tool_activity_id=execution["activity_id"],
            tool_execution_id=execution["tool_execution_id"],
            idempotency_key=execution["idempotency_key"], deadline_at_ms=deadline_at_ms,
            attempt=execution["attempt"], clock=self.clock,
            **_prior_context(execution),
        )
        timeout = remaining_ms / 1000
        try:
            async with asyncio.timeout(timeout):
                value = tool.reconcile(ctx)
                if inspect.isawaitable(value):
                    value = await value
                return value
        except (RuntimeFault, AttemptOwnershipLost):
            raise
        except Exception:
            return None

    async def _settle_dispatch_failure(
        self,
        tool: _RegisteredTool,
        execution: dict[str, Any],
        parent_activity_id: str,
        fencing_token: int,
        *,
        code: str,
        message: str,
        reported_result: ToolResultEnvelope | None = None,
    ) -> tuple[ToolResultEnvelope, dict[str, Any]]:
        safe_failure = tool.manifest.effect_class is ToolEffectClass.READ_ONLY
        status = "FAILED" if safe_failure else "UNKNOWN"
        code = _bounded_error_code(code, "TOOL_EXECUTION_FAILED")
        message = _bounded_error_message(message, "tool execution failed")
        if reported_result is None:
            result = ToolResultEnvelope(
                status=ToolResultStatus.FAILURE if safe_failure else ToolResultStatus.UNKNOWN,
                error_code=code,
                error_message=message,
            )
        else:
            result = reported_result
            target_result_status = (
                ToolResultStatus.FAILURE
                if safe_failure
                else ToolResultStatus.UNKNOWN
            )
            if result.status is not target_result_status:
                result = result.model_copy(update={"status": target_result_status})
            try:
                encoded_preview = canonical_json(result.preview).encode("utf-8")
            except (TypeError, ValueError):
                encoded_preview = b""
            if len(encoded_preview) > self.inline_result_max_bytes:
                result = result.model_copy(update={
                    "preview": encoded_preview[: self.inline_result_max_bytes].decode(
                        "utf-8", errors="replace",
                    )
                })
        settled = await self.store.settle_tool_execution(
            tool_execution_id=execution["tool_execution_id"],
            parent_activity_id=parent_activity_id,
            fencing_token=fencing_token,
            effect_status=status,
            result=result.model_dump(mode="json"), result_ref=result.result_ref,
            error={"code": code, "message": message},
            external_object_id=result.external_object_id,
            retry_at=(
                self.clock.now_ms()
                if safe_failure and int(execution["attempt"]) < tool.manifest.max_attempts
                else None
            ),
            now_ms=self.clock.now_ms(),
        )
        return result, settled

    async def _commit_result(
        self,
        execution: dict[str, Any],
        parent_activity_id: str,
        fencing_token: int,
        result: ToolResultEnvelope,
        *,
        evidence: EvidenceSet | None = None,
        return_full: bool = False,
    ) -> ToolResultEnvelope:
        original_payload = result.model_dump(mode="json")
        evidence_index: list[dict[str, Any]] = []
        if execution["tool_name"] == "knowledge_search":
            if evidence is None:
                raise RuntimeFault(
                    "EVIDENCE_CONTRACT_INVALID",
                    "knowledge_search SUCCESS requires a current EvidenceSet",
                    500,
                    {"tool_execution_id": execution["tool_execution_id"]},
                )
            if not isinstance(result.preview, dict):
                raise RuntimeFault(
                    "EVIDENCE_CONTRACT_INVALID",
                    "knowledge_search preview must be an object",
                    500,
                )
            evidence_index = _compact_evidence_index(evidence)
            result = result.model_copy(
                update={"preview": _bounded_knowledge_preview(result.preview, self.inline_result_max_bytes)}
            )
            original_payload = result.model_dump(mode="json")

        serialized = canonical_json(
            evidence.model_dump(mode="json") if evidence is not None else original_payload
        ).encode("utf-8")
        result_ref: str | None = result.result_ref
        artifact_metadata: dict[str, Any] | None = None
        full_result_ref: str | None = None
        stored = result
        artifact_relation = (
            "ARTIFACT_READ" if execution["tool_name"] == "read_artifact" else "TOOL_RESULT"
        )
        if evidence is not None or len(serialized) > self.inline_result_max_bytes:
            ref = None
            if result_ref is None or evidence is not None:
                ref = await self.artifact_store.put_bytes(
                    serialized,
                    purpose=ArtifactPurpose.INTERNAL,
                    media_type=(
                        "application/vnd.sxw.evidence-set+json"
                        if evidence is not None
                        else "application/json"
                    ),
                    filename=f"{execution['tool_execution_id']}.json",
                )
                result_ref = ref.artifact_id
                if evidence is None:
                    full_result_ref = result_ref
            if evidence is None:
                preview: Any = serialized[: self.inline_result_max_bytes].decode(
                    "utf-8", errors="replace"
                )
                stored = result.model_copy(update={"preview": preview, "result_ref": result_ref})
            else:
                stored = result.model_copy(update={"result_ref": result_ref})
                artifact_relation = "EVIDENCE_SET"
            if ref is not None:
                artifact_metadata = {
                    "artifact_id": ref.artifact_id,
                    "sha256": ref.digest_sha256,
                    "size_bytes": ref.size_bytes,
                    "media_type": ref.media_type,
                    "storage_path": f"sha256/{ref.artifact_id[:2]}/{ref.artifact_id}",
                    "created_at": int(ref.created_at.timestamp() * 1000),
                }
        persisted_result = stored.model_dump(mode="json")
        if full_result_ref is not None:
            # Internal current-codec marker: ``result_ref`` alone can also be a
            # Tool-owned output Artifact, so recovery must not guess that every
            # reference contains a serialized ToolResult envelope.
            persisted_result["full_result_ref"] = full_result_ref
        if evidence is not None:
            # The complete content lives only in Artifact.  This compact index is sufficient
            # for deterministic citation generation inside the finalize transaction.
            persisted_result["evidence_set_ref"] = result_ref
            persisted_result["evidence_index"] = evidence_index
        settled_execution = await self.store.settle_tool_execution(
            tool_execution_id=execution["tool_execution_id"],
            parent_activity_id=parent_activity_id,
            fencing_token=fencing_token,
            effect_status="COMMITTED",
            result=persisted_result, result_ref=result_ref,
            error=None, external_object_id=result.external_object_id,
            artifact_metadata=artifact_metadata, artifact_relation=artifact_relation,
            now_ms=self.clock.now_ms(),
        )
        durable_result = _tool_result_from_ledger(settled_execution)
        if return_full:
            return result.model_copy(update={
                "result_ref": durable_result.result_ref,
                "external_object_id": durable_result.external_object_id,
            })
        return durable_result


def _compact_evidence_index(evidence_set: EvidenceSet) -> list[dict[str, Any]]:
    keep = (
        "n", "evidence_id", "title", "doc_id", "document_id",
        "document_version_id", "index_version", "content_hash", "dataset_id", "scope",
        "query_id", "page", "span_start", "span_end",
    )
    compact: list[dict[str, Any]] = []
    for evidence_item in evidence_set.evidence:
        item = evidence_item.model_dump(mode="json")
        entry: dict[str, Any] = {}
        for key in keep:
            value = item.get(key)
            entry[key] = _truncate_utf8(value, 2048) if isinstance(value, str) else value
        compact.append(entry)
    return compact


def _bounded_knowledge_preview(value: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    if max_bytes < len(b'{"hits":[]}'):
        return {}
    base: dict[str, Any] = {"hits": []}
    for key in ("count", "degraded"):
        if key in value:
            base[key] = value[key]
    if "note" in value:
        note_budget = max(0, max_bytes - len(canonical_json(base).encode("utf-8")) - 32)
        base["note"] = _truncate_utf8(str(value["note"]), note_budget)
    used = len(canonical_json(base).encode("utf-8"))
    for raw in value.get("hits") or []:
        if not isinstance(raw, dict) or used >= max_bytes:
            break
        hit = {
            key: (
                _truncate_utf8(str(raw.get(key) or ""), 512)
                if key in {"title", "doc_id"}
                else raw.get(key)
            )
            for key in ("n", "title", "doc_id")
            if key in raw
        }
        candidate = dict(base)
        candidate["hits"] = [*base["hits"], hit]
        remaining = max(
            0, max_bytes - len(canonical_json(candidate).encode("utf-8")) - 128
        )
        content = str(raw.get("content") or "")
        hit["content"] = _truncate_utf8(content, remaining)
        base["hits"].append(hit)
        used = len(canonical_json(base).encode("utf-8"))
        if used > max_bytes:
            base["hits"].pop()
            break
    if len(base["hits"]) < len(value.get("hits") or []):
        candidate = dict(base, truncated=True)
        if len(canonical_json(candidate).encode("utf-8")) <= max_bytes:
            base = candidate
    return base


def _truncate_utf8(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def _tool_result_document(execution: dict[str, Any]) -> dict[str, Any]:
    raw_result = execution.get("result_json")
    try:
        value = json.loads(raw_result) if isinstance(raw_result, str) else raw_result
    except (TypeError, ValueError) as exc:
        raise RuntimeFault(
            "TOOL_RESULT_LEDGER_INVALID",
            "persisted ToolResult is not valid JSON",
            500,
            {"tool_execution_id": execution.get("tool_execution_id")},
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeFault(
            "TOOL_RESULT_LEDGER_INVALID",
            "persisted ToolResult must be an object",
            500,
            {"tool_execution_id": execution.get("tool_execution_id")},
        )
    return value


def _tool_result_from_ledger(execution: dict[str, Any]) -> ToolResultEnvelope:
    """Project the internal ToolExecution ledger row back to public v1.

    Knowledge retrieval and large-result recovery append internal references
    beside the envelope.  Those enrichments are not part of the frozen public
    schema, so replay extracts only ToolResultEnvelope fields.
    """
    value = _tool_result_document(execution)

    public = {
        key: value[key]
        for key in ToolResultEnvelope.model_fields
        if key in value
    }
    for field_name in ("result_ref", "external_object_id"):
        ledger_value = execution.get(field_name)
        nested_value = public.get(field_name)
        if ledger_value is not None and nested_value not in {None, ledger_value}:
            raise RuntimeFault(
                "TOOL_RESULT_LEDGER_MISMATCH",
                f"persisted ToolResult {field_name} disagrees with its ledger column",
                500,
                {
                    "tool_execution_id": execution.get("tool_execution_id"),
                    "field": field_name,
                },
            )
        if nested_value is None and ledger_value is not None:
            public[field_name] = ledger_value
    return ToolResultEnvelope.model_validate(public)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value} is forbidden")


def _is_tool_output_validation_error(error: ValidationError) -> bool:
    return error.title in {"ToolExecutionOutput", "ToolResultEnvelope"}


def _prior_context(execution: dict[str, Any]) -> dict[str, Any]:
    if not execution.get("result_json"):
        return {
            "prior_result_ref": execution.get("result_ref"),
            "prior_external_object_id": execution.get("external_object_id"),
        }
    prior = _tool_result_from_ledger(execution)
    return {
        "prior_result_ref": prior.result_ref,
        "prior_external_object_id": prior.external_object_id,
        "prior_preview": prior.preview,
        "prior_error_code": prior.error_code,
        "prior_error_message": prior.error_message,
    }


def _bounded_error_code(value: Any, fallback: str) -> str:
    text = str(value) if value is not None and value != "" else fallback
    return text[:128]


def _bounded_error_message(value: Any, fallback: str) -> str:
    text = str(value) if value is not None and value != "" else fallback
    return text[:8192]
