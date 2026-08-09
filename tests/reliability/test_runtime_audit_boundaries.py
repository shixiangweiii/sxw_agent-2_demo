from __future__ import annotations

import uuid
import sqlite3
from dataclasses import dataclass

import pytest

from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.runtime.application.admission import AdmissionService, CreateRunInput
from agent.runtime.domain.errors import RuntimeFault
from agent.runtime.domain.models import (
    ActivityStatus,
    EventType,
    ReleaseManifest,
    RunStatus,
    ToolResultEnvelope,
    ToolResultStatus,
    Visibility,
)
from agent.runtime.ports.store import EventDraft


@dataclass
class FakeClock:
    value: int = 1_900_000_000_000

    def now_ms(self) -> int:
        return self.value

    def monotonic(self) -> float:
        return self.value / 1000

    def advance(self, milliseconds: int) -> None:
        self.value += milliseconds


async def _runtime(tmp_path):
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    await store.register_release(
        ReleaseManifest(engine="native_loop", components={"audit": "v1"}),
        activate=True,
    )
    return store, FakeClock()


def _request(*, client_request_id: str, attachment_refs: tuple[str, ...] = ()):
    return CreateRunInput(
        client_request_id=client_request_id,
        conversation_id=None,
        principal_id="audit-user",
        agent_id="audit-agent",
        engine="native_loop",
        text="audit request",
        attachment_refs=attachment_refs,
        deadline_at=None,
    )


async def _admit(store, clock, *, key: str, request=None):
    service = AdmissionService(store, clock=clock, default_deadline_ms=60_000)
    return await service.create(
        request or _request(client_request_id=str(uuid.uuid4())),
        idempotency_key=key,
    )


async def _claim_and_start(store, clock, *, lease_ms: int = 1_000):
    claim = await store.claim_next(
        worker_id="audit-worker", lease_ms=lease_ms, now_ms=clock.now_ms(),
    )
    assert claim is not None
    activity = await store.mark_activity_running(
        claim.activity.activity_id,
        worker_id="audit-worker",
        fencing_token=claim.activity.fencing_token,
        now_ms=clock.now_ms(),
    )
    return claim, activity


