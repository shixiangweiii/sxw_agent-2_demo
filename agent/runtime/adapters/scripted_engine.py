"""Deterministic EngineAdapter used by reliability tests and local demos."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

from agent.runtime.domain.models import EngineOutcome, EngineOutcomeKind
from agent.runtime.ports.engine import EngineRunRequest, RuntimeIO


class ScriptedEngineAdapter:
    name = "scripted"

    def __init__(
        self,
        script: Sequence[dict[str, Any]] | Callable[[EngineRunRequest], Sequence[dict[str, Any]]],
        *,
        release_fingerprint: str = "scripted-release-v1",
    ) -> None:
        self.script = script
        self.release_fingerprint = release_fingerprint
        self.calls = 0

    async def execute(self, request: EngineRunRequest, io: RuntimeIO) -> EngineOutcome:
        self.calls += 1
        steps = self.script(request) if callable(self.script) else self.script
        for step in steps:
            kind = step["type"]
            if kind == "sleep":
                await asyncio.sleep(float(step.get("seconds", 0)))
            elif kind == "text":
                await io.emit("text", {"delta": str(step.get("delta", ""))})
            elif kind == "event":
                await io.emit(str(step["event_type"]), dict(step.get("payload") or {}))
            elif kind == "retry":
                return EngineOutcome(
                    kind=EngineOutcomeKind.RETRYABLE_FAILURE,
                    error_code=str(step.get("code", "SCRIPTED_RETRY")),
                    message=str(step.get("message", "scripted retry")),
                    retry_after_ms=step.get("retry_after_ms"),
                )
            elif kind == "fail":
                return EngineOutcome(
                    kind=EngineOutcomeKind.TERMINAL_FAILURE,
                    error_code=str(step.get("code", "SCRIPTED_FAILURE")),
                    message=str(step.get("message", "scripted failure")),
                )
            elif kind == "wait_input":
                return EngineOutcome(
                    kind=EngineOutcomeKind.WAITING_INPUT,
                    pending_input=dict(step.get("pending_input") or {}),
                )
            elif kind == "cancel":
                return EngineOutcome(kind=EngineOutcomeKind.CANCELLED)
            elif kind == "raise":
                raise RuntimeError(str(step.get("message", "scripted crash")))
            else:
                raise ValueError(f"unknown scripted step: {kind}")
            if await io.is_cancelled():
                return EngineOutcome(kind=EngineOutcomeKind.CANCELLED)
        return EngineOutcome(kind=EngineOutcomeKind.COMPLETED)

