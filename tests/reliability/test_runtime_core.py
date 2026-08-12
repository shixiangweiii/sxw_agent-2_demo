from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from dataclasses import dataclass

import pytest

from tests.reliability.support.runtime_releases import activate_test_releases

from agent.runtime.adapters.filesystem_artifact import FilesystemArtifactStore
from agent.runtime.adapters.scripted_engine import ScriptedEngineAdapter
from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.runtime.application.admission import AdmissionService, CreateRunInput
from agent.runtime.application.coordinator import EngineRegistry, RunCoordinator
from agent.runtime.application.tool_broker import ToolBroker
from agent.runtime.domain.errors import AttemptOwnershipLost, RuntimeFault
from agent.runtime.domain.models import (
    EngineOutcome,
    EngineOutcomeKind,
    EngineName,
    EventType,
    ReleaseManifest,
    RunStatus,
    ToolEffectClass,
    ToolManifest,
    ToolResultEnvelope,
    ToolResultStatus,
    WorkingState,
    sha256_json,
)
from agent.runtime.ports.store import EventDraft


@dataclass
class FakeClock:
    value: int = 1_800_000_000_000

    def now_ms(self) -> int:
        return self.value

    def monotonic(self) -> float:
        return self.value / 1000

    def advance(self, milliseconds: int) -> None:
        self.value += milliseconds


class FakeRandom:
    def uniform(self, low: float, high: float) -> float:
        return (low + high) / 2


@pytest.fixture
async def runtime(tmp_path):
    db = RuntimeDatabase(tmp_path / "runtime.db", busy_timeout_ms=5000)
    store = SqliteRuntimeStore(db)
    await store.initialize()
    clock = FakeClock()
    releases = await activate_test_releases(store, marker="v1")
    return store, clock, releases, tmp_path


async def admit(
    store,
    clock,
    *,
    key="idem-1",
    text="hello",
    conversation_id=None,
    engine: EngineName = "native_loop",
    client_request_id=None,
):
    service = AdmissionService(store, clock=clock, default_deadline_ms=60_000)
    return await service.create(
        CreateRunInput(
            client_request_id=client_request_id or str(uuid.uuid4()),
            conversation_id=conversation_id,
            principal_id="demo-user",
            agent_id="demo-agent",
            engine=engine,
            text=text,
            attachment_refs=(),
            deadline_at=None,
        ),
        idempotency_key=key,
    )


async def claim_and_start(store, clock):
    claim = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="worker-a", lease_ms=1000, now_ms=clock.now_ms(),
    )
    assert claim is not None
    activity = await store.mark_activity_running(
        claim.activity.activity_id,
        worker_id="worker-a",
        fencing_token=claim.activity.fencing_token,
        now_ms=clock.now_ms(),
    )
    return claim, activity


@pytest.mark.asyncio
async def test_rel_01_idempotency_replayed_ten_times_is_one_run(runtime):
    store, clock, _, _ = runtime
    client_id = str(uuid.uuid4())
    results = await asyncio.gather(*[
        admit(store, clock, key="same-key", client_request_id=client_id)
        for _ in range(10)
    ])
    assert len({item.run.envelope.run_id for item in results}) == 1
    assert sum(not item.reused for item in results) == 1


@pytest.mark.asyncio
async def test_rel_02_same_key_different_digest_conflicts(runtime):
    store, clock, _, _ = runtime
    client_id = str(uuid.uuid4())
    await admit(store, clock, key="same-key", text="one", client_request_id=client_id)
    with pytest.raises(RuntimeFault, match="different request") as raised:
        await admit(store, clock, key="same-key", text="two", client_request_id=client_id)
    assert raised.value.code == "IDEMPOTENCY_KEY_REUSE"


@pytest.mark.asyncio
async def test_rel_03_conversation_allows_only_one_active_run(runtime):
    store, clock, _, _ = runtime
    first = await admit(store, clock, key="first")
    with pytest.raises(RuntimeFault) as raised:
        await admit(
            store, clock, key="second",
            conversation_id=first.run.envelope.conversation_id,
        )
    assert raised.value.code == "CONVERSATION_BUSY"


