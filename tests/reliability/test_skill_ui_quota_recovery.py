from __future__ import annotations

from tests.reliability.support.runtime_releases import activate_test_release

import json
import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.runtime.application.admission import AdmissionService, CreateRunInput
from agent.runtime.application.events import CommittedEventSink
from agent.runtime.domain.errors import RuntimeFault
from agent.runtime.domain.models import EventType, ReleaseManifest


@dataclass
class _Clock:
    value: int = 2_360_000_000_000

    def now_ms(self) -> int:
        return self.value

    def monotonic(self) -> float:
        return self.value / 1000

    def advance(self, milliseconds: int) -> None:
        self.value += milliseconds


def _payload_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8"))


async def _attempt_environment(tmp_path):
    clock = _Clock()
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    release = await activate_test_release(store,
        ReleaseManifest(
            engine="native_loop",
            components={"skill-ui-quota-test": "current"},
        ),
    )
    run = (await AdmissionService(
        store,
        clock=clock,
        default_deadline_ms=60_000,
    ).create(
        CreateRunInput(
            client_request_id=str(uuid.uuid4()),
            conversation_id=None,
            principal_id="demo-user",
            agent_id="demo-agent",
            engine="native_loop",
            text="stream skill progress",
            attachment_refs=(),
            deadline_at=None,
        ),
        idempotency_key=str(uuid.uuid4()),
    )).run
    claim = await store.claim_next(
        worker_id="skill-quota-worker-1",
        lease_ms=30_000,
        now_ms=clock.now_ms(),
        release_map={"native_loop": release},
    )
    assert claim is not None
    first = await store.mark_activity_running(
        claim.activity.activity_id,
        worker_id="skill-quota-worker-1",
        fencing_token=claim.activity.fencing_token,
        now_ms=clock.now_ms(),
    )
    return store, clock, release, run, first


def _sink(
    store: SqliteRuntimeStore,
    clock: _Clock,
    run: Any,
    activity: Any,
    *,
    max_event_bytes: int = 64 * 1024,
    max_events: int,
    max_total_bytes: int,
) -> CommittedEventSink:
    return CommittedEventSink(
        store,
        run_id=run.envelope.run_id,
        activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        deadline_at_ms=run.envelope.deadline_at,
        clock=clock,
        max_skill_event_bytes=max_event_bytes,
        max_skill_events=max_events,
        max_skill_event_total_bytes=max_total_bytes,
    )


async def _retry_attempt(
    store: SqliteRuntimeStore,
    clock: _Clock,
    release: str,
    run: Any,
    activity: Any,
) -> Any:
    await store.schedule_retry(
        run_id=run.envelope.run_id,
        activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        fire_at=clock.now_ms() + 1_000,
        error={"code": "TEST_RETRY", "retryable": True},
        now_ms=clock.now_ms(),
    )
    clock.advance(1_000)
    assert await store.fire_due_timers(now_ms=clock.now_ms()) == 1
    claim = await store.claim_next(
        worker_id="skill-quota-worker-2",
        lease_ms=30_000,
        now_ms=clock.now_ms(),
        release_map={"native_loop": release},
    )
    assert claim is not None
    return await store.mark_activity_running(
        claim.activity.activity_id,
        worker_id="skill-quota-worker-2",
        fencing_token=claim.activity.fencing_token,
        now_ms=clock.now_ms(),
    )


@pytest.mark.asyncio
async def test_skill_ui_event_count_quota_accumulates_from_prior_attempt_events(
    tmp_path,
) -> None:
    store, clock, release, run, first = await _attempt_environment(tmp_path)
    first_sink = _sink(
        store, clock, run, first, max_events=2, max_total_bytes=1_000_000,
    )
    await first_sink.emit("skill_event", {
        "dataType": "progress",
        "data": {"step": 1},
        "isThinking": True,
    })
    await first_sink.close()

    second = await _retry_attempt(store, clock, release, run, first)
    second_sink = _sink(
        store, clock, run, second, max_events=2, max_total_bytes=1_000_000,
    )
    await second_sink.emit("skill_event", {
        "dataType": "progress",
        "data": {"step": 2},
        "isThinking": True,
    })
    with pytest.raises(RuntimeFault) as exceeded:
        await second_sink.emit("skill_event", {
            "dataType": "progress",
            "data": {"step": 3},
            "isThinking": True,
        })
    assert exceeded.value.code == "SKILL_UI_LIMIT_EXCEEDED"
    await second_sink.close()

    events = await store.list_events(run.envelope.run_id, visibility=None)
    skill_events = [
        event for event in events
        if event.event_type is EventType.SKILL_UI_FRAME_COMMITTED
    ]
    assert len(skill_events) == 2
    assert [event.payload["data"]["step"] for event in skill_events] == [1, 2]


@pytest.mark.asyncio
async def test_skill_ui_byte_quota_accumulates_from_prior_attempt_events(
    tmp_path,
) -> None:
    store, clock, release, run, first = await _attempt_environment(tmp_path)
    first_payload = {
        "dataType": "progress",
        "data": {"text": "一" * 40},
        "isThinking": True,
    }
    second_payload = {
        "dataType": "progress",
        "data": {"text": "二" * 40},
        "isThinking": True,
    }
    total_limit = _payload_size(first_payload) + _payload_size(second_payload) - 1
    first_sink = _sink(
        store, clock, run, first, max_events=100, max_total_bytes=total_limit,
    )
    await first_sink.emit("skill_event", first_payload)
    await first_sink.close()

    second = await _retry_attempt(store, clock, release, run, first)
    second_sink = _sink(
        store, clock, run, second, max_events=100, max_total_bytes=total_limit,
    )
    with pytest.raises(RuntimeFault) as exceeded:
        await second_sink.emit("skill_event", second_payload)
    assert exceeded.value.code == "SKILL_UI_LIMIT_EXCEEDED"
    await second_sink.close()

    events = await store.list_events(run.envelope.run_id, visibility=None)
    skill_events = [
        event for event in events
        if event.event_type is EventType.SKILL_UI_FRAME_COMMITTED
    ]
    assert len(skill_events) == 1
    assert skill_events[0].payload == first_payload


@pytest.mark.asyncio
async def test_skill_ui_rejects_one_oversized_utf8_frame_without_committing_it(
    tmp_path,
) -> None:
    store, clock, _release, run, activity = await _attempt_environment(tmp_path)
    payload = {
        "dataType": "progress",
        "data": {"text": "进"},
        "isThinking": True,
    }
    size = _payload_size(payload)
    sink = _sink(
        store,
        clock,
        run,
        activity,
        max_event_bytes=size - 1,
        max_events=100,
        max_total_bytes=1_000_000,
    )

    with pytest.raises(RuntimeFault) as exceeded:
        await sink.emit("skill_event", payload)

    assert exceeded.value.code == "SKILL_UI_LIMIT_EXCEEDED"
    await sink.close()
    events = await store.list_events(run.envelope.run_id, visibility=None)
    assert all(
        event.event_type is not EventType.SKILL_UI_FRAME_COMMITTED
        for event in events
    )
