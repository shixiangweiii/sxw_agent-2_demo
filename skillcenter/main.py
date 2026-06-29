"""skill-center 服务入口（FastAPI）。

精简复刻 albert-skill-center 的 SkillRuntimeController：技能目录 + 同步/流式执行网关。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI

from common.obs import TraceMiddleware, get_logger, log_kv, setup_logging
from skillcenter import skills
from skillcenter.a2a_api import router as a2a_router
from skillcenter.api import router as runtime_router
from skillcenter.config import get_settings

settings = get_settings()
setup_logging(settings.log_level)
logger = get_logger("skillcenter.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log_kv(logger, logging.INFO, "Boot", "skill-center starting",
           port=settings.skill_center_port, skills=len(skills.SKILL_DEFS))
    yield
    log_kv(logger, logging.INFO, "Boot", "skill-center stopped")


app = FastAPI(title="sxw-skill-center", version="0.1.0", lifespan=lifespan)
app.add_middleware(TraceMiddleware, service="skillcenter")
app.include_router(runtime_router)
app.include_router(a2a_router)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "service": "skillcenter",
            "skills": [s.skill_id for s in skills.SKILL_DEFS]}
