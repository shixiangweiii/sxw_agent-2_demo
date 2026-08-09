from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.runtime.application.admission import AdmissionService, CreateRunInput
from agent.runtime.application.coordinator import EngineRegistry, RunCoordinator
from agent.runtime.domain.models import (
    EngineOutcome,
    EngineOutcomeKind,
    EventType,
    ReleaseManifest,
    RunStatus,
    WorkingState,
)
from agent.runtime.worker.dispatcher import RuntimeWorker
from common.obs import set_trace_id
from common.trace import configure_tracing


@dataclass
class FakeClock:
    value: int = 2_100_000_000_000

    def now_ms(self) -> int:
        return self.value

    def monotonic(self) -> float:
        return self.value / 1000

    def advance(self, milliseconds: int) -> None:
        self.value += milliseconds


class CheckpointThenKilledAdapter:
    name = "native_loop"

    def __init__(self, release: str) -> None:
        self.release_fingerprint = release
        self.calls = 0

    async def execute(self, request, io):
        self.calls += 1
        assert request.attempt == 1
        assert request.checkpoint is None
        await io.emit("text", {"delta": "committed-before-worker-loss"})
        saved = await io.checkpoint(
            WorkingState(
                goal="trace-independent recovery",
                confirmed_facts=[{"fact": "checkpoint survived", "source": "runtime"}],
                release_fingerprint=request.envelope.release_fingerprint,
            ),
            expected_revision=0,
            engine_state={"phase": "MODEL_RESPONSE_COMMITTED", "slot": 0},
        )
        assert saved.revision == 1
        # Simulated process kill: no EngineOutcome and no Activity settlement.
        # The Activity remains RUNNING until its durable lease expires.
        raise asyncio.CancelledError


class ResumeFromCheckpointAdapter:
    name = "native_loop"

    def __init__(self, release: str) -> None:
        self.release_fingerprint = release
        self.calls = 0

    async def execute(self, request, io):
        self.calls += 1
        assert request.attempt == 2
        assert request.checkpoint is not None
        assert request.checkpoint.revision == 1
        assert request.checkpoint.engine_state == {
            "phase": "MODEL_RESPONSE_COMMITTED",
            "slot": 0,
        }
        await io.emit("text", {"delta": "resumed-after-lease-recovery"})
        return EngineOutcome(kind=EngineOutcomeKind.COMPLETED)


def _configure_trace(mode: str, trace_root: Path) -> None:
    configure_tracing(
        enabled=mode != "disabled",
        trace_dir=str(trace_root),
        payload_level="full",
        retention_days=7,
        engine=f"rel-029-{mode}",
    )


def _semantic_payload(event) -> dict[str, Any] | None:
    payload = dict(event.payload) if event.payload is not None else None
    if event.event_type is EventType.CHECKPOINT_COMMITTED and payload is not None:
        # Checkpoint identity is a per-Run UUID; revision/content are the
        # trace-independent semantics compared across isolated scenarios.
        payload.pop("checkpoint_id", None)
    return payload


