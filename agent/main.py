"""agent 服务入口（FastAPI）。

装配可观测性 + 健康检查 + 运行时上下文（LLM/工具/会话/制品），挂载 SSE 对话路由。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from agent.api.chat import router as chat_router
from agent.api.documents import router as documents_router
from agent.config import get_settings
from agent.context import (
    attach_a2a_agents,
    attach_claude_skill_tools,
    attach_skill_tools,
    build_agent_context,
)
from common.obs import TraceMiddleware, get_logger, log_kv, setup_logging

settings = get_settings()
setup_logging(settings.log_level)
logger = get_logger("agent.main")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    ctx = build_agent_context(settings)
    await attach_skill_tools(ctx)        # 从 skill-center 拉技能目录 → 工具集（best-effort）
    attach_claude_skill_tools(ctx)       # 本地 claude-skill（沙箱执行）→ 工具集
    await attach_a2a_agents(ctx)         # A2A 远程子代理（RemoteA2aAgent）→ 工具集（best-effort）
    app.state.ctx = ctx
    log_kv(logger, logging.INFO, "Boot", "agent service starting",
           engine=settings.engine, model=settings.llm_model, port=settings.agent_port,
           tools=[getattr(t, "name", getattr(t, "__name__", "?")) for t in ctx.tools])
    yield
    log_kv(logger, logging.INFO, "Boot", "agent service stopped")


app = FastAPI(title="sxw-agent-runtime", version="0.1.0", lifespan=lifespan)
app.add_middleware(TraceMiddleware, service="agent")
app.include_router(chat_router)
app.include_router(documents_router)
app.mount("/chat-ui", StaticFiles(directory=WEB_DIR, html=True), name="chat-ui")


@app.get("/")
async def root() -> RedirectResponse:
    """Open the browser chat UI by default."""
    return RedirectResponse(url="/chat-ui/")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """存活探针：返回当前引擎与模型。"""
    return {"status": "ok", "service": "agent", "engine": settings.engine, "model": settings.llm_model}
