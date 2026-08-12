"""SQLite authority for documents, immutable versions, chunks and index jobs.

Every public write method owns one short ``BEGIN IMMEDIATE`` transaction.  Parsing,
captioning, embedding and projection construction deliberately live outside these
transactions in :mod:`arag.persistence.service`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from arag.persistence.models import (
    DocumentVersion,
    IndexJob,
    IndexJobState,
    NON_TERMINAL_JOB_STATES,
    StoredChunk,
    SubmittedDocument,
)
from arag.store.base import Chunk
from common.sqlite_schema import ensure_current_schema


class RagPersistenceError(RuntimeError):
    """Base class for deterministic persistence failures."""


class IndexJobConflict(RagPersistenceError):
    """A state transition lost its compare-and-swap."""


def utc_epoch_ms() -> int:
    return int(time.time() * 1000)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4()}"


class RagRepository:
    """Explicit-SQL repository; vector/BM25 projections never become authority."""

    def __init__(
        self,
        db_path: str | Path = "local_storage/arag/rag.db",
        storage_root: str | Path = "local_storage/arag",
    ) -> None:
        self.db_path = Path(db_path)
        self.storage_root = Path(storage_root)
        self._source_root = self.storage_root / "documents" / "sha256"

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.db_path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        try:
            await conn.execute("PRAGMA busy_timeout = 5000")
            deadline = time.monotonic() + 5
            while True:
                try:
                    await conn.execute("PRAGMA journal_mode = WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                        raise
                    await asyncio.sleep(0.01)
            await conn.execute("PRAGMA synchronous = FULL")
            await conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            await conn.close()

    async def initialize(self) -> None:
        """Install the current schema on an empty file, or verify an existing one.

        There is no migration path: a database carrying anything other than the
        current schema is rejected and must be deleted by the operator.
        """
        schema_path = Path(__file__).with_name("schema.sql")
        async with self.connection() as conn:
            await ensure_current_schema(
                conn,
                schema_bytes=schema_path.read_bytes(),
                db_path=self.db_path,
                label="rag",
            )

    async def submit_document(
        self,
        *,
        dataset_id: str,
        external_doc_id: str,
        title: str,
        content: str,
        metadata: dict[str, Any],
    ) -> SubmittedDocument:
        dataset_id = dataset_id.strip()
        external_doc_id = external_doc_id.strip()
        if not dataset_id or not external_doc_id:
            raise ValueError("dataset_id and doc_id must be non-empty")
        content_bytes = content.encode("utf-8")
        content_digest = sha256_hex(content_bytes)
        content_uri = await self._put_source_blob(content_digest, content_bytes)
        now = utc_epoch_ms()

        async with self.connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                doc_row = await self._fetchone(
                    conn,
                    "SELECT document_id FROM documents WHERE dataset_id=? AND external_doc_id=?",
                    (dataset_id, external_doc_id),
                )
                document_id = str(doc_row["document_id"]) if doc_row else _new_id("doc")
                if doc_row is None:
                    await conn.execute(
                        "INSERT INTO documents(document_id,dataset_id,external_doc_id,created_at) VALUES(?,?,?,?)",
                        (document_id, dataset_id, external_doc_id, now),
                    )

                version_row = await self._fetchone(
                    conn,
                    "SELECT version_id FROM document_versions WHERE document_id=? AND content_digest=?",
                    (document_id, content_digest),
                )
                if version_row is not None:
                    version_id = str(version_row["version_id"])
                    job_row = await self._fetchone(
                        conn, "SELECT job_id FROM index_jobs WHERE version_id=?", (version_id,)
                    )
                    if job_row is None:
                        raise RagPersistenceError(f"version {version_id} has no index job")
                    await conn.commit()
                    return SubmittedDocument(
                        document_id=document_id,
                        version_id=version_id,
                        job_id=str(job_row["job_id"]),
                        reused=True,
                    )

                # Epoch milliseconds can tie or move backwards. Per-document version time is
                # strictly monotonic so recovery preserves admission order without UUID order.
                latest = await self._fetchone(
                    conn,
                    "SELECT MAX(created_at) AS created_at FROM document_versions WHERE document_id=?",
                    (document_id,),
                )
                now = max(now, int(latest["created_at"] or 0) + 1)

                version_id = _new_id("dver")
                job_id = _new_id("ijob")
                await conn.execute(
                    """
                    INSERT INTO document_versions(
                        version_id,document_id,content_digest,content_uri,title,metadata_json,created_at
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        version_id,
                        document_id,
                        content_digest,
                        content_uri,
                        title,
                        canonical_json(metadata),
                        now,
                    ),
                )
                await conn.execute(
                    """
                    INSERT INTO index_jobs(
                        job_id,document_id,version_id,state,attempt,created_at,updated_at
                    ) VALUES(?,?,?,'PREPARED',0,?,?)
                    """,
                    (job_id, document_id, version_id, now, now),
                )
            except BaseException:
                await conn.rollback()
                raise
            else:
                await conn.commit()
        return SubmittedDocument(
            document_id=document_id, version_id=version_id, job_id=job_id, reused=False
        )

    async def get_job(self, job_id: str) -> IndexJob | None:
        async with self.connection() as conn:
            row = await self._fetchone(conn, self._JOB_SELECT + " WHERE j.job_id=?", (job_id,))
            return self._job_from_row(row) if row else None

    async def next_recoverable_job(self) -> IndexJob | None:
        placeholders = ",".join("?" for _ in NON_TERMINAL_JOB_STATES)
        states = tuple(state.value for state in NON_TERMINAL_JOB_STATES)
        async with self.connection() as conn:
            row = await self._fetchone(
                conn,
                self._JOB_SELECT
                + f"""
                  JOIN document_versions v ON v.version_id=j.version_id
                  WHERE j.state IN ({placeholders})
                    AND NOT EXISTS (
                      SELECT 1 FROM index_jobs older_j
                      JOIN document_versions older_v ON older_v.version_id=older_j.version_id
                      WHERE older_j.document_id=j.document_id
                        AND older_v.created_at < v.created_at
                        AND older_j.state IN ({placeholders})
                    )
                  ORDER BY j.updated_at,j.job_id LIMIT 1
                """,
                (*states, *states),
            )
            return self._job_from_row(row) if row else None

    async def get_version(self, version_id: str) -> DocumentVersion:
        async with self.connection() as conn:
            row = await self._fetchone(
                conn,
                """
                SELECT v.*,d.dataset_id,d.external_doc_id
                FROM document_versions v JOIN documents d ON d.document_id=v.document_id
                WHERE v.version_id=?
                """,
                (version_id,),
            )
        if row is None:
            raise KeyError(version_id)
        return DocumentVersion(
            document_id=str(row["document_id"]),
            version_id=str(row["version_id"]),
            dataset_id=str(row["dataset_id"]),
            external_doc_id=str(row["external_doc_id"]),
            title=str(row["title"]),
            content_digest=str(row["content_digest"]),
            content_uri=str(row["content_uri"]),
            metadata=json.loads(str(row["metadata_json"])),
        )

    async def read_version_content(self, version: DocumentVersion) -> str:
        path = self._path_from_uri(version.content_uri)
        data = path.read_bytes()
        digest = sha256_hex(data)
        if digest != version.content_digest:
            raise RagPersistenceError(
                f"document content integrity error for {version.version_id}: {digest}"
            )
        return data.decode("utf-8")

    async def begin_build(self, job_id: str) -> IndexJob:
        """Recover any incomplete build by clearing only that immutable version's staging rows."""
        async with self.connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                row = await self._fetchone(conn, "SELECT * FROM index_jobs WHERE job_id=?", (job_id,))
                if row is None:
                    raise KeyError(job_id)
                state = IndexJobState(str(row["state"]))
                if state in {IndexJobState.ACTIVATED, IndexJobState.FAILED}:
                    await conn.commit()
                    job = await self.get_job(job_id)
                    assert job is not None
                    return job
                version_id = str(row["version_id"])
                await conn.execute(
                    "DELETE FROM chunk_embeddings WHERE chunk_id IN (SELECT chunk_id FROM chunks WHERE version_id=?)",
                    (version_id,),
                )
                await conn.execute("DELETE FROM chunks WHERE version_id=?", (version_id,))
                now = utc_epoch_ms()
                await conn.execute(
                    """
                    UPDATE index_jobs
                    SET state='BUILDING',attempt=attempt+1,error_code=NULL,error_message=NULL,updated_at=?
                    WHERE job_id=?
                    """,
                    (now, job_id),
                )
            except BaseException:
                await conn.rollback()
                raise
            else:
                await conn.commit()
        job = await self.get_job(job_id)
        assert job is not None
        return job

    async def store_build(
        self,
        *,
        job_id: str,
        chunks: Sequence[Chunk],
        vectors: Sequence[Sequence[float]],
        embedding_model: str,
    ) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunks/vectors count mismatch")
        now = utc_epoch_ms()
        rows: list[tuple[Any, ...]] = []
        embedding_rows: list[tuple[Any, ...]] = []
        for ordinal, (chunk, values) in enumerate(zip(chunks, vectors, strict=True)):
            vector_blob, dim, checksum = encode_vector(values)
            content_hash = sha256_hex(chunk.content.encode("utf-8"))
            metadata = dict(chunk.metadata)
            rows.append(
                (
                    chunk.chunk_id,
                    str(metadata["document_version_id"]),
                    ordinal,
                    content_hash,
                    chunk.title,
                    chunk.content,
                    canonical_json(metadata),
                    metadata.get("page"),
                    int(metadata.get("span_start", 0)),
                    int(metadata.get("span_end", len(chunk.content))),
                    now,
                )
            )
            embedding_rows.append(
                (chunk.chunk_id, embedding_model, dim, vector_blob, checksum, now)
            )

        async with self.connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                job = await self._fetchone(conn, "SELECT state,version_id FROM index_jobs WHERE job_id=?", (job_id,))
                if job is None or str(job["state"]) != IndexJobState.BUILDING.value:
                    raise IndexJobConflict(f"job {job_id} is not BUILDING")
                if any(row[1] != str(job["version_id"]) for row in rows):
                    raise RagPersistenceError("chunk version does not match index job")
                await conn.executemany(
                    """
                    INSERT INTO chunks(
                        chunk_id,version_id,ordinal,content_hash,title,content,metadata_json,
                        page,span_start,span_end,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    rows,
                )
                await conn.executemany(
                    """
                    INSERT INTO chunk_embeddings(
                        chunk_id,embedding_model,dim,vector_blob,checksum,created_at
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    embedding_rows,
                )
                cursor = await conn.execute(
                    "UPDATE index_jobs SET state='VALIDATING',expected_chunk_count=?,updated_at=? WHERE job_id=? AND state='BUILDING'",
                    (len(chunks), now, job_id),
                )
                if cursor.rowcount != 1:
                    raise IndexJobConflict(f"job {job_id} lost BUILDING CAS")
            except BaseException:
                await conn.rollback()
                raise
            else:
                await conn.commit()

    async def validate_build(self, job_id: str) -> str:
        """Validate immutable staging rows and atomically advance VALIDATING -> READY."""
        async with self.connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                job = await self._fetchone(conn, "SELECT * FROM index_jobs WHERE job_id=?", (job_id,))
                if job is None or str(job["state"]) != IndexJobState.VALIDATING.value:
                    raise IndexJobConflict(f"job {job_id} is not VALIDATING")
                rows = await self._fetchall(
                    conn,
                    """
                    SELECT c.chunk_id,c.ordinal,c.content_hash,e.dim,e.vector_blob,e.checksum
                    FROM chunks c LEFT JOIN chunk_embeddings e ON e.chunk_id=c.chunk_id
                    WHERE c.version_id=? ORDER BY c.ordinal
                    """,
                    (str(job["version_id"]),),
                )
                expected = int(job["expected_chunk_count"] or 0)
                if len(rows) != expected:
                    raise RagPersistenceError(f"chunk count mismatch: {len(rows)} != {expected}")
                dims: set[int] = set()
                digest_parts: list[str] = []
                for row in rows:
                    if row["vector_blob"] is None:
                        raise RagPersistenceError(f"missing embedding for {row['chunk_id']}")
                    blob = bytes(row["vector_blob"])
                    checksum = sha256_hex(blob)
                    if checksum != str(row["checksum"]):
                        raise RagPersistenceError(f"embedding checksum mismatch for {row['chunk_id']}")
                    dim = int(row["dim"])
                    if len(blob) != dim * 4:
                        raise RagPersistenceError(f"embedding byte length mismatch for {row['chunk_id']}")
                    dims.add(dim)
                    digest_parts.append(
                        f"{row['chunk_id']}:{row['content_hash']}:{dim}:{checksum}"
                    )
                if len(dims) > 1:
                    raise RagPersistenceError(f"mixed embedding dimensions: {sorted(dims)}")
                projection_digest = sha256_hex("\n".join(digest_parts).encode("utf-8"))
                cursor = await conn.execute(
                    """
                    UPDATE index_jobs SET state='READY',projection_digest=?,updated_at=?
                    WHERE job_id=? AND state='VALIDATING'
                    """,
                    (projection_digest, utc_epoch_ms(), job_id),
                )
                if cursor.rowcount != 1:
                    raise IndexJobConflict(f"job {job_id} lost VALIDATING CAS")
            except BaseException:
                await conn.rollback()
                raise
            else:
                await conn.commit()
        return projection_digest

    async def activate(self, job_id: str) -> bool:
        """Switch a document's active version and job state in one authority transaction."""
        async with self.connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                job = await self._fetchone(conn, "SELECT * FROM index_jobs WHERE job_id=?", (job_id,))
                if job is None:
                    raise KeyError(job_id)
                if str(job["state"]) == IndexJobState.ACTIVATED.value:
                    await conn.commit()
                    return False
                if str(job["state"]) != IndexJobState.READY.value:
                    raise IndexJobConflict(f"job {job_id} is not READY")
                now = utc_epoch_ms()
                await conn.execute(
                    """
                    INSERT INTO active_document_versions(document_id,version_id,activated_at)
                    VALUES(?,?,?)
                    ON CONFLICT(document_id) DO UPDATE SET
                      version_id=excluded.version_id, activated_at=excluded.activated_at
                    """,
                    (str(job["document_id"]), str(job["version_id"]), now),
                )
                cursor = await conn.execute(
                    "UPDATE index_jobs SET state='ACTIVATED',updated_at=? WHERE job_id=? AND state='READY'",
                    (now, job_id),
                )
                if cursor.rowcount != 1:
                    raise IndexJobConflict(f"job {job_id} lost READY CAS")
                meta = await self._fetchone(
                    conn,
                    "SELECT generation FROM projection_metadata WHERE projection_name='active_chunks'",
                )
                generation = int(meta["generation"]) + 1 if meta else 1
                await conn.execute(
                    """
                    INSERT INTO projection_metadata(projection_name,generation,source_digest,state,updated_at)
                    VALUES('active_chunks',?,?, 'STALE',?)
                    ON CONFLICT(projection_name) DO UPDATE SET
                      generation=excluded.generation,source_digest=excluded.source_digest,
                      state=excluded.state,updated_at=excluded.updated_at
                    """,
                    (generation, str(job["projection_digest"] or ""), now),
                )
            except BaseException:
                await conn.rollback()
                raise
            else:
                await conn.commit()
        return True

    async def fail_job(self, job_id: str, *, code: str, message: str) -> None:
        async with self.connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                await conn.execute(
                    """
                    UPDATE index_jobs SET state='FAILED',error_code=?,error_message=?,updated_at=?
                    WHERE job_id=? AND state!='ACTIVATED'
                    """,
                    (code, message[:2000], utc_epoch_ms(), job_id),
                )
            except BaseException:
                await conn.rollback()
                raise
            else:
                await conn.commit()

    async def list_active_chunks(self) -> list[StoredChunk]:
        async with self.connection() as conn:
            rows = await self._fetchall(
                conn,
                """
                SELECT c.*,v.document_id,d.dataset_id,d.external_doc_id,
                       e.embedding_model,e.vector_blob,e.dim,e.checksum
                FROM active_document_versions a
                JOIN document_versions v ON v.version_id=a.version_id
                JOIN documents d ON d.document_id=v.document_id
                JOIN chunks c ON c.version_id=a.version_id
                LEFT JOIN chunk_embeddings e ON e.chunk_id=c.chunk_id
                ORDER BY d.dataset_id,d.external_doc_id,c.ordinal
                """,
            )
        return [self._stored_chunk_from_row(row) for row in rows]

    async def replace_active_embeddings(
        self,
        *,
        expected_source_digest: str,
        chunks: Sequence[StoredChunk],
        vectors: Sequence[Sequence[float]],
        embedding_model: str,
    ) -> bool:
        """Replace the active vector projection behind an active-source guard.

        Embedding is intentionally performed by the caller before this method.
        This transaction only verifies that the immutable active chunk set is
        still the one that was embedded, then upserts the derived float32 rows.
        A concurrent active-version switch therefore returns ``False`` instead
        of publishing a snapshot for a stale source.
        """
        if len(chunks) != len(vectors):
            raise ValueError("chunks/vectors count mismatch")
        if not embedding_model.strip():
            raise ValueError("embedding_model must be non-empty")

        now = utc_epoch_ms()
        embedding_rows: list[tuple[Any, ...]] = []
        for chunk, values in zip(chunks, vectors, strict=True):
            vector_blob, dim, checksum = encode_vector(values)
            embedding_rows.append(
                (chunk.chunk_id, embedding_model, dim, vector_blob, checksum, now)
            )

        async with self.connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                active_rows = await self._fetchall(
                    conn,
                    """
                    SELECT c.chunk_id,c.version_id,c.content_hash
                    FROM active_document_versions a
                    JOIN document_versions v ON v.version_id=a.version_id
                    JOIN documents d ON d.document_id=v.document_id
                    JOIN chunks c ON c.version_id=a.version_id
                    ORDER BY d.dataset_id,d.external_doc_id,c.ordinal
                    """,
                )
                active_parts = [
                    f"{row['version_id']}:{row['chunk_id']}:{row['content_hash']}"
                    for row in active_rows
                ]
                active_digest = sha256_hex("\n".join(active_parts).encode("utf-8"))
                expected_parts = [
                    f"{chunk.version_id}:{chunk.chunk_id}:{chunk.content_hash}"
                    for chunk in chunks
                ]
                if (
                    active_digest != expected_source_digest
                    or active_parts != expected_parts
                ):
                    await conn.rollback()
                    return False

                await conn.executemany(
                    """
                    INSERT INTO chunk_embeddings(
                        chunk_id,embedding_model,dim,vector_blob,checksum,created_at
                    ) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(chunk_id) DO UPDATE SET
                        embedding_model=excluded.embedding_model,
                        dim=excluded.dim,
                        vector_blob=excluded.vector_blob,
                        checksum=excluded.checksum,
                        created_at=excluded.created_at
                    """,
                    embedding_rows,
                )
                meta = await self._fetchone(
                    conn,
                    "SELECT generation FROM projection_metadata WHERE projection_name='active_chunks'",
                )
                generation = int(meta["generation"]) + 1 if meta else 1
                await conn.execute(
                    """
                    INSERT INTO projection_metadata(
                        projection_name,generation,source_digest,state,updated_at
                    ) VALUES('active_chunks',?,?,'STALE',?)
                    ON CONFLICT(projection_name) DO UPDATE SET
                        generation=excluded.generation,
                        source_digest=excluded.source_digest,
                        state=excluded.state,
                        updated_at=excluded.updated_at
                    """,
                    (generation, expected_source_digest, now),
                )
            except BaseException:
                await conn.rollback()
                raise
            else:
                await conn.commit()
        return True

    async def active_version_id(self, dataset_id: str, external_doc_id: str) -> str | None:
        async with self.connection() as conn:
            row = await self._fetchone(
                conn,
                """
                SELECT a.version_id FROM documents d
                JOIN active_document_versions a ON a.document_id=d.document_id
                WHERE d.dataset_id=? AND d.external_doc_id=?
                """,
                (dataset_id, external_doc_id),
            )
            return str(row["version_id"]) if row else None

    async def mark_projection_state(self, *, source_digest: str, state: str) -> None:
        if state not in {"READY", "DEGRADED"}:
            raise ValueError(f"unsupported projection state: {state}")
        async with self.connection() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                await conn.execute(
                    """
                    INSERT INTO projection_metadata(projection_name,generation,source_digest,state,updated_at)
                    VALUES('active_chunks',1,?,?,?)
                    ON CONFLICT(projection_name) DO UPDATE SET
                      source_digest=excluded.source_digest,state=excluded.state,updated_at=excluded.updated_at
                    """,
                    (source_digest, state, utc_epoch_ms()),
                )
            except BaseException:
                await conn.rollback()
                raise
            else:
                await conn.commit()

    async def projection_state(self) -> str:
        async with self.connection() as conn:
            row = await self._fetchone(
                conn,
                "SELECT state FROM projection_metadata WHERE projection_name='active_chunks'",
            )
            return str(row["state"]) if row else "MISSING"

    async def count_chunks_for_version(self, version_id: str) -> int:
        async with self.connection() as conn:
            row = await self._fetchone(
                conn, "SELECT COUNT(*) AS n FROM chunks WHERE version_id=?", (version_id,)
            )
            return int(row["n"]) if row else 0

    async def _put_source_blob(self, digest: str, content: bytes) -> str:
        target = self._source_root / digest[:2] / digest
        if target.exists():
            if sha256_hex(target.read_bytes()) != digest:
                raise RagPersistenceError(f"source CAS collision/integrity error: {digest}")
            return f"sha256:{digest}"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f".{digest}.{uuid.uuid4().hex}.tmp"
        with tmp.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(tmp, target)
            dir_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if tmp.exists():
                tmp.unlink()
        return f"sha256:{digest}"

    def _path_from_uri(self, uri: str) -> Path:
        kind, separator, digest = uri.partition(":")
        if kind != "sha256" or not separator or len(digest) != 64:
            raise RagPersistenceError(f"unsupported document URI: {uri}")
        return self._source_root / digest[:2] / digest

    @staticmethod
    async def _fetchone(
        conn: aiosqlite.Connection, sql: str, parameters: Sequence[Any] = ()
    ) -> aiosqlite.Row | None:
        cursor = await conn.execute(sql, parameters)
        return await cursor.fetchone()

    @staticmethod
    async def _fetchall(
        conn: aiosqlite.Connection, sql: str, parameters: Sequence[Any] = ()
    ) -> list[aiosqlite.Row]:
        cursor = await conn.execute(sql, parameters)
        return list(await cursor.fetchall())

    @staticmethod
    def _job_from_row(row: aiosqlite.Row) -> IndexJob:
        return IndexJob(
            job_id=str(row["job_id"]),
            document_id=str(row["document_id"]),
            version_id=str(row["version_id"]),
            dataset_id=str(row["dataset_id"]),
            external_doc_id=str(row["external_doc_id"]),
            state=IndexJobState(str(row["state"])),
            attempt=int(row["attempt"]),
            error_code=str(row["error_code"]) if row["error_code"] is not None else None,
            error_message=str(row["error_message"]) if row["error_message"] is not None else None,
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    @staticmethod
    def _stored_chunk_from_row(row: aiosqlite.Row) -> StoredChunk:
        return StoredChunk(
            chunk_id=str(row["chunk_id"]),
            version_id=str(row["version_id"]),
            ordinal=int(row["ordinal"]),
            content_hash=str(row["content_hash"]),
            document_id=str(row["document_id"]),
            dataset_id=str(row["dataset_id"]),
            external_doc_id=str(row["external_doc_id"]),
            title=str(row["title"]),
            content=str(row["content"]),
            metadata=json.loads(str(row["metadata_json"])),
            page=int(row["page"]) if row["page"] is not None else None,
            span_start=int(row["span_start"]),
            span_end=int(row["span_end"]),
            embedding_model=(
                str(row["embedding_model"])
                if row["embedding_model"] is not None
                else None
            ),
            vector=bytes(row["vector_blob"]) if row["vector_blob"] is not None else None,
            vector_dim=int(row["dim"]) if row["dim"] is not None else None,
            vector_checksum=str(row["checksum"]) if row["checksum"] is not None else None,
        )

    _JOB_SELECT = """
        SELECT j.*,d.dataset_id,d.external_doc_id
        FROM index_jobs j JOIN documents d ON d.document_id=j.document_id
    """


def encode_vector(values: Sequence[float]) -> tuple[bytes, int, str]:
    import numpy as np

    array = np.asarray(values, dtype="<f4")
    if array.ndim != 1 or array.size == 0:
        raise ValueError("embedding must be a non-empty 1D vector")
    blob = array.tobytes(order="C")
    return blob, int(array.size), sha256_hex(blob)
