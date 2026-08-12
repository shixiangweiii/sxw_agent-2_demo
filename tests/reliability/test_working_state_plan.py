from __future__ import annotations

from tests.reliability.support.runtime_releases import activate_test_release

import sqlite3
import uuid

import pytest

from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.runtime.application.admission import AdmissionService, CreateRunInput
from agent.runtime.domain.models import EventType, ReleaseManifest, WorkingState


@pytest.mark.asyncio
async def test_model_plan_and_checkpoint_are_atomic_and_unchanged_plan_is_not_republished(tmp_path):
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    await activate_test_release(store,
        ReleaseManifest(engine="agent_loop", components={"test": "plan-v1"}),
    )
    run = (await AdmissionService(store).create(
        CreateRunInput(
            client_request_id=str(uuid.uuid4()),
            conversation_id=None,
            principal_id="demo-user",
            agent_id="demo-agent",
            engine="agent_loop",
            text="make a plan",
            attachment_refs=(),
            deadline_at=None,
        ),
        idempotency_key="working-plan",
    )).run
    claim = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="plan-worker",
        lease_ms=30_000,
        now_ms=run.envelope.created_at,
    )
    assert claim is not None
    activity = await store.mark_activity_running(
        claim.activity.activity_id,
        worker_id="plan-worker",
        fencing_token=claim.activity.fencing_token,
        now_ms=run.envelope.created_at,
    )
    plan = [
        {"step": 1, "title": "inspect", "status": "running"},
        {"step": 2, "title": "answer", "status": "planned"},
    ]
    first = await store.save_checkpoint(
        run_id=run.envelope.run_id,
        activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        expected_revision=0,
        working_state=WorkingState(
            goal="make a plan",
            model_plan=plan,
        ),
        now_ms=run.envelope.created_at,
    )
    second = await store.save_checkpoint(
        run_id=run.envelope.run_id,
        activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        expected_revision=first.revision,
        working_state=first.working_state,
        engine_state={"phase": "same-plan"},
        now_ms=run.envelope.created_at,
    )
    events = await store.list_events(run.envelope.run_id, visibility=None)
    plan_events = [event for event in events if event.event_type is EventType.MODEL_PLAN_UPDATED]
    assert [event.payload["title"] for event in plan_events] == ["inspect", "answer"]
    assert all(event.payload["total"] == 2 for event in plan_events)

    async with store.db.transaction() as conn:
        await conn.execute(
            """CREATE TRIGGER inject_plan_event_failure BEFORE INSERT ON run_events
               WHEN NEW.event_type='MODEL_PLAN_UPDATED'
               BEGIN SELECT RAISE(ABORT,'injected plan event failure'); END"""
        )
    changed = first.working_state.model_copy(update={
        "model_plan": [
            {"step": 1, "title": "inspect", "status": "done"},
            {"step": 2, "title": "answer", "status": "running"},
        ]
    })
    with pytest.raises(sqlite3.IntegrityError, match="injected plan"):
        await store.save_checkpoint(
            run_id=run.envelope.run_id,
            activity_id=activity.activity_id,
            fencing_token=activity.fencing_token,
            expected_revision=second.revision,
            working_state=changed,
            now_ms=run.envelope.created_at,
        )
    latest = await store.latest_checkpoint(run.envelope.run_id)
    assert latest is not None
    assert latest.revision == second.revision
    assert latest.working_state.model_plan == plan
