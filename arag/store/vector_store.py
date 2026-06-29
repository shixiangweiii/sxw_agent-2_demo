"""向量库端口 + 本地实现（numpy 余弦）。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import replace
from typing import Optional

import numpy as np

from arag.store.base import Chunk


def _l2_normalize(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


class VectorStore(ABC):
    """向量库端口（依赖倒置）。本地用 numpy 余弦；生产可换 pgvector / Milvus。"""

    @abstractmethod
    async def add(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        ...

    @abstractmethod
    async def search(self, query_vector: list[float], top_k: int = 10) -> list[Chunk]:
        ...

    @abstractmethod
    async def clear(self) -> None:
        ...


class LocalVectorStore(VectorStore):
    """内存向量库：L2 归一化后用点积（= 余弦）检索。"""

    def __init__(self) -> None:
        self._chunks: list[Chunk] = []
        self._matrix: Optional[np.ndarray] = None

    async def add(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        if not chunks:
            return
        mat = _l2_normalize(np.asarray(vectors, dtype=np.float32))
        self._matrix = mat if self._matrix is None else np.vstack([self._matrix, mat])
        self._chunks.extend(chunks)

    async def search(self, query_vector: list[float], top_k: int = 10) -> list[Chunk]:
        if self._matrix is None or not self._chunks:
            return []
        q = _l2_normalize(np.asarray([query_vector], dtype=np.float32))[0]
        scores = self._matrix @ q
        order = np.argsort(-scores)[:top_k]
        return [replace(self._chunks[int(i)], score=float(scores[int(i)])) for i in order]

    async def clear(self) -> None:
        self._chunks = []
        self._matrix = None
