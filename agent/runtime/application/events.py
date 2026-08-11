from __future__ import annotations

import asyncio
import time
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

# 逐条记进轨迹的事件类型。text 走累积（一次回答几百个 delta，逐条记会把 span
# 撑爆且没有诊断价值），改成 TTFT + 计数 + 一个 answer payload。
_TRACED_EVENTS = frozenset({
    "tool_call", "tool_result", "citation", "plan_step", "skill_event", "error",
})


def _traced_fields(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """按事件类型挑出有诊断价值的字段（避免整包 payload 灌进 span）。"""
    if event_type == "tool_call":
        return {"tool": payload.get("name"), "call_id": payload.get("id"),
                "args": payload.get("args")}
    if event_type == "tool_result":
        return {"tool": payload.get("name"), "call_id": payload.get("id"),
                "response": payload.get("response")}
    if event_type == "citation":
        refs = payload.get("citations") or []
        return {"doc_ids": [r.get("doc_id") for r in refs if isinstance(r, dict)]}
    if event_type == "plan_step":
        return {"step": payload.get("step"), "total": payload.get("total"),
                "title": payload.get("title"), "status": payload.get("status")}
    if event_type == "skill_event":
        return {"data_type": payload.get("dataType"),
                "is_thinking": payload.get("isThinking")}
    if event_type == "error":
        return {"message": payload.get("message"), "reason": payload.get("reason")}
    return {"payload": payload}


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
        # ── 轨迹旁路（诊断，绝不影响提交语义）──────────────────────────────
        # 这里是三代引擎唯一共同的事件出口，所以一处埋点即产出对等信号——
        # 评测的失败归因规则正建立在这种对等上，否则"谁埋点更深"会变成引擎
        # 对比里的假优势。
        self._span: Any = None
        self._trace_started = time.monotonic()
        self.ttft_ms: float | None = None
        self.event_counts: dict[str, int] = {}
        self.had_error = False

    def attach_trace_span(self, span: Any) -> None:
        """绑定本次 attempt 的 engine span（对 tracing 关闭时的空实现安全）。"""
        self._span = span
        self._trace_started = time.monotonic()

    def _trace_event(self, event_type: str, payload: dict[str, Any]) -> None:
        self.event_counts[event_type] = self.event_counts.get(event_type, 0) + 1
        if event_type == "error":
            self.had_error = True
        if self._span is None or event_type not in _TRACED_EVENTS:
            return
        self._span.add_event(event_type, **_traced_fields(event_type, payload))

    def trace_rollup(self) -> dict[str, Any]:
        """收尾属性；由 RunCoordinator 在关闭 engine span 前写入。"""
        return {
            "ttft_ms": self.ttft_ms,
            "total_ms": round((time.monotonic() - self._trace_started) * 1000, 1),
            "answer_chars": len(self.assistant_text),
            "event_counts": self.event_counts or None,
            "had_error": self.had_error,
        }

    @property
    def assistant_text(self) -> str:
        return "".join(self._full_text)

    def seed_assistant_text(self, text: str) -> None:
        """Restore a completed engine checkpoint without duplicating delta events."""
        if self._full_text:
            raise RuntimeError("assistant text was already produced in this attempt")
        self._full_text.append(text)

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        '''
        拿到锁 → flush text buffer → store.append_events()
        '''
        if self._closed:
            raise RuntimeError("event sink is closed")
        self._raise_background_error()
        # 记在最前面：error / citation 都会提前 return，放后面会漏计。
        self._trace_event(str(event_type), payload)
        if event_type in {"text", EventType.OUTPUT_DELTA_COMMITTED}:
            await self.emit_text(str(payload.get("delta", "")))
            return
        async with self._lock: # 拿到锁，非 text 事件（tool_call、plan_step、error 等）走锁保护的同步写入路径。锁保证 flush text buffer 和写入新事件之间的顺序——模型流输出必须在 ToolCall 事实之前完成持久化
            await self._flush_locked() # 先把积攒的 text delta 刷入 DB（OUTPUT_DELTA_COMMITTED），确保 100ms/2KiB 聚合的文本在后续 ToolCall/ToolResult 之前有序提交
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
                self.citations.extend(payload.get("citations") or []) # 引用信息只收集到内存，不立即写 DB。最终由 terminal 提交时一起落库（final assistant + citation + success terminal 同一事务）
                return
            canonical = _EVENT_MAP.get(event_type) # 通过 _EVENT_MAP 将引擎内部事件名映射为规范 EventType
            if canonical is None:
                canonical = EventType(event_type)
            await self.store.append_events( # 调用 store.append_events() 写入 run_events 表，带 fencing_token 做所有权校验
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
        if self.ttft_ms is None:
            self.ttft_ms = round((time.monotonic() - self._trace_started) * 1000, 1)
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
