"""检索 API：/v1/retrieve（混合召回，agent 调它）、/v1/rag（arag 独立端到端问答）。"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Request

from arag.context import AragContext
from arag.schemas import (
    RagRequest,
    RagResponse,
    RetrievedChunk,
    RetrieveRequest,
    RetrieveResponse,
)
from arag.store.base import Chunk
from common.obs import get_logger, log_kv

router = APIRouter(prefix="/v1", tags=["retrieve"])
logger = get_logger("arag.retrieve")


def _to_out(chunks: list[Chunk], sources: dict[str, str]) -> list[RetrievedChunk]:
    return [
        RetrievedChunk(
            chunk_id=c.chunk_id,
            doc_id=c.doc_id,
            title=c.title,
            content=c.content,
            score=c.score,
            source=sources.get(c.chunk_id, "fused"),
        )
        for c in chunks
    ]


@router.post("/retrieve", response_model=RetrieveResponse)
async def retrieve(req: RetrieveRequest, request: Request) -> RetrieveResponse:
    ctx: AragContext = request.app.state.ctx
    t0 = time.perf_counter()
    chunks, rewrites, sources = await ctx.retriever.retrieve(req.query, req.top_k, req.use_rewrite)
    cost_ms = int((time.perf_counter() - t0) * 1000)
    log_kv(logger, logging.INFO, "QaRetrieve", "served", query=req.query,
           returned=len(chunks), cost_ms=cost_ms)
    return RetrieveResponse(query=req.query, rewrites=rewrites,
                            chunks=_to_out(chunks, sources), cost_ms=cost_ms)


@router.post("/rag", response_model=RagResponse)
async def rag(req: RagRequest, request: Request) -> RagResponse:
    ctx: AragContext = request.app.state.ctx
    t0 = time.perf_counter()
    chunks, _rewrites, sources = await ctx.retriever.retrieve(req.query, req.top_k)
    answer = await ctx.generator.generate(req.query, chunks)
    cost_ms = int((time.perf_counter() - t0) * 1000)
    log_kv(logger, logging.INFO, "Rag", "served", query=req.query,
           returned=len(chunks), cost_ms=cost_ms)
    return RagResponse(answer=answer, chunks=_to_out(chunks, sources), cost_ms=cost_ms)
