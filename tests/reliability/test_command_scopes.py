from __future__ import annotations

import uuid

import pytest

from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.runtime.application.admission import AdmissionService, CreateRunInput
from agent.runtime.domain.models import ReleaseManifest


@pytest.mark.asyncio
async def test_cancel_and_signal_idempotency_ids_are_scoped_to_run(tmp_path):
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    await store.register_release(
        ReleaseManifest(engine="native_loop", components={"test": "command-scope-v1"}),
        activate=True,
    )
    service = AdmissionService(store)

    async def create(key: str):
        return (await service.create(
            CreateRunInput(
                client_request_id=str(uuid.uuid4()),
                conversation_id=None,
                principal_id="demo-user",
                agent_id="demo-agent",
                engine="native_loop",
                text=key,
                attachment_refs=(),
                deadline_at=None,
            ),
            idempotency_key=key,
        )).run

    cancel_a, cancel_b = await create("cancel-scope-a"), await create("cancel-scope-b")
    first, _ = await store.request_cancel(
        run_id=cancel_a.envelope.run_id,
        command_id="same-command",
        reason="stop",
        now_ms=cancel_a.envelope.created_at,
    )
    second, _ = await store.request_cancel(
        run_id=cancel_b.envelope.run_id,
        command_id="same-command",
        reason="stop",
        now_ms=cancel_b.envelope.created_at,
    )
    assert first.status.value == second.status.value == "CANCELLED"

    wait_a, wait_b = await create("signal-scope-a"), await create("signal-scope-b")
    waits = []
    expected_run_ids = {wait_a.envelope.run_id, wait_b.envelope.run_id}
    for index in range(2):
        claim = await store.claim_next(
            worker_id=f"signal-worker-{index}",
            lease_ms=30_000,
            now_ms=max(wait_a.envelope.created_at, wait_b.envelope.created_at),
        )
        assert claim is not None
        run = claim.run
        assert run.envelope.run_id in expected_run_ids
        activity = await store.mark_activity_running(
            claim.activity.activity_id,
            worker_id=f"signal-worker-{index}",
            fencing_token=claim.activity.fencing_token,
            now_ms=run.envelope.created_at,
        )
        await store.wait_for_input(
            run_id=run.envelope.run_id,
            activity_id=activity.activity_id,
            fencing_token=activity.fencing_token,
            pending_input={"type": "APPROVAL"},
            now_ms=run.envelope.created_at,
        )
        waits.append((run, activity))

    for run, activity in waits:
        result = await store.submit_signal(
            run_id=run.envelope.run_id,
            signal_id="same-signal",
            wait_activity_id=activity.activity_id,
            signal_type="approval",
            payload={"approved": True},
            payload_digest="same-digest",
            now_ms=run.envelope.created_at,
        )
        assert result.status == "CONSUMED"
