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
    # ── 主链路第 0 步：进程启动期一次性装配 ──────────────────────────────
    # 工具集只在这里组装一次，之后每个请求复用同一份 ctx.tools（见 agent/context.py）。
    # 顺序有讲究：先建基础上下文（LLM/内置工具/会话/制品），再依次挂三类"扩展智能体"。
    ctx = build_agent_context(settings)
    # 下面三行的失败语义**故意不同**，这是本项目"失败要响亮 vs 降级要静默"的分界：
    #   - skill-center / A2A 是可选下游 → 拉不到就跳过，不阻断启动（best-effort 降级）；
    #   - claude-skill 是本地配置 → 技能包非法直接抛 SkillPackageInvalidError 让启动失败（fail-fast），
    #     因为那属于"配置写错了"，静默跳过会让人误以为技能可用。
    await attach_skill_tools(ctx)        # 从 skill-center 拉技能目录 → 工具集（best-effort）
    attach_claude_skill_tools(ctx)       # 本地 claude-skill（沙箱执行）→ 工具集
    await attach_a2a_agents(ctx)         # A2A 远程子代理（RemoteA2aAgent）→ 工具集（best-effort）
    # 挂到 app.state 供路由取用；FastAPI 里这是跨请求共享单例的标准位置。
    app.state.ctx = ctx
    log_kv(logger, logging.INFO, "Boot", "agent service starting",
           engine=settings.engine, model=settings.llm_model, port=settings.agent_port,
           tools=[getattr(t, "name", getattr(t, "__name__", "?")) for t in ctx.tools])
    yield
    log_kv(logger, logging.INFO, "Boot", "agent service stopped")


app = FastAPI(title="sxw-agent-runtime", version="0.1.0", lifespan=lifespan)
# TraceMiddleware 必须在最外层：它给每个请求生成/透传 trace_id 并写入 contextvar，
# 之后本服务所有日志、以及调 arag/skill-center 时带的 x-trace-id 都取自这里（common/obs.py）。
app.add_middleware(TraceMiddleware, service="agent")
app.include_router(chat_router)          # 主链路入口：POST /api/v1/chat/{uuid}/stream
app.include_router(documents_router)     # Web UI 文档入库代理 → 转发给 arag /v1/index
app.mount("/chat-ui", StaticFiles(directory=WEB_DIR, html=True), name="chat-ui")


@app.get("/")
async def root() -> RedirectResponse:
    """Open the browser chat UI by default."""
    return RedirectResponse(url="/chat-ui/")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """存活探针：返回当前引擎与模型。"""
    return {"status": "ok", "service": "agent", "engine": settings.engine, "model": settings.llm_model}
