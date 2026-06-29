"""AgentChatClient：agent 侧轻量一次性补全（用于规划/汇总等非工具调用场景）。

与 ADK Runner 工具执行链解耦——规划/汇总只需单轮 LLM 文本，走 openai 兼容直连更轻。
默认关闭 thinking 以保证低延迟与干净文本。
"""
from __future__ import annotations

from typing import Any, Optional

import openai

from agent.config import AgentSettings


class AgentChatClient:
    def __init__(self, settings: AgentSettings) -> None:
        self._client = openai.AsyncOpenAI(
            api_key=settings.dashscope_api_key,
            base_url=settings.llm_base_url,
        )
        self._model = settings.llm_model

    async def complete(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 512,
        thinking: bool = False,
        extra_body: Optional[dict[str, Any]] = None,
    ) -> str:
        body: dict[str, Any] = dict(extra_body or {})
        body.setdefault("enable_thinking", thinking)
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            extra_body=body,
        )
        return resp.choices[0].message.content or ""
