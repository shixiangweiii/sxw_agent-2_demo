"""Versioned domain vocabulary for the Canonical Runtime."""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1"
EngineName = Literal["plan_execute", "agent_loop", "native_loop"]


class RunStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    DISPATCH_PENDING = "DISPATCH_PENDING"
    RUNNING = "RUNNING"
    WAITING_RETRY = "WAITING_RETRY"
    WAITING_INPUT = "WAITING_INPUT"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    REJECTED = "REJECTED"


TERMINAL_RUN_STATUSES = frozenset({
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.TIMED_OUT,
    RunStatus.REJECTED,
})


class ActivityStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    WAITING_RETRY = "WAITING_RETRY"
    WAITING_INPUT = "WAITING_INPUT"
    RECONCILE = "RECONCILE"
    MANUAL = "MANUAL"


class ActivityType(StrEnum):
    ENGINE_RUN = "ENGINE_RUN"
    MODEL_CALL = "MODEL_CALL"
    TOOL_CALL = "TOOL_CALL"
    RETRIEVAL = "RETRIEVAL"
    CHECKPOINT = "CHECKPOINT"
    WAIT_INPUT = "WAIT_INPUT"
    FINALIZE = "FINALIZE"


class EventType(StrEnum):
    USER_MESSAGE_COMMITTED = "USER_MESSAGE_COMMITTED"
    RUN_STATUS_CHANGED = "RUN_STATUS_CHANGED"
    ACTIVITY_STATUS_CHANGED = "ACTIVITY_STATUS_CHANGED"
    OUTPUT_GENERATION_STARTED = "OUTPUT_GENERATION_STARTED"
    OUTPUT_DELTA_COMMITTED = "OUTPUT_DELTA_COMMITTED"
    MODEL_MESSAGE_COMMITTED = "MODEL_MESSAGE_COMMITTED"
    TOOL_CALL_COMMITTED = "TOOL_CALL_COMMITTED"
    TOOL_RESULT_COMMITTED = "TOOL_RESULT_COMMITTED"
    MODEL_PLAN_UPDATED = "MODEL_PLAN_UPDATED"
    SKILL_UI_FRAME_COMMITTED = "SKILL_UI_FRAME_COMMITTED"
    RETRIEVAL_COMMITTED = "RETRIEVAL_COMMITTED"
    CHECKPOINT_COMMITTED = "CHECKPOINT_COMMITTED"
    SIGNAL_RECORDED = "SIGNAL_RECORDED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    ASSISTANT_MESSAGE_COMMITTED = "ASSISTANT_MESSAGE_COMMITTED"
    CITATION_SET_COMMITTED = "CITATION_SET_COMMITTED"
    RUN_TERMINATED = "RUN_TERMINATED"


class Visibility(StrEnum):
    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"


class Sensitivity(StrEnum):
    PUBLIC = "PUBLIC"
    PRIVATE = "PRIVATE"
    SENSITIVE = "SENSITIVE"


class ToolEffectClass(StrEnum):
    READ_ONLY = "READ_ONLY"
    IDEMPOTENT_EFFECT = "IDEMPOTENT_EFFECT"
    NON_IDEMPOTENT_EFFECT = "NON_IDEMPOTENT_EFFECT"
    UNKNOWN_EFFECT = "UNKNOWN_EFFECT"


class ToolEffectStatus(StrEnum):
    PREPARED = "PREPARED"
    DISPATCHED = "DISPATCHED"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"
    MANUAL_REQUIRED = "MANUAL_REQUIRED"


class ToolResultStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    INTERRUPT = "INTERRUPT"
    NO_OUTPUT = "NO_OUTPUT"
    UNKNOWN = "UNKNOWN"


class RetrievalStatus(StrEnum):
    HIT = "HIT"
    MISS = "MISS"
    DEGRADED = "DEGRADED"
    DENIED = "DENIED"
    ERROR = "ERROR"


