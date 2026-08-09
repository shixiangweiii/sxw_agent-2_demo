"""Worker 进程装配：LLM、只读工具目录与 Skill 并发协调器。

跨 attempt 会话、Artifact 和检查点不属于该进程级容器；它们的权威分别是
Canonical Event、内容寻址 CAS 与 Runtime Store。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from agent.config import AgentSettings
from agent.llm.chat import AgentChatClient
from agent.llm.hardened_litellm import HardenedLiteLlm, build_llm
from agent.a2a.loader import load_a2a_agent_tools
from agent.claude_skill.catalog import load_claude_skills
from agent.claude_skill.claude_skill_tool import ClaudeSkillTool
from agent.claude_skill.contracts import SkillRuntimeConfig
from agent.claude_skill.execution_coordinator import SkillExecutionCoordinator
from agent.skills.catalog import load_skill_tools
from agent.tools.builtin_tools import build_builtin_tools, simulate_unstable_operation
from agent.tools.knowledge_search import build_knowledge_search_tool


# 进程级共享装配（每请求一份的运行参数见 engine/base.py 的 RunContext）。
@dataclass
class AgentContext:
    settings: AgentSettings
    llm: HardenedLiteLlm                        # ADK 循环里"调模型"最终落到它
    chat: AgentChatClient                       # 轻量单轮补全：plan_execute 规划相 +
                                                # native_loop 的压缩摘要与原生 researcher
    tools: list[Callable[..., Any]]             # ★ 三代引擎共享的工具集，启动期组装、请求期只读
    skill_coordinator: SkillExecutionCoordinator  # Claude SKILL 并发/独占资源治理（进程级单例）


def build_agent_context(settings: AgentSettings) -> AgentContext:
    return AgentContext(
        settings=settings,
        llm=build_llm(settings),
        chat=AgentChatClient(settings),
        tools=[*build_builtin_tools(), build_knowledge_search_tool(settings),
               simulate_unstable_operation],
        skill_coordinator=SkillExecutionCoordinator(settings.skill_max_parallel_calls),
    )


async def attach_skill_tools(ctx: AgentContext) -> None:
    """从 skill-center 拉技能目录并加入 ctx.tools（三代引擎共享）。skill-center 不可用则跳过。"""
    skill_tools = await load_skill_tools(ctx.settings, agent_uuid=ctx.settings.agent_uuid)
    ctx.tools.extend(skill_tools)


def attach_claude_skill_tools(ctx: AgentContext) -> None:
    """加载本地 claude-skill 包 → ClaudeSkillTool 注入 ctx.tools（沙箱执行，三代引擎共享）。"""
    skills = load_claude_skills()
    runtime_config = SkillRuntimeConfig(
        call_timeout_seconds=ctx.settings.skill_call_timeout_seconds,
        max_llm_calls=ctx.settings.skill_max_llm_calls,
        result_max_chars=ctx.settings.skill_result_max_chars,
    )
    ctx.tools.extend(
        ClaudeSkillTool(
            skill=skill,
            llm=ctx.llm,
            sandbox_provider=ctx.settings.sandbox_provider,
            coordinator=ctx.skill_coordinator,
            runtime_config=runtime_config,
        )
        for skill in skills
    )


async def attach_a2a_agents(ctx: AgentContext) -> None:
    """从 skill-center 发现 A2A 子代理 → RemoteA2aAgent → AgentTool 注入 ctx.tools。best-effort。"""
    a2a_tools = await load_a2a_agent_tools(ctx.settings)
    ctx.tools.extend(a2a_tools)
