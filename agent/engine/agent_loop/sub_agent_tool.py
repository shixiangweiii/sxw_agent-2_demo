"""子代理委派工具：把复杂子任务交给专精子代理（ADK AgentTool）。"""
from __future__ import annotations

from typing import Any

from google.adk.agents import LlmAgent
from google.adk.tools.agent_tool import AgentTool


def build_sub_agent_tool(llm: Any) -> AgentTool:
    researcher = LlmAgent(
        name="researcher",
        model=llm,
        description="研究助手：把一个子问题分解并给出结构化要点。",
        instruction="你是研究助手。针对给定子问题，给出条理清晰的结构化要点（不超过 6 条），不要寒暄。",
    )
    return AgentTool(agent=researcher)
