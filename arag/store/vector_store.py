"""向量库端口 + 本地持久化实现（numpy 余弦）。"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np

from arag.store.base import Chunk
from common.obs import get_logger, log_kv

SCHEMA_VERSION = 1
logger = get_logger("arag.vector_store")


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
    """本地向量库：L2 归一化后用点积检索，并将向量和 chunk 元数据落盘。"""

    def __init__(self, storage_dir: str | Path = "local_storage/embedding") -> None:
        self._storage_dir = Path(storage_dir)
        self._manifest_path = self._storage_dir / "manifest.json"
        self._chunks_path = self._storage_dir / "chunks.json"
        self._vectors_path = self._storage_dir / "vectors.npy"
        self._chunks: list[Chunk] = []
        self._matrix: Optional[np.ndarray] = None
        self._load()

    def _chunk_to_dict(self, chunk: Chunk) -> dict[str, Any]:
        return {
            "chunk_id": chunk.chunk_id,
            "doc_id": chunk.doc_id,
            "title": chunk.title,
            "content": chunk.content,
            "metadata": chunk.metadata,
        }

    def _chunk_from_dict(self, data: dict[str, Any]) -> Chunk:
        return Chunk(
            chunk_id=str(data["chunk_id"]),
            doc_id=str(data["doc_id"]),
            title=str(data.get("title", "")),
            content=str(data.get("content", "")),
            metadata=dict(data.get("metadata") or {}),
        )

    def _load(self) -> None:
        required = [self._manifest_path, self._chunks_path, self._vectors_path]
        if not any(path.exists() for path in required):
            return
        if not all(path.exists() for path in required):
            log_kv(
                logger,
                logging.WARNING,
                "VectorStore",
                "partial embedding storage ignored",
                storage_dir=str(self._storage_dir),
            )
            return
        try:
            manifest = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            if int(manifest.get("schema_version", 0)) != SCHEMA_VERSION:
                raise ValueError(f"unsupported schema_version={manifest.get('schema_version')}")

            raw_chunks = json.loads(self._chunks_path.read_text(encoding="utf-8"))
            chunks = [self._chunk_from_dict(item) for item in raw_chunks]
            matrix = np.load(self._vectors_path).astype(np.float32, copy=False)
            if matrix.ndim == 1:
                matrix = matrix.reshape(1, -1)
            if matrix.ndim != 2:
                raise ValueError(f"vectors.npy must be a 2D matrix, got ndim={matrix.ndim}")
            if len(chunks) != matrix.shape[0]:
                raise ValueError(f"chunks/vectors count mismatch: {len(chunks)} != {matrix.shape[0]}")

            self._chunks = chunks
            self._matrix = matrix if chunks else None
            log_kv(
                logger,
                logging.INFO,
                "VectorStore",
                "loaded persisted embeddings",
                storage_dir=str(self._storage_dir),
                count=len(self._chunks),
                vector_dim=int(matrix.shape[1]) if matrix.ndim == 2 else 0,
            )
        except Exception as exc:
            self._chunks = []
            self._matrix = None
            log_kv(
                logger,
                logging.WARNING,
                "VectorStore",
                "failed to load persisted embeddings, start empty",
                storage_dir=str(self._storage_dir),
                error=str(exc),
            )

    def _matrix_or_empty(self) -> np.ndarray:
        if self._matrix is None:
            return np.zeros((0, 0), dtype=np.float32)
        return self._matrix.astype(np.float32, copy=False)

    def _persist(self) -> None:
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        matrix = self._matrix_or_empty()
        vector_dim = int(matrix.shape[1]) if matrix.ndim == 2 and matrix.shape[0] else 0
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "vector_dim": vector_dim,
            "count": len(self._chunks),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        chunks_tmp = self._chunks_path.with_suffix(".json.tmp")
        vectors_tmp = self._vectors_path.with_suffix(".npy.tmp")
        manifest_tmp = self._manifest_path.with_suffix(".json.tmp")

        chunks_tmp.write_text(
            json.dumps([self._chunk_to_dict(c) for c in self._chunks], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        chunks_tmp.replace(self._chunks_path)

        with vectors_tmp.open("wb") as f:
            np.save(f, matrix)
        vectors_tmp.replace(self._vectors_path)

        manifest_tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest_tmp.replace(self._manifest_path)

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

        self._persist()

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
        for path in (
            self._manifest_path,
            self._chunks_path,
            self._vectors_path,
            self._manifest_path.with_suffix(".json.tmp"),
            self._chunks_path.with_suffix(".json.tmp"),
            self._vectors_path.with_suffix(".npy.tmp"),
        ):
            if path.exists():
                path.unlink()
