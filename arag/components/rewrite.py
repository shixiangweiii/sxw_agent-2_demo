"""查询改写：把用户问题扩展为多个等价检索表述以提升召回率。"""
from __future__ import annotations

import logging

from arag.components.llm import ChatClient
from common.obs import get_logger, log_kv

logger = get_logger("arag.rewrite")

_SYS = (
    "你是检索查询改写助手。把用户问题改写成 1-2 个语义等价但表述不同的检索查询，"
    "每行一个，不要编号、不要解释、不要输出原句以外的多余内容。"
)


class QueryRewriter:
    def __init__(self, chat: ChatClient) -> None:
        self._chat = chat

    async def rewrite(self, query: str, max_n: int = 2) -> list[str]:
        """返回 [原始 query, ...改写]；任何异常兜底只返回原始 query。"""
        try:
            text = await self._chat.complete(_SYS, query, max_tokens=128, temperature=0.3)
        except Exception as exc:  # noqa: BLE001 - 改写失败不应阻断检索
            log_kv(logger, logging.WARNING, "QueryRewrite", "rewrite failed, fallback to raw",
                   error=type(exc).__name__)
            return [query]
        extra: list[str] = []
        for line in text.splitlines():
            s = line.strip().lstrip("-•*0123456789.、 ").strip()
            if s and s != query:
                extra.append(s)
        result = [query] + extra[:max_n]
        return list(dict.fromkeys(result))
