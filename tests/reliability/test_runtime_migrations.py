from __future__ import annotations

import asyncio

import pytest

from agent.runtime.adapters.sqlite.database import (
    RuntimeDatabase,
    SchemaCompatibilityError,
)


@pytest.mark.asyncio
async def test_api_and_worker_can_initialize_the_same_empty_runtime_database(tmp_path):
    path = tmp_path / "runtime.db"
    api = RuntimeDatabase(path)
    worker = RuntimeDatabase(path)

    await asyncio.gather(api.migrate(), worker.migrate())
    await api.verify()
    async with api.read() as conn:
        rows = await (await conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )).fetchall()
    assert [row["version"] for row in rows] == [1, 2]


@pytest.mark.asyncio
async def test_existing_v1_database_upgrades_to_tool_reconciliation_capability(tmp_path):
    path = tmp_path / "runtime.db"
    database = RuntimeDatabase(path)
    current_migrations = database._migrations_dir
    v1_only = tmp_path / "v1-migrations"
    v1_only.mkdir()
    source = current_migrations / "001_runtime.sql"
    (v1_only / source.name).write_bytes(source.read_bytes())
    database._migrations_dir = v1_only
    await database.migrate()

    database._migrations_dir = current_migrations
    await database.migrate()
    await database.verify()
    async with database.read() as conn:
        versions = await (await conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        )).fetchall()
        columns = await (await conn.execute(
            "PRAGMA table_info(tool_executions)"
        )).fetchall()
    assert [row["version"] for row in versions] == [1, 2]
    assert "supports_reconcile" in {row["name"] for row in columns}


@pytest.mark.asyncio
async def test_runtime_migration_checksum_and_unknown_version_fail_fast(tmp_path):
    database = RuntimeDatabase(tmp_path / "runtime.db")
    await database.migrate()
    async with database.transaction() as conn:
        await conn.execute(
            "UPDATE schema_migrations SET checksum='rewritten' WHERE version=1"
        )
    with pytest.raises(SchemaCompatibilityError, match="checksum mismatch"):
        await database.migrate()

    other = RuntimeDatabase(tmp_path / "newer.db")
    await other.migrate()
    async with other.transaction() as conn:
        await conn.execute(
            """INSERT INTO schema_migrations(version,name,checksum,applied_at)
               VALUES (999,'999_future.sql','future',0)"""
        )
    with pytest.raises(SchemaCompatibilityError, match="newer than this runtime"):
        await other.migrate()
