"""子代理委派工具：把复杂子任务交给专精子代理（ADK AgentTool）。"""
from __future__ import annotations

from typing import Any

from google.adk.agents import LlmAgent
from google.adk.tools.agent_tool import AgentTool
from google.genai.types import FunctionDeclaration


_RESEARCHER_REQUEST_DESCRIPTION = "自包含的子问题描述，需展开代词与省略信息。"


class ResearcherAgentTool(AgentTool):
    """Freeze the same public request schema as the Native researcher tool."""

    result_protocol = "plain"

    def _get_declaration(self) -> FunctionDeclaration:
        declaration = super()._get_declaration()
        schema = declaration.parameters_json_schema
        properties = schema.get("properties") if isinstance(schema, dict) else None
        request = properties.get("request") if isinstance(properties, dict) else None
        if not isinstance(request, dict):
            raise RuntimeError("researcher AgentTool declaration lacks request schema")
        request["description"] = _RESEARCHER_REQUEST_DESCRIPTION
        return declaration


def build_sub_agent_tool(llm: Any) -> AgentTool:
    researcher = LlmAgent(
        name="researcher",
        model=llm,
        description="研究助手：把一个子问题分解并给出结构化要点。",
        instruction="你是研究助手。针对给定子问题，给出条理清晰的结构化要点（不超过 6 条），不要寒暄。",
    )
    return ResearcherAgentTool(agent=researcher)
