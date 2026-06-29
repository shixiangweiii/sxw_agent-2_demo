"""混合召回编排：query → rewrite → (向量 + 全文) 双路召回 → RRF 融合 → 过滤。"""
from __future__ import annotations

import logging

from arag.components.embedding import Embedder
from arag.components.filter import FilterConfig, low_value_filter
from arag.components.reranker import rrf_fuse
from arag.components.rewrite import QueryRewriter
from arag.store.base import Chunk
from arag.store.fulltext_index import FullTextIndex
from arag.store.vector_store import VectorStore
from common.obs import get_logger, log_kv

logger = get_logger("arag.retriever")


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
        rewrites = await self._rewriter.rewrite(query) if use_rewrite else [query]

        # 向量召回：对每个改写 embed + search
        query_vectors = await self._embedder.embed(rewrites)
        vector_hits: list[Chunk] = []
        for qv in query_vectors:
            vector_hits.extend(await self._vector_store.search(qv, self._branch_top_k))

        # 全文召回：对每个改写 BM25 search
        fulltext_hits: list[Chunk] = []
        for q in rewrites:
            fulltext_hits.extend(await self._fulltext_index.search(q, self._branch_top_k))

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

        log_kv(logger, logging.INFO, "QaRetrieve", "hybrid retrieve done",
               query=query, rewrites=len(rewrites), vector_hits=len(vlist),
               fulltext_hits=len(flist), returned=len(filtered))
        return filtered, rewrites, sources
