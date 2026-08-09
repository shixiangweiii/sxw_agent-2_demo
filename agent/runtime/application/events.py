from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from agent.runtime.domain.models import (
    CheckpointRecord,
    EventType,
    Visibility,
    WorkingState,
)
from agent.runtime.ports.clock import Clock, SystemClock
from agent.runtime.ports.store import EventDraft, RuntimeStore


_EVENT_MAP: dict[str, EventType] = {
    "tool_call": EventType.TOOL_CALL_COMMITTED,
    "tool_result": EventType.TOOL_RESULT_COMMITTED,
    "plan_step": EventType.MODEL_PLAN_UPDATED,
    "skill_event": EventType.SKILL_UI_FRAME_COMMITTED,
    "retrieval": EventType.RETRIEVAL_COMMITTED,
}


class CommittedEventSink:
    """Batches text deltas and commits before any subscriber can observe them."""

    def __init__(
        self,
        store: RuntimeStore,
        *,
        run_id: str,
        activity_id: str,
        fencing_token: int,
        deadline_at_ms: int,
        flush_ms: int = 100,
        flush_bytes: int = 2048,
        clock: Clock | None = None,
    ) -> None:
        self.store = store
        self.run_id = run_id
        self.activity_id = activity_id
        self.fencing_token = fencing_token
        self.deadline_at_ms = deadline_at_ms
        self.flush_ms = flush_ms
        self.flush_bytes = flush_bytes
        self.clock = clock or SystemClock()
        self._buffer: list[str] = []
        self._buffer_bytes = 0
        self._full_text: list[str] = []
        self._timer: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._background_error: BaseException | None = None
        self._closed = False
        self.engine_error: dict[str, Any] | None = None
        self.citations: list[dict[str, Any]] = []

    @property
    def assistant_text(self) -> str:
        return "".join(self._full_text)

    def seed_assistant_text(self, text: str) -> None:
        """Restore a completed engine checkpoint without duplicating delta events."""
        if self._full_text:
            raise RuntimeError("assistant text was already produced in this attempt")
        self._full_text.append(text)

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._closed:
            raise RuntimeError("event sink is closed")
        self._raise_background_error()
        if event_type in {"text", EventType.OUTPUT_DELTA_COMMITTED}:
            await self.emit_text(str(payload.get("delta", "")))
            return
        async with self._lock:
            await self._flush_locked()
            if event_type == "error":
                # Engine diagnostics are not a public delivery event and never
                # decide terminal status by themselves.  Coordinator considers
                # them together with the explicit EngineOutcome.
                self.engine_error = dict(payload)
                await self.store.append_events(
                    self.run_id,
                    [EventDraft(
                        EventType.MODEL_MESSAGE_COMMITTED,
                        {"engine_error": payload},
                        activity_id=self.activity_id,
                        visibility=Visibility.INTERNAL,
                        occurred_at=self.clock.now_ms(),
                    )],
                    activity_id=self.activity_id,
                    fencing_token=self.fencing_token,
                    now_ms=self.clock.now_ms(),
                )
                return
            if event_type == "citation":
                self.citations.extend(payload.get("citations") or [])
                return
            canonical = _EVENT_MAP.get(event_type)
            if canonical is None:
                canonical = EventType(event_type)
            await self.store.append_events(
                self.run_id,
                [EventDraft(
                    canonical, dict(payload), activity_id=self.activity_id,
                    producer="engine", occurred_at=self.clock.now_ms(),
                )],
                activity_id=self.activity_id,
                fencing_token=self.fencing_token,
                now_ms=self.clock.now_ms(),
            )

    async def emit_text(self, delta: str) -> None:
        if not delta:
            return
        async with self._lock:
            self._buffer.append(delta)
            self._full_text.append(delta)
            self._buffer_bytes += len(delta.encode("utf-8"))
            if self._buffer_bytes >= self.flush_bytes:
                await self._flush_locked()
            elif self._timer is None:
                self._timer = asyncio.create_task(self._flush_after_delay())

    async def force_flush(self) -> None:
        self._raise_background_error()
        async with self._lock:
            await self._flush_locked()

    async def close(self) -> None:
        self._closed = True
        timer = self._timer
        self._timer = None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
            with suppress(asyncio.CancelledError):
                await timer
        self._raise_background_error()
        async with self._lock:
            await self._flush_locked()

    async def _flush_after_delay(self) -> None:
        try:
            await asyncio.sleep(self.flush_ms / 1000)
            async with self._lock:
                self._timer = None
                await self._flush_locked()
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # surfaced on the engine task's next boundary
            self._background_error = exc

    async def _flush_locked(self) -> None:
        if not self._buffer:
            return
        text = "".join(self._buffer)
        self._buffer.clear()
        self._buffer_bytes = 0
        timer = self._timer
        self._timer = None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        await self.store.append_events(
            self.run_id,
            [EventDraft(
                EventType.OUTPUT_DELTA_COMMITTED,
                {"delta": text},
                activity_id=self.activity_id,
                producer="engine",
                occurred_at=self.clock.now_ms(),
            )],
            activity_id=self.activity_id,
            fencing_token=self.fencing_token,
            now_ms=self.clock.now_ms(),
        )

    def _raise_background_error(self) -> None:
        if self._background_error is not None:
            error = self._background_error
            self._background_error = None
            raise error

    async def checkpoint(
        self,
        working_state: WorkingState,
        *,
        expected_revision: int,
        engine_state: dict[str, Any] | None = None,
        engine_state_ref: str | None = None,
    ) -> CheckpointRecord:
        await self.force_flush()
        return await self.store.save_checkpoint(
            run_id=self.run_id,
            activity_id=self.activity_id,
            fencing_token=self.fencing_token,
            expected_revision=expected_revision,
            working_state=working_state,
            engine_state=engine_state,
            engine_state_ref=engine_state_ref,
            now_ms=self.clock.now_ms(),
        )

    async def is_cancelled(self) -> bool:
        return await self.store.is_cancel_requested(self.run_id)

    def remaining_ms(self) -> int:
        return max(0, self.deadline_at_ms - self.clock.now_ms())
