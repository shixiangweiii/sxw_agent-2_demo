"""技能目录加载：从 skill-center 拉技能（含快照）→ 包装成 SelectedSkillTool 列表。

复刻 `list_user_skills_v2` + 工具装配；skill-center 不可用时返回空（不阻断 agent 启动）。
"""
from __future__ import annotations

import logging

import httpx

from agent.config import AgentSettings
from agent.skills.client import SkillCenterClient
from agent.skills.selected_skill_tool import SelectedSkillTool
from common.obs import get_logger, get_trace_id, log_kv
from common.skill_contract import SkillListRequest, SkillListResult

logger = get_logger("agent.skill")


async def load_skill_tools(
    settings: AgentSettings, agent_uuid: str = "demo-agent", snapshot_tag: str = "PUBLISHED",
) -> list[SelectedSkillTool]:
    base_url = settings.skill_center_base_url.rstrip("/")
    url = f"{base_url}/api/v1/skills/runtime/list"
    request = SkillListRequest(tenantId=agent_uuid, snapshotTag=snapshot_tag)
    client = SkillCenterClient(settings)
    tools: list[SelectedSkillTool] = []
    try:
        async with httpx.AsyncClient(timeout=settings.skill_center_timeout_ms / 1000.0) as http:
            resp = await http.post(
                url, json=request.model_dump(by_alias=True),
                headers={"X-Trace-Id": get_trace_id() or "", "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
        result = SkillListResult(**data["result"])
        for skill in result.skills:
            for tool in skill.tools:
                tools.append(SelectedSkillTool(
                    skill_id=skill.skill_id,
                    tool_name=tool.name,
                    description=f"{skill.name}：{tool.description}",
                    input_schema=tool.input_schema,
                    client=client,
                ))
        log_kv(logger, logging.INFO, "SkillCatalog", "loaded",
               count=len(tools), snapshot=snapshot_tag)
    except Exception as exc:  # noqa: BLE001 - skill-center 不可用不阻断启动
        log_kv(logger, logging.WARNING, "SkillCatalog", "load failed (skill-center down?), skip",
               error=type(exc).__name__)
    return tools
