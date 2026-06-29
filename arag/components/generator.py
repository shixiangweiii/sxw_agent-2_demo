"""答案生成：仅依据召回资料作答，并以 [n] 标注引用（arag 独立 RAG 能力）。

注意：demo 主链路由 agent 侧 LLM 生成；此 generator 让 arag 也能独立端到端问答（/v1/rag）。
"""
from __future__ import annotations

from arag.components.llm import ChatClient
from arag.store.base import Chunk

_SYS = (
    "你是知识问答助手。只能依据提供的资料回答；在引用某条资料处用 [n] 标注其序号；"
    "若资料不足以回答，请如实说明，不要编造。"
)
_NO_RESULT = "未检索到相关资料，无法回答该问题。"


class Generator:
    def __init__(self, chat: ChatClient) -> None:
        self._chat = chat

    async def generate(self, query: str, chunks: list[Chunk]) -> str:
        if not chunks:
            return _NO_RESULT
        context = "\n\n".join(
            f"[{i + 1}] {c.title}\n{c.content}" for i, c in enumerate(chunks)
        )
        user = f"问题：{query}\n\n资料：\n{context}\n\n请基于以上资料回答，并用 [n] 标注引用来源。"
        return await self._chat.complete(_SYS, user, max_tokens=800)
