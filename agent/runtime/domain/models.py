"""Versioned domain vocabulary for the Canonical Runtime."""
from __future__ import annotations

import hashlib
import json
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
    INCOMPATIBLE_RELEASE = "INCOMPATIBLE_RELEASE"


TERMINAL_RUN_STATUSES = frozenset({
    RunStatus.SUCCEEDED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.TIMED_OUT,
    RunStatus.REJECTED,
    RunStatus.INCOMPATIBLE_RELEASE,
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


class EngineOutcomeKind(StrEnum):
    COMPLETED = "COMPLETED"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    TERMINAL_FAILURE = "TERMINAL_FAILURE"
    WAITING_INPUT = "WAITING_INPUT"
    CANCELLED = "CANCELLED"


class RuntimeEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION
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
    release_fingerprint: str


class CanonicalEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    event_id: str
    schema_version: str = SCHEMA_VERSION
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
    engine_state_ref: str | None = None
    release_fingerprint: str
    schema_version: str = SCHEMA_VERSION
    created_at: int


class EngineOutcome(BaseModel):
    kind: EngineOutcomeKind
    error_code: str | None = None
    message: str | None = None
    pending_input: dict[str, Any] | None = None
    retry_after_ms: int | None = None


class ToolManifest(BaseModel):
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

    model_config = ConfigDict(extra="forbid")

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
    schema_version: str = SCHEMA_VERSION
    engine: EngineName
    runtime_contract: str = "canonical-runtime-v1"
    engine_contract: str = "engine-adapter-v1"
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
    RunStatus.DISPATCH_PENDING: frozenset({RunStatus.RUNNING, RunStatus.CANCEL_REQUESTED, RunStatus.CANCELLED, RunStatus.TIMED_OUT, RunStatus.INCOMPATIBLE_RELEASE}),
    RunStatus.RUNNING: frozenset({RunStatus.WAITING_RETRY, RunStatus.WAITING_INPUT, RunStatus.CANCEL_REQUESTED, RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.TIMED_OUT, RunStatus.INCOMPATIBLE_RELEASE}),
    RunStatus.WAITING_RETRY: frozenset({
        RunStatus.DISPATCH_PENDING, RunStatus.CANCEL_REQUESTED, RunStatus.CANCELLED,
        RunStatus.TIMED_OUT, RunStatus.INCOMPATIBLE_RELEASE,
    }),
    RunStatus.WAITING_INPUT: frozenset({
        RunStatus.DISPATCH_PENDING, RunStatus.CANCEL_REQUESTED, RunStatus.CANCELLED,
        RunStatus.TIMED_OUT, RunStatus.INCOMPATIBLE_RELEASE,
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
