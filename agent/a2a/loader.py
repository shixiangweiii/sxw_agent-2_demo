"""A2A 子代理加载：skill-center /instance/list 发现 → ADK RemoteA2aAgent（agent-card）→ AgentTool。

复刻 `app/core/agent/remote_agent/a2a/custom_remote_a2a_agent.py` 的客户端形态：
ADK 原生 RemoteA2aAgent（依赖 a2a-sdk）按 agent-card 解析 + JSON-RPC 调用远程代理；
包成 AgentTool 即可作为工具被主代理委派（无需改引擎）。
"""
from __future__ import annotations

import logging

import httpx
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.tools.agent_tool import AgentTool

from agent.config import AgentSettings
from common.obs import get_logger, get_trace_id, log_kv

logger = get_logger("agent.a2a")


async def load_a2a_agent_tools(settings: AgentSettings) -> list[AgentTool]:
    base_url = settings.skill_center_base_url.rstrip("/")
    url = f"{base_url}/api/v1/a2a-agents/instance/list"
    tools: list[AgentTool] = []
    try:
        async with httpx.AsyncClient(timeout=settings.skill_center_timeout_ms / 1000.0) as http:
            resp = await http.post(
                url,
                json={"tenantId": settings.agent_uuid, "snapshotTag": "PUBLISHED"},
                headers={"X-Trace-Id": get_trace_id() or "", "Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
        for inst in data["result"]["a2AAgentInstances"]:
            remote = RemoteA2aAgent(
                name=inst["agentInstanceId"],
                agent_card=inst["cardUrl"],     # ADK 在首次调用时按 well-known 解析该卡
                description=inst.get("description", ""),
            )
            tools.append(AgentTool(agent=remote))
        log_kv(logger, logging.INFO, "A2ALoad", "loaded",
               count=len(tools), names=[i["agentInstanceId"] for i in data["result"]["a2AAgentInstances"]])
    except Exception as exc:  # noqa: BLE001 - skill-center / a2a 不可用不阻断启动
        log_kv(logger, logging.WARNING, "A2ALoad", "load failed, skip", error=type(exc).__name__)
    return tools
