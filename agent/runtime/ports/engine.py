from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from agent.runtime.domain.models import (
    CheckpointRecord,
    EngineOutcome,
    RuntimeEnvelope,
    WorkingState,
)
from agent.runtime.ports.store import EventDraft


@dataclass(frozen=True)
class EngineRunRequest:
    envelope: RuntimeEnvelope
    activity_id: str
    fencing_token: int
    attempt: int
    input_text: str
    history: tuple[dict[str, Any], ...]
    checkpoint: CheckpointRecord | None
    resume_payload: dict[str, Any] | None


class RuntimeIO(Protocol):
    @property
    def tool_broker(self) -> Any: ...
    async def emit(self, event_type: str, payload: dict[str, Any]) -> None: ...
    async def force_flush(self) -> None: ...
    async def abort(self) -> None: ...
    async def checkpoint(
        self,
        working_state: WorkingState,
        *,
        expected_revision: int,
        engine_state: dict[str, Any] | None = None,
        events: Sequence[EventDraft] = (),
    ) -> CheckpointRecord: ...
    async def is_cancelled(self) -> bool: ...
    def remaining_ms(self) -> int: ...
    def seed_assistant_text(self, text: str) -> None: ...
    def set_final_assistant(
        self, text: str, message_id: str, generation_id: str,
    ) -> None: ...
    @property
    def final_assistant(self) -> tuple[str, str, str] | None: ...


class EngineAdapter(Protocol):
    name: str
    release_fingerprint: str

    async def execute(self, request: EngineRunRequest, io: RuntimeIO) -> EngineOutcome: ...
