"""agent 运行时上下文：装配 LLM / 工具 / 会话 / 制品（依赖注入容器）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from google.adk.artifacts import InMemoryArtifactService

from agent.artifacts.artifact_service import build_artifact_service
from agent.config import AgentSettings
from agent.llm.chat import AgentChatClient
from agent.llm.hardened_litellm import HardenedLiteLlm, build_llm
from agent.a2a.loader import load_a2a_agent_tools
from agent.claude_skill.catalog import load_claude_skills
from agent.claude_skill.claude_skill_tool import ClaudeSkillTool
from agent.session.session_service import SessionManager
from agent.skills.catalog import load_skill_tools
from agent.tools.builtin_tools import build_builtin_tools, simulate_unstable_operation
from agent.tools.knowledge_search import build_knowledge_search_tool


@dataclass
class AgentContext:
    settings: AgentSettings
    llm: HardenedLiteLlm
    chat: AgentChatClient
    tools: list[Callable[..., Any]]
    session_manager: SessionManager
    artifact_service: InMemoryArtifactService


def build_agent_context(settings: AgentSettings) -> AgentContext:
    return AgentContext(
        settings=settings,
        llm=build_llm(settings),
        chat=AgentChatClient(settings),
        tools=[*build_builtin_tools(), build_knowledge_search_tool(settings),
               simulate_unstable_operation],
        session_manager=SessionManager(),
        artifact_service=build_artifact_service(),
    )


async def attach_skill_tools(ctx: AgentContext) -> None:
    """从 skill-center 拉技能目录并加入 ctx.tools（两代引擎共享）。skill-center 不可用则跳过。"""
    skill_tools = await load_skill_tools(ctx.settings, agent_uuid=ctx.settings.agent_uuid)
    ctx.tools.extend(skill_tools)


def attach_claude_skill_tools(ctx: AgentContext) -> None:
    """加载本地 claude-skill 包 → ClaudeSkillTool 注入 ctx.tools（沙箱执行，两代引擎共享）。"""
    skills = load_claude_skills()
    ctx.tools.extend(
        ClaudeSkillTool(skill, ctx.llm, ctx.settings.sandbox_provider) for skill in skills
    )


async def attach_a2a_agents(ctx: AgentContext) -> None:
    """从 skill-center 发现 A2A 子代理 → RemoteA2aAgent → AgentTool 注入 ctx.tools。best-effort。"""
    a2a_tools = await load_a2a_agent_tools(ctx.settings)
    ctx.tools.extend(a2a_tools)
