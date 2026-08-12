from __future__ import annotations

from tests.reliability.support.runtime_releases import activate_test_release

import asyncio
import sqlite3
import uuid
from dataclasses import dataclass

import pytest

from agent.runtime.adapters.filesystem_artifact import FilesystemArtifactStore
from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.runtime.application.admission import AdmissionService, CreateRunInput
from agent.runtime.application.tool_broker import ToolBroker
from agent.runtime.domain.errors import RuntimeFault
from agent.runtime.domain.models import (
    EventType,
    ReleaseManifest,
    RunStatus,
    ToolEffectClass,
    ToolManifest,
    sha256_json,
)
from agent.runtime.ports.store import EventDraft


@dataclass
class FakeClock:
    value: int = 2_000_000_000_000

    def now_ms(self) -> int:
        return self.value

    def monotonic(self) -> float:
        return self.value / 1000

    def advance(self, milliseconds: int) -> None:
        self.value += milliseconds


async def _environment(tmp_path):
    path = tmp_path / "runtime.db"
    store = SqliteRuntimeStore(RuntimeDatabase(path))
    await store.initialize()
    await activate_test_release(store,
        ReleaseManifest(engine="native_loop", components={"fault-test": "v1"}),
    )
    clock = FakeClock()
    run = (await AdmissionService(
        store, clock=clock, default_deadline_ms=120_000,
    ).create(
        CreateRunInput(
            client_request_id=str(uuid.uuid4()),
            conversation_id=None,
            principal_id="demo-user",
            agent_id="demo-agent",
            engine="native_loop",
            text="fault injection",
            attachment_refs=(),
            deadline_at=None,
        ),
        idempotency_key=str(uuid.uuid4()),
    )).run
    return path, store, clock, run


async def _start(store, clock, *, worker="worker-a", lease_ms=1_000):
    claim = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id=worker, lease_ms=lease_ms, now_ms=clock.now_ms()
    )
    assert claim is not None
    activity = await store.mark_activity_running(
        claim.activity.activity_id,
        worker_id=worker,
        fencing_token=claim.activity.fencing_token,
        now_ms=clock.now_ms(),
    )
    return claim, activity


@pytest.mark.asyncio
async def test_fi_01_admission_commit_survives_kill_before_claim(tmp_path):
    path, _store, clock, run = await _environment(tmp_path)
    restarted = SqliteRuntimeStore(RuntimeDatabase(path))
    await restarted.initialize()

    assert (await restarted.get_run(run.envelope.run_id)).status is RunStatus.DISPATCH_PENDING
    first = await restarted.claim_next(
        release_map=await restarted.active_releases(),
        worker_id="after-restart", lease_ms=1_000, now_ms=clock.now_ms()
    )
    second = await restarted.claim_next(
        release_map=await restarted.active_releases(),
        worker_id="duplicate", lease_ms=1_000, now_ms=clock.now_ms()
    )
    assert first is not None
    assert second is None


@pytest.mark.asyncio
async def test_fi_02_kill_before_llm_requeues_without_model_output(tmp_path):
    path, store, clock, run = await _environment(tmp_path)
    await _start(store, clock)
    clock.advance(1_001)

    restarted = SqliteRuntimeStore(RuntimeDatabase(path))
    await restarted.initialize()
    assert await restarted.recover_expired(now_ms=clock.now_ms()) == 1
    events = await restarted.list_events(run.envelope.run_id, visibility=None)
    assert not any(event.event_type is EventType.OUTPUT_DELTA_COMMITTED for event in events)
    assert await restarted.claim_next(
        release_map=await restarted.active_releases(),
        worker_id="replacement", lease_ms=1_000, now_ms=clock.now_ms()
    ) is not None


@pytest.mark.asyncio
async def test_fi_03_llm_return_before_event_commit_is_not_visible(tmp_path):
    path, store, clock, run = await _environment(tmp_path)
    _claim, activity = await _start(store, clock)
    model_return = "generated but process died before commit"

    with pytest.raises(RuntimeError, match="simulated kill"):
        async with store.db.transaction() as conn:
            await store._append_in_tx(conn, run.envelope.run_id, [  # noqa: SLF001
                EventDraft(
                    EventType.OUTPUT_DELTA_COMMITTED,
                    {"delta": model_return},
                    activity_id=activity.activity_id,
                    occurred_at=clock.now_ms(),
                )
            ])
            raise RuntimeError("simulated kill before event commit")

    restarted = SqliteRuntimeStore(RuntimeDatabase(path))
    await restarted.initialize()
    assert not any(
        event.event_type is EventType.OUTPUT_DELTA_COMMITTED
        for event in await restarted.list_events(run.envelope.run_id, visibility=None)
    )
    clock.advance(1_001)
    assert await restarted.recover_expired(now_ms=clock.now_ms()) == 1


