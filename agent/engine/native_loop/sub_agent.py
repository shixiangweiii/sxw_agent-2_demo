"""researcher 子代理的原生实现（Agent-as-Tool）。

与 `agent/engine/loop_tools/sub_agent_tool.py`（ADK ``AgentTool`` 版）**同名、同描述、
同参数**，只是内核换成本引擎自己的循环。这样两代引擎面对的工具面逐个相同，
对比才落在"循环归谁驱动"上。

researcher 不带任何工具：它只做一次有界的结构化推理，对主循环表现为一次普通 tool_use。
"""
from __future__ import annotations

from typing import Any

from agent.llm.chat import AgentChatClient

# 与 ADK 版 LlmAgent 的 description / instruction 保持一致。
RESEARCHER_DESCRIPTION = "研究助手：把一个子问题分解并给出结构化要点。"
RESEARCHER_INSTRUCTION = (
    "你是研究助手。针对给定子问题，给出条理清晰的结构化要点（不超过 6 条），不要寒暄。"
)

_MAX_TOKENS = 1024


def build_researcher_tool(chat: AgentChatClient) -> Any:
    """返回一个可被 ``from_function`` 适配的异步函数工具。"""

    async def researcher(request: str) -> dict[str, Any]:
        """研究助手：把一个子问题分解并给出结构化要点。

        Args:
            request: 自包含的子问题描述，需展开代词与省略信息。
        """
        text = (request or "").strip()
        if not text:
            return {"error": "request 必须是非空字符串", "hint": "请给出自包含的子问题描述后重试。"}
        answer = await chat.complete(
            system=RESEARCHER_INSTRUCTION,
            user=text,
            max_tokens=_MAX_TOKENS,
        )
        return {"request": text, "result": answer}

    return researcher
