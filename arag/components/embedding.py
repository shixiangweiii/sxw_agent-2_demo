"""嵌入客户端：DashScope text-embedding-v3（OpenAI 兼容端点直连）。"""
from __future__ import annotations

import logging

import openai

from arag.config import AragSettings
from common.obs import get_logger, log_kv

logger = get_logger("arag.embedding")

# DashScope 批量嵌入单次上限（保守取 10）
_BATCH_SIZE = 10


class Embedder:
    def __init__(self, settings: AragSettings) -> None:
        self._client = openai.AsyncOpenAI(
            api_key=settings.dashscope_api_key,
            base_url=settings.llm_base_url,
        )
        self._model = settings.embedding_model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入；自动分批以适配上游 batch 上限。"""
        out: list[list[float]] = []
        for i in range(0, len(texts), _BATCH_SIZE):
            batch = texts[i:i + _BATCH_SIZE]
            resp = await self._client.embeddings.create(model=self._model, input=batch)
            out.extend([d.embedding for d in resp.data])
        log_kv(logger, logging.INFO, "Embedding", "embedded", count=len(texts), model=self._model)
        return out

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed([text]))[0]
