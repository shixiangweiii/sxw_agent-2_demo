"""技能执行：把 claude-skill 当作一个 ADK 子代理在沙箱中运行。

复刻 `app/core/claude_skill/skill_runner.py` / `skill_agent_builder.py`（精简）：
子代理 = LlmAgent(SKILL.md 指令 + 沙箱工具集)，经 Runner 跑；事件流出 UI（skill_event），
最终文本返回给父 LLM（两契约）。
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from google.adk.agents import LlmAgent
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import InMemoryRunner
from google.genai import types

from agent.claude_skill.catalog import ClaudeSkill
from agent.claude_skill.sandbox.base import BaseSandbox
from agent.claude_skill.toolset import build_sandbox_tools
from agent.plugins.tool_args_guard_plugin import ToolArgsGuardPlugin
from agent.stream.event_converters import StreamEvent, adk_event_to_stream_events
from common.obs import get_logger, log_kv

logger = get_logger("agent.claude_skill")

_APP = "claude-skill"


async def run_skill(
    skill: ClaudeSkill,
    query: str,
    sandbox: BaseSandbox,
    llm: Any,
    ui_emit: Callable[[StreamEvent], Awaitable[None]],
) -> str:
    await sandbox.try_create()
    agent = LlmAgent(
        name=f"skill_{skill.skill_id}".replace("-", "_"),
        model=llm,
        instruction=skill.instruction,
        tools=build_sandbox_tools(sandbox),
    )
    runner = InMemoryRunner(agent=agent, app_name=_APP, plugins=[ToolArgsGuardPlugin()])
    session = await runner.session_service.create_session(app_name=_APP, user_id="skill")
    message = types.Content(role="user", parts=[types.Part(text=query)])

    final_parts: list[str] = []
    async for event in runner.run_async(
        user_id="skill", session_id=session.id, new_message=message,
        run_config=RunConfig(streaming_mode=StreamingMode.SSE),
    ):
        for se in adk_event_to_stream_events(event):
            if se.event == "text":
                final_parts.append(se.data.get("delta", ""))
            # 子代理活动流出 UI：包成 skill_event（subEvent 标明 text/tool_call/tool_result）
            await ui_emit(StreamEvent("skill_event", {
                "skill": skill.skill_id, "subEvent": se.event, **se.data,
            }))

    text = "".join(final_parts).strip()
    log_kv(logger, logging.INFO, "ClaudeSkill", "run done",
           skill=skill.skill_id, provider=sandbox.get_sandbox_provider().value, answer_len=len(text))
    return text