@pytest.mark.asyncio
async def test_rel_03_concurrent_new_runs_share_one_conversation_winner(runtime):
    store, clock, _, tmp_path = runtime
    seed = (await admit(store, clock, key="conversation-seed")).run
    await store.request_cancel(
        run_id=seed.envelope.run_id,
        command_id="close-seed",
        reason="make the conversation eligible for its next turn",
        now_ms=clock.now_ms(),
    )

    # Independent Store instances model two API processes reaching admission at
    # the same time.  Both commands are genuinely new (different request/key),
    # so idempotency cannot collapse the race before the conversation guard.
    contenders = [
        SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db")),
        SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db")),
    ]
    for contender in contenders:
        await contender.initialize()
    barrier = asyncio.Barrier(2)

    async def create_new(contender, *, key: str, text: str):
        await barrier.wait()
        try:
            return await admit(
                contender,
                clock,
                key=key,
                text=text,
                conversation_id=seed.envelope.conversation_id,
            )
        except RuntimeFault as exc:
            return exc

    results = await asyncio.gather(
        create_new(contenders[0], key="concurrent-new-a", text="new request A"),
        create_new(contenders[1], key="concurrent-new-b", text="new request B"),
    )
    admitted = [item for item in results if not isinstance(item, RuntimeFault)]
    rejected = [item for item in results if isinstance(item, RuntimeFault)]
    assert len(admitted) == 1
    assert len(rejected) == 1
    assert rejected[0].code == "CONVERSATION_BUSY"
    assert admitted[0].reused is False

    async with store.db.read() as conn:
        rows = await (await conn.execute(
            "SELECT turn_seq,state FROM runs WHERE conversation_id=? ORDER BY turn_seq",
            (seed.envelope.conversation_id,),
        )).fetchall()
        conversation = await (await conn.execute(
            "SELECT next_turn_seq FROM conversations WHERE conversation_id=?",
            (seed.envelope.conversation_id,),
        )).fetchone()
    assert [row["turn_seq"] for row in rows] == [1, 2]
    assert sum(row["state"] not in {
        "SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "REJECTED",
    } for row in rows) == 1
    # The losing admission rolled back its conversation revision/turn allocation.
    assert conversation["next_turn_seq"] == 3


@pytest.mark.asyncio
async def test_rel_04_05_terminal_is_unique_and_cancel_cannot_override(runtime):
    store, clock, releases, _ = runtime
    admitted = await admit(store, clock)
    adapter = ScriptedEngineAdapter([{"type": "text", "delta": "answer"}],
                                     release_fingerprint=releases["native_loop"])
    coordinator = RunCoordinator(
        store, EngineRegistry({"native_loop": adapter}),
        clock=clock, random_source=FakeRandom(), event_flush_bytes=1,
    )
    claim = await store.claim_next(
        worker_id="worker-a", lease_ms=1000, now_ms=clock.now_ms(),
        release_map=await store.active_releases(),
    )
    assert claim is not None
    assert await coordinator.execute_claim(claim, worker_id="worker-a") is RunStatus.SUCCEEDED
    with pytest.raises(RuntimeFault) as raised:
        await store.request_cancel(
            run_id=admitted.run.envelope.run_id, command_id="cancel-new",
            reason="too late", now_ms=clock.now_ms(),
        )
    assert raised.value.code == "RUN_ALREADY_TERMINAL"
    events = await store.list_events(admitted.run.envelope.run_id, visibility=None)
    assert sum(item.event_type is EventType.RUN_TERMINATED for item in events) == 1


@pytest.mark.asyncio
async def test_rel_04_concurrent_finalize_has_one_terminal_cas_winner(runtime):
    store, clock, _, tmp_path = runtime
    run = (await admit(store, clock, key="two-finalizers")).run
    _, activity = await claim_and_start(store, clock)
    finalizers = [
        SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db")),
        SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db")),
    ]
    for finalizer in finalizers:
        await finalizer.initialize()
    barrier = asyncio.Barrier(2)

    async def finalize(finalizer, text: str):
        await barrier.wait()
        try:
            return await finalizer.finalize_success(
                run_id=run.envelope.run_id,
                activity_id=activity.activity_id,
                fencing_token=activity.fencing_token,
                assistant_text=text,
                citations=[],
                now_ms=clock.now_ms(),
            )
        except RuntimeFault as exc:
            return exc

    results = await asyncio.gather(
        finalize(finalizers[0], "finalizer A"),
        finalize(finalizers[1], "finalizer B"),
    )
    winners = [item for item in results if not isinstance(item, RuntimeFault)]
    losers = [item for item in results if isinstance(item, RuntimeFault)]
    assert len(winners) == 1
    assert winners[0].terminal_status is RunStatus.SUCCEEDED
    assert len(losers) == 1
    assert losers[0].code in {"RUN_ALREADY_TERMINAL", "STALE_FENCING_TOKEN"}

    events = await store.list_events(run.envelope.run_id, visibility=None)
    terminal = [item for item in events if item.event_type is EventType.RUN_TERMINATED]
    messages = [
        item for item in events
        if item.event_type is EventType.ASSISTANT_MESSAGE_COMMITTED
    ]
    citations = [item for item in events if item.event_type is EventType.CITATION_SET_COMMITTED]
    assert len(terminal) == len(messages) == len(citations) == 1
    assert messages[0].payload["text"] in {"finalizer A", "finalizer B"}


@pytest.mark.asyncio
async def test_rel_06_rolled_back_event_batch_leaves_no_seq_hole(runtime):
    store, clock, _, _ = runtime
    run = (await admit(store, clock)).run
    before = run.next_seq
    with pytest.raises(sqlite3.IntegrityError):
        await store.append_events(run.envelope.run_id, [
            EventDraft(EventType.MODEL_PLAN_UPDATED, {"n": 1}, event_id="evt_duplicate"),
            EventDraft(EventType.MODEL_PLAN_UPDATED, {"n": 2}, event_id="evt_duplicate"),
        ])
    committed = await store.append_events(
        run.envelope.run_id,
        [EventDraft(EventType.MODEL_PLAN_UPDATED, {"n": 3})],
    )
    assert committed[0].seq == before


