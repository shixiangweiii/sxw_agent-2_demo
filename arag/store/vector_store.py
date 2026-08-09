"""向量库端口 + 非权威的本地内存实现（numpy 余弦）。

服务主链路使用 :mod:`arag.projection.snapshot`。这里的实现只保留为端口级
实验替身；它不读写磁盘，避免与 ``rag.db`` 形成双事实源。
"""
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
    async def all_chunks(self) -> list[Chunk]:
        ...

    @abstractmethod
    async def clear(self) -> None:
        ...


class LocalVectorStore(VectorStore):
    """本地临时向量投影；进程退出即丢弃，绝不作为 Document truth。"""

    def __init__(self) -> None:
        self._chunks: list[Chunk] = []
        self._matrix: Optional[np.ndarray] = None

    async def add(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        if not chunks:
            return
        if len(chunks) != len(vectors):
            raise ValueError(f"chunks/vectors count mismatch: {len(chunks)} != {len(vectors)}")

        mat = _l2_normalize(np.asarray(vectors, dtype=np.float32))
        index_by_chunk_id = {chunk.chunk_id: i for i, chunk in enumerate(self._chunks)}
        appended_rows: list[np.ndarray] = []

        for row_idx, chunk in enumerate(chunks):
            existing_idx = index_by_chunk_id.get(chunk.chunk_id)
            if existing_idx is None:
                index_by_chunk_id[chunk.chunk_id] = len(self._chunks)
                self._chunks.append(chunk)
                appended_rows.append(mat[row_idx])
            else:
                self._chunks[existing_idx] = chunk
                if self._matrix is None:
                    raise RuntimeError("vector store invariant violated: chunks exist without matrix")
                self._matrix[existing_idx] = mat[row_idx]

        if appended_rows:
            append_mat = np.vstack(appended_rows).astype(np.float32, copy=False)
            self._matrix = append_mat if self._matrix is None else np.vstack([self._matrix, append_mat])


    async def search(self, query_vector: list[float], top_k: int = 10) -> list[Chunk]:
        if self._matrix is None or not self._chunks:
            return []
        q = _l2_normalize(np.asarray([query_vector], dtype=np.float32))[0]
        scores = self._matrix @ q
        order = np.argsort(-scores)[:top_k]
        return [replace(self._chunks[int(i)], score=float(scores[int(i)])) for i in order]

    async def all_chunks(self) -> list[Chunk]:
        return [replace(chunk, score=0.0) for chunk in self._chunks]

    async def clear(self) -> None:
        self._chunks = []
        self._matrix = None
