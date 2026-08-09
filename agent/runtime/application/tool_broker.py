from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from agent.runtime.domain.artifact import ArtifactPurpose
from agent.runtime.domain.errors import RuntimeFault
from agent.runtime.domain.models import (
    ToolEffectClass,
    ToolManifest,
    ToolResultEnvelope,
    ToolResultStatus,
    canonical_json,
    ms_to_rfc3339,
    sha256_json,
)
from agent.runtime.ports.artifact import ArtifactStore
from agent.runtime.ports.clock import Clock, SystemClock

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
    reconcile: ReconcileHook | None = None


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
        reconcile: ReconcileHook | None = None,
    ) -> None:
        if manifest.effect_class is ToolEffectClass.IDEMPOTENT_EFFECT and not manifest.supports_idempotency:
            raise ValueError(f"{manifest.name}: IDEMPOTENT_EFFECT requires supports_idempotency")
        if manifest.supports_reconcile and reconcile is None:
            raise ValueError(f"{manifest.name}: supports_reconcile requires a hook")
        if manifest.name in self._tools:
            raise ValueError(f"duplicate tool manifest: {manifest.name}")
        self._tools[manifest.name] = _RegisteredTool(manifest, executor, reconcile)

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
        reconcile_override: ReconcileHook | None = None,
    ) -> ToolResultEnvelope:
        if manifest_override is not None and executor_override is not None:
            tool = _RegisteredTool(manifest_override, executor_override, reconcile_override)
        else:
            try:
                tool = self._tools[tool_name]
            except KeyError as exc:
                raise RuntimeFault("TOOL_NOT_REGISTERED", f"tool is not registered: {tool_name}") from exc
        manifest = tool.manifest
        request_digest = sha256_json(arguments)
        execution = await self.store.prepare_tool_execution(
            run_id=run_id,
            parent_activity_id=parent_activity_id,
            fencing_token=fencing_token,
            logical_key=logical_key,
            tool_name=tool_name,
            release_digest=manifest.release_digest,
            effect_class=manifest.effect_class,
            request_digest=request_digest,
            request=arguments,
            supports_reconcile=manifest.supports_reconcile,
            now_ms=self.clock.now_ms(),
        )
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
                    return await self._materialize_committed(
                        tool, execution, arguments, parent_activity_id, deadline_at_ms,
                    )
                return _tool_result_from_ledger(execution)
            if status == "MANUAL_REQUIRED":
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
                    return await self._commit_result(
                        execution, parent_activity_id, fencing_token, recovered,
                        return_full=manifest.result_policy == "ARTIFACT_BOUNDED_READ",
                    )
                if recovered is not None and recovered.status is ToolResultStatus.UNKNOWN:
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
                    return await self._require_manual(
                        execution, parent_activity_id, fencing_token,
                    )

            if int(execution["attempt"]) >= manifest.max_attempts:
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
            except TimeoutError as exc:
                result, execution = await self._settle_dispatch_failure(
                    tool, execution, parent_activity_id, fencing_token,
                    code="TOOL_TIMEOUT", message=str(exc) or "tool timed out",
                )
            except Exception as exc:  # after dispatch, side-effect failures are conservative
                result, execution = await self._settle_dispatch_failure(
                    tool, execution, parent_activity_id, fencing_token,
                    code=type(exc).__name__, message=str(exc),
                )
            else:
                result = _normalize_tool_result(value)
                if result.status not in {
                    ToolResultStatus.FAILURE,
                    ToolResultStatus.UNKNOWN,
                }:
                    # Keep Store/Artifact commit failures outside the executor exception
                    # classifier: a stale fence or SQLite failure is not a Tool failure.
                    return await self._commit_result(
                        execution,
                        parent_activity_id,
                        fencing_token,
                        result,
                        return_full=manifest.result_policy == "ARTIFACT_BOUNDED_READ",
                    )
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
                return await self._require_manual(
                    execution, parent_activity_id, fencing_token,
                )
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
        return _normalize_tool_result(value)

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
        return_full: bool = False,
    ) -> ToolResultEnvelope:
        original_payload = result.model_dump(mode="json")
        evidence_set: dict[str, Any] | None = None
        evidence_index: list[dict[str, Any]] = []
        if (
            execution["tool_name"] == "knowledge_search"
            and result.status is ToolResultStatus.SUCCESS
            and isinstance(result.preview, dict)
        ):
            raw_preview = dict(result.preview)
            raw_evidence = raw_preview.pop("__evidence_set__", None)
            if isinstance(raw_evidence, dict):
                evidence_set = _normalize_evidence_set(
                    raw_evidence,
                    execution,
                    retrieved_at_ms=self.clock.now_ms(),
                )
            else:
                evidence_set = _evidence_set_from_legacy_hits(
                    raw_preview,
                    execution,
                    retrieved_at_ms=self.clock.now_ms(),
                )
            evidence_index = _compact_evidence_index(evidence_set)
            result = result.model_copy(
                update={"preview": _bounded_knowledge_preview(raw_preview, self.inline_result_max_bytes)}
            )
            original_payload = result.model_dump(mode="json")

        serialized = canonical_json(
            evidence_set if evidence_set is not None else original_payload
        ).encode("utf-8")
        result_ref: str | None = result.result_ref
        artifact_metadata: dict[str, Any] | None = None
        stored = result
        artifact_relation = (
            "ARTIFACT_READ" if execution["tool_name"] == "read_artifact" else "TOOL_RESULT"
        )
        if evidence_set is not None or len(serialized) > self.inline_result_max_bytes:
            ref = None
            if result_ref is None or evidence_set is not None:
                ref = await self.artifact_store.put_bytes(
                    serialized,
                    purpose=ArtifactPurpose.INTERNAL,
                    media_type=(
                        "application/vnd.sxw.evidence-set+json"
                        if evidence_set is not None
                        else "application/json"
                    ),
                    filename=f"{execution['tool_execution_id']}.json",
                )
                result_ref = ref.artifact_id
            if evidence_set is None:
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
        if evidence_set is not None:
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


