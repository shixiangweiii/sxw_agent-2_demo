from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from agent.runtime.adapters.filesystem_artifact import FilesystemArtifactStore
from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.runtime.application.admission import AdmissionService, CreateRunInput
from agent.runtime.application.tool_broker import ToolBroker
from agent.runtime.domain.models import (
    EventType,
    ReleaseManifest,
    RunStatus,
    ToolEffectClass,
    ToolManifest,
)


@dataclass
class FakeClock:
    value: int = 1_800_000_000_000

    def now_ms(self) -> int:
        return self.value

    def monotonic(self) -> float:
        return self.value / 1000


async def _runtime(tmp_path: Path):
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    clock = FakeClock()
    await store.register_release(
        ReleaseManifest(engine="native_loop", components={"test": "evidence-v1"}),
        activate=True,
    )
    admitted = await AdmissionService(store, clock=clock).create(
        CreateRunInput(
            client_request_id=str(uuid.uuid4()),
            conversation_id=None,
            principal_id="demo-user",
            agent_id="demo-agent",
            engine="native_loop",
            text="question",
            attachment_refs=(),
            deadline_at=None,
        ),
        idempotency_key="evidence-test",
    )
    claim = await store.claim_next(
        worker_id="worker-a", lease_ms=30_000, now_ms=clock.now_ms()
    )
    assert claim is not None
    activity = await store.mark_activity_running(
        claim.activity.activity_id,
        worker_id="worker-a",
        fencing_token=claim.activity.fencing_token,
        now_ms=clock.now_ms(),
    )
    return store, clock, admitted.run, activity


def _manifest() -> ToolManifest:
    return ToolManifest(
        name="knowledge_search",
        release_digest="knowledge-v1",
        effect_class=ToolEffectClass.READ_ONLY,
        timeout_seconds=2,
        max_attempts=2,
        concurrency_safe=True,
    )


@pytest.mark.asyncio
async def test_evidence_artifact_and_citation_finalize_are_traceable(tmp_path: Path) -> None:
    store, clock, run, activity = await _runtime(tmp_path)
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")
    broker = ToolBroker(store, artifacts, clock=clock, inline_result_max_bytes=512)
    full_content = "durable-evidence-" + "x" * 5000
    calls = 0

    async def search(_arguments, _context):
        nonlocal calls
        calls += 1
        return {
            "hits": [{"n": 1, "title": "SQLite", "doc_id": "doc-1", "content": full_content}],
            "count": 1,
            "note": "cite with [n]",
            "__evidence_set__": {
                "schema_version": "1",
                "query": "durability",
                "query_id": "qry-1",
                "retrieval_status": "HIT",
                "rewrites": ["durability"],
                "cost_ms": 12,
                "evidence": [{
                    "n": 1,
                    "evidence_id": "ev-1",
                    "title": "SQLite",
                    "doc_id": "doc-1",
                    "document_id": "doc-internal-1",
                    "document_version_id": "dver-1",
                    "index_version": "dver-1",
                    "content_hash": "hash-1",
                    "dataset_id": "kb",
                    "scope": "public",
                    "query_id": "qry-1",
                    "page": 2,
                    "span_start": 10,
                    "span_end": 20,
                    "content": full_content,
                    "score": 0.9,
                    "source": "fused",
                }],
            },
        }

    broker.register(_manifest(), search)
    result = await broker.execute(
        run_id=run.envelope.run_id,
        parent_activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        logical_key="native:turn:0:call:0",
        tool_name="knowledge_search",
        arguments={"query": "durability"},
        deadline_at_ms=run.envelope.deadline_at,
    )
    replay = await broker.execute(
        run_id=run.envelope.run_id,
        parent_activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        logical_key="native:turn:0:call:0",
        tool_name="knowledge_search",
        arguments={"query": "durability"},
        deadline_at_ms=run.envelope.deadline_at,
    )

    assert result.result_ref
    assert replay == result
    assert calls == 1
    assert "__evidence_set__" not in result.preview
    assert len(json.dumps(result.preview, ensure_ascii=False).encode()) <= 512
    artifact = await artifacts.read_bounded(result.result_ref, max_bytes=16_000)
    evidence_set = json.loads(artifact.data)
    assert evidence_set["evidence"][0]["content"] == full_content
    assert evidence_set["tool_execution_id"]

    async with store.db.read() as conn:
        link = await (await conn.execute(
            "SELECT relation,event_id FROM artifact_links WHERE artifact_id=?",
            (result.result_ref,),
        )).fetchone()
        execution = await (await conn.execute(
            "SELECT result_json FROM tool_executions WHERE run_id=?",
            (run.envelope.run_id,),
        )).fetchone()
    assert link["relation"] == "EVIDENCE_SET"
    committed_result = json.loads(execution["result_json"])
    assert committed_result["evidence_set_ref"] == result.result_ref
    assert committed_result["evidence_index"][0]["document_version_id"] == "dver-1"
    assert full_content not in execution["result_json"]
    committed_events = await store.list_events(run.envelope.run_id, visibility=None)
    tool_result_event = next(
        event for event in committed_events if event.event_type is EventType.TOOL_RESULT_COMMITTED
    )
    assert link["event_id"] == tool_result_event.event_id

    finalized = await store.finalize_success(
        run_id=run.envelope.run_id,
        activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        assistant_text="SQLite provides durable commits [1].",
        citations=[{"malicious_request_local_citation": True}],
        now_ms=clock.now_ms(),
    )
    assert finalized.status is RunStatus.SUCCEEDED
    events = await store.list_events(run.envelope.run_id, visibility=None)
    citation_event = next(
        event for event in events if event.event_type is EventType.CITATION_SET_COMMITTED
    )
    citations = citation_event.payload["citations"]
    assert len(citations) == 1
    assert citations[0]["evidence_id"] == "ev-1"
    assert citations[0]["evidence_set_ref"] == result.result_ref
    assert citations[0]["document_version_id"] == "dver-1"
    assert "malicious_request_local_citation" not in str(citations)
    terminal = next(event for event in events if event.event_type is EventType.RUN_TERMINATED)
    assert terminal.payload["citation_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("assistant_text", ["No evidence was found.", "Unbacked marker [1]."])
async def test_no_committed_evidence_produces_empty_citation_set(
    tmp_path: Path, assistant_text: str
) -> None:
    store, clock, run, activity = await _runtime(tmp_path)
    finalized = await store.finalize_success(
        run_id=run.envelope.run_id,
        activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        assistant_text=assistant_text,
        citations=[{"must": "be ignored"}],
        now_ms=clock.now_ms(),
    )
    assert finalized.status is RunStatus.SUCCEEDED
    events = await store.list_events(run.envelope.run_id, visibility=None)
    citation_event = next(
        event for event in events if event.event_type is EventType.CITATION_SET_COMMITTED
    )
    assert citation_event.payload == {"citations": []}
