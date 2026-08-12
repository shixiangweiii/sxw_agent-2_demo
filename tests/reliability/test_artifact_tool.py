from __future__ import annotations

from tests.reliability.support.runtime_releases import activate_test_release

import json
import uuid

import pytest

from agent.runtime.adapters.artifact_tools import build_read_artifact_tool
from agent.runtime.adapters.filesystem_artifact import FilesystemArtifactStore
from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.runtime.application.admission import AdmissionService, CreateRunInput
from agent.runtime.application.tool_broker import ToolBroker
from agent.runtime.domain.artifact import ArtifactPurpose
from agent.runtime.domain.models import (
    EventType,
    ReleaseManifest,
    ToolEffectClass,
    ToolManifest,
)


@pytest.mark.asyncio
async def test_bounded_artifact_read_is_materialized_for_model_but_not_copied_to_ledger(tmp_path):
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    await activate_test_release(store,
        ReleaseManifest(engine="native_loop", components={"artifact-read": "v1"}),
    )
    run = (await AdmissionService(store).create(
        CreateRunInput(
            client_request_id=str(uuid.uuid4()),
            conversation_id=None,
            principal_id="demo-user",
            agent_id="demo-agent",
            engine="native_loop",
            text="read the artifact",
            attachment_refs=(),
            deadline_at=None,
        ),
        idempotency_key="artifact-read-run",
    )).run
    claim = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="artifact-worker",
        lease_ms=30_000,
        now_ms=run.envelope.created_at,
    )
    assert claim is not None
    activity = await store.mark_activity_running(
        claim.activity.activity_id,
        worker_id="artifact-worker",
        fencing_token=claim.activity.fencing_token,
        now_ms=run.envelope.created_at,
    )

    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")
    content = ("bounded-artifact-content\n" * 1_000).encode()
    ref = await artifacts.put_bytes(
        content,
        purpose=ArtifactPurpose.UPLOAD,
        media_type="text/plain",
        filename="large.txt",
    )
    await store.register_artifact_metadata(
        artifact_id=ref.artifact_id,
        sha256=ref.digest_sha256,
        size_bytes=ref.size_bytes,
        media_type=ref.media_type,
        storage_path=f"sha256/{ref.artifact_id[:2]}/{ref.artifact_id}",
        created_at=int(ref.created_at.timestamp() * 1000),
    )

    broker = ToolBroker(store, artifacts, inline_result_max_bytes=512)
    read_artifact = build_read_artifact_tool(artifacts, store.get_artifact_metadata)

    async def invoke_read_artifact(arguments, _context):
        return await read_artifact(**arguments)

    broker.register(ToolManifest(
        name="read_artifact",
        release_digest="artifact-read-v1",
        effect_class=ToolEffectClass.READ_ONLY,
        timeout_seconds=2,
        max_attempts=2,
        result_policy="ARTIFACT_BOUNDED_READ",
        concurrency_safe=True,
    ), invoke_read_artifact)
    arguments = {"artifact_id": ref.artifact_id, "offset": 0, "max_bytes": len(content)}
    first = await broker.execute(
        run_id=run.envelope.run_id,
        parent_activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        logical_key="artifact-read:0",
        tool_name="read_artifact",
        arguments=arguments,
        deadline_at_ms=run.envelope.deadline_at,
    )
    replay = await broker.execute(
        run_id=run.envelope.run_id,
        parent_activity_id=activity.activity_id,
        fencing_token=activity.fencing_token,
        logical_key="artifact-read:0",
        tool_name="read_artifact",
        arguments=arguments,
        deadline_at_ms=run.envelope.deadline_at,
    )

    assert first.preview["content"].encode() == content
    assert replay.preview == first.preview
    assert first.result_ref == ref.artifact_id
    events = await store.list_events(run.envelope.run_id, visibility=None)
    assert sum(event.event_type is EventType.TOOL_CALL_COMMITTED for event in events) == 1
    assert sum(event.event_type is EventType.TOOL_RESULT_COMMITTED for event in events) == 1
    async with store.db.read() as conn:
        execution = await (await conn.execute(
            "SELECT result_json,attempt FROM tool_executions WHERE run_id=?",
            (run.envelope.run_id,),
        )).fetchone()
        link = await (await conn.execute(
            "SELECT relation,event_id FROM artifact_links WHERE artifact_id=?",
            (ref.artifact_id,),
        )).fetchone()
    persisted = json.loads(execution["result_json"])
    assert execution["attempt"] == 1
    assert len(str(persisted["preview"])) < len(content)
    assert link["relation"] == "ARTIFACT_READ"
    assert link["event_id"]
