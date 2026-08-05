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
from google.genai.types import FunctionDeclaration

from agent.config import AgentSettings
from common.obs import get_logger, get_trace_id, log_kv

logger = get_logger("agent.a2a")

_A2A_REQUEST_DESCRIPTION = (
    "远程 A2A 子代理的每次调用都会创建独立会话，不继承当前父 Agent 的对话历史。"
    "request 必须是自包含的任务说明：展开所有代词和省略信息，并完整给出目标、"
    "范围、约束、输入数据及完成任务所需的上下文。"
)


class SelfContainedA2AAgentTool(AgentTool):
    """只增强 A2A request 声明，不改写 ADK 的执行与响应 schema。"""

    def _get_declaration(self) -> FunctionDeclaration:
        declaration = super()._get_declaration()
        json_schema = declaration.parameters_json_schema
        if json_schema is not None:
            properties = json_schema.get("properties")
            request_schema = properties.get("request") if isinstance(properties, dict) else None
            if not isinstance(request_schema, dict):
                raise RuntimeError("A2A AgentTool 声明缺少 request JSON schema")
            request_schema["description"] = _A2A_REQUEST_DESCRIPTION
            return declaration

        parameters = declaration.parameters
        properties = getattr(parameters, "properties", None) if parameters is not None else None
        request_schema = properties.get("request") if isinstance(properties, dict) else None
        if request_schema is None:
            raise RuntimeError("A2A AgentTool 声明缺少 request schema")
        request_schema.description = _A2A_REQUEST_DESCRIPTION
        return declaration


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
            tools.append(SelfContainedA2AAgentTool(agent=remote))
        log_kv(logger, logging.INFO, "A2ALoad", "loaded",
               count=len(tools), names=[i["agentInstanceId"] for i in data["result"]["a2AAgentInstances"]])
    except Exception as exc:  # noqa: BLE001 - skill-center / a2a 不可用不阻断启动
        log_kv(logger, logging.WARNING, "A2ALoad", "load failed, skip", error=type(exc).__name__)
    return tools
