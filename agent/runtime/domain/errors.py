"""Stable Runtime error codes used by the store and HTTP adapter."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class RuntimeFault(Exception):
    code: str
    message: str
    http_status: int = 400
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message


class AttemptOwnershipLost(RuntimeError):
    """The current Worker no longer owns this attempt.

    This signal must cross Engine/Coordinator unchanged so the Worker can stop
    local work and let durable lease recovery decide the next owner. It is not
    a ToolResult and never terminalizes a Run.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_OWNERSHIP_FAULT_CODES = frozenset({
    "ACTIVITY_FENCING_STALE",
    "STALE_FENCING_TOKEN",
    "ACTIVITY_LEASE_EXPIRED",
    "ACTIVITY_LEASE_REQUIRED",
    "CHECKPOINT_REVISION_CONFLICT",
    "CLAIM_RELEASE_MISMATCH",
})


def raise_if_ownership_lost(exc: RuntimeFault) -> None:
    if exc.code in _OWNERSHIP_FAULT_CODES:
        raise AttemptOwnershipLost(exc.code, exc.message) from exc


def not_found(kind: str, object_id: str) -> RuntimeFault:
    return RuntimeFault(
        f"{kind.upper()}_NOT_FOUND", f"{kind} {object_id} does not exist", 404,
    )


def conflict(code: str, message: str, **details: Any) -> RuntimeFault:
    return RuntimeFault(code, message, 409, details or None)


def unavailable(code: str, message: str, **details: Any) -> RuntimeFault:
    return RuntimeFault(code, message, 503, details or None)
