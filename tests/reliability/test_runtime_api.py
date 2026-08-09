from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agent.runtime.adapters.filesystem_artifact import FilesystemArtifactStore
from agent.runtime.adapters.scripted_engine import ScriptedEngineAdapter
from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.api.traces import router as traces_router
from agent.runtime.api.artifacts import router as artifact_router
from agent.runtime.api.runs import router as run_router, stream_events
from agent.runtime.application.coordinator import EngineRegistry, RunCoordinator
from agent.runtime.domain.errors import RuntimeFault
from agent.runtime.domain.models import EventType, ReleaseManifest, RunStatus
from agent.runtime.worker.dispatcher import RuntimeWorker
from common.obs import use_trace_id
from common.trace import (
    KIND_ENGINE,
    KIND_LLM,
    KIND_TOOL,
    STATUS_ERROR,
    STATUS_OK,
    configure_tracing,
    start_span,
)


def _build_api(
    store: SqliteRuntimeStore,
    artifacts: FilesystemArtifactStore,
    *,
    heartbeat_seconds: float = 15,
) -> FastAPI:
    app = FastAPI()
    app.state.runtime_store = store
    app.state.artifact_store = artifacts
    app.state.settings = SimpleNamespace(
        runtime_default_deadline_seconds=60,
        runtime_sse_heartbeat_seconds=heartbeat_seconds,
        runtime_sse_poll_ms=1,
    )
    app.include_router(artifact_router)
    app.include_router(run_router)
    app.include_router(traces_router)

    @app.exception_handler(RuntimeFault)
    async def handle(_request: Request, exc: RuntimeFault):
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message,
                               "details": exc.details}},
        )

    return app


@pytest.fixture
async def api_env(tmp_path):
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    releases = {}
    for engine in ("plan_execute", "agent_loop", "native_loop"):
        releases[engine] = await store.register_release(
            ReleaseManifest(engine=engine, components={"test": "api-v1"}), activate=True,
        )
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")
    app = _build_api(store, artifacts)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, store, artifacts, releases


def run_body(*, engine="native_loop", conversation_id=None, text="hello"):
    return {
        "client_request_id": str(uuid.uuid4()),
        "conversation_id": conversation_id,
        "principal_id": "demo-user",
        "agent_id": "demo-agent",
        "engine": engine,
        "input": {"text": text, "attachment_refs": []},
    }


@pytest.mark.asyncio
async def test_create_status_location_and_idempotent_replay(api_env):
    client, _, _, _ = api_env
    body = run_body()
    first = await client.post("/api/v1/runs", json=body, headers={"Idempotency-Key": "api-key"})
    replay = await client.post("/api/v1/runs", json=body, headers={"Idempotency-Key": "api-key"})
    assert first.status_code == 202
    assert first.headers["Location"] == f"/api/v1/runs/{first.json()['run_id']}"
    assert replay.status_code == 202
    assert replay.json()["run_id"] == first.json()["run_id"]
    assert replay.json()["reused"] is True
    status = await client.get(first.headers["Location"])
    assert status.json()["last_seq"] == 4
    assert status.json()["engine"] == "native_loop"


@pytest.mark.asyncio
async def test_create_requires_idempotency_key(api_env):
    client, _, _, _ = api_env
    response = await client.post("/api/v1/runs", json=run_body())
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


