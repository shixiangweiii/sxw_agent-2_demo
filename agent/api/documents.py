"""Document indexing proxy for the browser chat UI.

The browser stays same-origin with the agent service and sends extracted text here.
This endpoint forwards the payload to arag /v1/index so the existing RAG retrieval
and citation path remains unchanged.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from agent.context import AgentContext
from common.obs import get_logger, get_trace_id, log_kv

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])
logger = get_logger("agent.documents")


class WebIndexDocument(BaseModel):
    doc_id: str
    title: str = ""
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class WebIndexRequest(BaseModel):
    documents: list[WebIndexDocument]


@router.post("/index")
async def index_documents(req: WebIndexRequest, request: Request) -> dict[str, Any]:
    """Forward browser-extracted documents to arag /v1/index."""
    if not req.documents:
        raise HTTPException(status_code=400, detail="documents must not be empty")

    ctx: AgentContext = request.app.state.ctx
    url = f"{ctx.settings.arag_base_url.rstrip('/')}/v1/index"
    payload = req.model_dump()
    try:
        async with httpx.AsyncClient(timeout=ctx.settings.arag_timeout_ms / 1000.0) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"x-trace-id": get_trace_id()},
            )
            resp.raise_for_status()
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

    log_kv(
        logger,
        logging.INFO,
        "DocumentIndex",
        "indexed",
        docs=len(req.documents),
        indexed_docs=data.get("indexed_docs"),
        indexed_chunks=data.get("indexed_chunks"),
    )
    return data
