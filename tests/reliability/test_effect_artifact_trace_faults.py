from __future__ import annotations

from tests.reliability.support.runtime_releases import activate_test_release

import asyncio
import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent.engine.native_loop.executor import execute_one
from agent.engine.native_loop.messages import ToolCall
from agent.engine.native_loop.tools import ToolRegistry, ToolSpec
from agent.runtime.adapters.filesystem_artifact import FilesystemArtifactStore
from agent.runtime.adapters.adk_engines import AdkEngineAdapter
from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.runtime.api.artifacts import router as artifact_router
from agent.runtime.application.admission import AdmissionService, CreateRunInput
from agent.runtime.application.tool_broker import ToolBroker
from agent.runtime.domain.artifact import ArtifactIntegrityError
from agent.runtime.domain.errors import RuntimeFault
from agent.runtime.domain.models import (
    ReleaseManifest,
    ToolEffectClass,
    ToolManifest,
    ToolResultEnvelope,
    ToolResultStatus,
)
from common.obs import set_trace_id
from common.trace import configure_tracing


@dataclass
class FakeClock:
    value: int = 1_800_000_000_000

    def now_ms(self) -> int:
        return self.value

    def monotonic(self) -> float:
        return self.value / 1000


async def _running_parent(tmp_path: Path):
    clock = FakeClock()
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    await activate_test_release(store,
        ReleaseManifest(engine="native_loop", components={"fault-boundary": "v1"}),
    )
    run = (
        await AdmissionService(
            store, clock=clock, default_deadline_ms=60_000
        ).create(
            CreateRunInput(
                client_request_id=str(uuid.uuid4()),
                conversation_id=None,
                principal_id="demo-user",
                agent_id="demo-agent",
                engine="native_loop",
                text="exercise effect fault boundary",
                attachment_refs=(),
                deadline_at=None,
            ),
            idempotency_key=str(uuid.uuid4()),
        )
    ).run
    claim = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="fault-worker",
        lease_ms=30_000,
        now_ms=clock.now_ms(),
    )
    assert claim is not None
    parent = await store.mark_activity_running(
        claim.activity.activity_id,
        worker_id="fault-worker",
        fencing_token=claim.activity.fencing_token,
        now_ms=clock.now_ms(),
    )
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")
    return store, clock, run, parent, artifacts


def _artifact_app(
    store: SqliteRuntimeStore, artifacts: FilesystemArtifactStore
) -> FastAPI:
    app = FastAPI()
    app.state.runtime_store = store
    app.state.artifact_store = artifacts
    app.state.settings = SimpleNamespace()
    app.include_router(artifact_router)

    @app.exception_handler(RuntimeFault)
    async def handle_runtime_fault(_request: Request, exc: RuntimeFault):
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    return app


