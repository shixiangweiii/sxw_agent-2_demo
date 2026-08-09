"""Atomic immutable search snapshots rebuilt from active rows in ``rag.db``."""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, replace
from typing import Optional

import jieba
import numpy as np
from rank_bm25 import BM25Okapi

from arag.persistence.repository import RagRepository, sha256_hex
from arag.store.base import Chunk
from arag.store.fulltext_index import FullTextIndex
from arag.store.vector_store import VectorStore, _l2_normalize


class ProjectionUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProjectionSnapshot:
    generation: int
    source_digest: str
    chunks: tuple[Chunk, ...]
    matrix: np.ndarray | None
    bm25: BM25Okapi | None
    vector_available: bool
    fulltext_available: bool
    degraded_reason: str | None = None


def _tokenize(text: str) -> list[str]:
    return [token for token in jieba.lcut(text) if token.strip()]


def _empty_snapshot(generation: int = 0) -> ProjectionSnapshot:
    return ProjectionSnapshot(
        generation=generation,
        source_digest=hashlib.sha256(b"").hexdigest(),
        chunks=(),
        matrix=None,
        bm25=None,
        vector_available=True,
        fulltext_available=True,
    )


class ProjectionManager:
    """Owns the one current snapshot; replacement is one Python reference assignment."""

    def __init__(self, repository: RagRepository) -> None:
        self.repository = repository
        self._snapshot = _empty_snapshot()
        self._lock = asyncio.Lock()
        self.vector_store = SnapshotVectorStore(self)
        self.fulltext_index = SnapshotFullTextIndex(self)

    @property
    def snapshot(self) -> ProjectionSnapshot:
        return self._snapshot

    async def rebuild(self) -> ProjectionSnapshot:
        """Build off to the side, then atomically expose all active versions together."""
        async with self._lock:
            stored = await self.repository.list_active_chunks()
            chunks: list[Chunk] = []
            vectors: list[np.ndarray] = []
            vector_available = True
            degraded_reason: str | None = None
            expected_dim: int | None = None
            digest_parts: list[str] = []

            for item in stored:
                metadata = dict(item.metadata)
                metadata.update(
                    {
                        "document_id": item.document_id,
                        "document_version_id": item.version_id,
                        "dataset_id": item.dataset_id,
                        "external_doc_id": item.external_doc_id,
                        "content_hash": item.content_hash,
                        "page": item.page,
                        "span_start": item.span_start,
                        "span_end": item.span_end,
                    }
                )
                chunks.append(
                    Chunk(
                        chunk_id=item.chunk_id,
                        doc_id=item.external_doc_id,
                        title=item.title,
                        content=item.content,
                        metadata=metadata,
                    )
                )
                digest_parts.append(f"{item.version_id}:{item.chunk_id}:{item.content_hash}")
                if item.vector is None or item.vector_dim is None or item.vector_checksum is None:
                    vector_available = False
                    degraded_reason = "MISSING_EMBEDDING"
                    continue
                if sha256_hex(item.vector) != item.vector_checksum:
                    vector_available = False
                    degraded_reason = "EMBEDDING_CHECKSUM_MISMATCH"
                    continue
                vector = np.frombuffer(item.vector, dtype="<f4")
                if vector.size != item.vector_dim:
                    vector_available = False
                    degraded_reason = "EMBEDDING_DIMENSION_MISMATCH"
                    continue
                if expected_dim is None:
                    expected_dim = item.vector_dim
                elif expected_dim != item.vector_dim:
                    vector_available = False
                    degraded_reason = "MIXED_EMBEDDING_DIMENSIONS"
                    continue
                vectors.append(vector)

            matrix: np.ndarray | None = None
            if chunks and vector_available and len(vectors) == len(chunks):
                matrix = _l2_normalize(np.vstack(vectors).astype(np.float32, copy=False))
                matrix.flags.writeable = False
            elif chunks:
                vector_available = False
            tokens = [_tokenize(chunk.content) for chunk in chunks]
            bm25 = BM25Okapi(tokens) if tokens else None
            source_digest = hashlib.sha256("\n".join(digest_parts).encode("utf-8")).hexdigest()
            snapshot = ProjectionSnapshot(
                generation=self._snapshot.generation + 1,
                source_digest=source_digest,
                chunks=tuple(chunks),
                matrix=matrix,
                bm25=bm25,
                vector_available=vector_available,
                fulltext_available=True,
                degraded_reason=degraded_reason,
            )
            self._snapshot = snapshot
            await self.repository.mark_projection_state(
                source_digest=source_digest, state="READY" if vector_available else "DEGRADED"
            )
            return snapshot

    async def clear_memory(self) -> None:
        """Drop derived process state only; the next rebuild uses SQLite truth."""
        async with self._lock:
            self._snapshot = ProjectionSnapshot(
                generation=self._snapshot.generation + 1,
                source_digest="",
                chunks=(),
                matrix=None,
                bm25=None,
                vector_available=False,
                fulltext_available=False,
                degraded_reason="PROJECTION_CLEARED",
            )

    async def invalidate(self, reason: str = "ACTIVE_VERSION_SWITCH") -> None:
        """Prevent serving the old snapshot across an authority pointer switch."""
        async with self._lock:
            current = self._snapshot
            self._snapshot = ProjectionSnapshot(
                generation=current.generation + 1,
                source_digest=current.source_digest,
                chunks=(),
                matrix=None,
                bm25=None,
                vector_available=False,
                fulltext_available=False,
                degraded_reason=reason,
            )