@pytest.mark.asyncio
async def test_create_deadline_must_be_an_absolute_rfc3339_timestamp(api_env):
    client, _, _, _ = api_env
    body = run_body()
    body["deadline_at"] = "2030-01-01T00:00:00"

    response = await client.post(
        "/api/v1/runs",
        json=body,
        headers={"Idempotency-Key": "naive-deadline"},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_committed_sse_replay_uses_seq_and_terminal_not_done(api_env):
    client, store, _, releases = api_env
    created = await client.post(
        "/api/v1/runs", json=run_body(), headers={"Idempotency-Key": "sse-key"},
    )
    run_id = created.json()["run_id"]
    adapter = ScriptedEngineAdapter(
        [{"type": "text", "delta": "A"}, {"type": "text", "delta": "B"}],
        release_fingerprint=releases["native_loop"],
    )
    coordinator = RunCoordinator(
        store, EngineRegistry({"native_loop": adapter}), event_flush_bytes=1,
    )
    claim = await store.claim_next(
        worker_id="api-test-worker", lease_ms=30_000,
        now_ms=(await store.get_run(run_id)).envelope.created_at,
    )
    assert claim is not None
    assert await coordinator.execute_claim(claim, worker_id="api-test-worker") is RunStatus.SUCCEEDED

    replay = await client.get(f"/api/v1/runs/{run_id}/events?after_seq=4")
    assert replay.status_code == 200
    body = replay.text
    ids = [int(line.split(":", 1)[1]) for line in body.splitlines() if line.startswith("id:")]
    assert ids == sorted(set(ids))
    assert all(item > 4 for item in ids)
    assert "event: text" in body
    assert "event: terminal" in body
    assert "event: done" not in body
    assert json.loads(next(
        line.removeprefix("data: ") for line in reversed(body.splitlines()) if line.startswith("data: ")
    ))["terminal_status"] == "SUCCEEDED"

    resumed = await client.get(
        f"/api/v1/runs/{run_id}/events", headers={"Last-Event-ID": str(ids[-2])},
    )
    resumed_ids = [
        int(line.split(":", 1)[1]) for line in resumed.text.splitlines() if line.startswith("id:")
    ]
    assert resumed_ids == [ids[-1]]


@pytest.mark.asyncio
async def test_no_subscription_is_needed_for_worker_completion(api_env):
    client, store, _, releases = api_env
    created = await client.post(
        "/api/v1/runs", json=run_body(), headers={"Idempotency-Key": "detached-key"},
    )
    run_id = created.json()["run_id"]
    coordinator = RunCoordinator(store, EngineRegistry({
        "native_loop": ScriptedEngineAdapter(
            [{"type": "text", "delta": "detached"}],
            release_fingerprint=releases["native_loop"],
        )
    }))
    claim = await store.claim_next(
        worker_id="detached-worker", lease_ms=30_000,
        now_ms=(await store.get_run(run_id)).envelope.created_at,
    )
    assert claim is not None
    await coordinator.execute_claim(claim, worker_id="detached-worker")
    status = await client.get(f"/api/v1/runs/{run_id}")
    assert status.json()["status"] == "SUCCEEDED"


@pytest.mark.asyncio
async def test_rel_10_closing_active_sse_body_does_not_cancel_worker_or_run(api_env):
    client, store, _, releases = api_env
    created = await client.post(
        "/api/v1/runs", json=run_body(), headers={"Idempotency-Key": "disconnect-key"},
    )
    run_id = created.json()["run_id"]

    # ASGITransport buffers a streaming response until it ends, so exercise the
    # real route's body iterator directly.  Starting after the admission batch
    # puts this subscription in the live-tail path while the Run is nonterminal.
    app = client._transport.app  # type: ignore[attr-defined]  # noqa: SLF001
    app.state.settings.runtime_sse_heartbeat_seconds = 0
    request = Request({
        "type": "http",
        "method": "GET",
        "path": f"/api/v1/runs/{run_id}/events",
        "headers": [],
        "query_string": b"after_seq=4",
        "app": app,
    })
    response = await stream_events(
        run_id, request, after_seq=4, last_event_id=None,
    )
    body_iterator = response.body_iterator
    assert await asyncio.wait_for(anext(body_iterator), timeout=1) == ": heartbeat\n\n"
    await body_iterator.aclose()  # type: ignore[attr-defined]

    disconnected = await store.get_run(run_id)
    assert disconnected.status is RunStatus.DISPATCH_PENDING
    before_worker = await store.list_events(run_id, visibility=None)
    assert not any(event.event_type is EventType.CANCEL_REQUESTED for event in before_worker)
    assert not any(event.event_type is EventType.RUN_TERMINATED for event in before_worker)

    coordinator = RunCoordinator(store, EngineRegistry({
        "native_loop": ScriptedEngineAdapter(
            [{"type": "text", "delta": "continued after disconnect"}],
            release_fingerprint=releases["native_loop"],
        )
    }))
    worker = RuntimeWorker(
        store=store,
        coordinator=coordinator,
        worker_id="disconnect-worker",
        release_map={"native_loop": releases["native_loop"]},
        poll_ms=1,
    )
    assert await worker.run_once() is True
    assert (await store.get_run(run_id)).terminal_status is RunStatus.SUCCEEDED

    replay = await client.get(f"/api/v1/runs/{run_id}/events?after_seq=4")
    assert replay.status_code == 200
    assert "continued after disconnect" in replay.text
    assert "event: terminal" in replay.text


@pytest.mark.asyncio
async def test_fi_11_terminal_before_first_sse_read_replays_after_api_store_restart(tmp_path):
    database_path = tmp_path / "runtime.db"
    artifact_path = tmp_path / "artifacts"
    original_store = SqliteRuntimeStore(RuntimeDatabase(database_path))
    await original_store.initialize()
    release = await original_store.register_release(
        ReleaseManifest(engine="native_loop", components={"test": "fi-11-v1"}),
        activate=True,
    )
    original_app = _build_api(
        original_store, FilesystemArtifactStore(artifact_path),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=original_app), base_url="http://before-kill",
    ) as original_client:
        created = await original_client.post(
            "/api/v1/runs",
            json=run_body(text="commit before first SSE read"),
            headers={"Idempotency-Key": "fi-11-key"},
        )
        run_id = created.json()["run_id"]

        coordinator = RunCoordinator(original_store, EngineRegistry({
            "native_loop": ScriptedEngineAdapter(
                [{"type": "text", "delta": "durably committed"}],
                release_fingerprint=release,
            )
        }))
        claim = await original_store.claim_next(
            worker_id="fi-11-before-kill",
            lease_ms=30_000,
            now_ms=(await original_store.get_run(run_id)).envelope.created_at,
        )
        assert claim is not None
        assert await coordinator.execute_claim(
            claim, worker_id="fi-11-before-kill",
        ) is RunStatus.SUCCEEDED
        # Deliberately never open /events on the original API instance.

    restarted_store = SqliteRuntimeStore(RuntimeDatabase(database_path))
    await restarted_store.initialize()
    restarted_app = _build_api(
        restarted_store, FilesystemArtifactStore(artifact_path),
    )
    committed = await restarted_store.list_events(run_id)
    expected_ids = [event.seq for event in committed]
    assert sum(event.event_type is EventType.RUN_TERMINATED for event in committed) == 1

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=restarted_app), base_url="http://after-restart",
    ) as restarted_client:
        status = await restarted_client.get(f"/api/v1/runs/{run_id}")
        replay = await restarted_client.get(f"/api/v1/runs/{run_id}/events?after_seq=0")

    assert status.status_code == 200
    assert status.json()["terminal"]["status"] == "SUCCEEDED"
    replayed_ids = [
        int(line.split(":", 1)[1])
        for line in replay.text.splitlines()
        if line.startswith("id:")
    ]
    assert replayed_ids == expected_ids
    assert "durably committed" in replay.text
    assert "event: terminal" in replay.text


