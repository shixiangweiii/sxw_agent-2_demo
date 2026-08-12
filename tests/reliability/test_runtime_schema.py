from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent.runtime.adapters.sqlite.database import RuntimeDatabase
from arag.persistence import repository as rag_repository_module
from arag.persistence.repository import RagRepository
from common.sqlite_schema import SchemaIdentityError, schema_digest


@pytest.mark.asyncio
async def test_api_and_worker_can_initialize_the_same_empty_runtime_database(tmp_path):
    path = tmp_path / "runtime.db"
    api = RuntimeDatabase(path)
    worker = RuntimeDatabase(path)

    # Both processes bootstrap the same empty file; the loser blocks on the write
    # lock and then observes the committed schema instead of creating a second one.
    await asyncio.gather(api.ensure_schema(), worker.ensure_schema())
    await api.ensure_schema()
    async with api.read() as conn:
        rows = await (await conn.execute(
            "SELECT id,schema_digest FROM schema_meta"
        )).fetchall()
    expected = schema_digest(api._schema_path.read_bytes())
    assert [(row["id"], row["schema_digest"]) for row in rows] == [(1, expected)]


@pytest.mark.asyncio
async def test_runtime_schema_identity_mismatch_fails_fast(tmp_path):
    database = RuntimeDatabase(tmp_path / "runtime.db")
    await database.ensure_schema()
    async with database.transaction() as conn:
        await conn.execute("UPDATE schema_meta SET schema_digest=?", ("0" * 64,))
    with pytest.raises(
        SchemaIdentityError, match="CURRENT_SCHEMA_MISMATCH: runtime database schema digest mismatch"
    ):
        await database.ensure_schema()


@pytest.mark.asyncio
async def test_foreign_database_without_schema_meta_is_never_adopted(tmp_path):
    database = RuntimeDatabase(tmp_path / "foreign.db")
    conn = await database.connect()
    try:
        await conn.execute("CREATE TABLE unrelated (x TEXT)")
    finally:
        await conn.close()
    with pytest.raises(SchemaIdentityError, match="schema_meta is missing"):
        await database.ensure_schema()


@pytest.mark.asyncio
async def test_foreign_database_with_only_a_view_is_not_treated_as_empty(tmp_path):
    database = RuntimeDatabase(tmp_path / "foreign-view.db")
    conn = await database.connect()
    try:
        await conn.execute("CREATE VIEW unrelated AS SELECT 1 AS value")
    finally:
        await conn.close()
    with pytest.raises(SchemaIdentityError, match="schema_meta is missing"):
        await database.ensure_schema()


@pytest.mark.asyncio
async def test_two_arag_processes_bootstrap_the_same_current_schema(tmp_path):
    path = tmp_path / "rag.db"
    first = RagRepository(path, tmp_path / "arag")
    second = RagRepository(path, tmp_path / "arag")

    await asyncio.gather(first.initialize(), second.initialize())
    async with first.connection() as conn:
        row = await (await conn.execute(
            "SELECT id,schema_digest FROM schema_meta"
        )).fetchone()
    assert row["id"] == 1
    assert row["schema_digest"] == schema_digest(
        Path(rag_repository_module.__file__).with_name("schema.sql").read_bytes()
    )
