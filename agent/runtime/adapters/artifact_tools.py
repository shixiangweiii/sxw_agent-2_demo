from __future__ import annotations

from typing import Any, Awaitable, Callable

from agent.runtime.domain.models import ToolResultEnvelope, ToolResultStatus
from agent.runtime.ports.artifact import ArtifactStore

MetadataLoader = Callable[[str], Awaitable[dict[str, Any]]]


def build_read_artifact_tool(
    artifact_store: ArtifactStore,
    metadata_loader: MetadataLoader,
):
    async def read_artifact(
        artifact_id: str,
        offset: int = 0,
        max_bytes: int = 32 * 1024,
    ) -> ToolResultEnvelope:
        """Read a verified, bounded UTF-8 slice from an Artifact.

        Args:
            artifact_id: SHA-256 Artifact identity returned by upload or another tool.
            offset: Zero-based byte offset.
            max_bytes: Bytes to read; defaults to 32KiB and cannot exceed 64KiB.
        """
        metadata = await metadata_loader(artifact_id)
        item = await artifact_store.read_bounded(
            artifact_id,
            offset=offset,
            max_bytes=max_bytes,
        )
        return ToolResultEnvelope(
            status=ToolResultStatus.SUCCESS,
            result_ref=artifact_id,
            preview={
                "artifact_id": artifact_id,
                "media_type": metadata["media_type"],
                "offset": item.start,
                "end_exclusive": item.end_exclusive,
                "total_size": item.total_size,
                "eof": item.end_exclusive >= item.total_size,
                "content": item.data.decode("utf-8", errors="replace"),
            },
        )

    return read_artifact
