from __future__ import annotations

import sqlite3

import pytest

from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.runtime.domain.models import ReleaseManifest


@pytest.mark.asyncio
async def test_worker_publishes_three_active_release_pointers_atomically(tmp_path):
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    manifests = tuple(
        ReleaseManifest(engine=engine, components={"source": "atomic-v1"})
        for engine in ("plan_execute", "agent_loop", "native_loop")
    )
    async with store.db.transaction() as conn:
        await conn.execute(
            """CREATE TRIGGER inject_release_batch_failure
               BEFORE INSERT ON active_releases
               WHEN NEW.engine='agent_loop'
               BEGIN SELECT RAISE(ABORT,'injected release activation failure'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected release"):
        await store.register_releases(manifests, activate=True)
    assert await store.active_releases() == {}
    async with store.db.read() as conn:
        count = await (await conn.execute(
            "SELECT COUNT(*) FROM release_manifests"
        )).fetchone()
    assert count[0] == 0

    async with store.db.transaction() as conn:
        await conn.execute("DROP TRIGGER inject_release_batch_failure")
    expected = {manifest.engine: manifest.fingerprint() for manifest in manifests}
    assert await store.register_releases(manifests, activate=True) == expected
    assert await store.active_releases() == expected
