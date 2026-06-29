"""ArtifactService 封装：构造 + 保存图片制品（ADK InMemoryArtifactService）。

生产可换 GCS/OSS 实现；业务只依赖此封装。
"""
from __future__ import annotations

from google.adk.artifacts import InMemoryArtifactService
from google.genai import types


def build_artifact_service() -> InMemoryArtifactService:
    return InMemoryArtifactService()


async def save_image_artifact(
    service: InMemoryArtifactService,
    *,
    app_name: str,
    user_id: str,
    session_id: str,
    filename: str,
    data: bytes,
    mime_type: str,
) -> int:
    """保存上传图片为制品，返回版本号。"""
    part = types.Part.from_bytes(data=data, mime_type=mime_type)
    return await service.save_artifact(
        app_name=app_name,
        user_id=user_id,
        session_id=session_id,
        filename=filename,
        artifact=part,
    )
