from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, File, Header, Request, UploadFile, status
from fastapi.responses import Response

from agent.runtime.domain.artifact import ArtifactError, ArtifactPurpose, ArtifactRef
from agent.runtime.domain.errors import RuntimeFault
from agent.runtime.ports.artifact import ArtifactStore

router = APIRouter(prefix="/api/v1/artifacts", tags=["artifacts"])
_RANGE = re.compile(r"^bytes=(\d+)-(\d*)$")


async def _upload_chunks(upload: UploadFile):
    while chunk := await upload.read(1024 * 1024):
        yield chunk


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_artifact(
    request: Request,
    file: Annotated[UploadFile, File(...)],
) -> dict[str, object]:
    store: ArtifactStore = request.app.state.artifact_store
    try:
        ref = await store.put_stream(
            _upload_chunks(file),
            purpose=ArtifactPurpose.UPLOAD,
            media_type=file.content_type or "application/octet-stream",
            filename=file.filename,
        )
    except ArtifactError as exc:
        raise RuntimeFault(exc.code, str(exc), 413 if "TOO_LARGE" in exc.code else 400) from exc
    runtime_store = request.app.state.runtime_store
    metadata = await runtime_store.register_artifact_metadata(
        artifact_id=ref.artifact_id,
        sha256=ref.digest_sha256,
        size_bytes=ref.size_bytes,
        media_type=ref.media_type,
        storage_path=f"sha256/{ref.artifact_id[:2]}/{ref.artifact_id}",
        created_at=int(ref.created_at.timestamp() * 1000),
    )
    # Return the same immutable metadata that future Worker attempts will read,
    # never a transient MIME/timestamp that lost an idempotent metadata race.
    durable_ref = ArtifactRef(
        artifact_id=str(metadata["artifact_id"]),
        digest_sha256=str(metadata["sha256"]),
        size_bytes=int(metadata["size_bytes"]),
        media_type=str(metadata["media_type"]),
        filename=ref.filename,
        created_at=datetime.fromtimestamp(int(metadata["created_at"]) / 1000, UTC),
    )
    return durable_ref.model_dump(mode="json")


@router.get("/{artifact_id}")
async def read_artifact(
    artifact_id: str,
    request: Request,
    range_header: Annotated[str | None, Header(alias="Range")] = None,
) -> Response:
    runtime_store = request.app.state.runtime_store
    metadata = await runtime_store.get_artifact_metadata(artifact_id)
    start = 0
    end_exclusive = None
    if range_header:
        if int(metadata["size_bytes"]) == 0:
            raise RuntimeFault(
                "ARTIFACT_RANGE_INVALID",
                "an empty artifact has no satisfiable byte range",
                416,
            )
        match = _RANGE.fullmatch(range_header.strip())
        if match is None:
            raise RuntimeFault("ARTIFACT_RANGE_INVALID", "only a single bytes=start-end range is supported", 416)
        start = int(match.group(1))
        if match.group(2):
            end_exclusive = int(match.group(2)) + 1
    store: ArtifactStore = request.app.state.artifact_store
    try:
        item = await store.read_range(
            artifact_id, start=start, end_exclusive=end_exclusive,
        )
    except ArtifactError as exc:
        code = (
            404 if exc.code == "ARTIFACT_NOT_FOUND"
            else 416 if exc.code in {
                "ARTIFACT_RANGE_INVALID", "ARTIFACT_READ_LIMIT_EXCEEDED",
            }
            else 409
        )
        raise RuntimeFault(exc.code, str(exc), code) from exc
    partial = range_header is not None or item.start > 0 or item.end_exclusive < item.total_size
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": f'"sha256:{artifact_id}"',
        "Content-Length": str(len(item.data)),
    }
    if partial:
        headers["Content-Range"] = f"bytes {item.start}-{item.end_exclusive - 1}/{item.total_size}"
    return Response(
        item.data,
        status_code=206 if partial else 200,
        media_type=metadata["media_type"],
        headers=headers,
    )