@pytest.mark.asyncio
async def test_cancel_api_is_explicit_and_idempotent(api_env):
    client, _, _, _ = api_env
    created = await client.post(
        "/api/v1/runs", json=run_body(), headers={"Idempotency-Key": "cancel-key"},
    )
    run_id = created.json()["run_id"]
    command = {"command_id": "cancel-command", "reason": "user clicked cancel"}
    first = await client.post(f"/api/v1/runs/{run_id}/cancel", json=command)
    replay = await client.post(f"/api/v1/runs/{run_id}/cancel", json=command)
    assert first.json()["run"]["status"] == "CANCELLED"
    assert replay.json()["reused"] is True


@pytest.mark.asyncio
async def test_artifact_upload_range_and_integrity_error(api_env):
    client, _, artifacts, _ = api_env
    uploaded = await client.post(
        "/api/v1/artifacts",
        files={"file": ("sample.txt", b"abcdef", "text/plain")},
    )
    assert uploaded.status_code == 201
    artifact_id = uploaded.json()["artifact_id"]
    ranged = await client.get(
        f"/api/v1/artifacts/{artifact_id}", headers={"Range": "bytes=1-3"},
    )
    assert ranged.status_code == 206
    assert ranged.content == b"bcd"
    assert ranged.headers["Content-Range"] == "bytes 1-3/6"

    path = artifacts.path_for_diagnostics(artifact_id)
    path.chmod(0o600)
    path.write_bytes(b"tampered")
    corrupted = await client.get(f"/api/v1/artifacts/{artifact_id}")
    assert corrupted.status_code == 409
    assert corrupted.json()["error"]["code"] == "ARTIFACT_INTEGRITY_ERROR"


