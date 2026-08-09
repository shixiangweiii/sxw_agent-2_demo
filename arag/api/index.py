"""文档入库 API：/v1/index（自定义文档）、/v1/index/sample（内置样本库）。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from collections.abc import Sequence

from fastapi import APIRouter, HTTPException, Request, status

from arag.context import AragContext
from arag.sample_data import SAMPLE_DOCUMENTS
from arag.schemas import IndexDocument, IndexJobResponse, IndexRequest, IndexResponse
from common.obs import get_logger, log_kv

router = APIRouter(prefix="/v1", tags=["index"])
logger = get_logger("arag.index")


async def _index(ctx: AragContext, docs: Sequence[IndexDocument]) -> IndexResponse:
    job_ids: list[str] = []
    reused_job_ids: list[str] = []
    for d in docs:
        submitted = await ctx.index_coordinator.submit(
            dataset_id=d.dataset_id,
            external_doc_id=d.doc_id,
            title=d.title,
            content=d.content,
            metadata=d.metadata,
        )
        job_ids.append(submitted.job_id)
        if submitted.reused:
            reused_job_ids.append(submitted.job_id)
    log_kv(logger, logging.INFO, "Index", "durably accepted", docs=len(docs), jobs=len(job_ids))
    return IndexResponse(
        accepted_docs=len(docs), job_ids=job_ids, reused_job_ids=reused_job_ids
    )


@router.post("/index", response_model=IndexResponse, status_code=status.HTTP_202_ACCEPTED)
async def index_documents(req: IndexRequest, request: Request) -> IndexResponse:
    ctx: AragContext = request.app.state.ctx
    return await _index(ctx, req.documents)


@router.post("/index/sample", response_model=IndexResponse, status_code=status.HTTP_202_ACCEPTED)
async def index_sample(request: Request) -> IndexResponse:
    """入库内置样本知识库（开箱即跑演示用）。"""
    ctx: AragContext = request.app.state.ctx
    return await _index(ctx, SAMPLE_DOCUMENTS)


@router.get("/index/jobs/{job_id}", response_model=IndexJobResponse)
async def get_index_job(job_id: str, request: Request) -> IndexJobResponse:
    ctx: AragContext = request.app.state.ctx
    job = await ctx.repository.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"code": "INDEX_JOB_NOT_FOUND"})
    error = None
    if job.error_code is not None:
        error = {"code": job.error_code, "message": job.error_message or ""}
    return IndexJobResponse(
        job_id=job.job_id,
        document_id=job.document_id,
        document_version_id=job.version_id,
        dataset_id=job.dataset_id,
        doc_id=job.external_doc_id,
        state=job.state.value,
        attempt=job.attempt,
        error=error,
        created_at=_rfc3339(job.created_at),
        updated_at=_rfc3339(job.updated_at),
    )


def _rfc3339(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
