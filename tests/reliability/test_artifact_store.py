from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime, timedelta, timezone

import pytest

from agent.runtime.adapters.filesystem_artifact import FilesystemArtifactStore
from agent.runtime.domain.artifact import (
    ArtifactIntegrityError,
    ArtifactLimits,
    ArtifactNotFoundError,
    ArtifactPurpose,
    ArtifactRangeError,
    ArtifactReadLimitError,
    ArtifactTooLargeError,
    InvalidArtifactIdError,
)
from agent.runtime.ports.artifact import ArtifactStore


@pytest.fixture
def tiny_limits() -> ArtifactLimits:
    return ArtifactLimits(
        upload_max_bytes=8,
        internal_max_bytes=32,
        preview_bytes=4,
        model_read_default_bytes=6,
        model_read_max_bytes=8,
        http_range_max_bytes=10,
    )


@pytest.mark.asyncio
async def test_write_is_content_addressed_durable_and_deduplicated(
    tmp_path, tiny_limits: ArtifactLimits
) -> None:
    store = FilesystemArtifactStore(tmp_path, limits=tiny_limits)
    assert isinstance(store, ArtifactStore)
    content = b"reliable"
    expected = hashlib.sha256(content).hexdigest()

    first, second = await asyncio.gather(
        store.put_bytes(
            content,
            purpose=ArtifactPurpose.UPLOAD,
            media_type="text/plain",
            filename="one.txt",
        ),
        store.put_bytes(
            content,
            purpose=ArtifactPurpose.UPLOAD,
            media_type="text/plain",
            filename="two.txt",
        ),
    )

    assert first.artifact_id == second.artifact_id == expected
    assert first.size_bytes == len(content)
    assert first.filename == "one.txt"
    blob_path = store.path_for_diagnostics(expected)
    assert blob_path == tmp_path / "sha256" / expected[:2] / expected
    assert blob_path.read_bytes() == content
    assert list((tmp_path / ".tmp").iterdir()) == []


@pytest.mark.asyncio
async def test_upload_and_internal_limits_are_distinct_and_temp_is_cleaned(
    tmp_path, tiny_limits: ArtifactLimits
) -> None:
    store = FilesystemArtifactStore(tmp_path, limits=tiny_limits)
    content = b"123456789"

    with pytest.raises(ArtifactTooLargeError) as error:
        await store.put_bytes(content, purpose=ArtifactPurpose.UPLOAD)
    assert error.value.limit_bytes == 8
    assert list((tmp_path / ".tmp").iterdir()) == []

    result = await store.put_bytes(content, purpose=ArtifactPurpose.INTERNAL)
    assert result.size_bytes == 9


@pytest.mark.asyncio
async def test_preview_model_read_and_http_range_have_separate_bounds(
    tmp_path, tiny_limits: ArtifactLimits
) -> None:
    store = FilesystemArtifactStore(tmp_path, limits=tiny_limits)
    ref = await store.put_bytes(
        b"0123456789abcdef", purpose=ArtifactPurpose.INTERNAL
    )

    preview = await store.read_preview(ref.artifact_id)
    assert (preview.start, preview.end_exclusive, preview.data) == (0, 4, b"0123")

    bounded = await store.read_bounded(ref.artifact_id, offset=3)
    assert (bounded.start, bounded.end_exclusive, bounded.data) == (
        3,
        9,
        b"345678",
    )

    ranged = await store.read_range(
        ref.artifact_id, start=4, end_exclusive=14
    )
    assert ranged.data == b"456789abcd"
    assert ranged.total_size == 16

    with pytest.raises(ArtifactReadLimitError):
        await store.read_preview(ref.artifact_id, max_bytes=5)
    with pytest.raises(ArtifactReadLimitError):
        await store.read_bounded(ref.artifact_id, max_bytes=9)
    with pytest.raises(ArtifactReadLimitError):
        await store.read_range(ref.artifact_id, start=0, end_exclusive=11)
    with pytest.raises(ArtifactReadLimitError):
        await store.read_range(ref.artifact_id, start=0)
    with pytest.raises(ArtifactRangeError):
        await store.read_range(ref.artifact_id, start=16)


@pytest.mark.asyncio
async def test_digest_tampering_is_rejected_before_bytes_are_returned(
    tmp_path, tiny_limits: ArtifactLimits
) -> None:
    store = FilesystemArtifactStore(tmp_path, limits=tiny_limits)
    ref = await store.put_bytes(b"truth", purpose=ArtifactPurpose.UPLOAD)
    blob_path = store.path_for_diagnostics(ref.artifact_id)
    blob_path.chmod(0o644)
    blob_path.write_bytes(b"tampered")

    with pytest.raises(ArtifactIntegrityError) as error:
        await store.read_bounded(ref.artifact_id)
    assert error.value.code == "ARTIFACT_INTEGRITY_ERROR"


