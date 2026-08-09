"""检索 API：/v1/retrieve（混合召回，agent 调它）、/v1/rag（arag 独立端到端问答）。"""
from __future__ import annotations

import asyncio
import logging
import hashlib
import time
import uuid

from fastapi import APIRouter, Request

from arag.context import AragContext
from arag.schemas import (
    RagRequest,
    RagResponse,
    RetrievedChunk,
    RetrieveRequest,
    RetrieveResponse,
    RetrievalStatus,
)
from arag.store.base import Chunk
from common.obs import get_logger, log_kv

router = APIRouter(prefix="/v1", tags=["retrieve"])
logger = get_logger("arag.retrieve")


def _to_out(
    chunks: list[Chunk], sources: dict[str, str], *, query_id: str
) -> list[RetrievedChunk]:
    return [
        RetrievedChunk(
            chunk_id=c.chunk_id,
            doc_id=c.doc_id,
            title=c.title,
            content=c.content,
            score=c.score,
            source=sources.get(c.chunk_id, "fused"),
            evidence_id="ev_" + hashlib.sha256(f"{query_id}:{c.chunk_id}".encode("utf-8")).hexdigest(),
            document_id=str(c.metadata["document_id"]),
            document_version_id=str(c.metadata["document_version_id"]),
            index_version=str(c.metadata["document_version_id"]),
            content_hash=str(c.metadata["content_hash"]),
            dataset_id=str(c.metadata.get("dataset_id", "default")),
            scope=str(c.metadata.get("scope", "public")),
            query_id=query_id,
            page=c.metadata.get("page"),
            span_start=int(c.metadata.get("span_start", 0)),
            span_end=int(c.metadata.get("span_end", len(c.content))),
        )
        for c in chunks
    ]


@router.post("/retrieve", response_model=RetrieveResponse)
async def retrieve(req: RetrieveRequest, request: Request) -> RetrieveResponse:
    ctx: AragContext = request.app.state.ctx
    t0 = time.perf_counter()
    query_id = req.query_id or f"qry_{uuid.uuid4()}"
    if req.scope != "public" or not req.datasets or any(not item.strip() for item in req.datasets):
        cost_ms = int((time.perf_counter() - t0) * 1000)
        return RetrieveResponse(
            status=RetrievalStatus.DENIED,
            query=req.query,
            query_id=query_id,
            rewrites=[req.query],
            chunks=[],
            cost_ms=cost_ms,
            degraded_reasons=["SCOPE_OR_DATASET_DENIED"],
        )
    remaining_seconds = (
        req.deadline_at.timestamp() - time.time()
        if req.deadline_at is not None
        else None
    )
    if remaining_seconds is not None and remaining_seconds <= 0:
        return _deadline_exceeded(req, query_id=query_id, started_at=t0)
    try:
        if remaining_seconds is None:
            result = await ctx.retriever.retrieve_detailed(
                req.query,
                req.top_k,
                req.use_rewrite,
                datasets=set(req.datasets),
                scope=req.scope,
            )
        else:
            # One absolute budget covers rewrite, query embedding and both
            # retrieval branches. asyncio cancellation propagates into their
            # HTTP awaits instead of starting a fresh timeout per component.
            async with asyncio.timeout(remaining_seconds):
                result = await ctx.retriever.retrieve_detailed(
                    req.query,
                    req.top_k,
                    req.use_rewrite,
                    datasets=set(req.datasets),
                    scope=req.scope,
                )
    except TimeoutError:
        return _deadline_exceeded(req, query_id=query_id, started_at=t0)
    cost_ms = int((time.perf_counter() - t0) * 1000)
    log_kv(logger, logging.INFO, "QaRetrieve", "served", query=req.query,
           returned=len(result.chunks), status=result.status.value, cost_ms=cost_ms)
    return RetrieveResponse(
        status=result.status,
        query=req.query,
        query_id=query_id,
        rewrites=result.rewrites,
        chunks=_to_out(result.chunks, result.sources, query_id=query_id),
        cost_ms=cost_ms,
        degraded_reasons=result.degraded_reasons,
    )


def _deadline_exceeded(
    req: RetrieveRequest,
    *,
    query_id: str,
    started_at: float,
) -> RetrieveResponse:
    cost_ms = int((time.perf_counter() - started_at) * 1000)
    log_kv(
        logger,
        logging.WARNING,
        "QaRetrieve",
        "absolute deadline exceeded",
        query=req.query,
        query_id=query_id,
        cost_ms=cost_ms,
    )
    return RetrieveResponse(
        status=RetrievalStatus.ERROR,
        query=req.query,
        query_id=query_id,
        rewrites=[req.query],
        chunks=[],
        cost_ms=cost_ms,
        degraded_reasons=["DEADLINE_EXCEEDED"],
    )


@router.post("/rag", response_model=RagResponse)
async def rag(req: RagRequest, request: Request) -> RagResponse:
    ctx: AragContext = request.app.state.ctx
    t0 = time.perf_counter()
    chunks, _rewrites, sources = await ctx.retriever.retrieve(req.query, req.top_k)
    answer = await ctx.generator.generate(req.query, chunks)
    cost_ms = int((time.perf_counter() - t0) * 1000)
    log_kv(logger, logging.INFO, "Rag", "served", query=req.query,
           returned=len(chunks), cost_ms=cost_ms)
    query_id = f"qry_{uuid.uuid4()}"
    return RagResponse(
        answer=answer, chunks=_to_out(chunks, sources, query_id=query_id), cost_ms=cost_ms
    )