def _normalize_evidence_set(
    raw: dict[str, Any],
    execution: dict[str, Any],
    *,
    retrieved_at_ms: int,
) -> dict[str, Any]:
    query = str(raw.get("query") or "")
    query_id = str(raw.get("query_id") or (
        "qry_" + hashlib.sha256(
            f"{execution['tool_execution_id']}:{query}".encode("utf-8")
        ).hexdigest()
    ))
    raw_dataset_scope = raw.get("dataset_scope")
    if not isinstance(raw_dataset_scope, list):
        raw_dataset_scope = raw.get("datasets")
    dataset_scope = [
        str(item) for item in (raw_dataset_scope or []) if str(item).strip()
    ]
    evidence: list[dict[str, Any]] = []
    for ordinal, item in enumerate(raw.get("evidence") or [], start=1):
        if not isinstance(item, dict):
            continue
        n = int(item.get("n") or ordinal)
        content = str(item.get("content") or "")
        content_hash = str(item.get("content_hash") or "")
        if len(content_hash) != 64 or any(char not in "0123456789abcdef" for char in content_hash):
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        evidence_id = str(item.get("evidence_id") or (
            "ev_" + hashlib.sha256(
                f"{execution['tool_execution_id']}:{n}:{content_hash}".encode("utf-8")
            ).hexdigest()
        ))
        doc_id = str(item.get("doc_id") or item.get("document_id") or "unknown")
        document_id = str(item.get("document_id") or doc_id)
        document_version_id = str(
            item.get("document_version_id")
            or item.get("index_version")
            or f"unversioned:{content_hash}"
        )
        index_version = str(item.get("index_version") or document_version_id)
        dataset_id = str(item.get("dataset_id") or "default")
        if dataset_id not in dataset_scope:
            dataset_scope.append(dataset_id)
        span_start = max(0, int(item.get("span_start") or 0))
        span_end = max(span_start, int(item.get("span_end") or len(content)))
        entry = {
            "n": n,
            "evidence_id": evidence_id,
            "chunk_id": str(item.get("chunk_id") or f"chunk_{content_hash}"),
            "doc_id": doc_id,
            "title": str(item.get("title") or ""),
            "document_id": document_id,
            "document_version_id": document_version_id,
            "index_version": index_version,
            "content_hash": content_hash,
            "dataset_id": dataset_id,
            "scope": str(item.get("scope") or raw.get("scope") or "public"),
            "query_id": str(item.get("query_id") or query_id),
            "page": item.get("page") if isinstance(item.get("page"), int) else None,
            "span_start": span_start,
            "span_end": span_end,
            "content": content,
            "score": float(item.get("score") or 0.0),
            "source": str(item.get("source") or "other"),
        }
        evidence.append(entry)
    if not dataset_scope:
        dataset_scope = ["default"]
    return {
        "schema_version": str(raw.get("schema_version") or "1"),
        "query": query,
        "query_id": query_id,
        "run_id": str(raw.get("run_id") or execution["run_id"]),
        "activity_id": str(raw.get("activity_id") or execution["activity_id"]),
        "principal_id": str(raw.get("principal_id") or "unknown"),
        "dataset_scope": dataset_scope,
        "scope": str(raw.get("scope") or "public"),
        "retrieval_status": str(raw.get("retrieval_status") or ("HIT" if evidence else "MISS")),
        "rewrites": list(raw.get("rewrites") or []),
        "cost_ms": raw.get("cost_ms"),
        "degraded_reasons": [str(item) for item in (raw.get("degraded_reasons") or [])],
        "tool_execution_id": execution["tool_execution_id"],
        "evidence": evidence,
        "retrieved_at": str(raw.get("retrieved_at") or ms_to_rfc3339(retrieved_at_ms)),
    }


