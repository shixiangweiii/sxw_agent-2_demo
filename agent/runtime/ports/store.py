from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from agent.runtime.domain.models import (
    ActivityRecord,
    CanonicalEvent,
    CheckpointRecord,
    EngineName,
    EventType,
    ReleaseManifest,
    RunRecord,
    RunStatus,
    Sensitivity,
    Visibility,
    WorkingState,
)


@dataclass(frozen=True)
class AdmissionCommand:
    request_id: str
    client_request_id: str
    idempotency_key: str
    request_digest: str
    conversation_id: str | None
    generated_conversation_id: str
    turn_id: str
    run_id: str
    principal_id: str
    agent_id: str
    engine: EngineName
    deadline_at: int
    cancel_token_id: str
    input_event_id: str
    input_text: str
    attachment_refs: tuple[str, ...]
    created_at: int
    # 诊断字段，不参与 request_digest（否则同一请求换个 trace_id 重放就会撞 409）。
    trace_id: str = ""


@dataclass(frozen=True)
class AdmissionResult:
    run: RunRecord
    reused: bool


@dataclass(frozen=True)
class EventDraft:
    event_type: EventType
    payload: dict[str, Any] | None = None
    payload_ref: str | None = None
    activity_id: str | None = None
    tool_execution_id: str | None = None
    producer: str = "runtime"
    visibility: Visibility = Visibility.PUBLIC
    sensitivity: Sensitivity = Sensitivity.PRIVATE
    terminal_status: RunStatus | None = None
    event_id: str | None = None
    occurred_at: int | None = None


@dataclass(frozen=True)
class Claim:
    run: RunRecord
    activity: ActivityRecord


@dataclass(frozen=True)
class SignalResult:
    signal_id: str
    status: str
    run: RunRecord
    reused: bool = False


@dataclass(frozen=True)
class ToolExecutionPreparation:
    """One deterministic ToolExecution slot in an atomically prepared batch.

    ``framework_call_id`` is attempt-local correlation metadata only.  Stable
    identity is derived exclusively from ``run_id + logical_key``.
    """

    logical_key: str
    tool_name: str
    release_digest: str
    effect_class: str
    request_digest: str
    request: dict[str, Any]
    supports_reconcile: bool = False
    framework_call_id: str | None = None


class RuntimeStore(Protocol):
    async def initialize(self) -> None: ...
    async def activate_current_releases(
        self, manifests: Sequence[ReleaseManifest],
    ) -> dict[str, str]: ...
    async def admit(self, command: AdmissionCommand) -> AdmissionResult: ...
    async def get_run(self, run_id: str) -> RunRecord: ...
    async def get_activity(self, activity_id: str) -> ActivityRecord: ...
    async def list_events(
        self, run_id: str, *, after_seq: int = 0, visibility: Visibility | None = Visibility.PUBLIC,
        limit: int = 500,
    ) -> list[CanonicalEvent]: ...
    async def append_events(
        self, run_id: str, drafts: Sequence[EventDraft], *, activity_id: str | None = None,
        fencing_token: int | None = None, now_ms: int | None = None,
    ) -> list[CanonicalEvent]: ...
    async def claim_next(
        self, *, worker_id: str, lease_ms: int, now_ms: int,
        release_map: Mapping[str, str],
    ) -> Claim | None: ...
    async def mark_activity_running(self, activity_id: str, *, worker_id: str, fencing_token: int, now_ms: int) -> ActivityRecord: ...
    async def renew_lease(self, activity_id: str, *, worker_id: str, fencing_token: int, lease_expires_at: int, now_ms: int) -> bool: ...
    async def recover_expired(self, *, now_ms: int) -> int: ...
    async def save_checkpoint(
        self, *, run_id: str, activity_id: str, fencing_token: int, expected_revision: int,
        working_state: WorkingState, engine_state: dict[str, Any] | None = None,
        now_ms: int, events: Sequence[EventDraft] = (),
    ) -> CheckpointRecord: ...
    async def latest_checkpoint(self, run_id: str) -> CheckpointRecord | None: ...
    async def compile_history(self, run_id: str) -> list[dict[str, Any]]: ...
    async def finalize_success(
        self, *, run_id: str, activity_id: str, fencing_token: int, assistant_text: str,
        citations: list[dict[str, Any]], now_ms: int,
        message_id: str | None = None, generation_id: str | None = None,
    ) -> RunRecord: ...
    async def finalize_failure(
        self, *, run_id: str, activity_id: str, fencing_token: int, code: str,
        message: str, now_ms: int, terminal_status: RunStatus = RunStatus.FAILED,
    ) -> RunRecord: ...
    async def schedule_retry(
        self, *, run_id: str, activity_id: str, fencing_token: int, fire_at: int,
        error: dict[str, Any], now_ms: int,
    ) -> None: ...
    async def fire_due_timers(self, *, now_ms: int, limit: int = 100) -> int: ...
    async def wait_for_input(
        self, *, run_id: str, activity_id: str, fencing_token: int,
        pending_input: dict[str, Any], now_ms: int,
    ) -> RunRecord: ...
    async def request_cancel(self, *, run_id: str, command_id: str, reason: str, now_ms: int) -> tuple[RunRecord, bool]: ...
    async def submit_signal(
        self, *, run_id: str, signal_id: str, wait_activity_id: str, signal_type: str,
        payload: dict[str, Any], payload_digest: str, now_ms: int,
    ) -> SignalResult: ...
    async def settle_reconciliation_query(
        self, *, run_id: str, activity_id: str, fencing_token: int,
        tool_execution_id: str, signal_id: str, expected_effect_revision: int,
        now_ms: int,
    ) -> RunRecord: ...
    async def is_cancel_requested(self, run_id: str) -> bool: ...
    async def prepare_tool_execution(
        self, *, run_id: str, parent_activity_id: str, fencing_token: int,
        logical_key: str, tool_name: str, release_digest: str, effect_class: str,
        request_digest: str, request: dict[str, Any], now_ms: int,
        supports_reconcile: bool = False,
        framework_call_id: str | None = None,
    ) -> dict[str, Any]: ...
    async def prepare_tool_execution_batch(
        self, *, run_id: str, parent_activity_id: str, fencing_token: int,
        preparations: Sequence[ToolExecutionPreparation], now_ms: int,
    ) -> list[dict[str, Any]]: ...
    async def unresolved_tool_execution_ids(self, run_id: str) -> list[str]: ...
    async def register_artifact_metadata(
        self, *, artifact_id: str, sha256: str, size_bytes: int,
        media_type: str, storage_path: str, created_at: int,
    ) -> dict[str, Any]: ...
    async def get_artifact_metadata(self, artifact_id: str) -> dict[str, Any]: ...
    async def referenced_artifact_ids(self) -> set[str]: ...
    async def heartbeat_worker(self, *, worker_id: str, release_map: dict[str, str], state: str, now_ms: int) -> None: ...
