from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from arag.api.index import router as index_router
from arag.api.retrieve import router as retrieve_router
from arag.components.retriever import HybridRetriever
from arag.persistence.models import IndexJobState
from arag.persistence.repository import RagRepository, RagSchemaError
from arag.persistence.service import IndexCoordinator
from arag.projection.snapshot import ProjectionManager, ProjectionUnavailableError
from arag.store.base import Chunk, Document


class IdentityEnricher:
    async def enrich(self, doc: Document) -> Document:
        return doc


class PipeChunker:
    def split(self, doc: Document) -> list[Chunk]:
        return [
            Chunk(
                chunk_id=f"temporary-{ordinal}",
                doc_id=doc.doc_id,
                title=doc.title,
                content=part.strip(),
                metadata=dict(doc.metadata),
            )
            for ordinal, part in enumerate(doc.content.split("|"))
            if part.strip()
        ]


class FakeEmbedder:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    @staticmethod
    def _vector(text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [float(digest[0] + 1), float(digest[1] + 1), float(digest[2] + 1)]


class FakeRewriter:
    async def rewrite(self, query: str) -> list[str]:
        return [query]


async def build_stack(tmp_path: Path) -> tuple[RagRepository, ProjectionManager, IndexCoordinator]:
    repository = RagRepository(tmp_path / "rag.db", tmp_path / "rag-store")
    await repository.initialize()
    projections = ProjectionManager(repository)
    await projections.rebuild()
    coordinator = IndexCoordinator(
        repository=repository,
        projections=projections,
        enricher=IdentityEnricher(),
        chunker=PipeChunker(),
        embedder=FakeEmbedder(),
        embedding_model="fake-v1",
    )
    return repository, projections, coordinator


async def _wait_for_healthy_active_embeddings(
    repository: RagRepository,
    projections: ProjectionManager,
    *,
    expected_model: str = "fake-v1",
) -> dict[str, bytes]:
    async with asyncio.timeout(2):
        while True:
            stored = await repository.list_active_chunks()
            healthy = bool(stored) and all(
                chunk.embedding_model == expected_model
                and chunk.vector is not None
                and chunk.vector_checksum == hashlib.sha256(chunk.vector).hexdigest()
                and chunk.vector_dim is not None
                and len(chunk.vector) == chunk.vector_dim * 4
                for chunk in stored
            )
            if (
                healthy
                and await repository.projection_state() == "READY"
                and projections.snapshot.vector_available
                and projections.snapshot.fulltext_available
            ):
                return {
                    chunk.chunk_id: chunk.vector
                    for chunk in stored
                    if chunk.vector is not None
                }
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_same_document_content_reuses_version_and_job(tmp_path: Path) -> None:
    repository, _projections, coordinator = await build_stack(tmp_path)
    first = await coordinator.submit(
        dataset_id="kb", external_doc_id="doc-1", title="v1", content="alpha", metadata={}
    )
    second = await coordinator.submit(
        dataset_id="kb", external_doc_id="doc-1", title="ignored", content="alpha", metadata={}
    )

    assert second.reused is True
    assert (second.document_id, second.version_id, second.job_id) == (
        first.document_id,
        first.version_id,
        first.job_id,
    )
    job = await coordinator.process_job(first.job_id)
    assert job.state == IndexJobState.ACTIVATED


@pytest.mark.asyncio
async def test_schema_identity_rewrite_fails_fast(tmp_path: Path) -> None:
    repository, _projections, _coordinator = await build_stack(tmp_path)
    async with repository.connection() as conn:
        await conn.execute("UPDATE schema_meta SET schema_checksum='tampered'")
        await conn.commit()
    with pytest.raises(RagSchemaError, match="checksum mismatch"):
        await repository.initialize()


@pytest.mark.asyncio
async def test_shorter_document_switch_hides_all_old_chunks(tmp_path: Path) -> None:
    repository, projections, coordinator = await build_stack(tmp_path)
    first = await coordinator.submit(
        dataset_id="kb",
        external_doc_id="doc-1",
        title="long",
        content="alpha|obsolete-only|also-obsolete",
        metadata={},
    )
    await coordinator.process_job(first.job_id)
    assert len(projections.snapshot.chunks) == 3

    second = await coordinator.submit(
        dataset_id="kb",
        external_doc_id="doc-1",
        title="short",
        content="alpha",
        metadata={},
    )
    await coordinator.process_job(second.job_id)

    assert second.version_id != first.version_id
    assert await repository.active_version_id("kb", "doc-1") == second.version_id
    assert await repository.count_chunks_for_version(first.version_id) == 3  # retained for audit
    assert [chunk.content for chunk in projections.snapshot.chunks] == ["alpha"]
    assert all(
        chunk.metadata["document_version_id"] == second.version_id
        for chunk in projections.snapshot.chunks
    )


@pytest.mark.asyncio
async def test_recovery_never_runs_newer_version_before_incomplete_older_version(
    tmp_path: Path,
) -> None:
    repository, _projections, coordinator = await build_stack(tmp_path)
    first = await coordinator.submit(
        dataset_id="kb", external_doc_id="doc-1", title="v1", content="old", metadata={}
    )
    second = await coordinator.submit(
        dataset_id="kb", external_doc_id="doc-1", title="v2", content="new", metadata={}
    )
    await repository.begin_build(first.job_id)  # crash after BUILDING commit

    recovered = await repository.next_recoverable_job()
    assert recovered is not None
    assert recovered.job_id == first.job_id
    await coordinator.process_job(first.job_id)
    next_job = await repository.next_recoverable_job()
    assert next_job is not None
    assert next_job.job_id == second.job_id


@pytest.mark.asyncio
async def test_vector_and_bm25_memory_projection_rebuild_from_sqlite_truth(tmp_path: Path) -> None:
    _repository, projections, coordinator = await build_stack(tmp_path)
    submitted = await coordinator.submit(
        dataset_id="kb",
        external_doc_id="doc-1",
        title="search",
        content="hybrid retrieval|durable sqlite",
        metadata={},
    )
    await coordinator.process_job(submitted.job_id)
    query_vector = FakeEmbedder._vector("hybrid retrieval")
    assert await projections.vector_store.search(query_vector, 1)
    assert await projections.fulltext_index.search("sqlite", 1)

    await projections.clear_memory()
    with pytest.raises(ProjectionUnavailableError):
        await projections.vector_store.search(query_vector, 1)
    await projections.rebuild()

    assert (await projections.vector_store.search(query_vector, 1))[0].content == "hybrid retrieval"
    assert (await projections.fulltext_index.search("sqlite", 1))[0].content == "durable sqlite"


@pytest.mark.asyncio
async def test_one_projection_branch_failure_is_stably_degraded(tmp_path: Path) -> None:
    repository, projections, coordinator = await build_stack(tmp_path)
    submitted = await coordinator.submit(
        dataset_id="kb", external_doc_id="doc-1", title="v1", content="keyword", metadata={}
    )
    await coordinator.process_job(submitted.job_id)
    async with repository.connection() as conn:
        await conn.execute("DELETE FROM chunk_embeddings")
        await conn.commit()
    await projections.rebuild()

    retriever = HybridRetriever(
        projections.vector_store,
        projections.fulltext_index,
        FakeEmbedder(),
        FakeRewriter(),
    )
    result = await retriever.retrieve_detailed(
        "keyword", datasets={"kb"}, scope="public", use_rewrite=False
    )
    assert result.status.value == "DEGRADED"
    assert [chunk.content for chunk in result.chunks] == ["keyword"]
    assert result.degraded_reasons == ["vector:ProjectionUnavailableError"]


@pytest.mark.asyncio
async def test_deleted_embedding_projection_is_automatically_reembedded_from_active_chunks(
    tmp_path: Path,
) -> None:
    repository, projections, coordinator = await build_stack(tmp_path)
    coordinator.poll_interval_seconds = 0.01
    coordinator.projection_validation_interval_seconds = 0.01
    coordinator.projection_repair_backoff_base_seconds = 0.02
    submitted = await coordinator.submit(
        dataset_id="kb",
        external_doc_id="doc-repair-delete",
        title="repair",
        content="hybrid retrieval|durable sqlite",
        metadata={},
    )
    await coordinator.process_job(submitted.job_id)
    query_vector = FakeEmbedder._vector("hybrid retrieval")
    baseline_vector = [
        chunk.content for chunk in await projections.vector_store.search(query_vector, 2)
    ]
    baseline_bm25 = [
        chunk.content for chunk in await projections.fulltext_index.search("sqlite", 2)
    ]
    original = {
        chunk.chunk_id: chunk.vector
        for chunk in await repository.list_active_chunks()
    }

    async with repository.connection() as conn:
        await conn.execute("DELETE FROM chunk_embeddings")
        await conn.commit()

    await coordinator.start()
    try:
        repaired = await _wait_for_healthy_active_embeddings(repository, projections)
        assert repaired == original
        assert [
            chunk.content for chunk in await projections.vector_store.search(query_vector, 2)
        ] == baseline_vector
        assert [
            chunk.content for chunk in await projections.fulltext_index.search("sqlite", 2)
        ] == baseline_bm25
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_corrupt_embedding_projection_is_automatically_reembedded_without_restart(
    tmp_path: Path,
) -> None:
    repository, projections, coordinator = await build_stack(tmp_path)
    coordinator.poll_interval_seconds = 0.01
    coordinator.projection_validation_interval_seconds = 0.01
    coordinator.projection_repair_backoff_base_seconds = 0.02
    submitted = await coordinator.submit(
        dataset_id="kb",
        external_doc_id="doc-repair-corrupt",
        title="repair",
        content="checksum recovery|lexical recovery",
        metadata={},
    )
    await coordinator.process_job(submitted.job_id)
    query_vector = FakeEmbedder._vector("checksum recovery")
    baseline_vector = [
        chunk.content for chunk in await projections.vector_store.search(query_vector, 2)
    ]
    baseline_bm25 = [
        chunk.content for chunk in await projections.fulltext_index.search("lexical", 2)
    ]
    original = {
        chunk.chunk_id: chunk.vector
        for chunk in await repository.list_active_chunks()
    }

    async with repository.connection() as conn:
        await conn.execute(
            "UPDATE chunk_embeddings SET vector_blob=zeroblob(length(vector_blob))"
        )
        await conn.commit()

    await coordinator.start()
    try:
        repaired = await _wait_for_healthy_active_embeddings(repository, projections)
        assert repaired == original
        assert [
            chunk.content for chunk in await projections.vector_store.search(query_vector, 2)
        ] == baseline_vector
        assert [
            chunk.content for chunk in await projections.fulltext_index.search("lexical", 2)
        ] == baseline_bm25
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_embedding_repair_publish_rejects_a_stale_active_source(tmp_path: Path) -> None:
    repository, _projections, coordinator = await build_stack(tmp_path)
    old = await coordinator.submit(
        dataset_id="kb",
        external_doc_id="doc-repair-race",
        title="old",
        content="old active source",
        metadata={},
    )
    await coordinator.process_job(old.job_id)
    stale_chunks = await repository.list_active_chunks()
    stale_digest = hashlib.sha256(
        "\n".join(
            f"{chunk.version_id}:{chunk.chunk_id}:{chunk.content_hash}"
            for chunk in stale_chunks
        ).encode("utf-8")
    ).hexdigest()
    old_checksum = stale_chunks[0].vector_checksum

    new = await coordinator.submit(
        dataset_id="kb",
        external_doc_id="doc-repair-race",
        title="new",
        content="new active source",
        metadata={},
    )
    await coordinator.process_job(new.job_id)

    published = await repository.replace_active_embeddings(
        expected_source_digest=stale_digest,
        chunks=stale_chunks,
        vectors=[[999.0, 999.0, 999.0] for _chunk in stale_chunks],
        embedding_model="fake-v1",
    )
    assert published is False
    assert await repository.active_version_id("kb", "doc-repair-race") == new.version_id
    async with repository.connection() as conn:
        cursor = await conn.execute(
            "SELECT checksum FROM chunk_embeddings WHERE chunk_id=?",
            (stale_chunks[0].chunk_id,),
        )
        row = await cursor.fetchone()
    assert row is not None and row["checksum"] == old_checksum


@pytest.mark.asyncio
async def test_embedding_projection_repair_failure_keeps_bm25_and_backs_off(
    tmp_path: Path,
) -> None:
    repository, projections, coordinator = await build_stack(tmp_path)

    class FailingEmbedder:
        def __init__(self) -> None:
            self.calls = 0
            self.called = asyncio.Event()

        async def embed(self, texts: list[str]) -> list[list[float]]:
            self.calls += 1
            self.called.set()
            raise RuntimeError("embedding provider unavailable")

    failing = FailingEmbedder()
    coordinator.embedder = failing
    coordinator.poll_interval_seconds = 0.005
    coordinator.projection_validation_interval_seconds = 0.005
    coordinator.projection_repair_backoff_base_seconds = 0.2
    coordinator.projection_repair_backoff_max_seconds = 0.2
    submitted = await coordinator.submit(
        dataset_id="kb",
        external_doc_id="doc-repair-backoff",
        title="repair",
        content="bm25 survives",
        metadata={},
    )
    # Build with the healthy embedder before injecting the repair failure.
    coordinator.embedder = FakeEmbedder()
    await coordinator.process_job(submitted.job_id)
    coordinator.embedder = failing
    async with repository.connection() as conn:
        await conn.execute("DELETE FROM chunk_embeddings")
        await conn.commit()

    await coordinator.start()
    try:
        await asyncio.wait_for(failing.called.wait(), timeout=1)
        await asyncio.sleep(0.05)
        assert failing.calls == 1
        assert await repository.projection_state() == "DEGRADED"
        assert not projections.snapshot.vector_available
        assert projections.snapshot.fulltext_available
        assert (
            await projections.fulltext_index.search("survives", 1)
        )[0].content == "bm25 survives"
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_projection_rebuild_window_is_degraded_not_transport_error(tmp_path: Path) -> None:
    _repository, projections, coordinator = await build_stack(tmp_path)
    submitted = await coordinator.submit(
        dataset_id="kb", external_doc_id="doc-1", title="v1", content="keyword", metadata={}
    )
    await coordinator.process_job(submitted.job_id)
    await projections.invalidate("TEST_REBUILD_WINDOW")
    retriever = HybridRetriever(
        projections.vector_store,
        projections.fulltext_index,
        FakeEmbedder(),
        FakeRewriter(),
    )
    result = await retriever.retrieve_detailed(
        "keyword", datasets={"kb"}, scope="public", use_rewrite=False
    )
    assert result.status.value == "DEGRADED"
    assert result.chunks == []
    assert result.degraded_reasons == [
        "vector:ProjectionUnavailableError",
        "fulltext:ProjectionUnavailableError",
    ]


@pytest.mark.asyncio
async def test_activated_pointer_projection_failure_is_retried_without_serving_old_snapshot(
    tmp_path: Path,
) -> None:
    repository = RagRepository(tmp_path / "rag.db", tmp_path / "rag-store")
    await repository.initialize()

    class GatedProjectionManager(ProjectionManager):
        def __init__(self, repo: RagRepository) -> None:
            super().__init__(repo)
            self.fail_enabled = False
            self.allow_recovery = asyncio.Event()
            self.failed = asyncio.Event()
            self.failed_attempts = 0

        async def rebuild(self):
            if self.fail_enabled and not self.allow_recovery.is_set():
                self.failed_attempts += 1
                self.failed.set()
                raise RuntimeError("injected projection build failure")
            return await super().rebuild()

    projections = GatedProjectionManager(repository)
    await projections.rebuild()
    coordinator = IndexCoordinator(
        repository=repository,
        projections=projections,
        enricher=IdentityEnricher(),
        chunker=PipeChunker(),
        embedder=FakeEmbedder(),
        embedding_model="fake-v1",
        poll_interval_seconds=0.01,
    )
    old = await coordinator.submit(
        dataset_id="kb", external_doc_id="doc-1", title="old", content="zebra", metadata={}
    )
    assert (await coordinator.process_job(old.job_id)).state is IndexJobState.ACTIVATED
    assert [chunk.content for chunk in projections.snapshot.chunks] == ["zebra"]

    projections.fail_enabled = True
    await coordinator.start()
    try:
        new = await coordinator.submit(
            dataset_id="kb", external_doc_id="doc-1", title="new", content="quantum", metadata={}
        )
        await asyncio.wait_for(projections.failed.wait(), timeout=1)

        job = await repository.get_job(new.job_id)
        assert job is not None and job.state is IndexJobState.ACTIVATED
        assert await repository.active_version_id("kb", "doc-1") == new.version_id
        assert await repository.projection_state() == "STALE"
        assert projections.snapshot.chunks == ()
        with pytest.raises(ProjectionUnavailableError):
            await projections.fulltext_index.search("old-only", 1)

        projections.allow_recovery.set()
        async with asyncio.timeout(1):
            while (
                await repository.projection_state() != "READY"
                or [chunk.content for chunk in projections.snapshot.chunks] != ["quantum"]
            ):
                await asyncio.sleep(0.01)
        assert projections.failed_attempts >= 1
        assert not await projections.fulltext_index.search("zebra", 1)
        assert (await projections.fulltext_index.search("quantum", 1))[0].content == "quantum"
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_index_api_returns_202_and_queryable_job(tmp_path: Path) -> None:
    repository, projections, coordinator = await build_stack(tmp_path)

    class Context:
        pass

    ctx = Context()
    ctx.repository = repository
    ctx.projections = projections
    ctx.index_coordinator = coordinator
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(index_router)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/index",
            json={
                "documents": [
                    {
                        "dataset_id": "kb",
                        "doc_id": "doc-api",
                        "title": "API",
                        "content": "accepted",
                    }
                ]
            },
        )
        assert response.status_code == 202
        job_id = response.json()["job_ids"][0]
        status_response = await client.get(f"/v1/index/jobs/{job_id}")
        assert status_response.status_code == 200
        assert status_response.json()["state"] == "PREPARED"

    assert (await coordinator.process_job(job_id)).state == IndexJobState.ACTIVATED


