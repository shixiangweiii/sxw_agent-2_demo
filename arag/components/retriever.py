"""混合召回编排：query → rewrite → (向量 + 全文) 双路召回 → RRF 融合 → 过滤。"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from arag.components.embedding import Embedder
from arag.components.filter import FilterConfig, low_value_filter
from arag.components.reranker import rrf_fuse
from arag.components.rewrite import QueryRewriter
from arag.store.base import Chunk
from arag.store.fulltext_index import FullTextIndex
from arag.store.vector_store import VectorStore
from arag.schemas import RetrievalStatus
from arag.projection.snapshot import ProjectionUnavailableError
from common.obs import get_logger, log_kv

logger = get_logger("arag.retriever")


@dataclass(frozen=True, slots=True)
class HybridRetrievalResult:
    chunks: list[Chunk]
    rewrites: list[str]
    sources: dict[str, str]
    status: RetrievalStatus
    degraded_reasons: list[str]


def _dedup_keep_best(chunks: list[Chunk]) -> list[Chunk]:
    best: dict[str, Chunk] = {}
    for c in chunks:
        if c.chunk_id not in best or c.score > best[c.chunk_id].score:
            best[c.chunk_id] = c
    return sorted(best.values(), key=lambda c: c.score, reverse=True)


class HybridRetriever:
    def __init__(
        self,
        vector_store: VectorStore,
        fulltext_index: FullTextIndex,
        embedder: Embedder,
        rewriter: QueryRewriter,
        *,
        branch_top_k: int = 10,
    ) -> None:
        self._vector_store = vector_store
        self._fulltext_index = fulltext_index
        self._embedder = embedder
        self._rewriter = rewriter
        self._branch_top_k = branch_top_k

    async def retrieve(
        self,
        query: str,
        top_k: int = 6,
        use_rewrite: bool = True,
    ) -> tuple[list[Chunk], list[str], dict[str, str]]:
        result = await self.retrieve_detailed(query, top_k, use_rewrite)
        return result.chunks, result.rewrites, result.sources

    async def retrieve_detailed(
        self,
        query: str,
        top_k: int = 6,
        use_rewrite: bool = True,
        *,
        datasets: set[str] | None = None,
        scope: str = "public",
    ) -> HybridRetrievalResult:
        rewrites = await self._rewriter.rewrite(query) if use_rewrite else [query]
        failures: list[str] = []
        projection_unavailable_failures = 0

        # 向量召回：对每个改写 embed + search
        vector_hits: list[Chunk] = []
        try:
            query_vectors = await self._embedder.embed(rewrites)
            for qv in query_vectors:
                vector_hits.extend(await self._vector_store.search(qv, self._branch_top_k))
        except Exception as exc:  # noqa: BLE001 - the healthy branch remains useful
            failures.append(f"vector:{type(exc).__name__}")
            projection_unavailable_failures += isinstance(exc, ProjectionUnavailableError)

        # 全文召回：对每个改写 BM25 search
        fulltext_hits: list[Chunk] = []
        try:
            for q in rewrites:
                fulltext_hits.extend(await self._fulltext_index.search(q, self._branch_top_k))
        except Exception as exc:  # noqa: BLE001 - the healthy branch remains useful
            failures.append(f"fulltext:{type(exc).__name__}")
            projection_unavailable_failures += isinstance(exc, ProjectionUnavailableError)

        def accessible(chunk: Chunk) -> bool:
            chunk_dataset = str(chunk.metadata.get("dataset_id", "default"))
            chunk_scope = str(chunk.metadata.get("scope", "public"))
            return (datasets is None or chunk_dataset in datasets) and chunk_scope == scope

        vector_hits = [chunk for chunk in vector_hits if accessible(chunk)]
        fulltext_hits = [chunk for chunk in fulltext_hits if accessible(chunk)]

        vlist = _dedup_keep_best(vector_hits)
        flist = _dedup_keep_best(fulltext_hits)
        vset = {c.chunk_id for c in vlist}
        fset = {c.chunk_id for c in flist}

        def source_of(cid: str) -> str:
            if cid in vset and cid in fset:
                return "fused"
            return "vector" if cid in vset else "fulltext"

        fused = rrf_fuse([vlist, flist], top_k=top_k * 2)
        filtered = low_value_filter(fused, FilterConfig(min_chars=2))[:top_k]
        sources = {c.chunk_id: source_of(c.chunk_id) for c in filtered}

        if len(failures) == 2 and projection_unavailable_failures == 2:
            # Known rebuild/invalidation window: authority remains healthy, projections are
            # temporarily unavailable, so this is DEGRADED rather than a transport ERROR.
            status = RetrievalStatus.DEGRADED
        elif len(failures) == 2:
            status = RetrievalStatus.ERROR
        elif failures:
            status = RetrievalStatus.DEGRADED
        elif filtered:
            status = RetrievalStatus.HIT
        else:
            status = RetrievalStatus.MISS

        log_kv(logger, logging.INFO, "QaRetrieve", "hybrid retrieve done",
               query=query, rewrites=len(rewrites), vector_hits=len(vlist),
               fulltext_hits=len(flist), returned=len(filtered))
        return HybridRetrievalResult(
            chunks=filtered,
            rewrites=rewrites,
            sources=sources,
            status=status,
            degraded_reasons=failures,
        )
