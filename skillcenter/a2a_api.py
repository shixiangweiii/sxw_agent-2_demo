"""A2A 代理实例注册表：/api/v1/a2a-agents/instance/list。

精简复刻 Java `A2AAgentInstanceController`：返回 A2A 子代理实例（含 agent-card 发现地址）。
真实里 agent-card + JSON-RPC 都在 skill-center；demo 把 A2A 运行时拆到独立 a2a_service，
skill-center 仅做注册表（/instance/list 指向其 agent-card）。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from common.obs import get_logger, log_kv
from skillcenter.config import get_settings

router = APIRouter(prefix="/api/v1/a2a-agents/instance", tags=["a2a"])
logger = get_logger("skillcenter.a2a")


class A2AInstanceListRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    tenant_id: str = Field(alias="tenantId")
    snapshot_tag: Optional[str] = Field(default="PUBLISHED", alias="snapshotTag")


@router.post("/list")
async def list_instances(req: A2AInstanceListRequest) -> dict[str, Any]:
    base = get_settings().a2a_service_base_url.rstrip("/")
    instances = [{
        "agentInstanceId": "math_expert",
        "name": "数学专家",
        "description": "擅长精确的算术与数学计算（A2A 远程子代理）。",
        "cardUrl": f"{base}/.well-known/agent-card.json",
    }]
    log_kv(logger, logging.INFO, "A2AInstanceList", "served",
           tenant=req.tenant_id, snapshot=req.snapshot_tag, count=len(instances))
    return {"success": True, "result": {"a2AAgentInstances": instances}}