@pytest.mark.asyncio
async def test_index_api_rejects_empty_or_duplicate_logical_documents(tmp_path: Path) -> None:
    repository, projections, coordinator = await build_stack(tmp_path)

    class Context:
        pass

    ctx = Context()
    ctx.repository = repository
    ctx.projections = projections
    ctx.index_coordinator = coordinator
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(index_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        empty = await client.post("/v1/index", json={"documents": []})
        assert empty.status_code == 422
        duplicate = await client.post(
            "/v1/index",
            json={"documents": [
                {"dataset_id": "kb", "doc_id": "same", "content": "v1"},
                {"dataset_id": "kb", "doc_id": "same", "content": "v2"},
            ]},
        )
        assert duplicate.status_code == 422


@pytest.mark.asyncio
async def test_retrieval_evidence_is_versioned_and_denial_is_explicit(tmp_path: Path) -> None:
    repository, projections, coordinator = await build_stack(tmp_path)
    submitted = await coordinator.submit(
        dataset_id="kb", external_doc_id="doc-evidence", title="Evidence", content="traceable", metadata={}
    )
    await coordinator.process_job(submitted.job_id)

    class Context:
        pass

    ctx = Context()
    ctx.retriever = HybridRetriever(
        projections.vector_store, projections.fulltext_index, FakeEmbedder(), FakeRewriter()
    )
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(retrieve_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/retrieve",
            json={
                "query": "traceable",
                "query_id": "qry-fixed",
                "datasets": ["kb"],
                "scope": "public",
                "use_rewrite": False,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "HIT"
        evidence = body["chunks"][0]
        assert evidence["query_id"] == "qry-fixed"
        assert evidence["document_version_id"] == submitted.version_id
        assert evidence["content_hash"]
        assert evidence["span_end"] > evidence["span_start"]

        denied = await client.post(
            "/v1/retrieve",
            json={"query": "traceable", "datasets": ["kb"], "scope": "admin"},
        )
        assert denied.status_code == 200
        assert denied.json()["status"] == "DENIED"


@pytest.mark.asyncio
async def test_retrieve_absolute_deadline_cancels_in_flight_work() -> None:
    class SlowRetriever:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = False

        async def retrieve_detailed(self, *args, **kwargs):
            self.started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    class Context:
        pass

    retriever = SlowRetriever()
    ctx = Context()
    ctx.retriever = retriever
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(retrieve_router)
    started_at = time.perf_counter()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/retrieve",
            json={
                "query": "slow",
                "query_id": "qry-deadline",
                "deadline_at": (
                    datetime.now(UTC) + timedelta(milliseconds=200)
                ).isoformat().replace("+00:00", "Z"),
            },
        )
    elapsed = time.perf_counter() - started_at
    assert response.status_code == 200
    assert response.json() == {
        "status": "ERROR",
        "query": "slow",
        "query_id": "qry-deadline",
        "rewrites": ["slow"],
        "chunks": [],
        "cost_ms": response.json()["cost_ms"],
        "degraded_reasons": ["DEADLINE_EXCEEDED"],
    }
    assert retriever.started.is_set()
    assert retriever.cancelled is True
    assert elapsed < 1


@pytest.mark.asyncio
async def test_retrieve_expired_deadline_does_not_start_retriever() -> None:
    class MustNotRun:
        calls = 0

        async def retrieve_detailed(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("expired retrieval must not start")

    class Context:
        pass

    retriever = MustNotRun()
    ctx = Context()
    ctx.retriever = retriever
    app = FastAPI()
    app.state.ctx = ctx
    app.include_router(retrieve_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/retrieve",
            json={
                "query": "expired",
                "deadline_at": "2000-01-01T00:00:00Z",
            },
        )
    assert response.status_code == 200
    assert response.json()["status"] == "ERROR"
    assert response.json()["degraded_reasons"] == ["DEADLINE_EXCEEDED"]
    assert retriever.calls == 0