@pytest.mark.asyncio
async def test_fi_04_prepared_tool_recovers_without_phantom_dispatch(tmp_path):
    path, store, clock, run = await _environment(tmp_path)
    _claim, parent = await _start(store, clock)
    prepared = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="fi04:call:0",
        tool_name="prepared_read",
        release_digest="v1",
        effect_class=ToolEffectClass.READ_ONLY,
        request_digest=sha256_json({"query": "x"}),
        request={"query": "x"},
        now_ms=clock.now_ms(),
    )
    assert prepared["effect_status"] == "PREPARED"
    clock.advance(1_001)

    restarted = SqliteRuntimeStore(RuntimeDatabase(path))
    await restarted.initialize()
    assert await restarted.recover_expired(now_ms=clock.now_ms()) == 1
    _replacement, resumed_parent = await _start(
        restarted, clock, worker="replacement"
    )
    calls = 0

    async def prepared_read(_arguments, _context):
        nonlocal calls
        calls += 1
        return {"value": "once"}

    broker = ToolBroker(
        restarted,
        FilesystemArtifactStore(tmp_path / "artifacts"),
        clock=clock,
    )
    broker.register(
        ToolManifest(
            name="prepared_read",
            release_digest="v1",
            effect_class=ToolEffectClass.READ_ONLY,
            timeout_seconds=1,
            max_attempts=2,
        ),
        prepared_read,
    )
    result = await broker.execute(
        run_id=run.envelope.run_id,
        parent_activity_id=resumed_parent.activity_id,
        fencing_token=resumed_parent.fencing_token,
        logical_key="fi04:call:0",
        tool_name="prepared_read",
        arguments={"query": "x"},
        deadline_at_ms=run.envelope.deadline_at,
    )
    assert result.preview == {"value": "once"}
    assert calls == 1


@pytest.mark.asyncio
async def test_fi_09_signal_commit_survives_kill_before_resume_claim(tmp_path):
    path, store, clock, run = await _environment(tmp_path)
    _claim, activity = await _start(store, clock)
    await store.wait_for_input(
        run_id=run.envelope.run_id,
        activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        pending_input={"type": "APPROVAL"},
        now_ms=clock.now_ms(),
    )
    await store.submit_signal(
        run_id=run.envelope.run_id,
        signal_id="fi09-signal",
        wait_activity_id=activity.activity_id,
        signal_type="approval",
        payload={"approved": True},
        payload_digest="fi09-digest",
        now_ms=clock.now_ms(),
    )

    restarted = SqliteRuntimeStore(RuntimeDatabase(path))
    await restarted.initialize()
    assert (await restarted.get_run(run.envelope.run_id)).status is RunStatus.DISPATCH_PENDING
    assert await restarted.claim_next(
        release_map=await restarted.active_releases(),
        worker_id="signal-resume", lease_ms=1_000, now_ms=clock.now_ms()
    ) is not None
    assert await restarted.claim_next(
        release_map=await restarted.active_releases(),
        worker_id="duplicate-resume", lease_ms=1_000, now_ms=clock.now_ms()
    ) is None