def _evidence_set_from_legacy_hits(
    preview: dict[str, Any],
    execution: dict[str, Any],
    *,
    retrieved_at_ms: int,
) -> dict[str, Any]:
    return _normalize_evidence_set(
        {
            "schema_version": "1",
            "retrieval_status": "HIT" if preview.get("hits") else "MISS",
            "evidence": list(preview.get("hits") or []),
        },
        execution,
        retrieved_at_ms=retrieved_at_ms,
    )


def _compact_evidence_index(evidence_set: dict[str, Any]) -> list[dict[str, Any]]:
    keep = (
        "n", "evidence_id", "title", "doc_id", "document_id",
        "document_version_id", "index_version", "content_hash", "dataset_id", "scope",
        "query_id", "page", "span_start", "span_end",
    )
    query_id = evidence_set.get("query_id")
    compact: list[dict[str, Any]] = []
    for item in evidence_set.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        entry: dict[str, Any] = {}
        for key in keep:
            value = item.get(key)
            if key == "query_id" and value is None:
                value = query_id
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


def _tool_result_from_ledger(execution: dict[str, Any]) -> ToolResultEnvelope:
    """Project the internal ToolExecution ledger row back to public v1.

    Knowledge retrieval appends a compact citation index beside the envelope in
    ``result_json``.  That internal enrichment is intentionally not part of the
    frozen public ToolResult schema, so committed replay extracts only envelope
    fields while citation finalization continues to read the full ledger JSON.
    """
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


def _normalize_tool_result(value: Any) -> ToolResultEnvelope:
    """Normalize legacy tool responses into the fixed v1 result vocabulary."""
    if isinstance(value, ToolResultEnvelope):
        return value
    if value is None:
        return ToolResultEnvelope(status=ToolResultStatus.NO_OUTPUT)
    if isinstance(value, dict):
        if value.get("interrupt") is True:
            return ToolResultEnvelope(
                status=ToolResultStatus.INTERRUPT,
                preview=value,
                pending_input=(
                    value.get("pending_input")
                    if isinstance(value.get("pending_input"), dict)
                    else None
                ),
            )
        raw_status = str(value.get("status") or "").upper()
        explicitly_failed = bool(value.get("isError") or value.get("is_error"))
        if explicitly_failed or raw_status == ToolResultStatus.FAILURE:
            message = value.get("content") or value.get("error") or value.get("message")
            return ToolResultEnvelope(
                status=ToolResultStatus.FAILURE,
                preview=value,
                result_ref=value.get("result_ref") or value.get("artifact_ref"),
                external_object_id=value.get("external_object_id"),
                error_code=_bounded_error_code(
                    value.get("errorCode") or value.get("error_code"),
                    "TOOL_REPORTED_FAILURE",
                ),
                error_message=_bounded_error_message(message, "tool reported a failure"),
            )
        if raw_status == ToolResultStatus.UNKNOWN:
            return ToolResultEnvelope(
                status=ToolResultStatus.UNKNOWN,
                preview=value,
                result_ref=value.get("result_ref") or value.get("artifact_ref"),
                external_object_id=value.get("external_object_id"),
                error_code=_bounded_error_code(
                    value.get("errorCode") or value.get("error_code"),
                    "TOOL_EFFECT_UNKNOWN",
                ),
                error_message=_bounded_error_message(
                    value.get("content") or value.get("message"),
                    "tool effect is unknown",
                ),
            )
        if raw_status == ToolResultStatus.NO_OUTPUT:
            return ToolResultEnvelope(status=ToolResultStatus.NO_OUTPUT)
    return ToolResultEnvelope(status=ToolResultStatus.SUCCESS, preview=value)


def _bounded_error_code(value: Any, fallback: str) -> str:
    text = str(value) if value is not None and value != "" else fallback
    return text[:128]


def _bounded_error_message(value: Any, fallback: str) -> str:
    text = str(value) if value is not None and value != "" else fallback
    return text[:8192]
