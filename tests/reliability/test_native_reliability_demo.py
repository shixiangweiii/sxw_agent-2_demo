from __future__ import annotations

import uuid

import pytest

from agent.runtime.adapters.filesystem_artifact import FilesystemArtifactStore
from agent.runtime.adapters.native_reliability_demo import DemoEffectsStore, NativeReliabilityDemoAdapter
from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.runtime.application.admission import AdmissionService, CreateRunInput
from agent.runtime.application.coordinator import EngineRegistry, RunCoordinator
from agent.runtime.application.tool_broker import ToolBroker
from agent.runtime.domain.models import ReleaseManifest, RunStatus, sha256_json


@pytest.mark.asyncio
async def test_r3_native_retry_checkpoint_signal_effect_artifact_end_to_end(tmp_path):
    db_path = tmp_path / "runtime.db"
    store = SqliteRuntimeStore(RuntimeDatabase(db_path))
    await store.initialize()
    release = await store.register_release(
        ReleaseManifest(engine="native_loop", components={"demo": "v1"}), activate=True,
    )
    admitted = await AdmissionService(store).create(
        CreateRunInput(
            client_request_id=str(uuid.uuid4()), conversation_id=None,
            principal_id="demo-user", agent_id="demo-agent", engine="native_loop",
            text="/reliability-demo", attachment_refs=(), deadline_at=None,
        ),
        idempotency_key="native-demo",
    )
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")
    adapter = NativeReliabilityDemoAdapter(
        release_fingerprint=release,
        tool_broker=ToolBroker(store, artifact_store, inline_result_max_bytes=512),
        effects=DemoEffectsStore(tmp_path / "effects.db"),
    )
    coordinator = RunCoordinator(store, EngineRegistry({"native_loop": adapter}))
    claim = await store.claim_next(
        worker_id="worker-before-wait", lease_ms=30_000,
        now_ms=admitted.run.envelope.created_at,
    )
    assert claim is not None
    assert await coordinator.execute_claim(claim, worker_id="worker-before-wait") is RunStatus.WAITING_INPUT
    waiting = await store.get_run(admitted.run.envelope.run_id)
    assert waiting.pending_input["type"] == "APPROVAL"
    checkpoint = await store.latest_checkpoint(admitted.run.envelope.run_id)
    assert checkpoint is not None
    assert checkpoint.engine_state["phase"] == "WAITING_APPROVAL"

    signal_payload = {"approved": True}
    signal_digest = sha256_json({
        "type": "approval", "payload": signal_payload,
        "wait_activity_id": waiting.current_activity_id,
    })
    consumed = await store.submit_signal(
        run_id=waiting.envelope.run_id, signal_id="approval-1",
        wait_activity_id=waiting.current_activity_id, signal_type="approval",
        payload=signal_payload, payload_digest=signal_digest,
        now_ms=waiting.updated_at + 1,
    )
    assert consumed.status == "CONSUMED"

    # Simulate process death after signal commit and before resume: rebuild Store,
    # Broker, adapter and Coordinator entirely from disk.
    restarted = SqliteRuntimeStore(RuntimeDatabase(db_path))
    await restarted.initialize()
    restarted_adapter = NativeReliabilityDemoAdapter(
        release_fingerprint=release,
        tool_broker=ToolBroker(restarted, artifact_store, inline_result_max_bytes=512),
        effects=DemoEffectsStore(tmp_path / "effects.db"),
    )
    restarted_coordinator = RunCoordinator(
        restarted, EngineRegistry({"native_loop": restarted_adapter}),
    )
    resumed = await restarted.claim_next(
        worker_id="worker-after-restart", lease_ms=30_000,
        now_ms=waiting.updated_at + 2,
    )
    assert resumed is not None
    assert await restarted_coordinator.execute_claim(
        resumed, worker_id="worker-after-restart",
    ) is RunStatus.SUCCEEDED
    terminal = await restarted.get_run(waiting.envelope.run_id)
    assert terminal.terminal_status is RunStatus.SUCCEEDED
    final_checkpoint = await restarted.latest_checkpoint(waiting.envelope.run_id)
    assert final_checkpoint.revision == 2
    assert len(final_checkpoint.working_state.artifact_refs) == 1

    async with restarted.db.read() as conn:
        tool = await (await conn.execute(
            "SELECT attempt,effect_status FROM tool_executions WHERE tool_name='slow_lookup'"
        )).fetchone()
        artifacts = await (await conn.execute(
            "SELECT COUNT(*) AS n FROM artifact_metadata"
        )).fetchone()
    assert tool["attempt"] == 3
    assert tool["effect_status"] == "COMMITTED"
    assert artifacts["n"] == 1

    import aiosqlite
    async with aiosqlite.connect(tmp_path / "effects.db") as conn:
        count = await (await conn.execute("SELECT COUNT(*) FROM demo_tasks")).fetchone()
    assert count[0] == 1

    replay = await restarted.submit_signal(
        run_id=waiting.envelope.run_id, signal_id="approval-1",
        wait_activity_id=waiting.current_activity_id, signal_type="approval",
        payload=signal_payload, payload_digest=signal_digest,
        now_ms=terminal.updated_at + 1,
    )
    assert replay.reused is True

