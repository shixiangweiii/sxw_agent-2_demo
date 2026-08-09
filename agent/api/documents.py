"""Document indexing proxy for the browser chat UI.

The browser stays same-origin with the agent service and sends extracted text here.
This endpoint forwards the payload to arag /v1/index so the existing RAG retrieval
and citation path remains unchanged.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from agent.config import AgentSettings
from common.obs import get_logger, get_trace_id, log_kv

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])
logger = get_logger("agent.documents")


class WebIndexDocument(BaseModel):
    doc_id: str
    dataset_id: str = "default"
    title: str = ""
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class WebIndexRequest(BaseModel):
    documents: list[WebIndexDocument]


@router.post("/index", status_code=status.HTTP_202_ACCEPTED)
async def index_documents(req: WebIndexRequest, request: Request) -> dict[str, Any]:
    """Forward browser-extracted documents to arag /v1/index."""
    if not req.documents:
        raise HTTPException(status_code=400, detail="documents must not be empty")

    settings: AgentSettings = request.app.state.settings
    url = f"{settings.arag_base_url.rstrip('/')}/v1/index"
    payload = req.model_dump()
    try:
        async with httpx.AsyncClient(timeout=settings.arag_timeout_ms / 1000.0) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"x-trace-id": get_trace_id()},
            )
            resp.raise_for_status()
            upstream_status = resp.status_code
            data = resp.json()
    except Exception as exc:  # noqa: BLE001 - UI should receive a clear arag unavailable status
        log_kv(
            logger,
            logging.WARNING,
            "DocumentIndex",
            "arag index failed",
            error=type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="文档入库失败，检查 arag 服务") from exc

    job_ids = data.get("job_ids")
    if upstream_status != status.HTTP_202_ACCEPTED or not isinstance(job_ids, list):
        raise HTTPException(status_code=502, detail="ARAG 未返回 durable index job 契约")
    if len(job_ids) != len(req.documents) or any(not isinstance(item, str) or not item for item in job_ids):
        raise HTTPException(status_code=502, detail="ARAG index job 数量与文档数量不一致")

    log_kv(
        logger,
        logging.INFO,
        "DocumentIndex",
        "durably accepted",
        docs=len(req.documents),
        job_ids=job_ids,
    )
    return data


@router.get("/index/jobs/{job_id}")
async def get_index_job(job_id: str, request: Request) -> dict[str, Any]:
    """Same-origin proxy used by the Web UI to wait for durable activation."""
    settings: AgentSettings = request.app.state.settings
    url = f"{settings.arag_base_url.rstrip('/')}/v1/index/jobs/{job_id}"
    try:
        async with httpx.AsyncClient(timeout=settings.arag_timeout_ms / 1000.0) as client:
            response = await client.get(url, headers={"x-trace-id": get_trace_id()})
            response.raise_for_status()
            return response.json()
    except Exception as exc:  # noqa: BLE001 - stable same-origin failure for UI
        raise HTTPException(status_code=503, detail="索引任务查询失败，检查 arag 服务") from exc