class SnapshotVectorStore(VectorStore):
    def __init__(self, manager: ProjectionManager) -> None:
        self._manager = manager

    async def add(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        raise RuntimeError("vector projection is read-only; persist an index job then rebuild")

    async def search(self, query_vector: list[float], top_k: int = 10) -> list[Chunk]:
        snapshot = self._manager.snapshot
        if not snapshot.vector_available:
            raise ProjectionUnavailableError(snapshot.degraded_reason or "VECTOR_PROJECTION_UNAVAILABLE")
        if snapshot.matrix is None or not snapshot.chunks:
            return []
        query = np.asarray([query_vector], dtype=np.float32)
        if query.shape[1] != snapshot.matrix.shape[1]:
            raise ProjectionUnavailableError(
                f"QUERY_DIMENSION_MISMATCH:{query.shape[1]}!={snapshot.matrix.shape[1]}"
            )
        normalized = _l2_normalize(query)[0]
        scores = snapshot.matrix @ normalized
        order = np.argsort(-scores)[:top_k]
        return [replace(snapshot.chunks[int(index)], score=float(scores[int(index)])) for index in order]

    async def all_chunks(self) -> list[Chunk]:
        return [replace(chunk, score=0.0) for chunk in self._manager.snapshot.chunks]

    async def clear(self) -> None:
        await self._manager.clear_memory()


class SnapshotFullTextIndex(FullTextIndex):
    def __init__(self, manager: ProjectionManager) -> None:
        self._manager = manager

    async def add(self, chunks: list[Chunk]) -> None:
        raise RuntimeError("BM25 projection is read-only; persist an index job then rebuild")

    async def search(self, query: str, top_k: int = 10) -> list[Chunk]:
        snapshot = self._manager.snapshot
        if not snapshot.fulltext_available:
            raise ProjectionUnavailableError(snapshot.degraded_reason or "FULLTEXT_PROJECTION_UNAVAILABLE")
        if snapshot.bm25 is None or not snapshot.chunks:
            return []
        query_tokens = _tokenize(query)
        query_set = set(query_tokens)
        scores = snapshot.bm25.get_scores(query_tokens)
        ranked = sorted(range(len(snapshot.chunks)), key=lambda index: scores[index], reverse=True)
        out: list[Chunk] = []
        for index in ranked:
            lexical_match = bool(query_set.intersection(_tokenize(snapshot.chunks[index].content)))
            if scores[index] <= 0 and not lexical_match:
                continue
            out.append(replace(snapshot.chunks[index], score=max(float(scores[index]), 1e-9)))
            if len(out) >= top_k:
                break
        return out

    async def clear(self) -> None:
        await self._manager.clear_memory()
