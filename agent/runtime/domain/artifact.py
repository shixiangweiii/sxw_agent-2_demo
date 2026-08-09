"""Artifact 领域契约。

Artifact 的身份就是内容的 SHA-256；文件系统只保存不可变 blob，来源、敏感级别和
retention 等可变信息由 Runtime Store 的 ``artifact_metadata`` / ``artifact_links``
负责。本模块刻意不依赖具体存储实现。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ARTIFACT_SCHEMA_VERSION = "1"
SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")

DEFAULT_UPLOAD_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_INTERNAL_MAX_BYTES = 100 * 1024 * 1024
DEFAULT_PREVIEW_BYTES = 8 * 1024
DEFAULT_MODEL_READ_BYTES = 32 * 1024
MAX_MODEL_READ_BYTES = 64 * 1024
MAX_HTTP_RANGE_BYTES = 1024 * 1024


class ArtifactPurpose(StrEnum):
    """决定写入大小限制；不表示可见性或 retention。"""

    UPLOAD = "UPLOAD"
    INTERNAL = "INTERNAL"


class ArtifactError(RuntimeError):
    """所有 Artifact Store 稳定错误的基类。"""

    code = "ARTIFACT_ERROR"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class InvalidArtifactIdError(ArtifactError):
    code = "INVALID_ARTIFACT_ID"


class ArtifactNotFoundError(ArtifactError):
    code = "ARTIFACT_NOT_FOUND"


class ArtifactTooLargeError(ArtifactError):
    code = "ARTIFACT_TOO_LARGE"

    def __init__(self, *, limit_bytes: int, observed_bytes: int) -> None:
        self.limit_bytes = limit_bytes
        self.observed_bytes = observed_bytes
        super().__init__(
            f"artifact exceeds {limit_bytes} byte limit "
            f"(observed at least {observed_bytes} bytes)"
        )


class ArtifactIntegrityError(ArtifactError):
    code = "ARTIFACT_INTEGRITY_ERROR"

    def __init__(self, artifact_id: str, actual_digest: str) -> None:
        self.artifact_id = artifact_id
        self.actual_digest = actual_digest
        super().__init__(
            f"artifact {artifact_id} failed SHA-256 verification: {actual_digest}"
        )


class ArtifactRangeError(ArtifactError):
    code = "ARTIFACT_RANGE_INVALID"


class ArtifactReadLimitError(ArtifactError):
    code = "ARTIFACT_READ_LIMIT_EXCEEDED"

    def __init__(self, *, requested_bytes: int, limit_bytes: int) -> None:
        self.requested_bytes = requested_bytes
        self.limit_bytes = limit_bytes
        super().__init__(
            f"requested {requested_bytes} bytes exceeds {limit_bytes} byte read limit"
        )


class ArtifactLimits(BaseModel):
    """部署级大小限制；测试可注入更小的值。"""

    model_config = ConfigDict(frozen=True)

    upload_max_bytes: int = Field(default=DEFAULT_UPLOAD_MAX_BYTES, gt=0)
    internal_max_bytes: int = Field(default=DEFAULT_INTERNAL_MAX_BYTES, gt=0)
    preview_bytes: int = Field(default=DEFAULT_PREVIEW_BYTES, gt=0)
    model_read_default_bytes: int = Field(default=DEFAULT_MODEL_READ_BYTES, gt=0)
    model_read_max_bytes: int = Field(default=MAX_MODEL_READ_BYTES, gt=0)
    http_range_max_bytes: int = Field(default=MAX_HTTP_RANGE_BYTES, gt=0)

    @model_validator(mode="after")
    def validate_read_limits(self) -> "ArtifactLimits":
        if self.preview_bytes > self.model_read_max_bytes:
            raise ValueError("preview_bytes cannot exceed model_read_max_bytes")
        if self.model_read_default_bytes > self.model_read_max_bytes:
            raise ValueError(
                "model_read_default_bytes cannot exceed model_read_max_bytes"
            )
        return self

    def write_limit_for(self, purpose: ArtifactPurpose) -> int:
        if purpose is ArtifactPurpose.UPLOAD:
            return self.upload_max_bytes
        return self.internal_max_bytes


class ArtifactRef(BaseModel):
    """写入成功后交给 Runtime metadata 事务持久化的不可变引用。"""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["1"] = ARTIFACT_SCHEMA_VERSION
    artifact_id: str
    digest_sha256: str
    size_bytes: int = Field(ge=0)
    media_type: str = "application/octet-stream"
    filename: str | None = None
    created_at: datetime

    @field_validator("artifact_id", "digest_sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not SHA256_HEX_PATTERN.fullmatch(value):
            raise ValueError("must be a lowercase SHA-256 hex digest")
        return value

    @field_validator("created_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def artifact_id_is_digest(self) -> "ArtifactRef":
        if self.artifact_id != self.digest_sha256:
            raise ValueError("artifact_id must equal digest_sha256")
        return self


class ArtifactSlice(BaseModel):
    """一次经过完整性校验的有界读取结果，区间为 ``[start, end)``。"""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["1"] = ARTIFACT_SCHEMA_VERSION
    artifact_id: str
    digest_sha256: str
    total_size: int = Field(ge=0)
    start: int = Field(ge=0)
    end_exclusive: int = Field(ge=0)
    data: bytes

    @field_validator("artifact_id", "digest_sha256")
    @classmethod
    def validate_digest(cls, value: str) -> str:
        if not SHA256_HEX_PATTERN.fullmatch(value):
            raise ValueError("must be a lowercase SHA-256 hex digest")
        return value

    @model_validator(mode="after")
    def validate_slice(self) -> "ArtifactSlice":
        if self.artifact_id != self.digest_sha256:
            raise ValueError("artifact_id must equal digest_sha256")
        if self.end_exclusive < self.start:
            raise ValueError("end_exclusive cannot be smaller than start")
        if self.end_exclusive > self.total_size:
            raise ValueError("end_exclusive cannot exceed total_size")
        if len(self.data) != self.end_exclusive - self.start:
            raise ValueError("data length does not match the declared range")
        return self


class ArtifactCleanupResult(BaseModel):
    """一次 orphan 扫描的确定性统计。"""

    model_config = ConfigDict(frozen=True)

    scanned: int = Field(ge=0)
    referenced: int = Field(ge=0)
    too_new: int = Field(ge=0)
    deleted: int = Field(ge=0)
    reclaimed_bytes: int = Field(ge=0)
    deleted_artifact_ids: tuple[str, ...] = ()