class EngineOutcomeKind(StrEnum):
    COMPLETED = "COMPLETED"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    TERMINAL_FAILURE = "TERMINAL_FAILURE"
    WAITING_INPUT = "WAITING_INPUT"
    CANCELLED = "CANCELLED"


class RuntimeEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal["1"] = SCHEMA_VERSION
    request_id: str
    client_request_id: str
    idempotency_key: str
    conversation_id: str
    turn_id: str
    run_id: str
    principal_id: str
    agent_id: str
    engine: EngineName
    deadline_at: int
    cancel_token_id: str
    release_fingerprint: str
    input_event_id: str
    attachment_refs: tuple[str, ...] = ()
    created_at: int


class WorkingState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = ""
    constraints: list[str] = Field(default_factory=list)
    model_plan: list[dict[str, Any]] = Field(default_factory=list)
    confirmed_facts: list[dict[str, Any]] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    pending_input: dict[str, Any] | None = None
    budget: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class CanonicalEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str
    schema_version: Literal["1"] = SCHEMA_VERSION
    run_id: str
    turn_id: str
    activity_id: str | None = None
    tool_execution_id: str | None = None
    seq: int
    event_type: EventType
    producer: str
    payload: dict[str, Any] | None = None
    payload_ref: str | None = None
    visibility: Visibility = Visibility.PUBLIC
    sensitivity: Sensitivity = Sensitivity.PRIVATE
    occurred_at: int
    terminal_status: RunStatus | None = None
    release_fingerprint: str


class RunRecord(BaseModel):
    envelope: RuntimeEnvelope
    status: RunStatus
    # Admission 观察到的诊断 trace_id，故意留在**冻结的 RuntimeEnvelope 之外**：
    # Trace 只诊断，不该混进入口身份契约（runtime-envelope-v1 是 R0 冻结的六契约之一）。
    # 但它必须持久化——API 与 Worker 是两个进程，contextvar 跨不过这道 DB 交接。
    # 空串表示调用方没带 x-trace-id；读侧回落到 run_id，见 RunCoordinator.execute_claim。
    trace_id: str = ""
    revision: int
    next_seq: int
    current_activity_id: str | None = None
    terminal_status: RunStatus | None = None
    terminal_payload: dict[str, Any] | None = None
    input_text: str
    pending_input: dict[str, Any] | None = None
    updated_at: int


class ActivityRecord(BaseModel):
    activity_id: str
    run_id: str
    type: ActivityType
    logical_key: str
    status: ActivityStatus
    attempt: int
    available_at: int
    lease_owner: str | None = None
    lease_expires_at: int | None = None
    fencing_token: int
    revision: int
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    resume_payload: dict[str, Any] | None = None
    created_at: int
    updated_at: int


class CheckpointRecord(BaseModel):
    checkpoint_id: str
    run_id: str
    activity_id: str
    revision: int
    working_state: WorkingState
    engine_state: dict[str, Any] | None = None
    created_at: int


class EngineOutcome(BaseModel):
    kind: EngineOutcomeKind
    error_code: str | None = None
    message: str | None = None
    pending_input: dict[str, Any] | None = None
    retry_after_ms: int | None = None


class ToolManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    release_digest: str
    effect_class: ToolEffectClass
    timeout_seconds: float = Field(gt=0)
    max_attempts: int = Field(default=1, ge=1)
    supports_idempotency: bool = False
    supports_reconcile: bool = False
    supports_cancel: bool = False
    result_policy: str = "INLINE_OR_ARTIFACT"
    concurrency_safe: bool = False
    exclusive_resources: tuple[str, ...] = ()