@pytest.mark.asyncio
async def test_invalid_and_missing_ids_cannot_escape_cas_root(
    tmp_path, tiny_limits: ArtifactLimits
) -> None:
    store = FilesystemArtifactStore(tmp_path, limits=tiny_limits)

    with pytest.raises(InvalidArtifactIdError):
        await store.read_bounded("../../etc/passwd")
    missing = "0" * 64
    with pytest.raises(ArtifactNotFoundError):
        await store.read_bounded(missing)


@pytest.mark.asyncio
async def test_orphan_cleanup_keeps_referenced_and_recent_blobs(
    tmp_path, tiny_limits: ArtifactLimits
) -> None:
    now = datetime(2026, 8, 9, 10, tzinfo=timezone.utc)
    store = FilesystemArtifactStore(
        tmp_path,
        limits=tiny_limits,
        clock=lambda: now,
    )
    referenced = await store.put_bytes(b"keep", purpose=ArtifactPurpose.UPLOAD)
    orphan = await store.put_bytes(b"orphan", purpose=ArtifactPurpose.UPLOAD)
    recent = await store.put_bytes(b"recent", purpose=ArtifactPurpose.UPLOAD)
    old_timestamp = (now - timedelta(days=2)).timestamp()
    os.utime(store.path_for_diagnostics(referenced.artifact_id), (old_timestamp,) * 2)
    os.utime(store.path_for_diagnostics(orphan.artifact_id), (old_timestamp,) * 2)

    result = await store.cleanup_orphans(
        referenced_artifact_ids={referenced.artifact_id},
        older_than=now - timedelta(hours=24),
    )

    assert result.scanned == 3
    assert result.referenced == 1
    assert result.too_new == 1
    assert result.deleted == 1
    assert result.deleted_artifact_ids == (orphan.artifact_id,)
    assert result.reclaimed_bytes == len(b"orphan")
    assert store.path_for_diagnostics(referenced.artifact_id).exists()
    assert store.path_for_diagnostics(recent.artifact_id).exists()
    assert not store.path_for_diagnostics(orphan.artifact_id).exists()


@pytest.mark.asyncio
async def test_blob_created_before_metadata_failure_is_a_cleanable_orphan(
    tmp_path, tiny_limits: ArtifactLimits
) -> None:
    now = datetime(2026, 8, 9, 10, tzinfo=timezone.utc)
    store = FilesystemArtifactStore(tmp_path, limits=tiny_limits)
    ref = await store.put_bytes(b"unlinked", purpose=ArtifactPurpose.UPLOAD)
    blob_path = store.path_for_diagnostics(ref.artifact_id)
    old_timestamp = (now - timedelta(days=2)).timestamp()
    os.utime(blob_path, (old_timestamp,) * 2)

    result = await store.cleanup_orphans(
        referenced_artifact_ids=set(),
        older_than=now - timedelta(hours=24),
    )

    assert result.deleted_artifact_ids == (ref.artifact_id,)
    assert not blob_path.exists()


@pytest.mark.asyncio
async def test_concurrent_dedup_publish_cannot_be_lost_to_orphan_cleanup(
    tmp_path, tiny_limits: ArtifactLimits
) -> None:
    now = datetime.now(timezone.utc)
    store = FilesystemArtifactStore(tmp_path, limits=tiny_limits)
    original = await store.put_bytes(b"old", purpose=ArtifactPurpose.UPLOAD)
    blob_path = store.path_for_diagnostics(original.artifact_id)
    old_timestamp = (now - timedelta(days=2)).timestamp()
    os.utime(blob_path, (old_timestamp,) * 2)

    republished, _cleanup = await asyncio.gather(
        store.put_bytes(b"old", purpose=ArtifactPurpose.UPLOAD),
        store.cleanup_orphans(
            referenced_artifact_ids=set(),
            older_than=now - timedelta(hours=24),
        ),
    )

    assert republished.artifact_id == original.artifact_id
    assert blob_path.exists()
    assert (await store.read_bounded(original.artifact_id)).data == b"old"


@pytest.mark.asyncio
async def test_stream_rejects_non_bytes_chunks_and_removes_temp_file(
    tmp_path, tiny_limits: ArtifactLimits
) -> None:
    store = FilesystemArtifactStore(tmp_path, limits=tiny_limits)

    async def invalid_stream():
        yield b"valid"
        yield "not-bytes"

    with pytest.raises(TypeError, match="chunks must be bytes"):
        await store.put_stream(
            invalid_stream(),  # type: ignore[arg-type]
            purpose=ArtifactPurpose.INTERNAL,
        )
    assert list((tmp_path / ".tmp").iterdir()) == []
