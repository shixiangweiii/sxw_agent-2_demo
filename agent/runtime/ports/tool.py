from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError


RECONCILIATION_MARKER_KIND = "TOOL_RECONCILIATION_ONLY"


class ToolReconciliationMarker(BaseModel):
    """Internal, exact Worker marker; not a public API/JSON Schema."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["TOOL_RECONCILIATION_ONLY"] = RECONCILIATION_MARKER_KIND
    tool_execution_id: str = Field(min_length=1)
    signal_id: str = Field(min_length=1)
    expected_effect_revision: int = Field(ge=1)

    @classmethod
    def parse_exact(cls, value: Any) -> "ToolReconciliationMarker | None":
        try:
            return cls.model_validate(value)
        except ValidationError:
            return None


class ToolReconciliationExecutor(Protocol):
    """Query-only execution port for an operator-authorized reconciliation.

    Implementations may call a registered reconcile hook, but must never call
    the original Tool executor or redispatch the external side effect.
    """

    async def reconcile_only(
        self,
        *,
        tool_execution_id: str,
        parent_activity_id: str,
        fencing_token: int,
        expected_effect_revision: int,
        deadline_at_ms: int,
    ) -> dict[str, Any]: ...
