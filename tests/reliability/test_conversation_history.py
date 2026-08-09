from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.runtime.application.admission import AdmissionService, CreateRunInput
from agent.runtime.domain.models import ReleaseManifest


@dataclass
class FixedClock:
    value: int = 1_900_000_000_000

    def now_ms(self) -> int:
        return self.value

    def monotonic(self) -> float:
        return self.value / 1000


@pytest.mark.asyncio
async def test_canonical_history_uses_conversation_turn_sequence_not_wall_clock_or_uuid(tmp_path):
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    await store.register_release(
        ReleaseManifest(engine="native_loop", components={"test": "history-v1"}),
        activate=True,
    )
    clock = FixedClock()
    admission = AdmissionService(store, clock=clock, default_deadline_ms=60_000)

    async def create(text: str, key: str, conversation_id: str | None):
        return (await admission.create(
            CreateRunInput(
                client_request_id=str(uuid.uuid4()),
                conversation_id=conversation_id,
                principal_id="demo-user",
                agent_id="demo-agent",
                engine="native_loop",
                text=text,
                attachment_refs=(),
                deadline_at=None,
            ),
            idempotency_key=key,
        )).run

    async def succeed(run, answer: str) -> None:
        claim = await store.claim_next(
            worker_id=f"worker-{answer}", lease_ms=30_000, now_ms=clock.now_ms()
        )
        assert claim is not None
        activity = await store.mark_activity_running(
            claim.activity.activity_id,
            worker_id=f"worker-{answer}",
            fencing_token=claim.activity.fencing_token,
            now_ms=clock.now_ms(),
        )
        await store.finalize_success(
            run_id=run.envelope.run_id,
            activity_id=activity.activity_id,
            fencing_token=activity.fencing_token,
            assistant_text=answer,
            citations=[],
            now_ms=clock.now_ms(),
        )

    first = await create("question one", "history-1", None)
    await succeed(first, "answer one")
    second = await create("question two", "history-2", first.envelope.conversation_id)
    await succeed(second, "answer two")
    third = await create("question three", "history-3", first.envelope.conversation_id)

    assert await store.compile_history(third.envelope.run_id) == [
        {"role": "user", "text": "question one", "attachment_refs": []},
        {"role": "assistant", "text": "answer one"},
        {"role": "user", "text": "question two", "attachment_refs": []},
        {"role": "assistant", "text": "answer two"},
    ]
    async with store.db.read() as conn:
        rows = await (await conn.execute(
            "SELECT turn_seq FROM runs WHERE conversation_id=? ORDER BY turn_seq",
            (first.envelope.conversation_id,),
        )).fetchall()
    assert [row["turn_seq"] for row in rows] == [1, 2, 3]
