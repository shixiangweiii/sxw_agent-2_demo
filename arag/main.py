"""arag 服务入口（FastAPI）。

装配可观测性 + 健康检查 + 运行时上下文（存储端口 + 检索流水线），挂载 index/retrieve 路由。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI

from arag.api.index import router as index_router
from arag.api.retrieve import router as retrieve_router
from arag.config import get_settings
from arag.context import build_context
from common.obs import TraceMiddleware, get_logger, log_kv, setup_logging

settings = get_settings()
setup_logging(settings.log_level)
logger = get_logger("arag.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # 按 *_BACKEND 选型装配存储端口 + 检索流水线，挂到 app.state.ctx 供路由使用。
    app.state.ctx = build_context(settings)
    log_kv(logger, logging.INFO, "Boot", "arag service starting",
           vector=settings.vector_backend, fulltext=settings.fulltext_backend,
           graph=settings.graph_backend, port=settings.arag_port)
    yield
    log_kv(logger, logging.INFO, "Boot", "arag service stopped")


app = FastAPI(title="sxw-arag", version="0.1.0", lifespan=lifespan)
app.add_middleware(TraceMiddleware, service="arag")
app.include_router(index_router)
app.include_router(retrieve_router)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """存活探针：返回当前存储后端选型。"""
    return {
        "status": "ok",
        "service": "arag",
        "backends": {
            "vector": settings.vector_backend,
            "fulltext": settings.fulltext_backend,
            "graph": settings.graph_backend,
        },
    }
