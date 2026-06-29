"""skill-center 运行时路由：/list 目录、/execute 同步。（/execute-streaming 在 S1 接入）"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from common.obs import get_logger, log_kv
from common.skill_contract import SkillListRequest, SkillListResult, SkillToolExecuteRequestDTO
from skillcenter import skills

router = APIRouter(prefix="/api/v1/skills/runtime", tags=["skill-runtime"])
logger = get_logger("skillcenter.api")


@router.post("/list")
async def list_skills(req: SkillListRequest) -> dict[str, Any]:
    """技能目录（含 tools[]+inputSchema），按 snapshotTag 返回。"""
    defs = skills.get_skill_defs(req.snapshot_tag or "PUBLISHED")
    log_kv(logger, logging.INFO, "SkillList", "served",
           tenant=req.tenant_id, snapshot=req.snapshot_tag, count=len(defs))
    return {"success": True, "result": SkillListResult(skills=defs).model_dump(by_alias=True)}


@router.post("/execute")
async def execute(req: SkillToolExecuteRequestDTO) -> dict[str, Any]:
    """同步执行技能工具，返回 MCP 形态结果。"""
    result = skills.execute_sync(req.skill_id, req.tool_name, req.arguments or {})
    log_kv(logger, logging.INFO, "SkillExecute", "served",
           skill=req.skill_id, tool=req.tool_name, is_error=result.is_error)
    # API 层始终 success=True；技能级错误体现在 result.isError
    return {"success": True, "result": result.model_dump(by_alias=True)}


@router.post("/execute-streaming")
async def execute_streaming(req: SkillToolExecuteRequestDTO) -> StreamingResponse:
    """流式执行：NDJSON（每行一个 SkillResultDTO）。"""
    async def generator() -> AsyncIterator[str]:
        async for result in skills.execute_streaming(req.skill_id, req.tool_name, req.arguments or {}):
            yield json.dumps(result.model_dump(by_alias=True, exclude_none=True), ensure_ascii=False) + "\n"
    log_kv(logger, logging.INFO, "SkillExecuteStreaming", "start",
           skill=req.skill_id, tool=req.tool_name)
    return StreamingResponse(generator(), media_type="text/event-stream")
