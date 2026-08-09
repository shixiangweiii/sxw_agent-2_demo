from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from agent.runtime.adapters.scripted_engine import ScriptedEngineAdapter
from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.runtime.application.admission import AdmissionService, CreateRunInput
from agent.runtime.application.coordinator import EngineRegistry, RunCoordinator
from agent.runtime.application.release_compatibility import ReleaseCompatibilityRegistry
from agent.runtime.domain.errors import RuntimeFault
from agent.runtime.domain.models import (
    SCHEMA_VERSION,
    EventType,
    ReleaseManifest,
    RunStatus,
    WorkingState,
)
from agent.runtime.ports.release_compatibility import (
    CheckpointUpgrader,
    CheckpointUpgradeKey,
    CheckpointUpgradeRequest,
    CheckpointUpgradeResult,
)


@dataclass
class FakeClock:
    value: int = 1_800_000_000_000

    def now_ms(self) -> int:
        return self.value

    def monotonic(self) -> float:
        return self.value / 1000


class RecordingUpgrader:
    def __init__(self, target_release: str, *, fail: bool = False) -> None:
        self.target_release = target_release
        self.fail = fail
        self.calls: list[CheckpointUpgradeRequest] = []

    def upgrade(self, request: CheckpointUpgradeRequest) -> CheckpointUpgradeResult:
        self.calls.append(request)
        if self.fail:
            raise ValueError("intentionally broken codec")
        state = request.checkpoint.working_state.model_copy(
            deep=True,
            update={"release_fingerprint": self.target_release},
        )
        return CheckpointUpgradeResult(
            working_state=state,
            engine_state={
                "codec": "v2",
                "converted_from": request.checkpoint.engine_state,
            },
        )


class NeverCalledUpgrader:
    def __init__(self) -> None:
        self.calls = 0

    def upgrade(self, request: CheckpointUpgradeRequest) -> CheckpointUpgradeResult:
        self.calls += 1
        raise AssertionError("exact release recovery must not invoke an upgrader")


async def _admit(store: SqliteRuntimeStore, clock: FakeClock, *, key: str):
    return await AdmissionService(store, clock=clock).create(
        CreateRunInput(
            client_request_id=str(uuid.uuid4()),
            conversation_id=None,
            principal_id="demo-user",
            agent_id="demo-agent",
            engine="native_loop",
            text="resume me",
            attachment_refs=(),
            deadline_at=None,
        ),
        idempotency_key=key,
    )


async def _prepare_source_checkpoint(tmp_path, *, key: str = "upgrade"):
    clock = FakeClock()
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    source = await store.register_release(
        ReleaseManifest(engine="native_loop", components={"codec": "v1"}),
        activate=True,
    )
    run = (await _admit(store, clock, key=key)).run
    claim = await store.claim_next(
        worker_id="source-worker", lease_ms=30_000, now_ms=clock.now_ms(),
    )
    assert claim is not None
    activity = await store.mark_activity_running(
        claim.activity.activity_id,
        worker_id="source-worker",
        fencing_token=claim.activity.fencing_token,
        now_ms=clock.now_ms(),
    )
    checkpoint = await store.save_checkpoint(
        run_id=run.envelope.run_id,
        activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        expected_revision=0,
        working_state=WorkingState(
            goal="survive release change",
            confirmed_facts=[{"fact": "source checkpoint is durable"}],
            release_fingerprint=source,
        ),
        engine_state={"codec": "v1", "cursor": 7},
        now_ms=clock.now_ms(),
    )
    await store.schedule_retry(
        run_id=run.envelope.run_id,
        activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        fire_at=clock.now_ms(),
        error={"code": "RESTART", "retryable": True},
        now_ms=clock.now_ms(),
    )
    assert await store.fire_due_timers(now_ms=clock.now_ms()) == 1
    target = await store.register_release(
        ReleaseManifest(engine="native_loop", components={"codec": "v2"}),
        activate=True,
    )
    key_obj = CheckpointUpgradeKey(
        engine="native_loop",
        from_release_fingerprint=source,
        from_schema_version=checkpoint.schema_version,
        to_release_fingerprint=target,
        to_schema_version=SCHEMA_VERSION,
    )
    return store, clock, run, activity, checkpoint, source, target, key_obj