@pytest.mark.asyncio
async def test_rel_07_final_message_and_terminal_rollback_together(runtime):
    store, clock, _, _ = runtime
    run = (await admit(store, clock)).run
    _, activity = await claim_and_start(store, clock)
    async with store.db.transaction() as conn:
        await conn.execute(
            """CREATE TRIGGER fail_terminal BEFORE INSERT ON run_events
               WHEN NEW.event_type='RUN_TERMINATED'
               BEGIN SELECT RAISE(ABORT,'injected terminal failure'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError):
        await store.finalize_success(
            run_id=run.envelope.run_id, activity_id=activity.activity_id,
            fencing_token=activity.fencing_token, assistant_text="must rollback",
            citations=[], now_ms=clock.now_ms(),
        )
    events = await store.list_events(run.envelope.run_id, visibility=None)
    assert not any(item.event_type is EventType.ASSISTANT_MESSAGE_COMMITTED for item in events)
    assert not any(item.event_type is EventType.CITATION_SET_COMMITTED for item in events)
    assert not any(item.event_type is EventType.RUN_TERMINATED for item in events)
    assert (await store.get_run(run.envelope.run_id)).terminal_status is None


@pytest.mark.asyncio
async def test_rel_09_after_seq_replay_is_lossless_and_duplicate_free(runtime):
    store, clock, _, _ = runtime
    run = (await admit(store, clock)).run
    await store.append_events(run.envelope.run_id, [
        EventDraft(EventType.MODEL_PLAN_UPDATED, {"n": n}) for n in range(5)
    ])
    all_visible = await store.list_events(run.envelope.run_id, after_seq=0)
    for cursor in range(0, all_visible[-1].seq + 1):
        tail = await store.list_events(run.envelope.run_id, after_seq=cursor)
        expected = [item for item in all_visible if item.seq > cursor]
        assert [item.seq for item in tail] == [item.seq for item in expected]
        assert len({item.seq for item in tail}) == len(tail)


@pytest.mark.asyncio
async def test_rel_11_checkpoint_cas_does_not_overwrite(runtime):
    store, clock, _, _ = runtime
    run = (await admit(store, clock)).run
    _, activity = await claim_and_start(store, clock)
    state = WorkingState(goal="test")
    checkpoint = await store.save_checkpoint(
        run_id=run.envelope.run_id, activity_id=activity.activity_id,
        fencing_token=activity.fencing_token, expected_revision=0,
        working_state=state, now_ms=clock.now_ms(),
    )
    assert checkpoint.revision == 1
    with pytest.raises(RuntimeFault) as raised:
        await store.save_checkpoint(
            run_id=run.envelope.run_id, activity_id=activity.activity_id,
            fencing_token=activity.fencing_token, expected_revision=0,
            working_state=state, now_ms=clock.now_ms(),
        )
    assert raised.value.code == "CHECKPOINT_REVISION_CONFLICT"


@pytest.mark.asyncio
async def test_checkpoint_commits_engine_events_in_the_same_order(runtime):
    store, clock, _, _ = runtime
    run = (await admit(store, clock)).run
    _, activity = await claim_and_start(store, clock)

    await store.save_checkpoint(
        run_id=run.envelope.run_id,
        activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        expected_revision=0,
        working_state=WorkingState(goal="start one model generation"),
        engine_state={"phase": "MODEL_REQUEST"},
        now_ms=clock.now_ms(),
        events=(EventDraft(
            EventType.OUTPUT_GENERATION_STARTED,
            {
                "message_id": "model-slot-0",
                "generation_id": "generation-1",
                "supersedes_generation_id": None,
                "reason": "initial",
            },
            activity_id=activity.activity_id,
            producer="engine:native_loop",
            occurred_at=clock.now_ms(),
        ),),
    )

    events = await store.list_events(run.envelope.run_id, visibility=None)
    tail = [event.event_type for event in events[-2:]]
    assert tail == [
        EventType.OUTPUT_GENERATION_STARTED,
        EventType.CHECKPOINT_COMMITTED,
    ]


@pytest.mark.asyncio
async def test_rel_12_old_fencing_token_cannot_commit_after_reclaim(runtime):
    store, clock, _, _ = runtime
    run = (await admit(store, clock)).run
    old_claim, _ = await claim_and_start(store, clock)
    prepared = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=old_claim.activity.activity_id,
        fencing_token=old_claim.activity.fencing_token,
        logical_key="stale-fence:tool:0",
        tool_name="stale_read",
        release_digest="v1",
        effect_class=ToolEffectClass.READ_ONLY,
        request_digest=sha256_json({"query": "before lease loss"}),
        request={"query": "before lease loss"},
        now_ms=clock.now_ms(),
    )
    await store.mark_tool_dispatched(
        tool_execution_id=prepared["tool_execution_id"],
        parent_activity_id=old_claim.activity.activity_id,
        fencing_token=old_claim.activity.fencing_token,
        now_ms=clock.now_ms(),
    )
    clock.advance(1001)
    assert await store.recover_expired(now_ms=clock.now_ms()) == 1
    new_claim = await store.claim_next(
        worker_id="worker-b", lease_ms=1000, now_ms=clock.now_ms(),
        release_map=await store.active_releases(),
    )
    assert new_claim is not None
    assert new_claim.activity.fencing_token > old_claim.activity.fencing_token
    with pytest.raises(RuntimeFault) as raised:
        await store.append_events(
            run.envelope.run_id,
            [EventDraft(EventType.MODEL_PLAN_UPDATED, {"stale": True})],
            activity_id=old_claim.activity.activity_id,
            fencing_token=old_claim.activity.fencing_token,
        )
    assert raised.value.code == "STALE_FENCING_TOKEN"

    stale_state = WorkingState(
        goal="must not be committed by the expired worker",
    )
    with pytest.raises(RuntimeFault) as stale_checkpoint:
        await store.save_checkpoint(
            run_id=run.envelope.run_id,
            activity_id=old_claim.activity.activity_id,
            fencing_token=old_claim.activity.fencing_token,
            expected_revision=0,
            working_state=stale_state,
            now_ms=clock.now_ms(),
        )
    assert stale_checkpoint.value.code == "STALE_FENCING_TOKEN"

    with pytest.raises(RuntimeFault) as stale_tool_result:
        await store.settle_tool_execution(
            tool_execution_id=prepared["tool_execution_id"],
            parent_activity_id=old_claim.activity.activity_id,
            fencing_token=old_claim.activity.fencing_token,
            effect_status="COMMITTED",
            result={"value": "late"},
            result_ref=None,
            error=None,
            external_object_id=None,
            now_ms=clock.now_ms(),
        )
    assert stale_tool_result.value.code == "STALE_FENCING_TOKEN"

    with pytest.raises(RuntimeFault) as stale_final:
        await store.finalize_success(
            run_id=run.envelope.run_id,
            activity_id=old_claim.activity.activity_id,
            fencing_token=old_claim.activity.fencing_token,
            assistant_text="late final answer",
            citations=[],
            now_ms=clock.now_ms(),
        )
    assert stale_final.value.code == "STALE_FENCING_TOKEN"
    assert await store.latest_checkpoint(run.envelope.run_id) is None
    assert (await store.get_tool_execution(prepared["tool_execution_id"]))[
        "effect_status"
    ] == "DISPATCHED"
    assert (await store.get_run(run.envelope.run_id)).terminal_status is None
    events = await store.list_events(run.envelope.run_id, visibility=None)
    assert not any(
        event.event_type in {
            EventType.CHECKPOINT_COMMITTED,
            EventType.TOOL_RESULT_COMMITTED,
            EventType.ASSISTANT_MESSAGE_COMMITTED,
            EventType.CITATION_SET_COMMITTED,
            EventType.RUN_TERMINATED,
        }
        for event in events
    )


@pytest.mark.asyncio
async def test_rel_13_expired_lease_recovered_only_once(runtime):
    store, clock, _, _ = runtime
    await admit(store, clock)
    await claim_and_start(store, clock)
    clock.advance(1001)
    results = await asyncio.gather(*[
        store.recover_expired(now_ms=clock.now_ms()) for _ in range(5)
    ])
    assert sum(results) == 1


@pytest.mark.asyncio
async def test_rel_14_retry_timer_fires_once(runtime):
    store, clock, _, _ = runtime
    run = (await admit(store, clock)).run
    _, activity = await claim_and_start(store, clock)
    await store.schedule_retry(
        run_id=run.envelope.run_id, activity_id=activity.activity_id,
        fencing_token=activity.fencing_token, fire_at=clock.now_ms() + 1000,
        error={"retryable": True}, now_ms=clock.now_ms(),
    )
    clock.advance(1000)
    results = await asyncio.gather(*[
        store.fire_due_timers(now_ms=clock.now_ms()) for _ in range(5)
    ])
    assert sum(results) == 1
    assert (await store.get_run(run.envelope.run_id)).status is RunStatus.DISPATCH_PENDING


@pytest.mark.asyncio
async def test_rel_15_16_wait_survives_store_restart_and_signal_is_idempotent(runtime):
    store, clock, _, tmp_path = runtime
    run = (await admit(store, clock)).run
    _, activity = await claim_and_start(store, clock)
    await store.wait_for_input(
        run_id=run.envelope.run_id, activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        pending_input={"type": "approval"}, now_ms=clock.now_ms(),
    )
    restarted = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await restarted.initialize()
    result = await restarted.submit_signal(
        run_id=run.envelope.run_id, signal_id="signal-1",
        wait_activity_id=activity.activity_id, signal_type="approval",
        payload={"approved": True}, payload_digest="digest-1", now_ms=clock.now_ms(),
    )
    replay = await restarted.submit_signal(
        run_id=run.envelope.run_id, signal_id="signal-1",
        wait_activity_id=activity.activity_id, signal_type="approval",
        payload={"approved": True}, payload_digest="digest-1", now_ms=clock.now_ms(),
    )
    assert result.status == "CONSUMED"
    assert replay.reused is True
    assert (await restarted.get_run(run.envelope.run_id)).status is RunStatus.DISPATCH_PENDING


@pytest.mark.asyncio
async def test_rel_16_late_signal_is_audited_then_rejected(runtime):
    store, clock, _, _ = runtime
    run = (await admit(store, clock)).run
    await store.request_cancel(
        run_id=run.envelope.run_id, command_id="cancel-1", reason="stop", now_ms=clock.now_ms(),
    )
    with pytest.raises(RuntimeFault) as raised:
        await store.submit_signal(
            run_id=run.envelope.run_id, signal_id="late-1",
            wait_activity_id=run.current_activity_id, signal_type="approval",
            payload={}, payload_digest="late-digest", now_ms=clock.now_ms(),
        )
    assert raised.value.code == "RUN_ALREADY_TERMINAL"
    async with store.db.read() as conn:
        row = await (await conn.execute(
            "SELECT status FROM signals WHERE signal_id='late-1'"
        )).fetchone()
    assert row["status"] == "REJECTED_LATE"


@pytest.mark.asyncio
async def test_rel_20_cancel_and_complete_have_commit_order_semantics(runtime):
    store, clock, _, _ = runtime
    first = (await admit(store, clock, key="first")).run
    _, activity = await claim_and_start(store, clock)
    cancelled, _ = await store.request_cancel(
        run_id=first.envelope.run_id, command_id="cancel-first", reason="race",
        now_ms=clock.now_ms(),
    )
    assert cancelled.status is RunStatus.CANCEL_REQUESTED
    settled = await store.finalize_success(
        run_id=first.envelope.run_id, activity_id=activity.activity_id,
        fencing_token=activity.fencing_token, assistant_text="late", citations=[],
        now_ms=clock.now_ms(),
    )
    assert settled.status is RunStatus.CANCELLED

    second = (await admit(store, clock, key="second")).run
    _, activity2 = await claim_and_start(store, clock)
    succeeded = await store.finalize_success(
        run_id=second.envelope.run_id, activity_id=activity2.activity_id,
        fencing_token=activity2.fencing_token, assistant_text="winner", citations=[],
        now_ms=clock.now_ms(),
    )
    assert succeeded.status is RunStatus.SUCCEEDED
    with pytest.raises(RuntimeFault):
        await store.request_cancel(
            run_id=second.envelope.run_id, command_id="cancel-late", reason="race",
            now_ms=clock.now_ms(),
        )


@pytest.mark.asyncio
async def test_rel_28_wrong_release_cannot_claim_or_terminalize_run(runtime):
    store, clock, releases, _ = runtime
    run = (await admit(store, clock)).run
    adapter = ScriptedEngineAdapter([], release_fingerprint="different-release")
    coordinator = RunCoordinator(store, EngineRegistry({"native_loop": adapter}), clock=clock)
    assert await store.claim_next(
        worker_id="wrong-worker", lease_ms=1000, now_ms=clock.now_ms(),
        release_map={"native_loop": "different-release"},
    ) is None
    assert (await store.get_run(run.envelope.run_id)).status is RunStatus.DISPATCH_PENDING

    claim = await store.claim_next(
        worker_id="worker-a", lease_ms=1000, now_ms=clock.now_ms(),
        release_map={"native_loop": releases["native_loop"]},
    )
    assert claim is not None
    with pytest.raises(AttemptOwnershipLost) as raised:
        await coordinator.execute_claim(claim, worker_id="worker-a")
    assert raised.value.code == "CLAIM_RELEASE_MISMATCH"
    assert (await store.get_run(run.envelope.run_id)).terminal_status is None


@pytest.mark.asyncio
async def test_rel_30_all_engine_names_share_coordinator_contract(runtime):
    store, clock, releases, _ = runtime
    for index, engine in enumerate(("plan_execute", "agent_loop", "native_loop")):
        run = (await admit(store, clock, key=f"key-{index}", engine=engine)).run
        adapter = ScriptedEngineAdapter(
            [{"type": "text", "delta": engine}], release_fingerprint=releases[engine],
        )
        coordinator = RunCoordinator(store, EngineRegistry({engine: adapter}), clock=clock,
                                     event_flush_bytes=1)
        claim = await store.claim_next(
            worker_id=f"worker-{index}", lease_ms=1000, now_ms=clock.now_ms(),
            release_map={engine: releases[engine]},
        )
        assert claim is not None
        assert await coordinator.execute_claim(claim, worker_id=f"worker-{index}") is RunStatus.SUCCEEDED
        assert (await store.get_run(run.envelope.run_id)).terminal_status is RunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_rel_17_tool_execution_reuses_committed_result_and_mismatch_fails(runtime):
    store, clock, _, tmp_path = runtime
    run = (await admit(store, clock)).run
    _, activity = await claim_and_start(store, clock)
    broker = ToolBroker(store, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock)
    calls = 0

    async def execute(args, context):
        nonlocal calls
        calls += 1
        return {"value": args["value"], "idempotency_key": context.idempotency_key}

    broker.register(ToolManifest(
        name="read", release_digest="v1", effect_class=ToolEffectClass.READ_ONLY,
        timeout_seconds=1, max_attempts=2, concurrency_safe=True,
    ), execute)
    first = await broker.execute(
        run_id=run.envelope.run_id, parent_activity_id=activity.activity_id,
        fencing_token=activity.fencing_token, logical_key="turn:0:call:0",
        tool_name="read", arguments={"value": 1}, deadline_at_ms=run.envelope.deadline_at,
    )
    replay = await broker.execute(
        run_id=run.envelope.run_id, parent_activity_id=activity.activity_id,
        fencing_token=activity.fencing_token, logical_key="turn:0:call:0",
        tool_name="read", arguments={"value": 1}, deadline_at_ms=run.envelope.deadline_at,
    )
    assert first == replay
    assert calls == 1
    with pytest.raises(RuntimeFault) as raised:
        await broker.execute(
            run_id=run.envelope.run_id, parent_activity_id=activity.activity_id,
            fencing_token=activity.fencing_token, logical_key="turn:0:call:0",
            tool_name="read", arguments={"value": 2}, deadline_at_ms=run.envelope.deadline_at,
        )
    assert raised.value.code == "TOOL_REPLAY_MISMATCH"


@pytest.mark.asyncio
async def test_rel_18_19_idempotent_unknown_retries_with_stable_key_then_manual(runtime):
    store, clock, _, tmp_path = runtime
    run = (await admit(store, clock)).run
    _, activity = await claim_and_start(store, clock)
    broker = ToolBroker(store, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock)
    calls = 0

    async def ambiguous(_args, _context):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)

    broker.register(ToolManifest(
        name="effect", release_digest="v1",
        effect_class=ToolEffectClass.IDEMPOTENT_EFFECT,
        timeout_seconds=0.001, max_attempts=2, supports_idempotency=True,
    ), ambiguous)
    first = await broker.execute(
        run_id=run.envelope.run_id, parent_activity_id=activity.activity_id,
        fencing_token=activity.fencing_token, logical_key="effect:0", tool_name="effect",
        arguments={}, deadline_at_ms=run.envelope.deadline_at,
    )
    second = await broker.execute(
        run_id=run.envelope.run_id, parent_activity_id=activity.activity_id,
        fencing_token=activity.fencing_token, logical_key="effect:0", tool_name="effect",
        arguments={}, deadline_at_ms=run.envelope.deadline_at,
    )
    third = await broker.execute(
        run_id=run.envelope.run_id, parent_activity_id=activity.activity_id,
        fencing_token=activity.fencing_token, logical_key="effect:0", tool_name="effect",
        arguments={}, deadline_at_ms=run.envelope.deadline_at,
    )
    assert first.status.value == "UNKNOWN"
    assert second.status.value == "UNKNOWN"
    assert third.status.value == "UNKNOWN"
    assert calls == 2
    execution = await store.get_tool_execution(
        next(item.tool_execution_id for item in await store.list_events(
            run.envelope.run_id, visibility=None,
        ) if item.event_type is EventType.TOOL_CALL_COMMITTED)
    )
    assert execution["effect_status"] == "MANUAL_REQUIRED"


@pytest.mark.asyncio
async def test_tool_attempt_budget_and_dispatched_read_only_recovery(runtime):
    store, clock, _, tmp_path = runtime
    run = (await admit(store, clock, key="tool-attempt-budget")).run
    _, activity = await claim_and_start(store, clock)
    broker = ToolBroker(store, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock)
    failures = 0

    async def always_fails(_args, _context):
        nonlocal failures
        failures += 1
        raise OSError("read failed")

    broker.register(ToolManifest(
        name="bounded_read",
        release_digest="v1",
        effect_class=ToolEffectClass.READ_ONLY,
        timeout_seconds=1,
        max_attempts=2,
    ), always_fails)
    results = []
    for _ in range(4):
        results.append(await broker.execute(
            run_id=run.envelope.run_id,
            parent_activity_id=activity.activity_id,
            fencing_token=activity.fencing_token,
            logical_key="bounded-read:0",
            tool_name="bounded_read",
            arguments={"query": "x"},
            deadline_at_ms=run.envelope.deadline_at,
        ))
    assert failures == 2
    assert all(result.status is ToolResultStatus.FAILURE for result in results)

    recovered_calls = 0

    async def recovered_read(_args, _context):
        nonlocal recovered_calls
        recovered_calls += 1
        return {"ok": True}

    broker.register(ToolManifest(
        name="recoverable_read",
        release_digest="v1",
        effect_class=ToolEffectClass.READ_ONLY,
        timeout_seconds=1,
        max_attempts=2,
    ), recovered_read)
    arguments = {"query": "recover"}
    prepared = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        logical_key="recoverable-read:0",
        tool_name="recoverable_read",
        release_digest="v1",
        effect_class=ToolEffectClass.READ_ONLY,
        request_digest=sha256_json(arguments),
        request=arguments,
        now_ms=clock.now_ms(),
    )
    await store.mark_tool_dispatched(
        tool_execution_id=prepared["tool_execution_id"],
        parent_activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        now_ms=clock.now_ms(),
    )
    recovered = await broker.execute(
        run_id=run.envelope.run_id,
        parent_activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        logical_key="recoverable-read:0",
        tool_name="recoverable_read",
        arguments=arguments,
        deadline_at_ms=run.envelope.deadline_at,
    )
    assert recovered.status is ToolResultStatus.SUCCESS
    assert recovered_calls == 1
    execution = await store.get_tool_execution(prepared["tool_execution_id"])
    assert execution["attempt"] == 2
    assert execution["effect_status"] == "COMMITTED"


@pytest.mark.asyncio
async def test_reconcile_hook_commits_uncertain_effect_without_redispatch(runtime):
    store, clock, _, tmp_path = runtime
    run = (await admit(store, clock, key="tool-reconcile-hook")).run
    _, activity = await claim_and_start(store, clock)
    broker = ToolBroker(store, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock)
    dispatches = 0
    reconciles = 0

    async def should_not_dispatch(_args, _context):
        nonlocal dispatches
        dispatches += 1
        return {"wrong": True}

    async def reconcile(context):
        nonlocal reconciles
        reconciles += 1
        assert context.parent_activity_id == activity.activity_id
        return ToolResultEnvelope(
            status=ToolResultStatus.SUCCESS,
            preview={"confirmed": True},
            external_object_id="external-1",
        )

    manifest = ToolManifest(
        name="reconciled_effect",
        release_digest="v1",
        effect_class=ToolEffectClass.UNKNOWN_EFFECT,
        timeout_seconds=1,
        max_attempts=1,
        supports_reconcile=True,
    )
    broker.register(manifest, should_not_dispatch, reconcile=reconcile)
    arguments = {"value": 1}
    prepared = await store.prepare_tool_execution(
        run_id=run.envelope.run_id,
        parent_activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        logical_key="reconciled:0",
        tool_name=manifest.name,
        release_digest=manifest.release_digest,
        effect_class=manifest.effect_class,
        request_digest=sha256_json(arguments),
        request=arguments,
        supports_reconcile=True,
        now_ms=clock.now_ms(),
    )
    await store.mark_tool_dispatched(
        tool_execution_id=prepared["tool_execution_id"],
        parent_activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        now_ms=clock.now_ms(),
    )
    result = await broker.execute(
        run_id=run.envelope.run_id,
        parent_activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        logical_key="reconciled:0",
        tool_name=manifest.name,
        arguments=arguments,
        deadline_at_ms=run.envelope.deadline_at,
    )
    assert result.preview == {"confirmed": True}
    assert reconciles == 1
    assert dispatches == 0
    execution = await store.get_tool_execution(prepared["tool_execution_id"])
    assert execution["effect_status"] == "COMMITTED"


@pytest.mark.asyncio
async def test_unresolved_effect_blocks_success_and_deadline_closes_timed_out(runtime):
    store, clock, releases, tmp_path = runtime
    run = (await admit(store, clock, key="unknown-terminal")).run
    broker = ToolBroker(store, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock)

    async def ambiguous(_args, _context):
        await asyncio.sleep(0.02)

    broker.register(ToolManifest(
        name="uncertain_effect",
        release_digest="v1",
        effect_class=ToolEffectClass.UNKNOWN_EFFECT,
        timeout_seconds=0.001,
        max_attempts=1,
    ), ambiguous)

    class UnknownEffectAdapter:
        name = "native_loop"
        release_fingerprint = releases["native_loop"]

        async def execute(self, request, io):
            await broker.execute(
                run_id=request.envelope.run_id,
                parent_activity_id=request.activity_id,
                fencing_token=request.fencing_token,
                logical_key="unknown:0",
                tool_name="uncertain_effect",
                arguments={"value": 1},
                deadline_at_ms=request.envelope.deadline_at,
            )
            await io.emit("text", {"delta": "model tried to finish"})
            return EngineOutcome(kind=EngineOutcomeKind.COMPLETED)

    coordinator = RunCoordinator(
        store,
        EngineRegistry({"native_loop": UnknownEffectAdapter()}),
        clock=clock,
    )
    claim = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="unknown-worker", lease_ms=1_000, now_ms=clock.now_ms(),
    )
    assert claim is not None
    status = await coordinator.execute_claim(claim, worker_id="unknown-worker")
    assert status is RunStatus.WAITING_INPUT
    waiting = await store.get_run(run.envelope.run_id)
    assert waiting.pending_input["type"] == "TOOL_RECONCILIATION_REQUIRED"
    unresolved = waiting.pending_input["unresolved_tool_execution_ids"]
    assert len(unresolved) == 1

    clock.advance(60_001)
    assert await store.expire_deadlines(now_ms=clock.now_ms()) == 1
    terminal = await store.get_run(run.envelope.run_id)
    assert terminal.terminal_status is RunStatus.TIMED_OUT
    assert terminal.terminal_payload["unresolved_tool_execution_ids"] == unresolved


@pytest.mark.asyncio
async def test_cancelled_unknown_effect_stays_cancel_requested_across_lease_recovery(runtime):
    store, clock, _, tmp_path = runtime
    run = (await admit(store, clock, key="cancel-unknown")).run
    _claim, activity = await claim_and_start(store, clock)
    broker = ToolBroker(store, FilesystemArtifactStore(tmp_path / "artifacts"), clock=clock)

    async def ambiguous(_args, _context):
        await asyncio.sleep(0.02)

    broker.register(ToolManifest(
        name="uncertain_cancel_effect",
        release_digest="v1",
        effect_class=ToolEffectClass.UNKNOWN_EFFECT,
        timeout_seconds=0.001,
    ), ambiguous)
    await broker.execute(
        run_id=run.envelope.run_id,
        parent_activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        logical_key="cancel-unknown:0",
        tool_name="uncertain_cancel_effect",
        arguments={},
        deadline_at_ms=run.envelope.deadline_at,
    )
    cancelled, _ = await store.request_cancel(
        run_id=run.envelope.run_id,
        command_id="cancel-unknown-command",
        reason="stop after ambiguous dispatch",
        now_ms=clock.now_ms(),
    )
    assert cancelled.status is RunStatus.CANCEL_REQUESTED

    clock.advance(1_001)
    assert await store.recover_expired(now_ms=clock.now_ms()) == 1
    recovered = await store.get_run(run.envelope.run_id)
    assert recovered.status is RunStatus.CANCEL_REQUESTED
    assert (await store.get_activity(activity.activity_id)).status.value == "RECONCILE"


@pytest.mark.asyncio
async def test_cancel_without_unknown_effect_closes_on_expired_worker_lease(runtime):
    store, clock, _, _ = runtime
    run = (await admit(store, clock, key="cancel-clean-recovery")).run
    await claim_and_start(store, clock)
    cancelled, _ = await store.request_cancel(
        run_id=run.envelope.run_id,
        command_id="cancel-clean-command",
        reason="worker will disappear",
        now_ms=clock.now_ms(),
    )
    assert cancelled.status is RunStatus.CANCEL_REQUESTED
    clock.advance(1_001)
    assert await store.recover_expired(now_ms=clock.now_ms()) == 1
    terminal = await store.get_run(run.envelope.run_id)
    assert terminal.terminal_status is RunStatus.CANCELLED


@pytest.mark.asyncio
async def test_rel_23_large_tool_result_is_artifact_backed_preview(runtime):
    store, clock, _, tmp_path = runtime
    run = (await admit(store, clock)).run
    _, activity = await claim_and_start(store, clock)
    broker = ToolBroker(
        store, FilesystemArtifactStore(tmp_path / "artifacts"),
        clock=clock, inline_result_max_bytes=128,
    )
    broker.register(ToolManifest(
        name="large", release_digest="v1", effect_class=ToolEffectClass.READ_ONLY,
        timeout_seconds=1,
    ), lambda _args, _context: {"data": "x" * 5000})
    result = await broker.execute(
        run_id=run.envelope.run_id, parent_activity_id=activity.activity_id,
        fencing_token=activity.fencing_token, logical_key="large:0", tool_name="large",
        arguments={}, deadline_at_ms=run.envelope.deadline_at,
    )
    assert result.result_ref
    assert len(result.preview) <= 128
    events = await store.list_events(run.envelope.run_id, visibility=None)
    tool_result = next(item for item in events if item.event_type is EventType.TOOL_RESULT_COMMITTED)
    assert "x" * 1000 not in str(tool_result.payload)


@pytest.mark.asyncio
async def test_large_tool_result_is_fully_rematerialized_from_artifact_after_restart(runtime):
    store, clock, _, tmp_path = runtime
    run = (await admit(store, clock, key="large-result-materialization")).run
    _, activity = await claim_and_start(store, clock)
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")
    calls = 0
    complete_preview = {
        "records": [
            {"ordinal": index, "content": "完整结果-" + "值" * 100}
            for index in range(50)
        ]
    }

    async def large_result(_arguments, _context):
        nonlocal calls
        calls += 1
        return complete_preview

    broker = ToolBroker(
        store,
        artifacts,
        clock=clock,
        inline_result_max_bytes=128,
    )
    broker.register(ToolManifest(
        name="large",
        release_digest="large-result-v1",
        effect_class=ToolEffectClass.READ_ONLY,
        timeout_seconds=1,
    ), large_result)
    first = await broker.execute(
        run_id=run.envelope.run_id,
        parent_activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        logical_key="large:materialize:0",
        tool_name="large",
        arguments={},
        deadline_at_ms=run.envelope.deadline_at,
    )
    event = next(
        item
        for item in await store.list_events(run.envelope.run_id, visibility=None)
        if item.event_type is EventType.TOOL_RESULT_COMMITTED
    )
    assert first.result_ref is not None
    assert first.preview != complete_preview

    # A fresh Broker models Worker restart.  Ordinary committed results never
    # re-run their executor; the complete current envelope comes from CAS.
    restarted = ToolBroker(
        store,
        FilesystemArtifactStore(tmp_path / "artifacts"),
        clock=clock,
        inline_result_max_bytes=128,
    )
    materialized = await restarted.materialize_committed_result(
        tool_execution_id=event.tool_execution_id,
        parent_activity_id=activity.activity_id,
        deadline_at_ms=run.envelope.deadline_at,
    )

    assert calls == 1
    assert materialized.status is ToolResultStatus.SUCCESS
    assert materialized.preview == complete_preview
    assert materialized.result_ref == first.result_ref
    execution = await store.get_tool_execution(event.tool_execution_id)
    persisted = json.loads(execution["result_json"])
    assert persisted["full_result_ref"] == first.result_ref
    assert "完整结果" not in str(persisted["preview"])