async def _snapshot_recovered_run(
    tmp_path: Path,
    *,
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    runtime_path = tmp_path / "runtime.db"
    trace_root = tmp_path / "traces"
    store = SqliteRuntimeStore(RuntimeDatabase(runtime_path))
    await store.initialize()
    release = await store.register_release(
        ReleaseManifest(engine="native_loop", components={"rel-029": "recovery-v1"}),
        activate=True,
    )
    clock = FakeClock()
    run = (await AdmissionService(
        store, clock=clock, default_deadline_ms=60_000,
    ).create(
        CreateRunInput(
            client_request_id=str(uuid.uuid4()),
            conversation_id=None,
            principal_id="demo-user",
            agent_id="demo-agent",
            engine="native_loop",
            text="trace-independent recovery",
            attachment_refs=(),
            deadline_at=None,
        ),
        idempotency_key=f"rel-029-{mode}",
    )).run

    _configure_trace(mode, trace_root)
    set_trace_id(f"rel-029-{mode}")
    writer_failures = 0

    with monkeypatch.context() as scoped:
        if mode == "writer-oserror":
            original_open = Path.open

            def fail_jsonl_open(path: Path, *args: Any, **kwargs: Any):
                nonlocal writer_failures
                if path.suffix == ".jsonl":
                    writer_failures += 1
                    raise OSError("injected span writer failure")
                return original_open(path, *args, **kwargs)

            scoped.setattr(Path, "open", fail_jsonl_open)

        killed = CheckpointThenKilledAdapter(release)
        first_worker = RuntimeWorker(
            store=store,
            coordinator=RunCoordinator(
                store,
                EngineRegistry({"native_loop": killed}),
                clock=clock,
                event_flush_bytes=1,
            ),
            worker_id="worker-before-kill",
            release_map={"native_loop": release},
            concurrency=1,
            lease_ms=1_000,
            clock=clock,
        )
        with pytest.raises(asyncio.CancelledError):
            await first_worker.run_once()
        assert killed.calls == 1

        checkpoint_before_restart = await store.latest_checkpoint(run.envelope.run_id)
        assert checkpoint_before_restart is not None
        assert checkpoint_before_restart.revision == 1
        before_restart = await store.list_events(
            run.envelope.run_id, visibility=None,
        )
        assert any(
            event.event_type is EventType.OUTPUT_DELTA_COMMITTED
            and event.payload == {"delta": "committed-before-worker-loss"}
            for event in before_restart
        )

        if mode == "deleted-file":
            trace_files = list(trace_root.rglob("*.jsonl"))
            assert trace_files, "the first worker attempt must have created a trace file"
            for trace_file in trace_files:
                trace_file.unlink()
            assert not list(trace_root.rglob("*.jsonl"))

        # Simulate process restart.  Trace in-memory state is discarded (and the
        # previous file may be missing), while a new Store reads only runtime.db.
        _configure_trace(mode, trace_root)
        restarted = SqliteRuntimeStore(RuntimeDatabase(runtime_path))
        await restarted.initialize()
        clock.advance(1_001)
        resumed = ResumeFromCheckpointAdapter(release)
        second_worker = RuntimeWorker(
            store=restarted,
            coordinator=RunCoordinator(
                restarted,
                EngineRegistry({"native_loop": resumed}),
                clock=clock,
                event_flush_bytes=1,
            ),
            worker_id="worker-after-restart",
            release_map={"native_loop": release},
            concurrency=1,
            lease_ms=1_000,
            clock=clock,
        )
        # run_once maintenance performs the actual expired-lease recovery before
        # atomically claiming the same Activity with a higher fencing token.
        assert await second_worker.run_once() is True
        assert resumed.calls == 1
        assert await second_worker.run_once() is False

    set_trace_id("-")
    configure_tracing(enabled=False, engine="rel-029-cleanup")
    if mode == "writer-oserror":
        assert writer_failures >= 2

    final = await restarted.get_run(run.envelope.run_id)
    activity = await restarted.get_activity(final.current_activity_id)
    checkpoint = await restarted.latest_checkpoint(run.envelope.run_id)
    assert checkpoint is not None
    public = await restarted.list_events(run.envelope.run_id)
    all_events = await restarted.list_events(run.envelope.run_id, visibility=None)

    # Replay from every public cursor is a pure read of committed rows and is
    # likewise independent from trace availability.
    for cursor in (0, *(event.seq for event in public)):
        replay = await restarted.list_events(run.envelope.run_id, after_seq=cursor)
        assert [event.seq for event in replay] == [
            event.seq for event in public if event.seq > cursor
        ]

    return {
        "status": final.status.value,
        "terminal_status": final.terminal_status.value,
        "terminal_payload": final.terminal_payload,
        "last_seq": final.next_seq - 1,
        "activity": {
            "status": activity.status.value,
            "attempt": activity.attempt,
            "fencing_token": activity.fencing_token,
        },
        "checkpoint": {
            "revision": checkpoint.revision,
            "working_state": checkpoint.working_state.model_dump(mode="json"),
            "engine_state": checkpoint.engine_state,
        },
        "public_events": [
            (event.seq, event.event_type.value, _semantic_payload(event), event.terminal_status)
            for event in public
        ],
        "terminal_events": sum(
            event.event_type is EventType.RUN_TERMINATED for event in all_events
        ),
        "assistant_events": [
            event.payload
            for event in all_events
            if event.event_type is EventType.ASSISTANT_MESSAGE_COMMITTED
        ],
    }


@pytest.mark.asyncio
async def test_rel_29_trace_failures_do_not_change_lease_recovery_checkpoint_or_replay(
    tmp_path,
    monkeypatch,
):
    snapshots = []
    for mode in ("disabled", "writer-oserror", "deleted-file"):
        snapshots.append(await _snapshot_recovered_run(
            tmp_path / mode,
            mode=mode,
            monkeypatch=monkeypatch,
        ))

    assert snapshots[0] == snapshots[1] == snapshots[2]
    snapshot = snapshots[0]
    assert snapshot["status"] == RunStatus.SUCCEEDED
    assert snapshot["terminal_status"] == RunStatus.SUCCEEDED
    assert snapshot["activity"] == {
        "status": "SUCCEEDED",
        "attempt": 2,
        "fencing_token": 2,
    }
    assert snapshot["checkpoint"]["revision"] == 1
    assert snapshot["checkpoint"]["engine_state"]["phase"] == "MODEL_RESPONSE_COMMITTED"
    assert snapshot["terminal_events"] == 1
    assert len(snapshot["assistant_events"]) == 1
