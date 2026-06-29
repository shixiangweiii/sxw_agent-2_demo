"""全文索引端口 + 本地实现（BM25 + jieba 中文分词）。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import replace
from typing import Optional

import jieba
from rank_bm25 import BM25Okapi

from arag.store.base import Chunk


def _tokenize(text: str) -> list[str]:
    return [t for t in jieba.lcut(text) if t.strip()]


class FullTextIndex(ABC):
    """全文索引端口（依赖倒置）。本地用 BM25 + jieba；生产可换 Elasticsearch。"""

    @abstractmethod
    async def add(self, chunks: list[Chunk]) -> None:
        ...

    @abstractmethod
    async def search(self, query: str, top_k: int = 10) -> list[Chunk]:
        ...

    @abstractmethod
    async def clear(self) -> None:
        ...


class LocalBM25Index(FullTextIndex):
    """内存 BM25：按 chunk_id upsert，每次 add 后重建索引（demo 量级足够）。"""

    def __init__(self) -> None:
        self._chunks: list[Chunk] = []
        self._tokens: list[list[str]] = []
        self._bm25: Optional[BM25Okapi] = None

    async def add(self, chunks: list[Chunk]) -> None:
        index_by_chunk_id = {chunk.chunk_id: i for i, chunk in enumerate(self._chunks)}
        for chunk in chunks:
            existing_idx = index_by_chunk_id.get(chunk.chunk_id)
            if existing_idx is None:
                index_by_chunk_id[chunk.chunk_id] = len(self._chunks)
                self._chunks.append(chunk)
                self._tokens.append(_tokenize(chunk.content))
            else:
                self._chunks[existing_idx] = chunk
                self._tokens[existing_idx] = _tokenize(chunk.content)
        self._bm25 = BM25Okapi(self._tokens) if self._tokens else None

    async def search(self, query: str, top_k: int = 10) -> list[Chunk]:
        if self._bm25 is None or not self._chunks:
            return []
        scores = self._bm25.get_scores(_tokenize(query))
        ranked = sorted(range(len(self._chunks)), key=lambda i: scores[i], reverse=True)
        out: list[Chunk] = []
        for i in ranked[:top_k]:
            if scores[i] <= 0:
                continue
            out.append(replace(self._chunks[i], score=float(scores[i])))
        return out

    async def clear(self) -> None:
        self._chunks = []
        self._tokens = []
        self._bm25 = None
