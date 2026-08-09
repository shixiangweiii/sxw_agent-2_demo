"""Persistence-facing immutable records for document indexing."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class IndexJobState(StrEnum):
    PREPARED = "PREPARED"
    BUILDING = "BUILDING"
    VALIDATING = "VALIDATING"
    READY = "READY"
    ACTIVATED = "ACTIVATED"
    FAILED = "FAILED"


NON_TERMINAL_JOB_STATES = frozenset(
    {
        IndexJobState.PREPARED,
        IndexJobState.BUILDING,
        IndexJobState.VALIDATING,
        IndexJobState.READY,
    }
)


@dataclass(frozen=True, slots=True)
class SubmittedDocument:
    document_id: str
    version_id: str
    job_id: str
    reused: bool


@dataclass(frozen=True, slots=True)
class IndexJob:
    job_id: str
    document_id: str
    version_id: str
    dataset_id: str
    external_doc_id: str
    state: IndexJobState
    attempt: int
    error_code: str | None
    error_message: str | None
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class DocumentVersion:
    document_id: str
    version_id: str
    dataset_id: str
    external_doc_id: str
    title: str
    content_digest: str
    content_uri: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StoredChunk:
    chunk_id: str
    version_id: str
    ordinal: int
    content_hash: str
    document_id: str
    dataset_id: str
    external_doc_id: str
    title: str
    content: str
    metadata: dict[str, Any]
    page: int | None
    span_start: int
    span_end: int
    embedding_model: str | None
    vector: bytes | None
    vector_dim: int | None
    vector_checksum: str | None
