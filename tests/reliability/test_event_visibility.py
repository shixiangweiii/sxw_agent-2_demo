from __future__ import annotations

import uuid

import pytest

from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.runtime.application.admission import AdmissionService, CreateRunInput
from agent.runtime.domain.models import EventType, ReleaseManifest, Visibility
from agent.runtime.ports.store import EventDraft


async def _run(store):
    await store.register_release(
        ReleaseManifest(engine="native_loop", components={"test": "events-v1"}),
        activate=True,
    )
    return (await AdmissionService(store).create(
        CreateRunInput(
            client_request_id=str(uuid.uuid4()),
            conversation_id=None,
            principal_id="demo-user",
            agent_id="demo-agent",
            engine="native_loop",
            text="events",
            attachment_refs=(),
            deadline_at=None,
        ),
        idempotency_key="event-visibility",
    )).run


@pytest.mark.asyncio
async def test_rel_08_reader_never_observes_uncommitted_event(tmp_path):
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    run = await _run(store)

    async with store.db.transaction() as writer:
        pending = await store._append_in_tx(writer, run.envelope.run_id, [  # noqa: SLF001
            EventDraft(EventType.MODEL_PLAN_UPDATED, {"phase": "pending"})
        ])
        assert not any(
            event.event_id == pending[0].event_id
            for event in await store.list_events(run.envelope.run_id, visibility=None)
        )

    assert any(
        event.event_id == pending[0].event_id
        for event in await store.list_events(run.envelope.run_id, visibility=None)
    )


@pytest.mark.asyncio
async def test_visible_seq_is_an_opaque_cursor_and_may_skip_internal_events(tmp_path):
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    run = await _run(store)
    committed = await store.append_events(run.envelope.run_id, [
        EventDraft(EventType.MODEL_PLAN_UPDATED, {"value": 1}),
        EventDraft(
            EventType.MODEL_MESSAGE_COMMITTED,
            {"internal": True},
            visibility=Visibility.INTERNAL,
        ),
        EventDraft(EventType.MODEL_PLAN_UPDATED, {"value": 2}),
    ])

    public = await store.list_events(
        run.envelope.run_id,
        after_seq=committed[0].seq,
    )
    assert [event.seq for event in public] == [committed[2].seq]
    assert committed[2].seq - committed[0].seq == 2
