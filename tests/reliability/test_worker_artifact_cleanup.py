from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from agent.runtime.adapters.filesystem_artifact import FilesystemArtifactStore
from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.runtime.domain.artifact import ArtifactPurpose
from agent.runtime.worker.dispatcher import RuntimeWorker


@dataclass
class FakeClock:
    value: int = 2_000_000_000_000

    def now_ms(self) -> int:
        return self.value

    def monotonic(self) -> float:
        return self.value / 1000

    def advance(self, milliseconds: int) -> None:
        self.value += milliseconds


class _UnusedCoordinator:
    async def execute_claim(self, *_args, **_kwargs):  # pragma: no cover
        raise AssertionError("an empty store must not dispatch an Activity")


@pytest.mark.asyncio
async def test_worker_periodically_reclaims_only_old_unreferenced_blobs(tmp_path) -> None:
    runtime = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await runtime.initialize()
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts")
    referenced = await artifacts.put_bytes(
        b"referenced", purpose=ArtifactPurpose.UPLOAD,
    )
    orphan = await artifacts.put_bytes(
        b"orphan", purpose=ArtifactPurpose.INTERNAL,
    )
    await runtime.register_artifact_metadata(
        artifact_id=referenced.artifact_id,
        sha256=referenced.digest_sha256,
        size_bytes=referenced.size_bytes,
        media_type=referenced.media_type,
        storage_path=f"sha256/{referenced.artifact_id[:2]}/{referenced.artifact_id}",
        created_at=int(referenced.created_at.timestamp() * 1000),
    )
    clock = FakeClock()
    old = (clock.now_ms() - 48 * 3_600_000) / 1000
    for ref in (referenced, orphan):
        os.utime(artifacts.path_for_diagnostics(ref.artifact_id), (old, old))

    worker = RuntimeWorker(
        store=runtime,
        coordinator=_UnusedCoordinator(),  # type: ignore[arg-type]
        worker_id="gc-test",
        release_map={},
        artifact_store=artifacts,
        artifact_cleanup_interval_ms=3_600_000,
        artifact_orphan_age_ms=24 * 3_600_000,
        clock=clock,
    )
    assert await worker.run_once() is False
    assert artifacts.path_for_diagnostics(referenced.artifact_id).exists()
    assert not artifacts.path_for_diagnostics(orphan.artifact_id).exists()

    second = await artifacts.put_bytes(
        b"second-orphan", purpose=ArtifactPurpose.INTERNAL,
    )
    os.utime(artifacts.path_for_diagnostics(second.artifact_id), (old, old))
    assert await worker.run_once() is False
    assert artifacts.path_for_diagnostics(second.artifact_id).exists()
    clock.advance(3_600_000)
    assert await worker.run_once() is False
    assert not artifacts.path_for_diagnostics(second.artifact_id).exists()