@pytest.mark.asyncio
async def test_fi_10_terminal_transaction_failure_restarts_and_finalizes_once(tmp_path):
    path, store, clock, run = await _environment(tmp_path)
    _claim, activity = await _start(store, clock)
    async with store.db.transaction() as conn:
        await conn.execute(
            """CREATE TRIGGER fi10_fail_terminal BEFORE INSERT ON run_events
               WHEN NEW.event_type='RUN_TERMINATED'
               BEGIN SELECT RAISE(ABORT,'fi10 terminal commit lost'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="fi10 terminal commit lost"):
        await store.finalize_success(
            run_id=run.envelope.run_id,
            activity_id=activity.activity_id,
            fencing_token=activity.fencing_token,
            assistant_text="rolled back final answer",
            citations=[],
            now_ms=clock.now_ms(),
        )

    # Reconstruct the Store as a restarted Worker would.  The failed transaction
    # left no semantic final events and the original fenced Activity is resumable.
    restarted = SqliteRuntimeStore(RuntimeDatabase(path))
    await restarted.initialize()
    before = await restarted.list_events(run.envelope.run_id, visibility=None)
    assert not any(
        event.event_type in {
            EventType.ASSISTANT_MESSAGE_COMMITTED,
            EventType.CITATION_SET_COMMITTED,
            EventType.RUN_TERMINATED,
        }
        for event in before
    )
    assert (await restarted.get_run(run.envelope.run_id)).terminal_status is None

    # Removing only the injected fault models the external failure disappearing;
    # the second attempt uses the same durable Run/Activity identity.
    async with restarted.db.transaction() as conn:
        await conn.execute("DROP TRIGGER fi10_fail_terminal")
    finalized = await restarted.finalize_success(
        run_id=run.envelope.run_id,
        activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        assistant_text="durable final answer",
        citations=[],
        now_ms=clock.now_ms(),
    )
    assert finalized.terminal_status is RunStatus.SUCCEEDED
    with pytest.raises(RuntimeFault) as duplicate:
        await restarted.finalize_success(
            run_id=run.envelope.run_id,
            activity_id=activity.activity_id,
            fencing_token=activity.fencing_token,
            assistant_text="must not finalize twice",
            citations=[],
            now_ms=clock.now_ms(),
        )
    assert duplicate.value.code in {"RUN_ALREADY_TERMINAL", "STALE_FENCING_TOKEN"}

    events = await restarted.list_events(run.envelope.run_id, visibility=None)
    assert sum(
        event.event_type is EventType.ASSISTANT_MESSAGE_COMMITTED for event in events
    ) == 1
    assert sum(
        event.event_type is EventType.CITATION_SET_COMMITTED for event in events
    ) == 1
    assert sum(event.event_type is EventType.RUN_TERMINATED for event in events) == 1


@pytest.mark.asyncio
async def test_fi_12_concurrent_cancel_and_finalize_only_reach_frozen_outcomes(tmp_path):
    path, store, clock, first_run = await _environment(tmp_path)
    race_store = SqliteRuntimeStore(RuntimeDatabase(path))
    await race_store.initialize()

    for ordinal in range(5):
        if ordinal == 0:
            run = first_run
        else:
            run = (await AdmissionService(
                store, clock=clock, default_deadline_ms=120_000,
            ).create(
                CreateRunInput(
                    client_request_id=str(uuid.uuid4()),
                    conversation_id=None,
                    principal_id="demo-user",
                    agent_id="demo-agent",
                    engine="native_loop",
                    text=f"cancel/finalize race {ordinal}",
                    attachment_refs=(),
                    deadline_at=None,
                ),
                idempotency_key=f"fi12-race-{ordinal}",
            )).run
        _claim, activity = await _start(
            store, clock, worker=f"fi12-worker-{ordinal}",
        )
        barrier = asyncio.Barrier(2)

        async def cancel():
            await barrier.wait()
            try:
                return await race_store.request_cancel(
                    run_id=run.envelope.run_id,
                    command_id=f"fi12-cancel-{ordinal}",
                    reason="concurrent cancellation",
                    now_ms=clock.now_ms(),
                )
            except RuntimeFault as exc:
                return exc

        async def finalize():
            await barrier.wait()
            try:
                return await store.finalize_success(
                    run_id=run.envelope.run_id,
                    activity_id=activity.activity_id,
                    fencing_token=activity.fencing_token,
                    assistant_text=f"concurrent answer {ordinal}",
                    citations=[],
                    now_ms=clock.now_ms(),
                )
            except RuntimeFault as exc:
                return exc

        cancel_result, final_result = await asyncio.gather(cancel(), finalize())
        terminal = await store.get_run(run.envelope.run_id)
        assert terminal.terminal_status in {RunStatus.SUCCEEDED, RunStatus.CANCELLED}
        if terminal.terminal_status is RunStatus.SUCCEEDED:
            assert isinstance(cancel_result, RuntimeFault)
            assert cancel_result.code == "RUN_ALREADY_TERMINAL"
            assert not isinstance(final_result, RuntimeFault)
            assert final_result.terminal_status is RunStatus.SUCCEEDED
        else:
            assert not isinstance(cancel_result, RuntimeFault)
            cancel_snapshot, reused = cancel_result
            assert reused is False
            assert cancel_snapshot.status is RunStatus.CANCEL_REQUESTED
            assert not isinstance(final_result, RuntimeFault)
            assert final_result.terminal_status is RunStatus.CANCELLED

        events = await store.list_events(run.envelope.run_id, visibility=None)
        assert sum(event.event_type is EventType.RUN_TERMINATED for event in events) == 1
        public_final = [
            event for event in events
            if event.event_type is EventType.ASSISTANT_MESSAGE_COMMITTED
        ]
        assert len(public_final) == (
            1 if terminal.terminal_status is RunStatus.SUCCEEDED else 0
        )
