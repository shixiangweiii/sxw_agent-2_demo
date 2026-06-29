"""决策/规划器：意图识别 + 步骤拆解，产出显式计划（Plan-Execute 的"规划"相）。"""
from __future__ import annotations

import logging

from agent.llm.chat import AgentChatClient
from common.obs import get_logger, log_kv

logger = get_logger("agent.planner")

_PLAN_SYS = (
    "你是任务规划器。把用户请求拆解成 1-4 个简洁的执行步骤，每行一个步骤，"
    "动词开头，不要编号、不要解释。若问题很简单，只给 1 步即可。"
    "对知识型/事实型问题，第一步应为『检索知识库』。"
)


class DecisionPlanner:
    def __init__(self, chat: AgentChatClient) -> None:
        self._chat = chat

    async def plan(self, query: str) -> list[str]:
        try:
            text = await self._chat.complete(_PLAN_SYS, query, max_tokens=200)
        except Exception as exc:  # noqa: BLE001 - 规划失败兜底为单步直答
            log_kv(logger, logging.WARNING, "Plan", "planning failed, fallback",
                   error=type(exc).__name__)
            return ["直接回答用户的问题"]
        steps: list[str] = []
        for line in text.splitlines():
            s = line.strip().lstrip("-•*0123456789.、 ").strip()
            if s:
                steps.append(s)
        steps = steps[:4]
        log_kv(logger, logging.INFO, "Plan", "planned", steps=len(steps))
        return steps or ["直接回答用户的问题"]
