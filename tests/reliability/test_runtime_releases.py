from __future__ import annotations

import asyncio
import sqlite3

import pytest

from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.runtime.application.admission import AdmissionService, CreateRunInput
from agent.runtime.domain.errors import RuntimeFault
from agent.runtime.domain.models import ReleaseManifest


def _manifests(version: str) -> tuple[ReleaseManifest, ...]:
    return tuple(
        ReleaseManifest(engine=engine, components={"source": version})
        for engine in ("plan_execute", "agent_loop", "native_loop")
    )


async def _admit_native(store: SqliteRuntimeStore, key: str):
    return await AdmissionService(store).create(
        CreateRunInput(
            client_request_id=f"request-{key}",
            conversation_id=None,
            principal_id="release-test-user",
            agent_id="release-test-agent",
            engine="native_loop",
            text="test exact release",
            attachment_refs=(),
            deadline_at=None,
        ),
        idempotency_key=key,
    )


@pytest.mark.asyncio
async def test_worker_publishes_three_active_release_pointers_atomically(tmp_path):
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    manifests = _manifests("atomic-v1")
    async with store.db.transaction() as conn:
        await conn.execute(
            """CREATE TRIGGER inject_release_batch_failure
               BEFORE INSERT ON active_releases
               WHEN NEW.engine='agent_loop'
               BEGIN SELECT RAISE(ABORT,'injected release activation failure'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected release"):
        await store.activate_current_releases(manifests)
    assert await store.active_releases() == {}
    async with store.db.read() as conn:
        count = await (await conn.execute(
            "SELECT COUNT(*) FROM release_manifests"
        )).fetchone()
    assert count[0] == 0

    async with store.db.transaction() as conn:
        await conn.execute("DROP TRIGGER inject_release_batch_failure")
    expected = {manifest.engine: manifest.fingerprint() for manifest in manifests}
    assert await store.activate_current_releases(manifests) == expected
    assert await store.active_releases() == expected


@pytest.mark.asyncio
async def test_release_activation_rejects_any_partial_engine_set(tmp_path):
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()

    with pytest.raises(ValueError, match="exactly plan_execute"):
        await store.activate_current_releases((
            ReleaseManifest(engine="native_loop", components={"source": "partial"}),
        ))

    assert await store.active_releases() == {}
    async with store.db.read() as conn:
        count = await (await conn.execute(
            "SELECT COUNT(*) FROM release_manifests"
        )).fetchone()
    assert count[0] == 0


@pytest.mark.asyncio
async def test_active_run_blocks_entire_new_release_activation(tmp_path):
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    first = _manifests("release-v1")
    first_map = await store.activate_current_releases(first)
    admitted = await _admit_native(store, "blocks-release")

    second = _manifests("release-v2")
    with pytest.raises(RuntimeFault) as raised:
        await store.activate_current_releases(second)
    assert raised.value.code == "ACTIVE_RUNS_BLOCK_RELEASE_ACTIVATION"
    assert raised.value.details["run_id"] == admitted.run.envelope.run_id
    assert await store.active_releases() == first_map
    async with store.db.read() as conn:
        second_count = await (await conn.execute(
            "SELECT count(*) FROM release_manifests WHERE release_fingerprint IN (?,?,?)",
            tuple(manifest.fingerprint() for manifest in second),
        )).fetchone()
    assert second_count[0] == 0


@pytest.mark.asyncio
async def test_same_release_multi_worker_activation_is_idempotent(tmp_path):
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    manifests = _manifests("same-release")
    expected = await store.activate_current_releases(manifests)
    assert await store.activate_current_releases(manifests) == expected
    assert await store.active_releases() == expected


@pytest.mark.asyncio
async def test_activation_and_admission_are_serialized_by_write_transaction(tmp_path):
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    first = _manifests("race-v1")
    first_map = await store.activate_current_releases(first)
    second = _manifests("race-v2")
    second_map = {item.engine: item.fingerprint() for item in second}

    activation, admission = await asyncio.gather(
        store.activate_current_releases(second),
        _admit_native(store, "activation-admission-race"),
        return_exceptions=True,
    )
    assert not isinstance(admission, BaseException)
    active = await store.active_releases()
    if isinstance(activation, RuntimeFault):
        assert activation.code == "ACTIVE_RUNS_BLOCK_RELEASE_ACTIVATION"
        assert active == first_map
    else:
        assert activation == second_map
        assert active == second_map
    assert admission.run.envelope.release_fingerprint == active["native_loop"]


@pytest.mark.asyncio
async def test_claim_requires_exact_engine_and_release_pair(tmp_path):
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    releases = await store.activate_current_releases(_manifests("claim-v1"))
    admitted = await _admit_native(store, "exact-claim")

    assert await store.claim_next(
        worker_id="wrong-release", lease_ms=30_000, now_ms=admitted.run.updated_at,
        release_map={"native_loop": "0" * 64},
    ) is None
    assert await store.claim_next(
        worker_id="wrong-engine", lease_ms=30_000, now_ms=admitted.run.updated_at,
        release_map={"agent_loop": releases["agent_loop"]},
    ) is None
    claim = await store.claim_next(
        worker_id="exact-worker", lease_ms=30_000, now_ms=admitted.run.updated_at,
        release_map={"native_loop": releases["native_loop"]},
    )
    assert claim is not None
    assert claim.run.envelope.run_id == admitted.run.envelope.run_id
