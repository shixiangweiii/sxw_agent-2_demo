"""arag 内部聊天客户端：DashScope qwen3.7-plus（OpenAI 兼容端点直连）。

用于查询改写、答案生成、图片描述（M5）。与 agent 侧的 ADK LiteLlm 解耦——arag 是独立服务。
"""
from __future__ import annotations

from typing import Any, Optional

import openai

from arag.config import AragSettings


class ChatClient:
    def __init__(self, settings: AragSettings) -> None:
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
        max_tokens: int = 1024,
        thinking: bool = False,
        extra_body: Optional[dict[str, Any]] = None,
    ) -> str:
        """单轮补全，返回最终正文（不含 reasoning_content）。

        ``thinking`` 默认关闭：检索改写/生成等内部调用不需要推理过程，
        关掉可把 qwen3.7-plus 延迟从 ~9s 降到 ~1.3s（实测）。
        """
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

    async def vision(self, prompt: str, image_url: str, *, max_tokens: int = 300) -> str:
        """多模态：对图片 URL 生成描述（qwen3.7-plus 视觉，OpenAI 兼容 content-array）。"""
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }],
            max_tokens=max_tokens,
            extra_body={"enable_thinking": False},
        )
        return resp.choices[0].message.content or ""
