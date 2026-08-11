from __future__ import annotations

import asyncio

import pytest

from agent.runtime.adapters.sqlite.database import (
    RuntimeDatabase,
    SchemaCompatibilityError,
)
from agent.runtime.domain.models import SCHEMA_VERSION


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
            "SELECT id,schema_version FROM schema_meta"
        )).fetchall()
    assert [(row["id"], row["schema_version"]) for row in rows] == [(1, SCHEMA_VERSION)]


@pytest.mark.asyncio
async def test_runtime_schema_identity_mismatch_fails_fast(tmp_path):
    database = RuntimeDatabase(tmp_path / "runtime.db")
    await database.ensure_schema()
    async with database.transaction() as conn:
        await conn.execute("UPDATE schema_meta SET schema_checksum='rewritten'")
    with pytest.raises(SchemaCompatibilityError, match="checksum mismatch"):
        await database.ensure_schema()

    other = RuntimeDatabase(tmp_path / "other.db")
    await other.ensure_schema()
    async with other.transaction() as conn:
        await conn.execute("UPDATE schema_meta SET schema_version='999'")
    with pytest.raises(SchemaCompatibilityError, match="schema_version mismatch"):
        await other.ensure_schema()


@pytest.mark.asyncio
async def test_foreign_database_without_schema_meta_is_never_adopted(tmp_path):
    database = RuntimeDatabase(tmp_path / "foreign.db")
    conn = await database.connect()
    try:
        await conn.execute("CREATE TABLE unrelated (x TEXT)")
    finally:
        await conn.close()
    with pytest.raises(SchemaCompatibilityError, match="schema_meta is missing"):
        await database.ensure_schema()