@pytest.mark.asyncio
async def test_rel_28_exact_release_resumes_without_running_upgrader(tmp_path):
    clock = FakeClock()
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    release = await store.register_release(
        ReleaseManifest(engine="native_loop", components={"codec": "same"}),
    )
    run = (await _admit(store, clock, key="exact")).run
    never = NeverCalledUpgrader()
    irrelevant_key = CheckpointUpgradeKey(
        engine="native_loop",
        from_release_fingerprint=release,
        from_schema_version=SCHEMA_VERSION,
        to_release_fingerprint="f" * 64,
        to_schema_version=SCHEMA_VERSION,
    )
    coordinator = RunCoordinator(
        store,
        EngineRegistry({
            "native_loop": ScriptedEngineAdapter(
                [{"type": "text", "delta": "ok"}],
                release_fingerprint=release,
            ),
        }),
        clock=clock,
        event_flush_bytes=1,
        release_compatibility=ReleaseCompatibilityRegistry({irrelevant_key: never}),
    )
    claim = await store.claim_next(
        worker_id="exact-worker", lease_ms=30_000, now_ms=clock.now_ms(),
    )
    assert claim is not None
    assert await coordinator.execute_claim(claim, worker_id="exact-worker") is RunStatus.SUCCEEDED
    assert never.calls == 0
    assert (await store.get_run(run.envelope.run_id)).envelope.release_fingerprint == release


