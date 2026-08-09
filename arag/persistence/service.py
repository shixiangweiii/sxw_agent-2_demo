"""Durable index-job application service and single-process recovery worker."""
from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import replace
from typing import Protocol

from arag.persistence.models import IndexJob, IndexJobState, StoredChunk, SubmittedDocument
from arag.persistence.repository import RagRepository, sha256_hex
from arag.projection.snapshot import ProjectionManager
from arag.store.base import Chunk, Document
from common.obs import get_logger, log_kv

logger = get_logger("arag.index_worker")


class DocumentEnricher(Protocol):
    async def enrich(self, doc: Document) -> Document: ...


class DocumentChunker(Protocol):
    def split(self, doc: Document) -> list[Chunk]: ...


class DocumentEmbedder(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class IndexCoordinator:
    """Executes external work outside SQLite transactions, then commits boundaries."""

    def __init__(
        self,
        *,
        repository: RagRepository,
        projections: ProjectionManager,
        enricher: DocumentEnricher,
        chunker: DocumentChunker,
        embedder: DocumentEmbedder,
        embedding_model: str,
        poll_interval_seconds: float = 0.25,
        projection_validation_interval_seconds: float | None = None,
        projection_repair_backoff_base_seconds: float | None = None,
        projection_repair_backoff_max_seconds: float = 30.0,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.repository = repository
        self.projections = projections
        self.enricher = enricher
        self.chunker = chunker
        self.embedder = embedder
        self.embedding_model = embedding_model
        self.poll_interval_seconds = poll_interval_seconds
        self.projection_validation_interval_seconds = (
            projection_validation_interval_seconds
            if projection_validation_interval_seconds is not None
            else max(5.0, poll_interval_seconds * 20)
        )
        self.projection_repair_backoff_base_seconds = (
            projection_repair_backoff_base_seconds
            if projection_repair_backoff_base_seconds is not None
            else max(0.05, poll_interval_seconds)
        )
        self.projection_repair_backoff_max_seconds = projection_repair_backoff_max_seconds
        if self.projection_validation_interval_seconds <= 0:
            raise ValueError("projection_validation_interval_seconds must be positive")
        if self.projection_repair_backoff_base_seconds <= 0:
            raise ValueError("projection_repair_backoff_base_seconds must be positive")
        if self.projection_repair_backoff_max_seconds <= 0:
            raise ValueError("projection_repair_backoff_max_seconds must be positive")
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._process_lock = asyncio.Lock()
        self._next_projection_validation_at = 0.0
        self._projection_repair_not_before = 0.0
        self._projection_repair_failures = 0

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="arag-index-worker")
        self._wake.set()  # startup recovery scan

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        task = self._task
        self._task = None
        if task is not None:
            await task

    async def submit(
        self,
        *,
        dataset_id: str,
        external_doc_id: str,
        title: str,
        content: str,
        metadata: dict[str, object],
    ) -> SubmittedDocument:
        submitted = await self.repository.submit_document(
            dataset_id=dataset_id,
            external_doc_id=external_doc_id,
            title=title,
            content=content,
            metadata=metadata,
        )
        if not submitted.reused:
            self._wake.set()
        else:
            job = await self.repository.get_job(submitted.job_id)
            if job is not None and job.state not in {IndexJobState.ACTIVATED, IndexJobState.FAILED}:
                self._wake.set()
        return submitted

    async def process_job(self, job_id: str) -> IndexJob:
        async with self._process_lock:
            job = await self.repository.get_job(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.state == IndexJobState.ACTIVATED:
                return job
            if job.state == IndexJobState.FAILED:
                return job
            try:
                if job.state == IndexJobState.READY:
                    await self._activate_and_refresh(job_id)
                else:
                    job = await self.repository.begin_build(job_id)
                    version = await self.repository.get_version(job.version_id)
                    content = await self.repository.read_version_content(version)
                    document = Document(
                        doc_id=version.external_doc_id,
                        title=version.title,
                        content=content,
                        metadata=dict(version.metadata),
                    )
                    document = await self.enricher.enrich(document)
                    raw_chunks = self.chunker.split(document)
                    chunks = _make_version_chunks(raw_chunks, version.version_id, version.document_id, version.dataset_id)
                    vectors = await self.embedder.embed([chunk.content for chunk in chunks]) if chunks else []
                    await self.repository.store_build(
                        job_id=job_id,
                        chunks=chunks,
                        vectors=vectors,
                        embedding_model=self.embedding_model,
                    )
                    await self.repository.validate_build(job_id)
                    await self._activate_and_refresh(job_id)
            except asyncio.CancelledError:
                # Abrupt process loss leaves a non-terminal job.  Startup recovery safely rebuilds it.
                raise
            except Exception as exc:  # noqa: BLE001 - failure is persisted, never silently lost
                await self.repository.fail_job(
                    job_id, code=type(exc).__name__, message=str(exc)
                )
                log_kv(
                    logger,
                    logging.ERROR,
                    "IndexJob",
                    "job failed",
                    job_id=job_id,
                    error=type(exc).__name__,
                    detail=str(exc),
                )
            result = await self.repository.get_job(job_id)
            assert result is not None
            return result

    async def _activate_and_refresh(self, job_id: str) -> None:
        """Commit authority first, then best-effort refresh its projection.

        Once ``activate`` commits, the index job is correctly terminal even if
        constructing an in-memory projection fails.  Keep the snapshot
        invalidated and leave projection metadata STALE; the independent repair
        scan will retry from SQLite truth without rolling back or hiding the
        newly active document version.
        """
        await self.projections.invalidate()
        await self.repository.activate(job_id)
        try:
            await self.projections.rebuild()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - durable truth is already active
            self._wake.set()
            log_kv(
                logger,
                logging.ERROR,
                "Projection",
                "active pointer committed; projection rebuild deferred",
                job_id=job_id,
                error=type(exc).__name__,
                detail=str(exc),
            )

    async def _projection_needs_repair(self) -> bool:
        now = asyncio.get_running_loop().time()
        if now < self._projection_repair_not_before:
            return False
        state = await self.repository.projection_state()
        if state in {"MISSING", "STALE", "DEGRADED"}:
            return True
        snapshot = self.projections.snapshot
        if (
            not snapshot.vector_available
            and not snapshot.fulltext_available
            and snapshot.degraded_reason in {
                "ACTIVE_VERSION_SWITCH",
                "ACTIVE_SOURCE_CHANGED_DURING_REPAIR",
                "PROJECTION_CLEARED",
            }
        ):
            return True

        # A READY metadata row cannot prove that its rebuildable vector blobs
        # remain intact: local corruption/deletion may happen after the current
        # process snapshot was built.  Validate them periodically, not on every
        # 250ms worker poll, so healthy large indexes do not become a hot loop.
        if now < self._next_projection_validation_at:
            return False
        self._next_projection_validation_at = (
            now + self.projection_validation_interval_seconds
        )
        stored = await self.repository.list_active_chunks()
        reason = await asyncio.to_thread(
            _embedding_repair_reason, stored, self.embedding_model
        )
        return reason is not None

    async def _repair_projection(self) -> bool:
        """Repair derived embeddings from active chunk truth and replace the snapshot.

        The potentially remote embedding call is outside SQLite transactions.
        The repository publishes its float32 result only when the active source
        digest still matches the rows read before that call.
        """
        async with self._process_lock:
            stored = await self.repository.list_active_chunks()
            reason = await asyncio.to_thread(
                _embedding_repair_reason, stored, self.embedding_model
            )
            if reason is not None:
                # Expose the known-safe BM25 branch while vector repair is in
                # flight or backing off after a provider failure.
                await self.projections.rebuild()
                vectors = (
                    await self.embedder.embed([chunk.content for chunk in stored])
                    if stored
                    else []
                )
                source_digest = _active_source_digest(stored)
                published = await self.repository.replace_active_embeddings(
                    expected_source_digest=source_digest,
                    chunks=stored,
                    vectors=vectors,
                    embedding_model=self.embedding_model,
                )
                if not published:
                    await self.projections.invalidate("ACTIVE_SOURCE_CHANGED_DURING_REPAIR")
                    self._wake.set()
                    return False
                log_kv(
                    logger,
                    logging.INFO,
                    "Projection",
                    "re-embedded active chunks from SQLite truth",
                    chunks=len(stored),
                    reason=reason,
                    source_digest=source_digest,
                )
            await self.projections.rebuild()
            return True

    def _record_projection_repair_failure(self) -> float:
        self._projection_repair_failures += 1
        delay = min(
            self.projection_repair_backoff_max_seconds,
            self.projection_repair_backoff_base_seconds
            * (2 ** min(self._projection_repair_failures - 1, 20)),
        )
        self._projection_repair_not_before = asyncio.get_running_loop().time() + delay
        return delay

    def _record_projection_repair_success(self) -> None:
        self._projection_repair_failures = 0
        self._projection_repair_not_before = 0.0
        self._next_projection_validation_at = (
            asyncio.get_running_loop().time()
            + self.projection_validation_interval_seconds
        )

    async def _run(self) -> None:
        while not self._stop.is_set():
            job = await self.repository.next_recoverable_job()
            if job is not None:
                await self.process_job(job.job_id)
                continue
            if await self._projection_needs_repair():
                try:
                    repaired = await self._repair_projection()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - durable truth stays readable
                    delay = self._record_projection_repair_failure()
                    log_kv(
                        logger,
                        logging.ERROR,
                        "Projection",
                        "repair attempt failed; retry is backed off",
                        error=type(exc).__name__,
                        detail=str(exc),
                        retry_in_seconds=delay,
                    )
                else:
                    if repaired:
                        self._record_projection_repair_success()
                    continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval_seconds)
            except TimeoutError:
                pass


def _active_source_digest(chunks: Sequence[StoredChunk]) -> str:
    parts = [
        f"{chunk.version_id}:{chunk.chunk_id}:{chunk.content_hash}"
        for chunk in chunks
    ]
    return sha256_hex("\n".join(parts).encode("utf-8"))


def _embedding_repair_reason(
    chunks: Sequence[StoredChunk], embedding_model: str
) -> str | None:
    expected_dim: int | None = None
    for chunk in chunks:
        model = chunk.embedding_model
        vector = chunk.vector
        dim = chunk.vector_dim
        checksum = chunk.vector_checksum
        if vector is None or dim is None or checksum is None:
            return "MISSING_EMBEDDING"
        if model != embedding_model:
            return "EMBEDDING_MODEL_MISMATCH"
        if sha256_hex(vector) != checksum:
            return "EMBEDDING_CHECKSUM_MISMATCH"
        if len(vector) != dim * 4:
            return "EMBEDDING_DIMENSION_MISMATCH"
        if expected_dim is None:
            expected_dim = dim
        elif expected_dim != dim:
            return "MIXED_EMBEDDING_DIMENSIONS"
    return None


def _make_version_chunks(
    chunks: Sequence[Chunk], version_id: str, document_id: str, dataset_id: str
) -> list[Chunk]:
    out: list[Chunk] = []
    search_from = 0
    for ordinal, chunk in enumerate(chunks):
        content_hash = hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()
        identity = hashlib.sha256(
            f"{version_id}:{ordinal}:{content_hash}".encode("utf-8")
        ).hexdigest()
        metadata = dict(chunk.metadata)
        position = max(search_from, 0)
        metadata.update(
            {
                "document_id": document_id,
                "document_version_id": version_id,
                "dataset_id": dataset_id,
                "content_hash": content_hash,
                "span_start": position,
                "span_end": position + len(chunk.content),
                "scope": str(metadata.get("scope", "public")),
            }
        )
        search_from = position + max(1, len(chunk.content))
        out.append(replace(chunk, chunk_id=f"chk_{identity}", metadata=metadata))
    return out