@pytest.mark.asyncio
async def test_duplicate_artifact_mime_cannot_disagree_with_durable_metadata(api_env):
    client, store, _, _ = api_env
    content = b"same content-addressed bytes"
    first = await client.post(
        "/api/v1/artifacts",
        files={"file": ("first.txt", content, "text/plain")},
    )
    replay = await client.post(
        "/api/v1/artifacts",
        files={"file": ("renamed.txt", content, "text/plain")},
    )
    conflict = await client.post(
        "/api/v1/artifacts",
        files={"file": ("pretend.png", content, "image/png")},
    )

    assert first.status_code == replay.status_code == 201
    assert replay.json()["artifact_id"] == first.json()["artifact_id"]
    assert replay.json()["created_at"] == first.json()["created_at"]
    assert replay.json()["media_type"] == "text/plain"
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "ARTIFACT_METADATA_CONFLICT"
    durable = await store.get_artifact_metadata(first.json()["artifact_id"])
    assert durable["media_type"] == "text/plain"


@pytest.mark.asyncio
async def test_artifact_http_read_limit_returns_range_error(api_env):
    client, _, _, _ = api_env
    content = b"x" * (1024 * 1024 + 1)
    uploaded = await client.post(
        "/api/v1/artifacts",
        files={"file": ("large.bin", content, "application/octet-stream")},
    )
    artifact_id = uploaded.json()["artifact_id"]

    unbounded = await client.get(f"/api/v1/artifacts/{artifact_id}")
    assert unbounded.status_code == 416
    assert unbounded.json()["error"]["code"] == "ARTIFACT_READ_LIMIT_EXCEEDED"

    bounded = await client.get(
        f"/api/v1/artifacts/{artifact_id}",
        headers={"Range": "bytes=0-1048575"},
    )
    assert bounded.status_code == 206
    assert len(bounded.content) == 1024 * 1024


@pytest.mark.asyncio
async def test_empty_artifact_is_readable_but_has_no_satisfiable_range(api_env):
    client, _, _, _ = api_env
    uploaded = await client.post(
        "/api/v1/artifacts",
        files={"file": ("empty.bin", b"", "application/octet-stream")},
    )
    artifact_id = uploaded.json()["artifact_id"]
    whole = await client.get(f"/api/v1/artifacts/{artifact_id}")
    assert whole.status_code == 200
    assert whole.content == b""
    ranged = await client.get(
        f"/api/v1/artifacts/{artifact_id}", headers={"Range": "bytes=0-"},
    )
    assert ranged.status_code == 416


@pytest.mark.asyncio
async def test_no_active_release_returns_503(tmp_path):
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "empty.db"))
    await store.initialize()
    app = FastAPI()
    app.state.runtime_store = store
    app.state.settings = SimpleNamespace(
        runtime_default_deadline_seconds=60,
        runtime_sse_heartbeat_seconds=15,
        runtime_sse_poll_ms=1,
    )
    app.include_router(run_router)

    @app.exception_handler(RuntimeFault)
    async def handle(_request: Request, exc: RuntimeFault):
        return JSONResponse(status_code=exc.http_status,
                            content={"error": {"code": exc.code}})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/runs", json=run_body(), headers={"Idempotency-Key": "no-release"},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "NO_ACTIVE_RELEASE"


