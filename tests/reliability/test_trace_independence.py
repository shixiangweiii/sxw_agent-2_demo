from __future__ import annotations

from tests.reliability.support.runtime_releases import activate_test_release

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
from common.obs import get_trace_id, set_trace_id
from common.trace import configure_tracing, get_trace


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
    release = await activate_test_release(store,
        ReleaseManifest(engine="native_loop", components={"rel-029": "recovery-v1"}),
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


class CompletingAdapter:
    """最小可完成引擎：本用例只关心 span 落在哪条 trace 上，不关心引擎语义。"""

    name = "native_loop"

    def __init__(self, release: str) -> None:
        self.release_fingerprint = release

    async def execute(self, request, io):
        await io.emit("text", {"delta": "ok"})
        return EngineOutcome(kind=EngineOutcomeKind.COMPLETED)


async def _admit(store, clock, *, trace_id: str, key: str):
    return (await AdmissionService(
        store, clock=clock, default_deadline_ms=60_000,
    ).create(
        CreateRunInput(
            client_request_id=str(uuid.uuid4()),
            conversation_id=None,
            principal_id="demo-user",
            agent_id="demo-agent",
            engine="native_loop",
            text="trace propagation",
            attachment_refs=(),
            deadline_at=None,
            trace_id=trace_id,
        ),
        idempotency_key=key,
    )).run


@pytest.mark.asyncio
async def test_rel_29_worker_recovers_the_admitted_trace_id_across_the_process_boundary(
    tmp_path,
):
    """Worker 进程没有 HTTP 中间件，trace_id 必须从持久化的 Run 上接力。

    回归的是重构把执行搬出 API 进程后引入的断裂：当时 Worker 侧 `get_trace_id()`
    恒为默认 "-"，于是（a）所有 Run 的 span 挤进同一条轨迹和同一个文件、
    （b）`GET /api/v1/traces/{trace_id}` 对每个 Run 都 404、
    （c）内存里那条 "-" 记录永不被 ring buffer 淘汰。

    ★ 本用例**刻意不调用 `set_trace_id`**——那正是旧用例掩盖住这个洞的原因。
    """
    runtime_path = tmp_path / "runtime.db"
    trace_root = tmp_path / "traces"
    store = SqliteRuntimeStore(RuntimeDatabase(runtime_path))
    await store.initialize()
    release = await activate_test_release(store,
        ReleaseManifest(engine="native_loop", components={"rel-029": "propagation-v1"}),
    )
    clock = FakeClock()

    carried = await _admit(store, clock, trace_id="rel-029-carried", key="rel-029-carried")
    anonymous = await _admit(store, clock, trace_id="", key="rel-029-anonymous")
    # 诊断字段必须持久化，否则跨不过 API→DB→Worker 这道进程边界。
    assert (await store.get_run(carried.envelope.run_id)).trace_id == "rel-029-carried"
    assert (await store.get_run(anonymous.envelope.run_id)).trace_id == ""

    _configure_trace("propagation", trace_root)
    worker = RuntimeWorker(
        store=store,
        coordinator=RunCoordinator(
            store,
            EngineRegistry({"native_loop": CompletingAdapter(release)}),
            clock=clock,
            event_flush_bytes=1,
        ),
        worker_id="worker-propagation",
        release_map={"native_loop": release},
        concurrency=1,
        lease_ms=60_000,
        clock=clock,
    )
    # run_once 是**直接 await** 的确定性入口（不像 run() 那样每个 claim 各起一个
    # task），所以它同时也是"绑定 trace_id 必须按 token 还原"的证据。
    assert await worker.run_once() is True
    assert get_trace_id() == "-", "worker 不得把 Run 的 trace_id 泄漏给调用方上下文"
    assert await worker.run_once() is True
    assert get_trace_id() == "-"
    assert await worker.run_once() is False

    # 1) 客户端带来的 trace_id 可按原值查回——这正是 API/eval harness 的取回路径。
    carried_trace = get_trace("rel-029-carried")
    assert carried_trace is not None, "worker 的 span 必须落在 admission 观察到的 trace 上"
    assert {span["trace_id"] for span in carried_trace["spans"]} == {"rel-029-carried"}
    engine_spans = [s for s in carried_trace["spans"] if s["kind"] == "engine"]
    assert [s["attributes"]["run_id"] for s in engine_spans] == [carried.envelope.run_id]

    # 事件旁路：CommittedEventSink 是三代引擎唯一共同出口，收尾字段挂在 engine
    # span 上，eval 的失败归因规则（trace_signals）正是读这些字段。
    rollup = engine_spans[0]["attributes"]
    assert rollup["finish_reason"] == "COMPLETED"
    assert rollup["event_counts"] == {"text": 1}
    assert rollup["had_error"] is False
    assert rollup["answer_chars"] == 2
    assert rollup["ttft_ms"] is not None

    # 2) 没带 x-trace-id 时回落到 run_id，仍然是每个 Run 一个唯一可查的键。
    fallback_trace = get_trace(anonymous.envelope.run_id)
    assert fallback_trace is not None
    assert {span["trace_id"] for span in fallback_trace["spans"]} == {
        anonymous.envelope.run_id
    }

    # 3) 两个 Run 不得共用一条 trace 或一个文件（旧行为下二者都塌进 "-"）。
    assert get_trace("-") is None
    assert carried_trace["trace_files"] != fallback_trace["trace_files"]
    assert len(list(trace_root.rglob("*.jsonl"))) == 2

    configure_tracing(enabled=False, engine="rel-029-cleanup")


@pytest.mark.asyncio
async def test_rel_29_one_trace_id_spanning_a_worker_restart_reads_back_as_one_trace(
    tmp_path,
):
    """Worker 重启后重试同一个 Run，两次 attempt 必须合成一条轨迹读回来。

    trace_id 取自持久化的 Run，跨重启不变；但内存 ring 被清空后会**新开一个文件**。
    只读最新的那个就会静默丢掉前一次 attempt 的全部 span——恰恰是排查"为什么重试"
    时最想看的那一段。
    """
    runtime_path = tmp_path / "runtime.db"
    trace_root = tmp_path / "traces"
    store = SqliteRuntimeStore(RuntimeDatabase(runtime_path))
    await store.initialize()
    release = await activate_test_release(store,
        ReleaseManifest(engine="native_loop", components={"rel-029": "merge-v1"}),
    )
    clock = FakeClock()
    run = await _admit(store, clock, trace_id="rel-029-merged", key="rel-029-merged")

    _configure_trace("merge", trace_root)
    first = RuntimeWorker(
        store=store,
        coordinator=RunCoordinator(
            store, EngineRegistry({"native_loop": CheckpointThenKilledAdapter(release)}),
            clock=clock, event_flush_bytes=1,
        ),
        worker_id="worker-before-restart",
        release_map={"native_loop": release},
        concurrency=1, lease_ms=1_000, clock=clock,
    )
    with pytest.raises(asyncio.CancelledError):
        await first.run_once()

    # 模拟进程重启：ring 清空、序号重置，同一个 trace_id 会落到第二个文件。
    _configure_trace("merge", trace_root)
    restarted = SqliteRuntimeStore(RuntimeDatabase(runtime_path))
    await restarted.initialize()
    clock.advance(1_001)
    second = RuntimeWorker(
        store=restarted,
        coordinator=RunCoordinator(
            restarted, EngineRegistry({"native_loop": ResumeFromCheckpointAdapter(release)}),
            clock=clock, event_flush_bytes=1,
        ),
        worker_id="worker-after-restart",
        release_map={"native_loop": release},
        concurrency=1, lease_ms=1_000, clock=clock,
    )
    assert await second.run_once() is True

    files = sorted(trace_root.rglob("*.jsonl"))
    assert len(files) == 2, "重启后应当写出第二个文件"

    trace = get_trace("rel-029-merged")
    assert trace is not None
    assert len(trace["trace_files"]) == 2
    engine_spans = [s for s in trace["spans"] if s["kind"] == "engine"]
    # 两次 attempt 都在：被 kill 的那次是 cancelled，恢复的那次 COMPLETED。
    assert [s["attributes"]["attempt"] for s in engine_spans] == [1, 2]
    assert engine_spans[0]["status"] == "cancelled"
    assert engine_spans[1]["attributes"]["finish_reason"] == "COMPLETED"
    # span_id 去重后不应有重复（内存 ring 与它自己的文件是同一批 span）。
    ids = [s["span_id"] for s in trace["spans"]]
    assert len(ids) == len(set(ids))

    configure_tracing(enabled=False, engine="rel-029-cleanup")


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
