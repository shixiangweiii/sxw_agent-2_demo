"""重排：互惠排名融合（RRF, Reciprocal Rank Fusion）。

混合召回的核心——把"向量"与"全文"两路排序无量纲地融合，
兼顾语义相似（向量）与关键词精确（BM25），无需训练 rerank 模型。
"""
from __future__ import annotations

from dataclasses import replace

from arag.store.base import Chunk


def rrf_fuse(ranked_lists: list[list[Chunk]], k: int = 60, top_k: int = 10) -> list[Chunk]:
    """RRF：score(d) = Σ 1/(k + rank_i(d))；rank 从 0 起。"""
    scores: dict[str, float] = {}
    best: dict[str, Chunk] = {}
    for ranked in ranked_lists:
        for rank, chunk in enumerate(ranked):
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (k + rank + 1)
            best.setdefault(chunk.chunk_id, chunk)
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
    return [replace(best[cid], score=float(score)) for cid, score in ordered]
