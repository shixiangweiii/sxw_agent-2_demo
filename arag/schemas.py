"""arag HTTP 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class IndexDocument(BaseModel):
    doc_id: str = Field(min_length=1, max_length=512)
    dataset_id: str = Field(default="default", min_length=1, max_length=256)
    title: str = ""
    content: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class IndexRequest(BaseModel):
    documents: list[IndexDocument] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def reject_ambiguous_duplicate_documents(self) -> "IndexRequest":
        identities = [(item.dataset_id, item.doc_id) for item in self.documents]
        if len(identities) != len(set(identities)):
            raise ValueError("one index request cannot contain duplicate dataset_id/doc_id pairs")
        return self


class IndexResponse(BaseModel):
    accepted_docs: int
    job_ids: list[str]
    reused_job_ids: list[str] = Field(default_factory=list)


class IndexJobResponse(BaseModel):
    job_id: str
    document_id: str
    document_version_id: str
    dataset_id: str
    doc_id: str
    state: Literal["PREPARED", "BUILDING", "VALIDATING", "READY", "ACTIVATED", "FAILED"]
    attempt: int
    error: dict[str, str] | None = None
    created_at: str
    updated_at: str


class RetrievalStatus(StrEnum):
    HIT = "HIT"
    MISS = "MISS"
    DEGRADED = "DEGRADED"
    DENIED = "DENIED"
    ERROR = "ERROR"


class RetrieveRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=6, ge=1, le=50)
    use_rewrite: bool = True
    query_id: str | None = None
    run_id: str | None = None
    activity_id: str | None = None
    principal_id: str = "demo-user"
    scope: str = "public"
    datasets: list[str] = Field(default_factory=lambda: ["default"])
    deadline_at: datetime | None = None

    @field_validator("deadline_at")
    @classmethod
    def require_absolute_deadline(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("deadline_at must be an absolute RFC3339 timestamp")
        return value


class RetrievedChunk(BaseModel):
    chunk_id: str
    doc_id: str
    title: str
    content: str
    score: float
    source: str          # 命中来源：vector | fulltext | fused
    evidence_id: str
    document_id: str
    document_version_id: str
    index_version: str
    content_hash: str
    dataset_id: str
    scope: str
    query_id: str
    page: int | None = None
    span_start: int
    span_end: int


class RetrieveResponse(BaseModel):
    status: RetrievalStatus
    query: str
    query_id: str
    rewrites: list[str]
    chunks: list[RetrievedChunk]
    cost_ms: int
    degraded_reasons: list[str] = Field(default_factory=list)


class RagRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=6, ge=1, le=50)


class RagResponse(BaseModel):
    answer: str
    chunks: list[RetrievedChunk]
    cost_ms: int