@pytest.mark.asyncio
async def test_rel_28_explicit_upgrade_preserves_source_and_resumes_target_adapter(tmp_path):
    store, clock, admitted, _, source_checkpoint, source, target, key = (
        await _prepare_source_checkpoint(tmp_path)
    )
    upgrader = RecordingUpgrader(target)

    def assert_upgraded(request):
        assert request.envelope.release_fingerprint == target
        assert request.checkpoint is not None
        assert request.checkpoint.revision == source_checkpoint.revision + 1
        assert request.checkpoint.release_fingerprint == target
        assert request.checkpoint.engine_state == {
            "codec": "v2",
            "converted_from": {"codec": "v1", "cursor": 7},
        }
        return [{"type": "text", "delta": "resumed"}]

    coordinator = RunCoordinator(
        store,
        EngineRegistry({
            "native_loop": ScriptedEngineAdapter(
                assert_upgraded, release_fingerprint=target,
            ),
        }),
        clock=clock,
        event_flush_bytes=1,
        release_compatibility=ReleaseCompatibilityRegistry({key: upgrader}),
    )
    claim = await store.claim_next(
        worker_id="target-worker", lease_ms=30_000, now_ms=clock.now_ms(),
    )
    assert claim is not None
    assert await coordinator.execute_claim(claim, worker_id="target-worker") is RunStatus.SUCCEEDED
    assert len(upgrader.calls) == 1

    current = await store.get_run(admitted.envelope.run_id)
    assert admitted.envelope.release_fingerprint == source  # admission snapshot remains immutable
    assert current.envelope.release_fingerprint == target  # explicit durable upgrade exception
    assert current.terminal_status is RunStatus.SUCCEEDED
    async with store.db.read() as conn:
        rows = await (await conn.execute(
            """SELECT checkpoint_id,revision,release_fingerprint
               FROM checkpoints WHERE run_id=? ORDER BY revision""",
            (admitted.envelope.run_id,),
        )).fetchall()
    assert [(row["revision"], row["release_fingerprint"]) for row in rows] == [
        (1, source),
        (2, target),
    ]
    assert rows[0]["checkpoint_id"] == source_checkpoint.checkpoint_id
    upgrade_events = [
        event for event in await store.list_events(admitted.envelope.run_id, visibility=None)
        if event.event_type is EventType.CHECKPOINT_COMMITTED
        and event.payload is not None
        and "upgrade" in event.payload
    ]
    assert len(upgrade_events) == 1
    assert upgrade_events[0].release_fingerprint == target
    assert upgrade_events[0].payload["upgrade"] == {
        "source_checkpoint_id": source_checkpoint.checkpoint_id,
        "source_revision": 1,
        "from_release_fingerprint": source,
        "from_schema_version": SCHEMA_VERSION,
        "to_release_fingerprint": target,
        "to_schema_version": SCHEMA_VERSION,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing", "throws", "wrong-key"])
async def test_rel_28_missing_or_wrong_upgrader_fails_closed(tmp_path, mode):
    store, clock, run, _, checkpoint, source, target, key = (
        await _prepare_source_checkpoint(tmp_path, key=f"bad-{mode}")
    )
    if mode == "missing":
        registry = ReleaseCompatibilityRegistry()
    elif mode == "throws":
        registry = ReleaseCompatibilityRegistry({key: RecordingUpgrader(target, fail=True)})
    else:
        wrong_key = CheckpointUpgradeKey(
            engine=key.engine,
            from_release_fingerprint=key.from_release_fingerprint,
            from_schema_version="0",
            to_release_fingerprint=key.to_release_fingerprint,
            to_schema_version=key.to_schema_version,
        )
        registry = ReleaseCompatibilityRegistry({wrong_key: RecordingUpgrader(target)})
    coordinator = RunCoordinator(
        store,
        EngineRegistry({
            "native_loop": ScriptedEngineAdapter([], release_fingerprint=target),
        }),
        clock=clock,
        release_compatibility=registry,
    )
    claim = await store.claim_next(
        worker_id="incompatible-worker", lease_ms=30_000, now_ms=clock.now_ms(),
    )
    assert claim is not None
    assert (
        await coordinator.execute_claim(claim, worker_id="incompatible-worker")
        is RunStatus.INCOMPATIBLE_RELEASE
    )
    current = await store.get_run(run.envelope.run_id)
    assert current.terminal_status is RunStatus.INCOMPATIBLE_RELEASE
    assert current.envelope.release_fingerprint == source
    assert (await store.latest_checkpoint(run.envelope.run_id)).checkpoint_id == checkpoint.checkpoint_id


@pytest.mark.asyncio
async def test_rel_28_upgrade_publish_rejects_old_fence_revision_and_wrong_engine(tmp_path):
    store, clock, run, first_activity, checkpoint, source, target, key = (
        await _prepare_source_checkpoint(tmp_path, key="cas-fence")
    )
    claim = await store.claim_next(
        worker_id="new-worker", lease_ms=30_000, now_ms=clock.now_ms(),
    )
    assert claim is not None
    current_activity = await store.mark_activity_running(
        claim.activity.activity_id,
        worker_id="new-worker",
        fencing_token=claim.activity.fencing_token,
        now_ms=clock.now_ms(),
    )
    converted = RecordingUpgrader(target).upgrade(CheckpointUpgradeRequest(
        key=key, checkpoint=checkpoint,
    ))
    common = dict(
        run_id=run.envelope.run_id,
        activity_id=current_activity.activity_id,
        source_checkpoint_id=checkpoint.checkpoint_id,
        from_release_fingerprint=source,
        from_schema_version=SCHEMA_VERSION,
        to_release_fingerprint=target,
        to_schema_version=SCHEMA_VERSION,
        working_state=converted.working_state,
        engine_state=converted.engine_state,
        now_ms=clock.now_ms(),
    )
    with pytest.raises(RuntimeFault) as old_fence:
        await store.publish_checkpoint_upgrade(
            **common,
            fencing_token=first_activity.fencing_token,
            expected_revision=checkpoint.revision,
        )
    assert old_fence.value.code == "STALE_FENCING_TOKEN"

    with pytest.raises(RuntimeFault) as stale_revision:
        await store.publish_checkpoint_upgrade(
            **common,
            fencing_token=current_activity.fencing_token,
            expected_revision=0,
        )
    assert stale_revision.value.code == "CHECKPOINT_REVISION_CONFLICT"

    wrong_engine_target = await store.register_release(
        ReleaseManifest(engine="agent_loop", components={"codec": "other-engine"}),
        activate=False,
    )
    with pytest.raises(RuntimeFault) as wrong_engine:
        await store.publish_checkpoint_upgrade(
            **{
                **common,
                "to_release_fingerprint": wrong_engine_target,
                "working_state": converted.working_state.model_copy(
                    update={"release_fingerprint": wrong_engine_target},
                ),
            },
            fencing_token=current_activity.fencing_token,
            expected_revision=checkpoint.revision,
        )
    assert wrong_engine.value.code == "CHECKPOINT_UPGRADE_ENGINE_MISMATCH"

    assert (await store.get_run(run.envelope.run_id)).envelope.release_fingerprint == source
    assert (await store.latest_checkpoint(run.envelope.run_id)).checkpoint_id == checkpoint.checkpoint_id

