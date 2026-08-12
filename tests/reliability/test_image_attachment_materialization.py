from __future__ import annotations

import hashlib

import pytest

from agent.runtime.adapters.filesystem_artifact import FilesystemArtifactStore
from agent.runtime.adapters.adk_engines import AdkEngineAdapter
from agent.runtime.domain.artifact import ArtifactPurpose


@pytest.mark.asyncio
async def test_large_image_is_reassembled_from_verified_bounded_ranges(tmp_path) -> None:
    content = b"image-bytes" * 100_000  # greater than the 1 MiB HTTP range cap
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    ref = await store.put_bytes(
        content,
        purpose=ArtifactPurpose.UPLOAD,
        media_type="image/png",
    )
    assert ref.artifact_id == hashlib.sha256(content).hexdigest()

    # _read_all is intentionally a small adapter boundary; constructing the
    # actual LLM/ADK context is unrelated to the CAS materialization contract.
    adapter = object.__new__(AdkEngineAdapter)
    adapter.artifact_store = store
    materialized = await adapter._read_all(  # noqa: SLF001 - contract regression
        ref.artifact_id,
        total_size=ref.size_bytes,
    )
    assert materialized == content
