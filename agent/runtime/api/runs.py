from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent.config import AgentSettings
from agent.runtime.application.admission import AdmissionService, CreateRunInput
from agent.runtime.domain.models import (
    CanonicalEvent,
    EventType,
    RunRecord,
    RunStatus,
    TERMINAL_RUN_STATUSES,
    ToolReconciliationPayload,
    ms_to_rfc3339,
    rfc3339_to_ms,
    sha256_json,
    utc_now_ms,
)
from agent.runtime.ports.store import RuntimeStore
from common.obs import get_trace_id

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])


class RunInputBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=200_000)
    attachment_refs: list[str] = Field(default_factory=list, max_length=32)


class CreateRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_request_id: UUID
    conversation_id: str | None = None
    principal_id: str = Field(default="demo-user", min_length=1, max_length=200)
    agent_id: str = Field(default="demo-agent", min_length=1, max_length=200)
    engine: Literal["plan_execute", "agent_loop", "native_loop"]
    input: RunInputBody
    deadline_at: datetime | None = None

    @field_validator("deadline_at")
    @classmethod
    def require_absolute_deadline(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("deadline_at must include an explicit UTC offset")
        return value


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(default="user requested cancellation", max_length=2000)


class SignalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signal_id: str = Field(min_length=1, max_length=200)
    wait_activity_id: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_reserved_signal_contract(self) -> "SignalBody":
        if self.type == "tool_reconciliation":
            self.payload = ToolReconciliationPayload.model_validate(
                self.payload
            ).model_dump(mode="json", exclude_none=True)
        return self


def _store(request: Request) -> RuntimeStore:
    return request.app.state.runtime_store


async def _run_json(run: RunRecord, store: RuntimeStore) -> dict[str, Any]:
    activity = (
        await store.get_activity(run.current_activity_id)
        if run.current_activity_id is not None
        else None
    )
    return {
        "schema_version": run.envelope.schema_version,
        "run_id": run.envelope.run_id,
        "turn_id": run.envelope.turn_id,
        "conversation_id": run.envelope.conversation_id,
        "request_id": run.envelope.request_id,
        "client_request_id": run.envelope.client_request_id,
        "principal_id": run.envelope.principal_id,
        "agent_id": run.envelope.agent_id,
        "engine": run.envelope.engine,
        "release_fingerprint": run.envelope.release_fingerprint,
        # 诊断关联键：让调用方从 Run 一跳到 /trace-ui 或 GET /api/v1/traces/{id}。
        "trace_id": run.trace_id,
        "status": run.status,
        "revision": run.revision,
        "current_activity_id": run.current_activity_id,
        "current_activity": (
            {
                "activity_id": activity.activity_id,
                "type": activity.type,
                "status": activity.status,
                "attempt": activity.attempt,
                "available_at": ms_to_rfc3339(activity.available_at),
                "lease_expires_at": ms_to_rfc3339(activity.lease_expires_at),
                "fencing_token": activity.fencing_token,
                "revision": activity.revision,
            }
            if activity is not None else None
        ),
        "pending_input": run.pending_input,
        "terminal": (
            {"status": run.terminal_status, "payload": run.terminal_payload}
            if run.terminal_status else None
        ),
        "last_seq": run.next_seq - 1,
        "deadline_at": ms_to_rfc3339(run.envelope.deadline_at),
        "created_at": ms_to_rfc3339(run.envelope.created_at),
        "updated_at": ms_to_rfc3339(run.updated_at),
    }


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    body: CreateRunBody,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    settings: AgentSettings = request.app.state.settings
    service = AdmissionService(
        _store(request),
        default_deadline_ms=settings.runtime_default_deadline_seconds * 1000,
    )
    result = await service.create(
        CreateRunInput(
            client_request_id=str(body.client_request_id),
            conversation_id=body.conversation_id,
            principal_id=body.principal_id,
            agent_id=body.agent_id,
            engine=body.engine,
            text=body.input.text,
            attachment_refs=tuple(body.input.attachment_refs),
            deadline_at=rfc3339_to_ms(body.deadline_at),
            # TraceMiddleware 已把 x-trace-id（或新生成的 id）放进 contextvar。
            # 这里把它固化到 Run 上，让另一个进程里的 Worker 能接上同一条轨迹。
            trace_id=get_trace_id(),
        ),
        idempotency_key=idempotency_key or "",
    )
    run = result.run
    base = f"/api/v1/runs/{run.envelope.run_id}"
    response.headers["Location"] = base
    return {
        "run_id": run.envelope.run_id,
        "turn_id": run.envelope.turn_id,
        "conversation_id": run.envelope.conversation_id,
        "status": run.status,
        "reused": result.reused,
        "status_url": base,
        "events_url": base + "/events",
    }


@router.get("/{run_id}")
async def get_run(run_id: str, request: Request) -> dict[str, Any]:
    store = _store(request)
    return await _run_json(await store.get_run(run_id), store)


@router.post("/{run_id}/cancel")
async def cancel_run(run_id: str, body: CancelBody, request: Request) -> dict[str, Any]:
    run, reused = await _store(request).request_cancel(
        run_id=run_id,
        command_id=body.command_id,
        reason=body.reason,
        now_ms=utc_now_ms(),
    )
    return {
        "run": await _run_json(run, _store(request)),
        "command_id": body.command_id,
        "reused": reused,
    }


@router.post("/{run_id}/signals")
async def submit_signal(run_id: str, body: SignalBody, request: Request) -> dict[str, Any]:
    digest = sha256_json({"type": body.type, "payload": body.payload,
                          "wait_activity_id": body.wait_activity_id})
    result = await _store(request).submit_signal(
        run_id=run_id,
        signal_id=body.signal_id,
        wait_activity_id=body.wait_activity_id,
        signal_type=body.type,
        payload=body.payload,
        payload_digest=digest,
        now_ms=utc_now_ms(),
    )
    return {
        "signal_id": result.signal_id,
        "status": result.status,
        "reused": result.reused,
        "run": await _run_json(result.run, _store(request)),
    }


_SSE_EVENT_NAMES: dict[EventType, str] = {
    EventType.USER_MESSAGE_COMMITTED: "user_message",
    EventType.OUTPUT_DELTA_COMMITTED: "text",
    EventType.TOOL_CALL_COMMITTED: "tool_call",
    EventType.TOOL_RESULT_COMMITTED: "tool_result",
    EventType.MODEL_PLAN_UPDATED: "plan_step",
    EventType.SKILL_UI_FRAME_COMMITTED: "skill_event",
    EventType.CITATION_SET_COMMITTED: "citation",
    EventType.ASSISTANT_MESSAGE_COMMITTED: "assistant_message",
    EventType.RUN_STATUS_CHANGED: "run_status",
    EventType.ACTIVITY_STATUS_CHANGED: "activity_status",
    EventType.RUN_TERMINATED: "terminal",
}


def _sse(event: CanonicalEvent) -> str:
    body = {
        "schema_version": event.schema_version,
        "event_id": event.event_id,
        "run_id": event.run_id,
        "turn_id": event.turn_id,
        "activity_id": event.activity_id,
        "tool_execution_id": event.tool_execution_id,
        "seq": event.seq,
        "event_type": event.event_type,
        "payload": event.payload,
        "payload_ref": event.payload_ref,
        "terminal_status": event.terminal_status,
        "occurred_at": ms_to_rfc3339(event.occurred_at),
        "release_fingerprint": event.release_fingerprint,
    }
    name = _SSE_EVENT_NAMES.get(event.event_type, event.event_type.lower())
    return (
        f"id: {event.seq}\n"
        f"event: {name}\n"
        f"data: {json.dumps(body, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


@router.get("/{run_id}/events")
async def stream_events(
    run_id: str,
    request: Request,
    after_seq: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    store = _store(request)
    await store.get_run(run_id)  # fail with 404 before the streaming response starts
    header_cursor = 0
    if last_event_id:
        try:
            header_cursor = max(0, int(last_event_id))
        except ValueError:
            header_cursor = 0
    initial_cursor = after_seq if after_seq is not None else header_cursor
    settings: AgentSettings = request.app.state.settings

    async def generate():
        cursor = initial_cursor
        last_write = time.monotonic()
        while True:
            events = await store.list_events(run_id, after_seq=cursor, limit=500)
            for event in events:
                cursor = event.seq
                yield _sse(event)
                last_write = time.monotonic()
                if event.event_type is EventType.RUN_TERMINATED:
                    return
            run = await store.get_run(run_id)
            if run.status in TERMINAL_RUN_STATUSES and not events:
                return
            if time.monotonic() - last_write >= settings.runtime_sse_heartbeat_seconds:
                yield ": heartbeat\n\n"
                last_write = time.monotonic()
            await asyncio.sleep(settings.runtime_sse_poll_ms / 1000)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