async def _dispatch_unknown_effect(store, clock, run, parent, *, logical_key: str):
    prepared = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key=logical_key,
        tool_name="uncertain_effect",
        release_digest="unknown-v1",
        effect_class="UNKNOWN_EFFECT",
        request_digest=f"digest:{logical_key}",
        request={},
        now_ms=clock.now_ms(),
    )
    await store.mark_tool_dispatched(
        tool_execution_id=prepared["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        now_ms=clock.now_ms(),
    )
    await store.settle_tool_execution(
        tool_execution_id=prepared["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        effect_status="UNKNOWN",
        result=ToolResultEnvelope(
            status=ToolResultStatus.UNKNOWN,
            error_code="ACK_LOST",
            error_message="runtime acknowledgement was lost",
        ).model_dump(mode="json"),
        result_ref=None,
        error={"code": "ACK_LOST"},
        external_object_id=None,
        now_ms=clock.now_ms(),
    )
    return prepared


@pytest.mark.asyncio
async def test_admission_atomically_links_distinct_input_attachments(tmp_path):
    store, clock = await _runtime(tmp_path)
    artifact_id = "a" * 64
    await store.register_artifact_metadata(
        artifact_id=artifact_id,
        sha256=artifact_id,
        size_bytes=3,
        media_type="image/png",
        storage_path=f"sha256/aa/{artifact_id}",
        created_at=clock.now_ms(),
    )
    request = _request(
        client_request_id=str(uuid.uuid4()),
        attachment_refs=(artifact_id, artifact_id),
    )

    admitted = await _admit(store, clock, key="attachment-admission", request=request)
    replay = await _admit(store, clock, key="attachment-admission", request=request)

    assert replay.reused is True
    assert replay.run.envelope.run_id == admitted.run.envelope.run_id
    assert replay.run.envelope.attachment_refs == (artifact_id, artifact_id)
    async with store.db.read() as conn:
        links = await (await conn.execute(
            """SELECT artifact_id,run_id,activity_id,event_id,relation,sensitivity
               FROM artifact_links WHERE run_id=?""",
            (admitted.run.envelope.run_id,),
        )).fetchall()
    assert [dict(row) for row in links] == [{
        "artifact_id": artifact_id,
        "run_id": admitted.run.envelope.run_id,
        "activity_id": admitted.run.current_activity_id,
        "event_id": admitted.run.envelope.input_event_id,
        "relation": "INPUT_ATTACHMENT",
        "sensitivity": "PRIVATE",
    }]


@pytest.mark.asyncio
async def test_attachment_link_rolls_back_when_admission_event_commit_fails(tmp_path):
    store, clock = await _runtime(tmp_path)
    artifact_id = "b" * 64
    await store.register_artifact_metadata(
        artifact_id=artifact_id,
        sha256=artifact_id,
        size_bytes=3,
        media_type="image/png",
        storage_path=f"sha256/bb/{artifact_id}",
        created_at=clock.now_ms(),
    )
    async with store.db.transaction() as conn:
        await conn.execute(
            """CREATE TRIGGER fail_input_event BEFORE INSERT ON run_events
               WHEN NEW.event_type='USER_MESSAGE_COMMITTED'
               BEGIN SELECT RAISE(ABORT,'injected admission event failure'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected admission event failure"):
        await _admit(
            store,
            clock,
            key="attachment-rollback",
            request=_request(
                client_request_id=str(uuid.uuid4()),
                attachment_refs=(artifact_id,),
            ),
        )

    async with store.db.read() as conn:
        counts = {}
        for table in ("runs", "run_requests", "artifact_links"):
            counts[table] = (await (await conn.execute(
                f"SELECT COUNT(*) AS n FROM {table}"
            )).fetchone())["n"]
    assert counts == {"runs": 0, "run_requests": 0, "artifact_links": 0}


@pytest.mark.asyncio
async def test_direct_cancel_records_activity_transition_and_cancels_retry_timer(tmp_path):
    store, clock = await _runtime(tmp_path)
    run = (await _admit(store, clock, key="cancel-ledger")).run
    _claim, activity = await _claim_and_start(store, clock)
    await store.schedule_retry(
        run_id=run.envelope.run_id,
        activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        fire_at=clock.now_ms() + 10_000,
        error={"code": "TRANSIENT"},
        now_ms=clock.now_ms(),
    )

    cancelled, reused = await store.request_cancel(
        run_id=run.envelope.run_id,
        command_id="cancel-ledger-command",
        reason="stop",
        now_ms=clock.now_ms(),
    )

    assert reused is False
    assert cancelled.terminal_status is RunStatus.CANCELLED
    assert (await store.get_activity(activity.activity_id)).status is ActivityStatus.CANCELLED
    events = await store.list_events(run.envelope.run_id, visibility=None)
    transition = next(
        event for event in events
        if event.event_type is EventType.ACTIVITY_STATUS_CHANGED
        and event.payload.get("reason") == "RUN_CANCELLED"
    )
    assert transition.payload["from"] == ActivityStatus.WAITING_RETRY
    assert transition.payload["to"] == ActivityStatus.CANCELLED
    assert sum(event.event_type is EventType.RUN_TERMINATED for event in events) == 1
    async with store.db.read() as conn:
        timer = await (await conn.execute(
            "SELECT state FROM timers WHERE run_id=?", (run.envelope.run_id,),
        )).fetchone()
    assert timer["state"] == "CANCELLED"
    clock.advance(10_000)
    assert await store.fire_due_timers(now_ms=clock.now_ms()) == 0


@pytest.mark.asyncio
async def test_new_cancel_command_does_not_emit_cancel_requested_self_transition(tmp_path):
    store, clock = await _runtime(tmp_path)
    run = (await _admit(store, clock, key="cancel-self-transition")).run
    await _claim_and_start(store, clock)

    first, first_reused = await store.request_cancel(
        run_id=run.envelope.run_id,
        command_id="cancel-command-1",
        reason="first request",
        now_ms=clock.now_ms(),
    )
    second, second_reused = await store.request_cancel(
        run_id=run.envelope.run_id,
        command_id="cancel-command-2",
        reason="second observer also requested cancel",
        now_ms=clock.now_ms(),
    )
    replay, replay_reused = await store.request_cancel(
        run_id=run.envelope.run_id,
        command_id="cancel-command-2",
        reason="second observer also requested cancel",
        now_ms=clock.now_ms(),
    )

    assert first.status is RunStatus.CANCEL_REQUESTED
    assert second.status is RunStatus.CANCEL_REQUESTED
    assert replay.status is RunStatus.CANCEL_REQUESTED
    assert first_reused is second_reused is False
    assert replay_reused is True
    events = await store.list_events(run.envelope.run_id, visibility=None)
    cancel_events = [
        event for event in events if event.event_type is EventType.CANCEL_REQUESTED
    ]
    cancel_transitions = [
        event for event in events
        if event.event_type is EventType.RUN_STATUS_CHANGED
        and event.payload.get("to") == RunStatus.CANCEL_REQUESTED
    ]
    assert len(cancel_events) == 2
    assert cancel_events[-1].payload["already_requested"] is True
    assert len(cancel_transitions) == 1
    assert cancel_transitions[0].payload["from"] == RunStatus.RUNNING


@pytest.mark.asyncio
async def test_cancel_with_unknown_effect_uses_conditional_waiting_to_requested_edge(
    tmp_path,
):
    store, clock = await _runtime(tmp_path)
    run = (await _admit(store, clock, key="cancel-unknown-from-wait")).run
    _claim, parent = await _claim_and_start(store, clock)
    prepared = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="cancel-unknown:0",
        tool_name="uncertain_effect",
        release_digest="unknown-v1",
        effect_class="UNKNOWN_EFFECT",
        request_digest="uncertain-request",
        request={},
        now_ms=clock.now_ms(),
    )
    await store.mark_tool_dispatched(
        tool_execution_id=prepared["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        now_ms=clock.now_ms(),
    )
    await store.settle_tool_execution(
        tool_execution_id=prepared["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        effect_status="UNKNOWN",
        result=ToolResultEnvelope(
            status=ToolResultStatus.UNKNOWN,
            error_code="ACK_LOST",
            error_message="runtime acknowledgement was lost",
        ).model_dump(mode="json"),
        result_ref=None,
        error={"code": "ACK_LOST"},
        external_object_id=None,
        now_ms=clock.now_ms(),
    )
    clock.advance(1_001)
    assert await store.recover_expired(now_ms=clock.now_ms()) == 1
    assert (await store.get_run(run.envelope.run_id)).status is RunStatus.WAITING_INPUT

    cancelling, reused = await store.request_cancel(
        run_id=run.envelope.run_id,
        command_id="cancel-unknown-effect",
        reason="do not resume uncertain work",
        now_ms=clock.now_ms(),
    )

    assert reused is False
    assert cancelling.status is RunStatus.CANCEL_REQUESTED
    assert cancelling.terminal_status is None
    events = await store.list_events(run.envelope.run_id, visibility=None)
    transition = next(
        event for event in events
        if event.event_type is EventType.RUN_STATUS_CHANGED
        and event.payload.get("to") == RunStatus.CANCEL_REQUESTED
    )
    assert transition.payload["from"] == RunStatus.WAITING_INPUT
    cancel = next(
        event for event in events
        if event.event_type is EventType.CANCEL_REQUESTED
    )
    assert cancel.payload["unresolved_tool_execution_ids"] == [
        prepared["tool_execution_id"]
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["retry", "wait"])
async def test_cancel_race_at_retry_or_wait_keeps_unknown_effect_in_reconcile(
    tmp_path,
    boundary,
):
    store, clock = await _runtime(tmp_path)
    run = (await _admit(store, clock, key=f"cancel-{boundary}-race")).run
    _claim, parent = await _claim_and_start(store, clock)
    prepared = await _dispatch_unknown_effect(
        store, clock, run, parent, logical_key=f"cancel-{boundary}:0",
    )
    cancelling, _ = await store.request_cancel(
        run_id=run.envelope.run_id,
        command_id=f"cancel-before-{boundary}",
        reason="cancel CAS committed before coordinator boundary",
        now_ms=clock.now_ms(),
    )
    assert cancelling.status is RunStatus.CANCEL_REQUESTED

    if boundary == "retry":
        await store.schedule_retry(
            run_id=run.envelope.run_id,
            activity_id=parent.activity_id,
            fencing_token=parent.fencing_token,
            fire_at=clock.now_ms() + 1_000,
            error={"code": "TRANSIENT"},
            now_ms=clock.now_ms(),
        )
    else:
        await store.wait_for_input(
            run_id=run.envelope.run_id,
            activity_id=parent.activity_id,
            fencing_token=parent.fencing_token,
            pending_input={"type": "approval"},
            now_ms=clock.now_ms(),
        )

    current = await store.get_run(run.envelope.run_id)
    assert current.status is RunStatus.CANCEL_REQUESTED
    assert current.terminal_status is None
    assert (await store.get_activity(parent.activity_id)).status is ActivityStatus.RECONCILE
    assert await store.unresolved_tool_execution_ids(run.envelope.run_id) == [
        prepared["tool_execution_id"]
    ]
    async with store.db.read() as conn:
        timers = (await (await conn.execute(
            "SELECT COUNT(*) AS n FROM timers WHERE run_id=?",
            (run.envelope.run_id,),
        )).fetchone())["n"]
    assert timers == 0
    events = await store.list_events(run.envelope.run_id, visibility=None)
    assert not any(
        event.event_type is EventType.RUN_TERMINATED for event in events
    )


@pytest.mark.asyncio
async def test_cancelled_run_with_unknown_effect_times_out_with_unresolved_payload(tmp_path):
    store, clock = await _runtime(tmp_path)
    run = (await _admit(store, clock, key="cancel-unknown-deadline")).run
    _claim, parent = await _claim_and_start(store, clock)
    prepared = await _dispatch_unknown_effect(
        store, clock, run, parent, logical_key="cancel-deadline:0",
    )
    await store.request_cancel(
        run_id=run.envelope.run_id,
        command_id="cancel-before-deadline",
        reason="cancel first, uncertainty remains",
        now_ms=clock.now_ms(),
    )

    terminal = await store.finalize_failure(
        run_id=run.envelope.run_id,
        activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        code="DEADLINE_EXCEEDED",
        message="unresolved effect reached the absolute deadline",
        terminal_status=RunStatus.TIMED_OUT,
        now_ms=clock.now_ms(),
    )

    assert terminal.terminal_status is RunStatus.TIMED_OUT
    assert terminal.terminal_payload["unresolved_tool_execution_ids"] == [
        prepared["tool_execution_id"]
    ]
    events = await store.list_events(run.envelope.run_id, visibility=None)
    transition = next(
        event for event in events
        if event.event_type is EventType.RUN_STATUS_CHANGED
        and event.payload.get("to") == RunStatus.TIMED_OUT
    )
    assert transition.payload["from"] == RunStatus.CANCEL_REQUESTED


@pytest.mark.asyncio
async def test_terminal_success_closes_never_dispatched_prepared_tool_atomically(tmp_path):
    store, clock = await _runtime(tmp_path)
    run = (await _admit(store, clock, key="prepared-short-circuit")).run
    _claim, parent = await _claim_and_start(store, clock)
    prepared = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="adk-batch:0:short-circuited",
        tool_name="translate",
        release_digest="translate-v1",
        effect_class="READ_ONLY",
        request_digest="invalid-args-digest",
        request={"__tool_args_parse_error__": True},
        framework_call_id="attempt-local-call",
        now_ms=clock.now_ms(),
    )

    terminal = await store.finalize_success(
        run_id=run.envelope.run_id,
        activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        assistant_text="The invalid tool call was corrected without dispatch.",
        citations=[],
        now_ms=clock.now_ms(),
    )

    assert terminal.terminal_status is RunStatus.SUCCEEDED
    execution = await store.get_tool_execution(prepared["tool_execution_id"])
    assert execution["effect_status"] == "FAILED"
    assert execution["attempt"] == 0
    assert (await store.get_activity(prepared["activity_id"])).status is ActivityStatus.CANCELLED
    events = await store.list_events(run.envelope.run_id, visibility=None)
    calls = [
        event for event in events
        if event.event_type is EventType.TOOL_CALL_COMMITTED
        and event.tool_execution_id == prepared["tool_execution_id"]
    ]
    results = [
        event for event in events
        if event.event_type is EventType.TOOL_RESULT_COMMITTED
        and event.tool_execution_id == prepared["tool_execution_id"]
    ]
    assert len(calls) == len(results) == 1
    assert results[0].payload["status"] == "FAILED"
    assert results[0].payload["synthetic"] is True
    assert results[0].payload["error"]["not_dispatched"] is True
    assistant = next(
        event for event in events
        if event.event_type is EventType.ASSISTANT_MESSAGE_COMMITTED
    )
    assert results[0].seq < assistant.seq


@pytest.mark.asyncio
async def test_prepared_tool_closure_rolls_back_with_failed_terminal_commit(tmp_path):
    store, clock = await _runtime(tmp_path)
    run = (await _admit(store, clock, key="prepared-terminal-rollback")).run
    _claim, parent = await _claim_and_start(store, clock)
    prepared = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="adk-batch:0:rollback",
        tool_name="translate",
        release_digest="translate-v1",
        effect_class="READ_ONLY",
        request_digest="invalid-args-digest",
        request={"__tool_args_parse_error__": True},
        now_ms=clock.now_ms(),
    )
    async with store.db.transaction() as conn:
        await conn.execute(
            """CREATE TRIGGER fail_terminal_after_prepared BEFORE INSERT ON run_events
               WHEN NEW.event_type='RUN_TERMINATED'
               BEGIN SELECT RAISE(ABORT,'injected terminal failure'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected terminal failure"):
        await store.finalize_success(
            run_id=run.envelope.run_id,
            activity_id=parent.activity_id,
            fencing_token=parent.fencing_token,
            assistant_text="must roll back",
            citations=[],
            now_ms=clock.now_ms(),
        )

    assert (await store.get_run(run.envelope.run_id)).terminal_status is None
    execution = await store.get_tool_execution(prepared["tool_execution_id"])
    assert execution["effect_status"] == "PREPARED"
    assert (await store.get_activity(prepared["activity_id"])).status is ActivityStatus.PENDING
    events = await store.list_events(run.envelope.run_id, visibility=None)
    assert not any(
        event.event_type in {
            EventType.TOOL_RESULT_COMMITTED,
            EventType.ASSISTANT_MESSAGE_COMMITTED,
            EventType.CITATION_SET_COMMITTED,
            EventType.RUN_TERMINATED,
        }
        for event in events
    )


@pytest.mark.asyncio
async def test_tool_completion_after_cancel_is_late_fact_and_cannot_succeed_run(tmp_path):
    store, clock = await _runtime(tmp_path)
    run = (await _admit(store, clock, key="cancel-tool-complete")).run
    _claim, activity = await _claim_and_start(store, clock)
    prepared = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        logical_key="cancel-race:0",
        tool_name="demo_effect",
        release_digest="demo-v1",
        effect_class="IDEMPOTENT_EFFECT",
        request_digest="request-digest",
        request={"business_key": "task-1"},
        now_ms=clock.now_ms(),
    )
    await store.mark_tool_dispatched(
        tool_execution_id=prepared["tool_execution_id"],
        parent_activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        now_ms=clock.now_ms(),
    )
    cancel_requested, _ = await store.request_cancel(
        run_id=run.envelope.run_id,
        command_id="cancel-before-tool-result",
        reason="cancel won the database race",
        now_ms=clock.now_ms(),
    )
    assert cancel_requested.status is RunStatus.CANCEL_REQUESTED

    await store.settle_tool_execution(
        tool_execution_id=prepared["tool_execution_id"],
        parent_activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        effect_status="COMMITTED",
        result=ToolResultEnvelope(
            status=ToolResultStatus.SUCCESS,
            preview={"created": True},
            external_object_id="task-1",
        ).model_dump(mode="json"),
        result_ref=None,
        error=None,
        external_object_id="task-1",
        now_ms=clock.now_ms(),
    )
    events = await store.list_events(run.envelope.run_id, visibility=None)
    result_event = next(
        event for event in events
        if event.event_type is EventType.TOOL_RESULT_COMMITTED
        and event.tool_execution_id == prepared["tool_execution_id"]
    )
    assert result_event.payload["late_result"] is True

    terminal = await store.finalize_success(
        run_id=run.envelope.run_id,
        activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        assistant_text="a late model result",
        citations=[],
        now_ms=clock.now_ms(),
    )
    assert terminal.terminal_status is RunStatus.CANCELLED
    events = await store.list_events(run.envelope.run_id, visibility=None)
    assert not any(
        event.event_type is EventType.ASSISTANT_MESSAGE_COMMITTED for event in events
    )


@pytest.mark.asyncio
async def test_deadline_late_signal_replay_stays_409_and_mismatch_is_stable(tmp_path):
    store, clock = await _runtime(tmp_path)
    run = (await _admit(store, clock, key="deadline-signal")).run
    _claim, activity = await _claim_and_start(store, clock)
    await store.wait_for_input(
        run_id=run.envelope.run_id,
        activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        pending_input={"type": "approval"},
        now_ms=clock.now_ms(),
    )
    clock.advance(60_000)
    assert await store.expire_deadlines(now_ms=clock.now_ms()) == 1
    terminal = await store.get_run(run.envelope.run_id)
    assert terminal.terminal_status is RunStatus.TIMED_OUT

    async def submit(digest: str):
        return await store.submit_signal(
            run_id=run.envelope.run_id,
            signal_id="late-approval",
            wait_activity_id=activity.activity_id,
            signal_type="approval",
            payload={"approved": True},
            payload_digest=digest,
            now_ms=clock.now_ms(),
        )

    for _ in range(2):
        with pytest.raises(RuntimeFault) as raised:
            await submit("digest-v1")
        assert raised.value.code == "RUN_ALREADY_TERMINAL"
        assert raised.value.http_status == 409

    with pytest.raises(RuntimeFault) as mismatch:
        await submit("digest-v2")
    assert mismatch.value.code == "SIGNAL_REPLAY_MISMATCH"
    assert mismatch.value.http_status == 409
    async with store.db.read() as conn:
        audit = await (await conn.execute(
            """SELECT status,COUNT(*) AS n FROM signals
               WHERE run_id=? AND signal_id=?""",
            (run.envelope.run_id, "late-approval"),
        )).fetchone()
    assert dict(audit) == {"status": "REJECTED_LATE", "n": 1}


@pytest.mark.asyncio
async def test_manual_effect_created_by_lease_recovery_consumes_audited_resolution(tmp_path):
    store, clock = await _runtime(tmp_path)
    run = (await _admit(store, clock, key="reconcile-signal")).run
    _claim, parent = await _claim_and_start(store, clock)
    prepared = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="uncertain:0",
        tool_name="uncertain_effect",
        release_digest="unknown-v1",
        effect_class="UNKNOWN_EFFECT",
        request_digest="uncertain-request",
        request={},
        now_ms=clock.now_ms(),
    )
    await store.mark_tool_dispatched(
        tool_execution_id=prepared["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        now_ms=clock.now_ms(),
    )
    await store.settle_tool_execution(
        tool_execution_id=prepared["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        effect_status="UNKNOWN",
        result=ToolResultEnvelope(
            status=ToolResultStatus.UNKNOWN,
            error_code="ACK_LOST",
            error_message="runtime acknowledgement was lost",
        ).model_dump(mode="json"),
        result_ref=None,
        error={"code": "ACK_LOST"},
        external_object_id=None,
        now_ms=clock.now_ms(),
    )
    await store.settle_tool_execution(
        tool_execution_id=prepared["tool_execution_id"],
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        effect_status="MANUAL_REQUIRED",
        result={
            "status": "UNKNOWN",
            "error_code": "ACK_LOST",
            "error_message": "operator decision required",
        },
        result_ref=None,
        error={"code": "ACK_LOST"},
        external_object_id=None,
        now_ms=clock.now_ms(),
    )
    clock.advance(1_001)
    assert await store.recover_expired(now_ms=clock.now_ms()) == 1
    waiting = await store.get_run(run.envelope.run_id)
    assert waiting.status is RunStatus.WAITING_INPUT
    assert waiting.pending_input["type"] == "TOOL_RECONCILIATION"
    assert (await store.get_activity(parent.activity_id)).status is ActivityStatus.RECONCILE

    signal = await store.submit_signal(
        run_id=run.envelope.run_id,
        signal_id="reconcile-decision",
        wait_activity_id=parent.activity_id,
        signal_type="tool_reconciliation",
        payload={
            "tool_execution_id": prepared["tool_execution_id"],
            "action": "mark_failed",
            "evidence": {"source": "external-ledger", "effect_absent": True},
            "result": {
                "status": "FAILURE",
                "error_code": "EFFECT_NOT_COMMITTED",
                "error_message": "external ledger confirms the effect is absent",
            },
        },
        payload_digest="reconcile-signal-digest",
        now_ms=clock.now_ms(),
    )
    assert signal.status == "CONSUMED"
    assert signal.run.status is RunStatus.DISPATCH_PENDING
    resumed = await store.get_activity(parent.activity_id)
    assert resumed.status is ActivityStatus.PENDING
    assert resumed.resume_payload == {
        "signal_id": "reconcile-decision",
        "type": "tool_reconciliation",
        "payload": {
            "tool_execution_id": prepared["tool_execution_id"],
            "action": "mark_failed",
        },
    }
    execution = await store.get_tool_execution(prepared["tool_execution_id"])
    assert execution["effect_status"] == "FAILED"
    assert (await store.get_activity(execution["activity_id"])).status is ActivityStatus.FAILED


@pytest.mark.asyncio
async def test_stale_fencing_rejects_late_final_result(tmp_path):
    store, clock = await _runtime(tmp_path)
    run = (await _admit(store, clock, key="stale-final")).run
    old_claim, _activity = await _claim_and_start(store, clock)
    clock.advance(1_001)
    assert await store.recover_expired(now_ms=clock.now_ms()) == 1
    replacement = await store.claim_next(
        worker_id="replacement-worker", lease_ms=1_000, now_ms=clock.now_ms(),
    )
    assert replacement is not None

    with pytest.raises(RuntimeFault) as stale:
        await store.finalize_success(
            run_id=run.envelope.run_id,
            activity_id=old_claim.activity.activity_id,
            fencing_token=old_claim.activity.fencing_token,
            assistant_text="late result must not win",
            citations=[],
            now_ms=clock.now_ms(),
        )
    assert stale.value.code == "STALE_FENCING_TOKEN"
    current = await store.get_run(run.envelope.run_id)
    assert current.terminal_status is None
    events = await store.list_events(run.envelope.run_id, visibility=None)
    assert not any(
        event.event_type is EventType.ASSISTANT_MESSAGE_COMMITTED for event in events
    )


@pytest.mark.asyncio
async def test_expired_claim_cannot_start_before_recovery_scan(tmp_path):
    store, clock = await _runtime(tmp_path)
    await _admit(store, clock, key="expired-claim-start")
    claim = await store.claim_next(
        worker_id="slow-worker", lease_ms=1_000, now_ms=clock.now_ms(),
    )
    assert claim is not None
    clock.advance(1_001)

    with pytest.raises(RuntimeFault) as expired:
        await store.mark_activity_running(
            claim.activity.activity_id,
            worker_id="slow-worker",
            fencing_token=claim.activity.fencing_token,
            now_ms=clock.now_ms(),
        )
    assert expired.value.code == "STALE_FENCING_TOKEN"
    assert (await store.get_activity(claim.activity.activity_id)).status is ActivityStatus.CLAIMED


@pytest.mark.asyncio
async def test_expired_running_lease_rejects_writes_before_recovery_scan(tmp_path):
    store, clock = await _runtime(tmp_path)
    run = (await _admit(store, clock, key="expired-running-write")).run
    _claim, activity = await _claim_and_start(store, clock, lease_ms=1_000)
    before = (await store.get_run(run.envelope.run_id)).next_seq
    clock.advance(1_001)

    with pytest.raises(RuntimeFault) as event_expired:
        await store.append_events(
            run.envelope.run_id,
            [EventDraft(
                EventType.MODEL_PLAN_UPDATED,
                {"late": True},
                occurred_at=clock.now_ms(),
            )],
            activity_id=activity.activity_id,
            fencing_token=activity.fencing_token,
            now_ms=clock.now_ms(),
        )
    assert event_expired.value.code == "STALE_FENCING_TOKEN"
    with pytest.raises(RuntimeFault) as final_expired:
        await store.finalize_success(
            run_id=run.envelope.run_id,
            activity_id=activity.activity_id,
            fencing_token=activity.fencing_token,
            assistant_text="expired result",
            citations=[],
            now_ms=clock.now_ms(),
        )
    assert final_expired.value.code == "STALE_FENCING_TOKEN"
    current = await store.get_run(run.envelope.run_id)
    assert current.next_seq == before
    assert current.terminal_status is None


@pytest.mark.asyncio
async def test_claimed_activity_cannot_write_before_running_transition(tmp_path):
    store, clock = await _runtime(tmp_path)
    run = (await _admit(store, clock, key="claimed-write")).run
    claim = await store.claim_next(
        worker_id="claimed-worker", lease_ms=1_000, now_ms=clock.now_ms(),
    )
    assert claim is not None
    before = (await store.get_run(run.envelope.run_id)).next_seq

    with pytest.raises(RuntimeFault) as not_running:
        await store.append_events(
            run.envelope.run_id,
            [EventDraft(EventType.MODEL_PLAN_UPDATED, {"too_early": True})],
            activity_id=claim.activity.activity_id,
            fencing_token=claim.activity.fencing_token,
        )
    assert not_running.value.code == "ACTIVITY_LEASE_REQUIRED"
    assert (await store.get_run(run.envelope.run_id)).next_seq == before

    await store.mark_activity_running(
        claim.activity.activity_id,
        worker_id="claimed-worker",
        fencing_token=claim.activity.fencing_token,
        now_ms=clock.now_ms(),
    )
    committed = await store.append_events(
        run.envelope.run_id,
        [EventDraft(EventType.MODEL_PLAN_UPDATED, {"after_start": True})],
        activity_id=claim.activity.activity_id,
        fencing_token=claim.activity.fencing_token,
    )
    assert committed[0].payload == {"after_start": True}


@pytest.mark.asyncio
async def test_activity_fence_cannot_authorize_writes_to_a_different_run(tmp_path):
    store, clock = await _runtime(tmp_path)
    first = (await _admit(store, clock, key="run-binding-first")).run
    _first_claim, first_activity = await _claim_and_start(store, clock)
    second = (await _admit(store, clock, key="run-binding-second")).run
    _second_claim, second_activity = await _claim_and_start(store, clock)

    with pytest.raises(RuntimeFault) as finalize_mismatch:
        await store.finalize_success(
            run_id=first.envelope.run_id,
            activity_id=second_activity.activity_id,
            fencing_token=second_activity.fencing_token,
            assistant_text="must not cross Run ownership",
            citations=[],
            now_ms=clock.now_ms(),
        )
    assert finalize_mismatch.value.code == "ACTIVITY_RUN_MISMATCH"

    with pytest.raises(RuntimeFault) as event_mismatch:
        await store.append_events(
            first.envelope.run_id,
            [EventDraft(EventType.MODEL_PLAN_UPDATED, {"cross_run": True})],
            activity_id=second_activity.activity_id,
            fencing_token=second_activity.fencing_token,
        )
    assert event_mismatch.value.code == "ACTIVITY_RUN_MISMATCH"
    assert (await store.get_run(first.envelope.run_id)).terminal_status is None
    assert (await store.get_run(second.envelope.run_id)).terminal_status is None
    assert (await store.get_activity(first_activity.activity_id)).status is ActivityStatus.RUNNING
    assert (await store.get_activity(second_activity.activity_id)).status is ActivityStatus.RUNNING


@pytest.mark.asyncio
async def test_failure_finalize_cannot_commit_success_without_atomic_final_message(tmp_path):
    store, clock = await _runtime(tmp_path)
    run = (await _admit(store, clock, key="failure-cannot-succeed")).run
    _claim, activity = await _claim_and_start(store, clock)
    before = await store.get_run(run.envelope.run_id)

    with pytest.raises(ValueError, match="non-success"):
        await store.finalize_failure(
            run_id=run.envelope.run_id,
            activity_id=activity.activity_id,
            fencing_token=activity.fencing_token,
            code="INVALID_CALLER",
            message="must not bypass atomic success finalization",
            terminal_status=RunStatus.SUCCEEDED,
            now_ms=clock.now_ms(),
        )
    with pytest.raises(RuntimeFault) as invalid_transition:
        await store.finalize_failure(
            run_id=run.envelope.run_id,
            activity_id=activity.activity_id,
            fencing_token=activity.fencing_token,
            code="POLICY_REJECT",
            message="REJECTED is not an execution terminal",
            terminal_status=RunStatus.REJECTED,
            now_ms=clock.now_ms(),
        )
    assert invalid_transition.value.code == "INVALID_RUN_STATE_TRANSITION"

    after = await store.get_run(run.envelope.run_id)
    assert after.terminal_status is None
    assert after.next_seq == before.next_seq
    events = await store.list_events(run.envelope.run_id, visibility=None)
    assert not any(
        event.event_type in {
            EventType.ASSISTANT_MESSAGE_COMMITTED,
            EventType.CITATION_SET_COMMITTED,
            EventType.RUN_TERMINATED,
        }
        for event in events
    )


@pytest.mark.asyncio
async def test_event_producer_cannot_forge_store_owned_or_post_terminal_public_events(
    tmp_path,
):
    store, clock = await _runtime(tmp_path)
    run = (await _admit(store, clock, key="event-authority")).run
    before = (await store.get_run(run.envelope.run_id)).next_seq

    with pytest.raises(RuntimeFault) as forged:
        await store.append_events(run.envelope.run_id, [
            EventDraft(
                EventType.RUN_TERMINATED,
                {"forged": True},
                terminal_status=RunStatus.SUCCEEDED,
            ),
        ])
    assert forged.value.code == "EVENT_AUTHORITY_VIOLATION"
    assert (await store.get_run(run.envelope.run_id)).next_seq == before

    await store.request_cancel(
        run_id=run.envelope.run_id,
        command_id="event-authority-cancel",
        reason="make terminal",
        now_ms=clock.now_ms(),
    )
    with pytest.raises(RuntimeFault) as post_terminal:
        await store.append_events(run.envelope.run_id, [
            EventDraft(EventType.MODEL_PLAN_UPDATED, {"too_late": True}),
        ])
    assert post_terminal.value.code == "RUN_ALREADY_TERMINAL"

    # Internal diagnostics may still be recorded without becoming an SSE fact.
    diagnostic = await store.append_events(run.envelope.run_id, [
        EventDraft(
            EventType.MODEL_MESSAGE_COMMITTED,
            {"late_diagnostic": True},
            visibility=Visibility.INTERNAL,
        ),
    ])
    assert diagnostic[0].visibility is Visibility.INTERNAL
    public = await store.list_events(run.envelope.run_id)
    assert diagnostic[0].event_id not in {event.event_id for event in public}
