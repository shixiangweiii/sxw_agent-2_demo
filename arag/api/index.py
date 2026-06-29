"""文档入库 API：/v1/index（自定义文档）、/v1/index/sample（内置样本库）。"""
from __future__ import annotations

import logging
from collections.abc import Sequence

from fastapi import APIRouter, Request

from arag.context import AragContext
from arag.processor.document import to_document
from arag.sample_data import SAMPLE_DOCUMENTS
from arag.schemas import IndexDocument, IndexRequest, IndexResponse
from arag.store.base import Chunk
from common.obs import get_logger, log_kv

router = APIRouter(prefix="/v1", tags=["index"])
logger = get_logger("arag.index")


async def _index(ctx: AragContext, docs: Sequence[IndexDocument]) -> IndexResponse:
    chunks: list[Chunk] = []
    for d in docs:
        document = to_document(d.doc_id, d.title, d.content, d.metadata)
        document = await ctx.image_processor.enrich(document)   # 多模态：图片 caption 入正文
        chunks.extend(ctx.chunker.split(document))
    if chunks:
        vectors = await ctx.embedder.embed([c.content for c in chunks])
        await ctx.vector_store.add(chunks, vectors)
        await ctx.fulltext_index.add(chunks)
    log_kv(logger, logging.INFO, "Index", "indexed", docs=len(docs), chunks=len(chunks))
    return IndexResponse(indexed_docs=len(docs), indexed_chunks=len(chunks))


@router.post("/index", response_model=IndexResponse)
async def index_documents(req: IndexRequest, request: Request) -> IndexResponse:
    ctx: AragContext = request.app.state.ctx
    return await _index(ctx, req.documents)


@router.post("/index/sample", response_model=IndexResponse)
async def index_sample(request: Request) -> IndexResponse:
    """入库内置样本知识库（开箱即跑演示用）。"""
    ctx: AragContext = request.app.state.ctx
    return await _index(ctx, SAMPLE_DOCUMENTS)