class ToolResultEnvelope(BaseModel):
    """Public v1 Tool result vocabulary, kept isomorphic to its frozen schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ToolResultStatus
    preview: Any | None = None
    result_ref: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    error_code: str | None = Field(default=None, min_length=1, max_length=128)
    error_message: str | None = Field(default=None, max_length=8192)
    external_object_id: str | None = Field(default=None, min_length=1, max_length=2048)
    pending_input: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_status_contract(self) -> "ToolResultEnvelope":
        if self.status in {ToolResultStatus.FAILURE, ToolResultStatus.UNKNOWN}:
            if self.error_code is None or self.error_message is None:
                raise ValueError(
                    f"{self.status.value} requires error_code and error_message"
                )
        if self.status is ToolResultStatus.INTERRUPT and self.pending_input is None:
            raise ValueError("INTERRUPT requires pending_input")
        if self.status is ToolResultStatus.NO_OUTPUT:
            if self.preview is not None or self.result_ref is not None:
                raise ValueError("NO_OUTPUT forbids preview and result_ref")
        require_json_value(self.preview, field="preview")
        require_json_value(self.pending_input, field="pending_input")
        return self


class EvidenceItem(BaseModel):
    """One fully attributable retrieval fact.

    No field has a synthetic default.  The retrieval boundary must provide the
    complete current provenance or reject the result before it reaches the
    durable Tool ledger.
    """

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    n: int = Field(ge=1)
    evidence_id: str = Field(min_length=1, max_length=255)
    chunk_id: str = Field(min_length=1, max_length=255)
    doc_id: str = Field(min_length=1, max_length=1024)
    title: str = Field(max_length=8192)
    document_id: str = Field(min_length=1, max_length=255)
    document_version_id: str = Field(min_length=1, max_length=255)
    index_version: str = Field(min_length=1, max_length=255)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dataset_id: str = Field(min_length=1, max_length=255)
    scope: str = Field(min_length=1, max_length=255)
    query_id: str = Field(min_length=1, max_length=255)
    page: int | None = Field(default=None, ge=0)
    span_start: int = Field(ge=0)
    span_end: int = Field(ge=0)
    content: str
    score: float = Field(allow_inf_nan=False)
    source: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_span(self) -> "EvidenceItem":
        if self.span_end < self.span_start:
            raise ValueError("span_end must be greater than or equal to span_start")
        return self


class EvidenceSet(BaseModel):
    """Current-only, lossless Evidence Artifact contract."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    query: str = Field(max_length=32768)
    query_id: str = Field(min_length=1, max_length=255)
    run_id: str = Field(pattern=r"^run_[0-9a-f]{32}$")
    activity_id: str = Field(pattern=r"^act_[0-9a-f]{32}$")
    principal_id: str = Field(min_length=1, max_length=255)
    dataset_scope: tuple[str, ...] = Field(min_length=1, max_length=256)
    scope: str = Field(min_length=1, max_length=255)
    retrieval_status: RetrievalStatus
    rewrites: tuple[str, ...] = Field(max_length=64)
    cost_ms: int | None = Field(default=None, ge=0)
    degraded_reasons: tuple[str, ...] = Field(max_length=128)
    tool_execution_id: str = Field(pattern=r"^tool_[0-9a-f]{32}$")
    evidence: tuple[EvidenceItem, ...] = Field(max_length=10000)
    retrieved_at: str = Field(pattern=r"Z$")

    @model_validator(mode="after")
    def validate_evidence_contract(self) -> "EvidenceSet":
        if len(set(self.dataset_scope)) != len(self.dataset_scope):
            raise ValueError("dataset_scope entries must be unique")
        if any(not item for item in self.dataset_scope):
            raise ValueError("dataset_scope entries must be non-empty")
        if self.retrieval_status is RetrievalStatus.HIT and not self.evidence:
            raise ValueError("HIT requires at least one EvidenceItem")
        if self.retrieval_status in {
            RetrievalStatus.MISS,
            RetrievalStatus.DENIED,
            RetrievalStatus.ERROR,
        } and self.evidence:
            raise ValueError(f"{self.retrieval_status.value} forbids EvidenceItem entries")
        ordinals = [item.n for item in self.evidence]
        if ordinals != list(range(1, len(ordinals) + 1)):
            raise ValueError("EvidenceItem.n must be contiguous and ordered from 1")
        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence_id must be unique within one EvidenceSet")
        for item in self.evidence:
            if item.query_id != self.query_id:
                raise ValueError("EvidenceItem query_id disagrees with EvidenceSet")
            if item.dataset_id not in self.dataset_scope:
                raise ValueError("EvidenceItem dataset_id is outside dataset_scope")
            if item.scope != self.scope:
                raise ValueError("EvidenceItem scope disagrees with EvidenceSet")
        return self


