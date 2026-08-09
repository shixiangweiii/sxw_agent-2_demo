"""Explicit, version-to-version checkpoint conversion contract.

Upgraders are synchronous pure functions by design: they receive an immutable
description of one committed checkpoint and return replacement values.  They
must not perform filesystem, database, model, tool or network I/O.  Publishing
the result is a separate fenced Store transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from agent.runtime.domain.models import CheckpointRecord, EngineName, WorkingState


@dataclass(frozen=True)
class CheckpointUpgradeKey:
    engine: EngineName
    from_release_fingerprint: str
    from_schema_version: str
    to_release_fingerprint: str
    to_schema_version: str


@dataclass(frozen=True)
class CheckpointUpgradeRequest:
    key: CheckpointUpgradeKey
    checkpoint: CheckpointRecord


@dataclass(frozen=True)
class CheckpointUpgradeResult:
    working_state: WorkingState
    engine_state: dict[str, Any] | None = None
    engine_state_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.working_state, WorkingState):
            raise TypeError("upgraded working_state must be a WorkingState")
        if self.engine_state is not None and not isinstance(self.engine_state, dict):
            raise TypeError("upgraded engine_state must be a dict or None")
        if self.engine_state_ref is not None and not isinstance(self.engine_state_ref, str):
            raise TypeError("upgraded engine_state_ref must be a string or None")
        if self.engine_state is not None and self.engine_state_ref is not None:
            raise ValueError("inline engine state and engine_state_ref are exclusive")


class CheckpointUpgrader(Protocol):
    def upgrade(self, request: CheckpointUpgradeRequest) -> CheckpointUpgradeResult:
        """Purely convert one exact committed checkpoint into the target codec."""
