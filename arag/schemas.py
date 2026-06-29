"""arag HTTP 请求/响应模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class IndexDocument(BaseModel):
    doc_id: str
    title: str = ""
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class IndexRequest(BaseModel):
    documents: list[IndexDocument]


class IndexResponse(BaseModel):
    indexed_docs: int
    indexed_chunks: int


class RetrieveRequest(BaseModel):
    query: str
    top_k: int = 6
    use_rewrite: bool = True


class RetrievedChunk(BaseModel):
    chunk_id: str
    doc_id: str
    title: str
    content: str
    score: float
    source: str          # 命中来源：vector | fulltext | fused


class RetrieveResponse(BaseModel):
    query: str
    rewrites: list[str]
    chunks: list[RetrievedChunk]
    cost_ms: int


class RagRequest(BaseModel):
    query: str
    top_k: int = 6


class RagResponse(BaseModel):
    answer: str
    chunks: list[RetrievedChunk]
    cost_ms: int