@pytest.mark.asyncio
async def test_trace_console_api_lists_summaries_and_fetches_one_trace(api_env, tmp_path):
    """Trace Console 的两个只读出口：列表浏览 + 单条取回。

    列表摘要来自扫描 trace 目录，所以这里直接用真实的 span 写入路径造数据，
    而不是塞假 JSON——文件名、trace_id 归属和 summary 聚合都要一起被验证。
    """
    client, _, _, _ = api_env
    trace_root = tmp_path / "console-traces"
    configure_tracing(
        enabled=True, payload_level="full", trace_dir=str(trace_root),
        retention_days=7, engine="runtime-worker",
    )
    try:
        for name, status in (("console-ok", STATUS_OK), ("console-bad", STATUS_ERROR)):
            with use_trace_id(name):
                with start_span("runtime.engine_attempt", KIND_ENGINE,
                                run_id=f"run_{name}", engine="native_loop") as root:
                    root.set_payload("answer", "hello console")
                    with start_span("native.llm", KIND_LLM, total_tokens=42) as llm:
                        llm.set_payload("contents", "prompt text")
                    if status is STATUS_ERROR:
                        with contextlib.suppress(RuntimeError):
                            with start_span("tool.broken", KIND_TOOL, tool="broken"):
                                raise RuntimeError("tool blew up")

        listing = await client.get("/api/v1/traces?limit=10")
        assert listing.status_code == 200
        body = listing.json()
        by_id = {item["trace_id"]: item for item in body["traces"]}
        assert {"console-ok", "console-bad"} <= set(by_id)
        assert by_id["console-ok"]["engine"] == "native_loop"
        assert by_id["console-ok"]["run_id"] == "run_console-ok"
        assert by_id["console-ok"]["process"] == "runtime-worker"
        assert by_id["console-ok"]["total_tokens"] == 42
        assert by_id["console-ok"]["in_flight"] is False
        assert by_id["console-bad"]["status"] == STATUS_ERROR
        assert by_id["console-bad"]["error_count"] == 1

        # 过滤器：状态与子串搜索都要真的收窄结果。
        errors = (await client.get("/api/v1/traces?status=error")).json()["traces"]
        assert [item["trace_id"] for item in errors] == ["console-bad"]
        searched = (await client.get("/api/v1/traces?q=console-ok")).json()["traces"]
        assert [item["trace_id"] for item in searched] == ["console-ok"]

        detail = await client.get("/api/v1/traces/console-ok?level=summary")
        assert detail.status_code == 200
        trace = detail.json()
        assert {span["kind"] for span in trace["spans"]} == {KIND_ENGINE, KIND_LLM}
        assert len(trace["trace_files"]) == 1
        # level 只能降级：summary 下 payload 变成 {chars, sha1, head} 摘要。
        engine_span = next(s for s in trace["spans"] if s["kind"] == KIND_ENGINE)
        assert set(engine_span["payloads"]["answer"]) == {"chars", "sha1", "head"}

        assert (await client.get("/api/v1/traces/nope")).status_code == 404
    finally:
        configure_tracing(enabled=False, engine="test-cleanup")


@pytest.mark.asyncio
async def test_trace_lookup_sanitizes_client_controlled_path(api_env, tmp_path):
    """trace_id 是客户端可控的，绝不能借它读到 trace 目录以外的文件。"""
    client, _, _, _ = api_env
    trace_root = tmp_path / "sanitize-traces"
    trace_root.mkdir()
    outside = tmp_path / "secret.jsonl"
    outside.write_text('{"span_id":"x","trace_id":"secret","kind":"llm"}\n', encoding="utf-8")
    configure_tracing(
        enabled=True, payload_level="none", trace_dir=str(trace_root),
        retention_days=7, engine="runtime-api",
    )
    try:
        for attempt in ("../secret", "../../etc/passwd", "..%2F..%2Fsecret"):
            response = await client.get(f"/api/v1/traces/{attempt}")
            assert response.status_code == 404, attempt
        assert outside.read_text(encoding="utf-8").startswith("{")
    finally:
        configure_tracing(enabled=False, engine="test-cleanup")


@pytest.mark.asyncio
async def test_trace_api_is_explicitly_unavailable_when_tracing_is_disabled(api_env):
    """关闭 tracing 时返回 503 而不是空列表——空列表会被误读成"没有轨迹"。"""
    client, _, _, _ = api_env
    configure_tracing(enabled=False, engine="test-disabled")
    assert (await client.get("/api/v1/traces")).status_code == 503
    assert (await client.get("/api/v1/traces/whatever")).status_code == 503
