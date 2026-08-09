"""本机内容寻址 Artifact Store。

Durability boundary：同一文件系统内写临时文件并 fsync，随后原子 rename 到
``sha256/<prefix>/<digest>``，最后 fsync 目标目录。只有完整流程成功后 ``put``
才返回；调用方此后才能提交 metadata/ref/event 事务。
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import tempfile
from collections.abc import AsyncIterable, Collection
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable

from agent.runtime.domain.artifact import (
    SHA256_HEX_PATTERN,
    ArtifactCleanupResult,
    ArtifactIntegrityError,
    ArtifactLimits,
    ArtifactNotFoundError,
    ArtifactPurpose,
    ArtifactRangeError,
    ArtifactReadLimitError,
    ArtifactRef,
    ArtifactSlice,
    ArtifactTooLargeError,
    InvalidArtifactIdError,
)


_COPY_CHUNK_BYTES = 1024 * 1024


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def _one_chunk(data: bytes) -> AsyncIterable[bytes]:
    yield data


class FilesystemArtifactStore:
    """单机磁盘 CAS；可被多个本机 API/Worker 进程并发使用。"""

    def __init__(
        self,
        root: str | Path,
        *,
        limits: ArtifactLimits | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.blob_root = self.root / "sha256"
        self.temp_root = self.root / ".tmp"
        self.lock_path = self.root / ".cas.lock"
        self.limits = limits or ArtifactLimits()
        self._clock = clock
        self._ensure_layout()

    def _ensure_layout(self) -> None:
        _ensure_directory_durable(self.root)
        _ensure_directory_durable(self.blob_root)
        _ensure_directory_durable(self.temp_root)

    async def put_bytes(
        self,
        data: bytes,
        *,
        purpose: ArtifactPurpose,
        media_type: str = "application/octet-stream",
        filename: str | None = None,
    ) -> ArtifactRef:
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        return await self.put_stream(
            _one_chunk(data),
            purpose=purpose,
            media_type=media_type,
            filename=filename,
        )

    async def put_stream(
        self,
        chunks: AsyncIterable[bytes],
        *,
        purpose: ArtifactPurpose,
        media_type: str = "application/octet-stream",
        filename: str | None = None,
    ) -> ArtifactRef:
        if not isinstance(purpose, ArtifactPurpose):
            purpose = ArtifactPurpose(purpose)
        if not media_type or not media_type.strip():
            raise ValueError("media_type cannot be empty")

        limit_bytes = self.limits.write_limit_for(purpose)
        fd, raw_temp_path = tempfile.mkstemp(
            prefix="artifact-", suffix=".part", dir=self.temp_root
        )
        temp_path = Path(raw_temp_path)
        handle = os.fdopen(fd, "wb")
        digest = hashlib.sha256()
        total = 0
        closed = False

        try:
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise TypeError("artifact stream chunks must be bytes")
                if not chunk:
                    continue
                observed = total + len(chunk)
                if observed > limit_bytes:
                    raise ArtifactTooLargeError(
                        limit_bytes=limit_bytes, observed_bytes=observed
                    )
                await asyncio.to_thread(_write_all, handle, chunk)
                digest.update(chunk)
                total = observed

            await asyncio.to_thread(_flush_fsync_close, handle)
            closed = True
            digest_hex = digest.hexdigest()
            destination = self._blob_path_unchecked(digest_hex)
            await asyncio.to_thread(
                _durably_publish,
                temp_path,
                destination,
                digest_hex,
                self.lock_path,
            )
            created_at = self._clock()
            if created_at.tzinfo is None or created_at.utcoffset() is None:
                raise ValueError("artifact clock must return a timezone-aware datetime")
            return ArtifactRef(
                artifact_id=digest_hex,
                digest_sha256=digest_hex,
                size_bytes=total,
                media_type=media_type.strip(),
                filename=filename,
                created_at=created_at.astimezone(timezone.utc),
            )
        finally:
            if not closed:
                await asyncio.to_thread(_close_quietly, handle)
            await asyncio.to_thread(_unlink_if_exists, temp_path)

    async def read_preview(
        self,
        artifact_id: str,
        *,
        max_bytes: int | None = None,
    ) -> ArtifactSlice:
        requested = self.limits.preview_bytes if max_bytes is None else max_bytes
        _validate_positive_limit(requested)
        if requested > self.limits.preview_bytes:
            raise ArtifactReadLimitError(
                requested_bytes=requested,
                limit_bytes=self.limits.preview_bytes,
            )
        return await self._read_with_max(
            artifact_id,
            start=0,
            max_bytes=requested,
        )

    async def read_bounded(
        self,
        artifact_id: str,
        *,
        offset: int = 0,
        max_bytes: int | None = None,
    ) -> ArtifactSlice:
        requested = (
            self.limits.model_read_default_bytes
            if max_bytes is None
            else max_bytes
        )
        _validate_positive_limit(requested)
        if requested > self.limits.model_read_max_bytes:
            raise ArtifactReadLimitError(
                requested_bytes=requested,
                limit_bytes=self.limits.model_read_max_bytes,
            )
        return await self._read_with_max(
            artifact_id,
            start=offset,
            max_bytes=requested,
        )

    async def _read_with_max(
        self,
        artifact_id: str,
        *,
        start: int,
        max_bytes: int,
    ) -> ArtifactSlice:
        if start < 0:
            raise ArtifactRangeError("range start cannot be negative")
        path = self._blob_path(artifact_id)
        return await asyncio.to_thread(
            _verified_read,
            path,
            artifact_id,
            start,
            None,
            max_bytes,
            False,
            self.lock_path,
        )

    async def read_range(
        self,
        artifact_id: str,
        *,
        start: int = 0,
        end_exclusive: int | None = None,
    ) -> ArtifactSlice:
        if start < 0:
            raise ArtifactRangeError("range start cannot be negative")
        if end_exclusive is not None and end_exclusive <= start:
            raise ArtifactRangeError("range end must be greater than start")
        if (
            end_exclusive is not None
            and end_exclusive - start > self.limits.http_range_max_bytes
        ):
            raise ArtifactReadLimitError(
                requested_bytes=end_exclusive - start,
                limit_bytes=self.limits.http_range_max_bytes,
            )
        path = self._blob_path(artifact_id)
        return await asyncio.to_thread(
            _verified_read,
            path,
            artifact_id,
            start,
            end_exclusive,
            self.limits.http_range_max_bytes,
            True,
            self.lock_path,
        )

    async def cleanup_orphans(
        self,
        *,
        referenced_artifact_ids: Collection[str],
        older_than: datetime,
    ) -> ArtifactCleanupResult:
        if older_than.tzinfo is None or older_than.utcoffset() is None:
            raise ValueError("older_than must be timezone-aware")
        referenced = {_validate_artifact_id(value) for value in referenced_artifact_ids}
        return await asyncio.to_thread(
            _cleanup_orphans_sync,
            self.blob_root,
            self.temp_root,
            self.lock_path,
            referenced,
            older_than.timestamp(),
        )

    def path_for_diagnostics(self, artifact_id: str) -> Path:
        """只供运维校验/测试定位；业务读取必须经过完整性校验 API。"""

        return self._blob_path(artifact_id)

    def _blob_path(self, artifact_id: str) -> Path:
        return self._blob_path_unchecked(_validate_artifact_id(artifact_id))

    def _blob_path_unchecked(self, artifact_id: str) -> Path:
        return self.blob_root / artifact_id[:2] / artifact_id


def _validate_artifact_id(artifact_id: str) -> str:
    if not isinstance(artifact_id, str) or not SHA256_HEX_PATTERN.fullmatch(
        artifact_id
    ):
        raise InvalidArtifactIdError(
            "artifact_id must be a lowercase 64-character SHA-256 digest"
        )
    return artifact_id


def _validate_positive_limit(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ArtifactRangeError("read limit must be a positive integer")


def _write_all(handle: BinaryIO, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = handle.write(view)
        if written is None or written <= 0:
            raise OSError("artifact temporary file write made no progress")
        view = view[written:]


def _flush_fsync_close(handle: BinaryIO) -> None:
    handle.flush()
    os.fsync(handle.fileno())
    handle.close()


def _close_quietly(handle: BinaryIO) -> None:
    try:
        handle.close()
    except OSError:
        pass


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _hash_open_file(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    handle.seek(0)
    while chunk := handle.read(_COPY_CHUNK_BYTES):
        digest.update(chunk)
    return digest.hexdigest()


def _hash_path(path: Path) -> str:
    with path.open("rb") as handle:
        return _hash_open_file(handle)


def _durably_publish(
    temp_path: Path,
    destination: Path,
    digest_hex: str,
    lock_path: Path,
) -> None:
    with _exclusive_cas_lock(lock_path):
        _durably_publish_locked(temp_path, destination, digest_hex)


def _durably_publish_locked(
    temp_path: Path,
    destination: Path,
    digest_hex: str,
) -> None:
    _ensure_directory_durable(destination.parent)

    # 内容寻址使并发 writer 的目标一致。已存在且完整时保留原 inode；若磁盘内容
    # 被篡改，则用本次已经 fsync 的正确副本原子替换。
    try:
        existing_is_valid = (
            destination.exists()
            and not destination.is_symlink()
            and destination.is_file()
            and _hash_path(destination) == digest_hex
        )
    except FileNotFoundError:
        # 与清理器并发时可能在 exists/hash 之间消失，本 writer 继续发布即可。
        existing_is_valid = False
    if existing_is_valid:
        # 去重命中仍刷新 housekeeping mtime。这样 cleanup 使用 admission 前的引用
        # 快照时，也不会删除一个刚被新 metadata 事务复用的历史 orphan。
        os.utime(destination, None)
        descriptor = os.open(destination, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        temp_path.unlink()
        _fsync_directory(temp_path.parent)
        return

    os.replace(temp_path, destination)
    os.chmod(destination, 0o444)
    descriptor = os.open(destination, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(destination.parent)
    # rename 跨两个目录；source dir 也必须 fsync，才能让临时目录项删除持久化。
    _fsync_directory(temp_path.parent)


def _open_blob_no_follow(path: Path) -> BinaryIO:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise ArtifactNotFoundError(f"artifact {path.name} does not exist") from exc
    except OSError as exc:
        # CAS 树中不应出现 symlink 或特殊节点；将其视作完整性错误而非跟随。
        raise ArtifactIntegrityError(path.name, "unsafe-or-unreadable-path") from exc
    return os.fdopen(descriptor, "rb")


def _verified_read(
    path: Path,
    artifact_id: str,
    start: int,
    requested_end: int | None,
    max_bytes: int,
    reject_oversized_open_end: bool,
    lock_path: Path,
) -> ArtifactSlice:
    with _shared_cas_lock(lock_path):
        return _verified_read_locked(
            path,
            artifact_id,
            start,
            requested_end,
            max_bytes,
            reject_oversized_open_end,
        )


def _verified_read_locked(
    path: Path,
    artifact_id: str,
    start: int,
    requested_end: int | None,
    max_bytes: int,
    reject_oversized_open_end: bool,
) -> ArtifactSlice:
    with _open_blob_no_follow(path) as handle:
        stat_before = os.fstat(handle.fileno())
        total_size = stat_before.st_size
        if start > total_size or (
            reject_oversized_open_end and start == total_size and total_size > 0
        ):
            raise ArtifactRangeError(
                f"range start {start} is outside artifact size {total_size}"
            )

        if requested_end is None:
            remaining = total_size - start
            if reject_oversized_open_end and remaining > max_bytes:
                raise ArtifactReadLimitError(
                    requested_bytes=remaining,
                    limit_bytes=max_bytes,
                )
            end = min(total_size, start + max_bytes)
        else:
            if requested_end > total_size:
                raise ArtifactRangeError(
                    f"range end {requested_end} exceeds artifact size {total_size}"
                )
            end = requested_end

        actual_digest = _hash_open_file(handle)
        stat_after_hash = os.fstat(handle.fileno())
        if (
            actual_digest != artifact_id
            or stat_before.st_size != stat_after_hash.st_size
            or stat_before.st_mtime_ns != stat_after_hash.st_mtime_ns
        ):
            raise ArtifactIntegrityError(artifact_id, actual_digest)

        handle.seek(start)
        data = handle.read(end - start)
        if len(data) != end - start:
            raise ArtifactIntegrityError(artifact_id, "short-read-after-verification")
        stat_after_read = os.fstat(handle.fileno())
        if (
            stat_before.st_size != stat_after_read.st_size
            or stat_before.st_mtime_ns != stat_after_read.st_mtime_ns
        ):
            raise ArtifactIntegrityError(artifact_id, "changed-during-read")

    return ArtifactSlice(
        artifact_id=artifact_id,
        digest_sha256=artifact_id,
        total_size=total_size,
        start=start,
        end_exclusive=end,
        data=data,
    )


def _cleanup_orphans_sync(
    blob_root: Path,
    temp_root: Path,
    lock_path: Path,
    referenced: set[str],
    cutoff_timestamp: float,
) -> ArtifactCleanupResult:
    with _exclusive_cas_lock(lock_path):
        return _cleanup_orphans_locked(
            blob_root,
            temp_root,
            referenced,
            cutoff_timestamp,
        )


def _cleanup_orphans_locked(
    blob_root: Path,
    temp_root: Path,
    referenced: set[str],
    cutoff_timestamp: float,
) -> ArtifactCleanupResult:
    scanned = 0
    referenced_count = 0
    too_new = 0
    deleted: list[str] = []
    reclaimed_bytes = 0
    changed_directories: set[Path] = set()

    for prefix_directory in blob_root.iterdir():
        if not prefix_directory.is_dir() or len(prefix_directory.name) != 2:
            continue
        for path in prefix_directory.iterdir():
            artifact_id = path.name
            if not SHA256_HEX_PATTERN.fullmatch(artifact_id) or path.is_symlink():
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            if not path.is_file():
                continue
            scanned += 1
            if artifact_id in referenced:
                referenced_count += 1
                continue
            if stat.st_mtime >= cutoff_timestamp:
                too_new += 1
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            deleted.append(artifact_id)
            reclaimed_bytes += stat.st_size
            changed_directories.add(prefix_directory)

    # 进程在 rename 前崩溃可能留下 .part；它们没有 artifact_id，也按相同 cutoff
    # 回收，但不计入 blob 的 scanned/deleted 语义。
    for temp_path in temp_root.glob("*.part"):
        try:
            temp_stat = temp_path.stat()
        except FileNotFoundError:
            continue
        if temp_stat.st_mtime >= cutoff_timestamp:
            continue
        try:
            temp_path.unlink()
        except FileNotFoundError:
            continue
        reclaimed_bytes += temp_stat.st_size
        changed_directories.add(temp_root)

    for directory in changed_directories:
        _fsync_directory(directory)

    return ArtifactCleanupResult(
        scanned=scanned,
        referenced=referenced_count,
        too_new=too_new,
        deleted=len(deleted),
        reclaimed_bytes=reclaimed_bytes,
        deleted_artifact_ids=tuple(sorted(deleted)),
    )


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_directory_durable(directory: Path) -> None:
    """创建缺失目录，并逐层 fsync 父目录中的新目录项。"""

    if directory.exists():
        if not directory.is_dir():
            raise NotADirectoryError(directory)
        return
    parent = directory.parent
    if parent == directory:
        raise FileNotFoundError(f"cannot create filesystem root {directory}")
    _ensure_directory_durable(parent)
    try:
        directory.mkdir()
    except FileExistsError:
        if not directory.is_dir():
            raise
    else:
        _fsync_directory(parent)


@contextmanager
def _exclusive_cas_lock(lock_path: Path):
    """串行化本机多进程 publish/cleanup，避免复用老 orphan 时被并发删除。"""

    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextmanager
def _shared_cas_lock(lock_path: Path):
    """阻止 cooperating cleanup/publish 在完整性校验与切片读取之间换 inode。"""

    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
