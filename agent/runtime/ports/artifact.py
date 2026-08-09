"""Artifact Store 端口。"""

from __future__ import annotations

from collections.abc import AsyncIterable, Collection
from datetime import datetime
from typing import Protocol, runtime_checkable

from agent.runtime.domain.artifact import (
    ArtifactCleanupResult,
    ArtifactPurpose,
    ArtifactRef,
    ArtifactSlice,
)


@runtime_checkable
class ArtifactStore(Protocol):
    """Runtime 使用的内容寻址 blob 端口。

    metadata/link 持久化不属于此端口；调用方应先 ``put_stream``，再在独立的
    Runtime DB 事务中保存返回的 :class:`ArtifactRef`。事务失败留下的 blob 是
    可由 ``cleanup_orphans`` 回收的安全 orphan。
    """

    async def put_stream(
        self,
        chunks: AsyncIterable[bytes],
        *,
        purpose: ArtifactPurpose,
        media_type: str = "application/octet-stream",
        filename: str | None = None,
    ) -> ArtifactRef:
        """流式落盘并返回内容引用；只有 durability boundary 完成后才返回。"""
        ...

    async def put_bytes(
        self,
        data: bytes,
        *,
        purpose: ArtifactPurpose,
        media_type: str = "application/octet-stream",
        filename: str | None = None,
    ) -> ArtifactRef:
        """小结果的便捷写入入口，限制与 ``put_stream`` 完全相同。"""
        ...

    async def read_preview(
        self,
        artifact_id: str,
        *,
        max_bytes: int | None = None,
    ) -> ArtifactSlice:
        """返回供模型或事件展示的短 preview。"""
        ...

    async def read_bounded(
        self,
        artifact_id: str,
        *,
        offset: int = 0,
        max_bytes: int | None = None,
    ) -> ArtifactSlice:
        """供 ``read_artifact`` 使用，受模型读取上限约束。"""
        ...

    async def read_range(
        self,
        artifact_id: str,
        *,
        start: int = 0,
        end_exclusive: int | None = None,
    ) -> ArtifactSlice:
        """供 HTTP Range 使用；区间为 ``[start, end_exclusive)``。"""
        ...

    async def cleanup_orphans(
        self,
        *,
        referenced_artifact_ids: Collection[str],
        older_than: datetime,
    ) -> ArtifactCleanupResult:
        """删除 cutoff 以前且不在 Runtime metadata/link 引用集合中的 blob。"""
        ...