class ToolExecutionOutput(BaseModel):
    """The only value accepted by ToolBroker's post-executor boundary."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    result: ToolResultEnvelope
    evidence: EvidenceSet | None = None

    @model_validator(mode="after")
    def validate_evidence_result(self) -> "ToolExecutionOutput":
        if not isinstance(self.result, ToolResultEnvelope):
            raise ValueError("result must be a ToolResultEnvelope")
        # Re-check the raw values here as well.  Pydantic's ``model_copy`` and
        # ``model_construct`` are intentionally able to bypass nested model
        # validation; neither is allowed to weaken the Broker boundary.
        require_json_value(self.result.preview, field="result.preview")
        require_json_value(self.result.pending_input, field="result.pending_input")
        if self.evidence is not None and self.result.status is not ToolResultStatus.SUCCESS:
            raise ValueError("EvidenceSet requires a SUCCESS ToolResultEnvelope")
        return self


class ToolReconciliationAction(StrEnum):
    MARK_COMMITTED = "mark_committed"
    MARK_FAILED = "mark_failed"
    RECONCILE = "reconcile"


class ToolReconciliationPayload(BaseModel):
    """Strict, bounded payload for an audited manual ToolEffect decision.

    Large evidence/results must already be in Artifact CAS and are referenced by
    ``result_ref``.  The inline bounds prevent the Signal row and canonical
    ToolResult projection from becoming an accidental blob store.
    """

    model_config = ConfigDict(extra="forbid")

    tool_execution_id: str = Field(pattern=r"^tool_[0-9a-f]{32}$")
    action: ToolReconciliationAction
    evidence: dict[str, Any] = Field(min_length=1)
    result: ToolResultEnvelope | None = None
    result_ref: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    external_object_id: str | None = Field(default=None, min_length=1, max_length=2048)

    @model_validator(mode="after")
    def validate_action_contract(self) -> "ToolReconciliationPayload":
        try:
            evidence_size = len(canonical_json(self.evidence).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise ValueError("evidence must be deterministic JSON") from exc
        if evidence_size > 4096:
            raise ValueError("inline evidence exceeds 4KiB; use an Artifact reference")

        if self.action is ToolReconciliationAction.RECONCILE:
            if self.result is not None or self.result_ref is not None:
                raise ValueError("reconcile cannot claim a result or result_ref")
            if self.external_object_id is not None:
                raise ValueError("reconcile cannot claim an external object")
            return self

        if self.result is None:
            raise ValueError(f"{self.action.value} requires a ToolResultEnvelope")
        nested_ref = self.result.result_ref
        if nested_ref is not None and self.result_ref is not None and nested_ref != self.result_ref:
            raise ValueError("result.result_ref and result_ref must identify the same Artifact")
        effective_ref = self.result_ref or nested_ref
        nested_external_object_id = self.result.external_object_id
        if (
            nested_external_object_id is not None
            and self.external_object_id is not None
            and nested_external_object_id != self.external_object_id
        ):
            raise ValueError(
                "result.external_object_id and external_object_id must identify the same object"
            )
        effective_external_object_id = (
            self.external_object_id or nested_external_object_id
        )
        # Re-validate after merging the top-level convenience fields.  Directly
        # mutating a nested Pydantic model would otherwise let a contradictory
        # NO_OUTPUT + result_ref payload bypass ToolResultEnvelope's contract.
        self.result = ToolResultEnvelope.model_validate({
            **self.result.model_dump(mode="python"),
            "result_ref": effective_ref,
            "external_object_id": effective_external_object_id,
        })
        self.result_ref = effective_ref
        self.external_object_id = effective_external_object_id

        result_size = len(
            canonical_json(self.result.model_dump(mode="json", exclude_none=True)).encode("utf-8")
        )
        if result_size > 8192:
            raise ValueError("inline result exceeds 8KiB; store it as an Artifact")

        if self.action is ToolReconciliationAction.MARK_COMMITTED:
            if self.result.status not in {
                ToolResultStatus.SUCCESS,
                ToolResultStatus.NO_OUTPUT,
            }:
                raise ValueError("mark_committed requires SUCCESS or NO_OUTPUT")
            if (
                self.result.status is ToolResultStatus.SUCCESS
                and self.result.preview is None
                and effective_ref is None
                and effective_external_object_id is None
            ):
                raise ValueError(
                    "mark_committed SUCCESS requires preview, result_ref, or external_object_id"
                )
        elif self.action is ToolReconciliationAction.MARK_FAILED:
            if self.result.status is not ToolResultStatus.FAILURE:
                raise ValueError("mark_failed requires FAILURE")
            if not self.result.error_code or not self.result.error_message:
                raise ValueError("mark_failed requires error_code and error_message")
            if self.external_object_id is not None:
                raise ValueError("mark_failed cannot claim an external object")
        return self


class ReleaseManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    engine: EngineName
    components: dict[str, str]

    def fingerprint(self) -> str:
        return sha256_json(self.model_dump(mode="json"))


RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.ACCEPTED: frozenset({
        RunStatus.DISPATCH_PENDING, RunStatus.CANCELLED,
        RunStatus.TIMED_OUT, RunStatus.REJECTED,
    }),
    # The pre-running states normally cancel directly.  CANCEL_REQUESTED is the
    # conditional edge used only when the Store finds a DISPATCHED/UNKNOWN (or
    # reconciling/manual) ToolEffect: that uncertainty must be reconciled or
    # timed out before the Run may claim CANCELLED.
    RunStatus.DISPATCH_PENDING: frozenset({RunStatus.RUNNING, RunStatus.CANCEL_REQUESTED, RunStatus.CANCELLED, RunStatus.TIMED_OUT}),
    RunStatus.RUNNING: frozenset({RunStatus.WAITING_RETRY, RunStatus.WAITING_INPUT, RunStatus.CANCEL_REQUESTED, RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.TIMED_OUT}),
    RunStatus.WAITING_RETRY: frozenset({
        RunStatus.DISPATCH_PENDING, RunStatus.CANCEL_REQUESTED, RunStatus.CANCELLED,
        RunStatus.TIMED_OUT,
    }),
    RunStatus.WAITING_INPUT: frozenset({
        RunStatus.DISPATCH_PENDING, RunStatus.CANCEL_REQUESTED, RunStatus.CANCELLED,
        RunStatus.TIMED_OUT,
    }),
    RunStatus.CANCEL_REQUESTED: frozenset({RunStatus.CANCELLED, RunStatus.TIMED_OUT}),
    **{state: frozenset() for state in TERMINAL_RUN_STATUSES},
}

ACTIVITY_TRANSITIONS: dict[ActivityStatus, frozenset[ActivityStatus]] = {
    ActivityStatus.PENDING: frozenset({
        ActivityStatus.CLAIMED,
        ActivityStatus.CANCELLED,
        # Cancel takeover after lease recovery, before a replacement claim.
        ActivityStatus.RECONCILE,
        # Recovery-only: an operator-authorized reconcile query was scheduled,
        # but its parent Worker died before the side-effect-free hook started.
        ActivityStatus.MANUAL,
    }),
    ActivityStatus.CLAIMED: frozenset({
        ActivityStatus.RUNNING,
        ActivityStatus.PENDING,
        ActivityStatus.CANCELLED,
        # Recovery-only: the parent lease expired after claim while an exact
        # reconcile-only marker already owned an unresolved ToolEffect.
        ActivityStatus.RECONCILE,
    }),
    ActivityStatus.RUNNING: frozenset({
        ActivityStatus.PENDING,  # recovery-only: expired lease, replay-safe boundary
        ActivityStatus.SUCCEEDED, ActivityStatus.FAILED, ActivityStatus.CANCELLED,
        ActivityStatus.WAITING_RETRY, ActivityStatus.WAITING_INPUT,
        ActivityStatus.RECONCILE, ActivityStatus.MANUAL,
    }),
    ActivityStatus.WAITING_RETRY: frozenset({
        ActivityStatus.PENDING, ActivityStatus.CANCELLED, ActivityStatus.RECONCILE,
    }),
    ActivityStatus.WAITING_INPUT: frozenset({
        ActivityStatus.PENDING, ActivityStatus.CANCELLED, ActivityStatus.RECONCILE,
    }),
    ActivityStatus.RECONCILE: frozenset({
        ActivityStatus.PENDING,
        ActivityStatus.SUCCEEDED,
        ActivityStatus.FAILED,
        ActivityStatus.MANUAL,
        # Only after a won cancel CAS and every uncertain effect has been
        # resolved.  Store terminalization owns this guarded edge.
        ActivityStatus.CANCELLED,
    }),
    ActivityStatus.MANUAL: frozenset({ActivityStatus.PENDING, ActivityStatus.SUCCEEDED, ActivityStatus.FAILED, ActivityStatus.CANCELLED}),
    ActivityStatus.SUCCEEDED: frozenset(),
    ActivityStatus.FAILED: frozenset(),
    ActivityStatus.CANCELLED: frozenset(),
}


def require_json_value(value: Any, *, field: str = "value") -> None:
    """Reject Python-shaped values that are not strict, finite UTF-8 JSON.

    ``json.dumps`` is deliberately permissive: it accepts tuples, integer
    object keys and NaN/Infinity.  Tool results cross a durable protocol
    boundary, so accepting those values would make their digest, SQLite JSON
    and provider projection disagree.  Validate the actual Python shape before
    any serializer has a chance to coerce it.
    """

    active: set[int] = set()

    def walk(item: Any, path: str) -> None:
        item_type = type(item)
        if item is None or item_type in {bool, int}:
            return
        if item_type is float:
            if not math.isfinite(item):
                raise ValueError(f"{path} must contain only finite numbers")
            return
        if item_type is str:
            try:
                item.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError(f"{path} must contain valid UTF-8 text") from exc
            return
        if item_type is list:
            identity = id(item)
            if identity in active:
                raise ValueError(f"{path} contains a cycle")
            active.add(identity)
            try:
                for index, nested in enumerate(item):
                    walk(nested, f"{path}[{index}]")
            finally:
                active.remove(identity)
            return
        if item_type is dict:
            identity = id(item)
            if identity in active:
                raise ValueError(f"{path} contains a cycle")
            active.add(identity)
            try:
                for key, nested in item.items():
                    if type(key) is not str:
                        raise ValueError(f"{path} object keys must be strings")
                    walk(key, f"{path}.<key>")
                    walk(nested, f"{path}.{key}")
            finally:
                active.remove(identity)
            return
        raise ValueError(
            f"{path} contains non-JSON value {item_type.__name__}"
        )

    try:
        walk(value, field)
    except RecursionError as exc:
        raise ValueError(f"{field} exceeds JSON nesting capacity") from exc


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def stable_id(prefix: str, run_id: str, logical_key: str) -> str:
    value = uuid.uuid5(uuid.NAMESPACE_URL, f"sxw-runtime:{run_id}:{logical_key}")
    return f"{prefix}_{value.hex}"


def utc_now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


def ms_to_rfc3339(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, UTC).isoformat().replace("+00:00", "Z")


def rfc3339_to_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.astimezone(UTC).timestamp() * 1000)