@pytest.mark.asyncio
async def test_rel_18_fi_06_external_success_then_runtime_commit_loss_reconciles_without_redispatch(
    tmp_path: Path,
) -> None:
    store, clock, run, parent, artifacts = await _running_parent(tmp_path)
    external_db = tmp_path / "external-effects.db"
    with sqlite3.connect(external_db) as conn:
        conn.execute(
            "CREATE TABLE effects (idempotency_key TEXT PRIMARY KEY, external_id TEXT NOT NULL)"
        )

    dispatches = 0
    reconciles = 0

    async def create_effect(_arguments, context):
        nonlocal dispatches
        dispatches += 1
        external_id = "external-task-1"
        with sqlite3.connect(external_db) as conn:
            conn.execute(
                "INSERT INTO effects(idempotency_key,external_id) VALUES (?,?)",
                (context.idempotency_key, external_id),
            )
        return ToolResultEnvelope(
            status=ToolResultStatus.SUCCESS,
            preview={"external_id": external_id},
            external_object_id=external_id,
        )

    async def reconcile_effect(context):
        nonlocal reconciles
        reconciles += 1
        with sqlite3.connect(external_db) as conn:
            row = conn.execute(
                "SELECT external_id FROM effects WHERE idempotency_key=?",
                (context.idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return ToolResultEnvelope(
            status=ToolResultStatus.SUCCESS,
            preview={"external_id": row[0], "reconciled": True},
            external_object_id=row[0],
        )

    broker = ToolBroker(store, artifacts, clock=clock)
    broker.register(
        ToolManifest(
            name="create_external_task",
            release_digest="external-task-v1",
            effect_class=ToolEffectClass.IDEMPOTENT_EFFECT,
            timeout_seconds=1,
            max_attempts=2,
            supports_idempotency=True,
            supports_reconcile=True,
        ),
        create_effect,
        reconcile=reconcile_effect,
    )

    original_settle = store.settle_tool_execution
    lose_first_commit = True

    async def injected_settle(**kwargs):
        nonlocal lose_first_commit
        if lose_first_commit and kwargs["effect_status"] == "COMMITTED":
            lose_first_commit = False
            raise sqlite3.OperationalError(
                "injected kill after external success before Runtime result commit"
            )
        return await original_settle(**kwargs)

    store.settle_tool_execution = injected_settle  # type: ignore[method-assign]
    call = dict(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="external-task:0",
        tool_name="create_external_task",
        arguments={"title": "durable demo"},
        deadline_at_ms=run.envelope.deadline_at,
    )

    with pytest.raises(sqlite3.OperationalError, match="external success"):
        await broker.execute(**call)

    async with store.db.read() as conn:
        uncertain = await (
            await conn.execute(
                "SELECT * FROM tool_executions WHERE run_id=?",
                (run.envelope.run_id,),
            )
        ).fetchone()
    assert uncertain["effect_status"] == "DISPATCHED"
    assert uncertain["result_json"] is None
    with sqlite3.connect(external_db) as conn:
        assert conn.execute("SELECT count(*) FROM effects").fetchone()[0] == 1

    result = await broker.execute(**call)

    assert result.status is ToolResultStatus.SUCCESS
    assert result.external_object_id == "external-task-1"
    assert result.preview["reconciled"] is True
    assert dispatches == 1
    assert reconciles == 1
    with sqlite3.connect(external_db) as conn:
        assert conn.execute("SELECT count(*) FROM effects").fetchone()[0] == 1
    settled = await store.get_tool_execution(uncertain["tool_execution_id"])
    assert settled["effect_status"] == "COMMITTED"
    assert settled["attempt"] == 1


@pytest.mark.asyncio
async def test_rel_19_non_idempotent_effect_never_retries_an_uncertain_dispatch(
    tmp_path: Path,
) -> None:
    store, clock, run, parent, artifacts = await _running_parent(tmp_path)
    calls = 0

    async def non_idempotent_effect(_arguments, _context):
        nonlocal calls
        calls += 1
        raise ConnectionError("outcome unknown after dispatch")

    broker = ToolBroker(store, artifacts, clock=clock)
    broker.register(
        ToolManifest(
            name="charge_once",
            release_digest="charge-once-v1",
            effect_class=ToolEffectClass.NON_IDEMPOTENT_EFFECT,
            timeout_seconds=1,
            max_attempts=7,
        ),
        non_idempotent_effect,
    )
    call = dict(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="charge:0",
        tool_name="charge_once",
        arguments={"amount": 100},
        deadline_at_ms=run.envelope.deadline_at,
    )

    first = await broker.execute(**call)
    replay = await broker.execute(**call)

    assert first.status is replay.status is ToolResultStatus.UNKNOWN
    assert first.error_code == replay.error_code == "TOOL_EFFECT_UNKNOWN"
    assert calls == 1
    async with store.db.read() as conn:
        execution = await (
            await conn.execute(
                "SELECT effect_class,effect_status,attempt FROM tool_executions WHERE run_id=?",
                (run.envelope.run_id,),
            )
        ).fetchone()
    assert dict(execution) == {
        "effect_class": "NON_IDEMPOTENT_EFFECT",
        "effect_status": "MANUAL_REQUIRED",
        "attempt": 1,
    }


@pytest.mark.asyncio
async def test_fi_05_kill_while_read_only_executor_is_running_recovers_once(
    tmp_path: Path,
) -> None:
    store, clock, run, parent, artifacts = await _running_parent(tmp_path)
    entered_executor = asyncio.Event()
    calls = 0

    async def interrupted_read(_arguments, _context):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered_executor.set()
            await asyncio.Event().wait()
        return {"value": "recovered exactly once"}

    broker = ToolBroker(store, artifacts, clock=clock)
    broker.register(
        ToolManifest(
            name="interrupted_read",
            release_digest="interrupted-read-v1",
            effect_class=ToolEffectClass.READ_ONLY,
            timeout_seconds=30,
            max_attempts=2,
            concurrency_safe=True,
        ),
        interrupted_read,
    )
    call = dict(
        run_id=run.envelope.run_id,
        parent_activity_id=parent.activity_id,
        fencing_token=parent.fencing_token,
        logical_key="interrupted-read:0",
        tool_name="interrupted_read",
        arguments={"query": "x"},
        deadline_at_ms=run.envelope.deadline_at,
    )
    executing = asyncio.create_task(broker.execute(**call))
    await asyncio.wait_for(entered_executor.wait(), timeout=1)
    executing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await executing

    async with store.db.read() as conn:
        uncertain = await (
            await conn.execute(
                "SELECT effect_status,attempt FROM tool_executions WHERE run_id=?",
                (run.envelope.run_id,),
            )
        ).fetchone()
    assert dict(uncertain) == {"effect_status": "DISPATCHED", "attempt": 1}

    clock.value += 30_001
    assert await store.recover_expired(now_ms=clock.now_ms()) == 1
    replacement_claim = await store.claim_next(
        release_map=await store.active_releases(),
        worker_id="replacement-worker",
        lease_ms=30_000,
        now_ms=clock.now_ms(),
    )
    assert replacement_claim is not None
    assert replacement_claim.activity.activity_id == parent.activity_id
    replacement = await store.mark_activity_running(
        replacement_claim.activity.activity_id,
        worker_id="replacement-worker",
        fencing_token=replacement_claim.activity.fencing_token,
        now_ms=clock.now_ms(),
    )
    call.update(
        parent_activity_id=replacement.activity_id,
        fencing_token=replacement.fencing_token,
    )

    result = await broker.execute(**call)

    assert result.status is ToolResultStatus.SUCCESS
    assert result.preview == {"value": "recovered exactly once"}
    assert calls == 2
    async with store.db.read() as conn:
        settled = await (
            await conn.execute(
                "SELECT effect_status,attempt FROM tool_executions WHERE run_id=?",
                (run.envelope.run_id,),
            )
        ).fetchone()
    assert dict(settled) == {"effect_status": "COMMITTED", "attempt": 2}


@pytest.mark.asyncio
async def test_rel_21_fi_07_published_blob_is_orphan_when_metadata_transaction_aborts(
    tmp_path: Path,
) -> None:
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")
    async with store.db.transaction() as conn:
        await conn.execute(
            """CREATE TRIGGER inject_artifact_metadata_failure
               BEFORE INSERT ON artifact_metadata
               BEGIN SELECT RAISE(ABORT,'injected metadata transaction failure'); END"""
        )

    app = _artifact_app(store, artifacts)
    content = b"published-before-runtime-metadata-commit"
    artifact_id = hashlib.sha256(content).hexdigest()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/artifacts",
            files={"file": ("fault.txt", content, "text/plain")},
        )

    assert response.status_code == 500
    blob_path = artifacts.path_for_diagnostics(artifact_id)
    assert blob_path.read_bytes() == content
    assert list((tmp_path / "artifacts" / ".tmp").iterdir()) == []
    async with store.db.read() as conn:
        metadata_count = await (
            await conn.execute(
                "SELECT count(*) AS n FROM artifact_metadata WHERE artifact_id=?",
                (artifact_id,),
            )
        ).fetchone()
    assert metadata_count["n"] == 0
    assert artifact_id not in await store.referenced_artifact_ids()

    old = datetime.now(timezone.utc) - timedelta(days=2)
    os.utime(blob_path, (old.timestamp(), old.timestamp()))
    cleanup = await artifacts.cleanup_orphans(
        referenced_artifact_ids=await store.referenced_artifact_ids(),
        older_than=datetime.now(timezone.utc) - timedelta(hours=24),
    )
    assert cleanup.deleted_artifact_ids == (artifact_id,)
    assert not blob_path.exists()


@pytest.mark.asyncio
async def test_rel_22_tampered_range_and_model_image_attachment_are_both_rejected(
    tmp_path: Path,
) -> None:
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")
    app = _artifact_app(store, artifacts)
    original = b"valid-image-payload"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        uploaded = await client.post(
            "/api/v1/artifacts",
            files={"file": ("image.png", original, "image/png")},
        )
        assert uploaded.status_code == 201
        artifact_id = uploaded.json()["artifact_id"]
        blob_path = artifacts.path_for_diagnostics(artifact_id)
        blob_path.chmod(0o600)
        blob_path.write_bytes(b"x" * len(original))

        ranged = await client.get(
            f"/api/v1/artifacts/{artifact_id}",
            headers={"Range": "bytes=1-5"},
        )

    assert ranged.status_code == 409
    assert ranged.json()["error"]["code"] == "ARTIFACT_INTEGRITY_ERROR"

    adapter = AdkEngineAdapter(
        engine="agent_loop",
        context=None,
        release_fingerprint="test-release",
        artifact_store=artifacts,
        artifact_metadata_loader=store.get_artifact_metadata,
        tool_broker=None,
    )
    with pytest.raises(ArtifactIntegrityError) as error:
        await adapter._read_all(  # noqa: SLF001 - exact image attachment adapter boundary
            artifact_id, total_size=len(original)
        )
    assert error.value.code == "ARTIFACT_INTEGRITY_ERROR"


@pytest.mark.asyncio
async def test_rel_23_full_trace_file_keeps_artifact_ref_but_not_large_tool_payload(
    tmp_path: Path,
) -> None:
    trace_root = tmp_path / "traces"
    trace_id = "rel23-artifact-trace"
    artifact_id = "a" * 64
    sentinel = "TRACE_PAYLOAD_MUST_NOT_LEAK_" + "sensitive" * 20_000

    async def large_tool_result(_args, _context):
        return {
            "status": "SUCCESS",
            "result_ref": artifact_id,
            "preview": sentinel,
            "content": sentinel,
        }

    registry = ToolRegistry(
        [
            ToolSpec(
                name="large_result",
                description="returns an artifact-backed result",
                parameters={"type": "object", "properties": {}},
                run=large_tool_result,
            )
        ]
    )
    configure_tracing(
        enabled=True,
        payload_level="full",
        trace_dir=str(trace_root),
        max_field_chars=len(sentinel) * 2,
        retention_days=7,
        engine="native_loop-test",
    )
    set_trace_id(trace_id)
    try:
        outcome = await execute_one(
            ToolCall(id="call-1", name="large_result", arguments="{}"),
            registry,
            invocation_id="invocation-1",
            state={},
        )
        assert outcome.ok is True
    finally:
        set_trace_id("-")
        configure_tracing(enabled=False, engine="test-cleanup")

    paths = list(trace_root.rglob("*.jsonl"))
    assert len(paths) == 1
    raw = paths[0].read_text(encoding="utf-8")
    assert sentinel not in raw
    span = json.loads(raw.strip())
    persisted_result = span["payloads"]["result"]
    assert persisted_result["result_ref"] == artifact_id
    assert persisted_result["preview"]["artifact_backed"] is True
    assert persisted_result["content"]["artifact_backed"] is True
    assert persisted_result["preview"]["chars"] == len(sentinel)
