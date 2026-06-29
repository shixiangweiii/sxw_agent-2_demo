"""ClaudeSkillTool：把一个 claude-skill 包装成 ADK 工具（一技能一实例）。

复刻 `single_loop/claude_skill_tool.py` 的两契约：
- LLM 契约：run_async 返回 {output: 最终文本}，作为 function_response 回父 LLM；
- UI 契约：技能子代理的事件经 ui_event_queue 实时流出（skill_event）。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from google.adk.tools import BaseTool, ToolContext
from google.adk.tools._gemini_schema_util import _to_gemini_schema
from google.genai.types import FunctionDeclaration

from agent.claude_skill.catalog import ClaudeSkill
from agent.claude_skill.sandbox.base import SandboxUnavailableError
from agent.claude_skill.sandbox.factory import build_sandbox
from agent.claude_skill.skill_runner import run_skill
from agent.skills.ui_event_queue import emit_skill_event
from agent.stream.event_converters import StreamEvent
from common.obs import get_logger, log_kv

logger = get_logger("agent.claude_skill")


class ClaudeSkillTool(BaseTool):
    def __init__(self, skill: ClaudeSkill, llm: Any, sandbox_provider: str) -> None:
        super().__init__(
            name=f"claude_skill_{skill.skill_id}".replace("-", "_"),
            description=f"{skill.name}：{skill.description}（在沙箱中以子代理方式执行）",
        )
        self._skill = skill
        self._llm = llm
        self._provider = sandbox_provider

    def _get_declaration(self) -> FunctionDeclaration:
        return FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters=_to_gemini_schema({
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "要交给该技能完成的任务描述（尽量保留用户原话关键信息）"},
                },
                "required": ["query"],
            }),
        )

    async def run_async(self, *, args: dict[str, Any], tool_context: Optional[ToolContext]) -> dict[str, Any]:
        query = (args or {}).get("query", "")
        sandbox = build_sandbox(self._provider)
        log_kv(logger, logging.INFO, "ClaudeSkill", "invoke",
               skill=self._skill.skill_id, provider=self._provider)

        async def emit(event: StreamEvent) -> None:
            await emit_skill_event(event)

        try:
            final = await run_skill(self._skill, query, sandbox, self._llm, emit)
        except SandboxUnavailableError as exc:
            log_kv(logger, logging.WARNING, "ClaudeSkill", "sandbox unavailable", error=str(exc))
            return {"output": f"沙箱不可用：{exc}", "isError": True}
        except Exception as exc:  # noqa: BLE001 - 技能失败降级，不中断父 turn
            log_kv(logger, logging.ERROR, "ClaudeSkill", "skill failed", error=type(exc).__name__)
            return {"output": "技能执行失败，请重试或换个说法。", "isError": True}
        finally:
            await sandbox.close()

        return {"output": final or "技能执行完成但无输出。", "skill": self._skill.skill_id}
