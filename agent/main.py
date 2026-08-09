"""Durable Agent Runtime API process.

This process owns HTTP admission, status, committed-event subscriptions and
Artifact upload/read.  It intentionally does not load an LLM, ADK Runner or
remote tool catalog; execution belongs to ``python -m agent.runtime.worker.main``.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from agent.api.documents import router as documents_router
from agent.api.traces import router as traces_router
from agent.config import get_settings
from agent.runtime.adapters.filesystem_artifact import FilesystemArtifactStore
from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.runtime.domain.errors import RuntimeFault
from agent.runtime.api.artifacts import router as artifacts_router
from agent.runtime.api.runs import router as runs_router
from common.obs import TraceMiddleware, get_logger, log_kv, setup_logging
from common.trace import configure_tracing

settings = get_settings()
setup_logging(settings.log_level)
configure_tracing(
    enabled=settings.trace_enabled,
    payload_level=settings.trace_payload_level,
    trace_dir=settings.trace_dir,
    max_field_chars=settings.trace_max_field_chars,
    retention_days=settings.trace_retention_days,
    engine="runtime-api",
)
logger = get_logger("agent.main")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    database = RuntimeDatabase(
        settings.runtime_db_path,
        busy_timeout_ms=settings.runtime_busy_timeout_ms,
    )
    store = SqliteRuntimeStore(database)
    await store.initialize()
    app.state.settings = settings
    app.state.runtime_store = store
    app.state.artifact_store = FilesystemArtifactStore(settings.artifact_root)
    log_kv(
        logger, logging.INFO, "Boot", "runtime API starting",
        port=settings.agent_port, db=settings.runtime_db_path,
    )
    yield
    log_kv(logger, logging.INFO, "Boot", "runtime API stopped")


app = FastAPI(title="sxw-canonical-agent-runtime", version="1.0.0", lifespan=lifespan)
app.add_middleware(TraceMiddleware, service="agent-api")
app.include_router(artifacts_router)
app.include_router(runs_router) # 注册 /api/v1/runs 接口，会话接入层直接和前端交互的接口
app.include_router(documents_router)
app.include_router(traces_router)
app.mount("/chat-ui", StaticFiles(directory=WEB_DIR, html=True), name="chat-ui")
# 只读诊断控制台。与 chat-ui 平级，共用 web/tokens.css。
app.mount(
    "/trace-ui",
    StaticFiles(directory=WEB_DIR / "trace-console", html=True),
    name="trace-ui",
)


@app.exception_handler(RuntimeFault)
async def runtime_fault_handler(_request: Request, exc: RuntimeFault) -> JSONResponse:
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


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/chat-ui/")


@app.get("/healthz")
async def healthz(request: Request) -> dict[str, object]:
    store = request.app.state.runtime_store
    return {
        "status": "ok",
        "service": "agent-runtime-api",
        "active_releases": await store.active_releases(),
    }
