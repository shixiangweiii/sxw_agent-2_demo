"""Test-only deterministic adapter for Runtime recovery fault injection.

This fixture intentionally does not participate in Worker assembly.  It keeps
the WAITING_INPUT, idempotent external effect, and Artifact recovery scenario
deterministic without reserving a magic production prompt.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite

from agent.runtime.application.tool_broker import ToolBroker, ToolCallContext
from agent.runtime.domain.models import (
    EngineOutcome,
    EngineOutcomeKind,
    ToolEffectClass,
    ToolManifest,
    ToolResultStatus,
    WorkingState,
)
from agent.runtime.ports.engine import EngineRunRequest, RuntimeIO


class DemoEffectsStore:
    """A separate test database that mimics an external idempotent system."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA synchronous=FULL")
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS demo_tasks (
                   task_id TEXT PRIMARY KEY,
                   business_key TEXT NOT NULL UNIQUE,
                   idempotency_key TEXT NOT NULL UNIQUE,
                   payload_json TEXT NOT NULL,
                   created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                ) STRICT"""
            )
            await conn.commit()

    async def create_task(
        self, *, business_key: str, idempotency_key: str, payload: dict[str, Any],
    ) -> dict[str, Any]:
        await self.initialize()
        task_id = f"demo_task_{business_key.removeprefix('run_')[:24]}"
        async with aiosqlite.connect(self.path, isolation_level=None) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.execute("BEGIN IMMEDIATE")
            try:
                await conn.execute(
                    """INSERT OR IGNORE INTO demo_tasks
                       (task_id,business_key,idempotency_key,payload_json)
                       VALUES (?,?,?,?)""",
                    (
                        task_id,
                        business_key,
                        idempotency_key,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    ),
                )
                row = await (
                    await conn.execute(
                        "SELECT * FROM demo_tasks WHERE idempotency_key=?",
                        (idempotency_key,),
                    )
                ).fetchone()
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return {
            "task_id": row["task_id"],
            "business_key": row["business_key"],
            "idempotency_key": row["idempotency_key"],
            "created": row["task_id"] == task_id,
            "evidence": "demo-side-effect-confirmed\n" * 600,
        }


class NativeReliabilityDemoAdapter:
    """A deterministic adapter used only by the reliability test process."""

    name = "native_loop"

    def __init__(
        self,
        *,
        release_fingerprint: str,
        tool_broker: ToolBroker,
        effects: DemoEffectsStore,
    ) -> None:
        self.release_fingerprint = release_fingerprint
        self.broker = tool_broker
        self.effects = effects
        self._register_tools()

    def _register_tools(self) -> None:
        async def slow_lookup(
            _arguments: dict[str, Any], context: ToolCallContext,
        ) -> dict[str, Any]:
            if context.attempt < 3:
                raise TimeoutError(f"simulated retryable lookup failure #{context.attempt}")
            return {
                "status": "ready",
                "attempt": context.attempt,
                "fact": "lookup-confirmed",
            }

        async def create_demo_task(
            arguments: dict[str, Any], context: ToolCallContext,
        ) -> dict[str, Any]:
            return await self.effects.create_task(
                business_key=context.run_id,
                idempotency_key=context.idempotency_key,
                payload=arguments,
            )

        self.broker.register(
            ToolManifest(
                name="slow_lookup",
                release_digest="demo-v1",
                effect_class=ToolEffectClass.READ_ONLY,
                timeout_seconds=5,
                max_attempts=3,
                concurrency_safe=True,
            ),
            slow_lookup,
        )
        self.broker.register(
            ToolManifest(
                name="create_demo_task",
                release_digest="demo-v1",
                effect_class=ToolEffectClass.IDEMPOTENT_EFFECT,
                timeout_seconds=5,
                max_attempts=2,
                supports_idempotency=True,
            ),
            create_demo_task,
        )

    async def execute(self, request: EngineRunRequest, io: RuntimeIO) -> EngineOutcome:
        current_revision = request.checkpoint.revision if request.checkpoint else 0
        signal = request.resume_payload
        if signal is None:
            lookup = None
            for _ in range(3):
                lookup = await self.broker.execute(
                    run_id=request.envelope.run_id,
                    parent_activity_id=request.activity_id,
                    fencing_token=request.fencing_token,
                    logical_key="demo:slow_lookup",
                    tool_name="slow_lookup",
                    arguments={"query": "reliability-demo"},
                    deadline_at_ms=request.envelope.deadline_at,
                )
                if lookup.status is ToolResultStatus.SUCCESS:
                    break
            if lookup is None or lookup.status is not ToolResultStatus.SUCCESS:
                return EngineOutcome(
                    kind=EngineOutcomeKind.RETRYABLE_FAILURE,
                    error_code="SLOW_LOOKUP_EXHAUSTED",
                    message="slow lookup did not recover",
                )
            pending = {
                "type": "APPROVAL",
                "schema": {
                    "type": "object",
                    "required": ["approved"],
                    "properties": {"approved": {"type": "boolean"}},
                },
                "prompt": "Approve creation of the idempotent demo task?",
            }
            await io.checkpoint(
                WorkingState(
                    goal="complete the native reliability demonstration",
                    confirmed_facts=[{"fact": "lookup-confirmed"}],
                    pending_input=pending,
                ),
                expected_revision=current_revision,
                engine_state={
                    "phase": "WAITING_APPROVAL",
                    "lookup": lookup.model_dump(mode="json"),
                },
            )
            await io.emit(
                "tool_call",
                {"id": "demo-request-input", "name": "request_input", "args": pending},
            )
            await io.emit(
                "tool_result",
                {
                    "id": "demo-request-input",
                    "name": "request_input",
                    "response": {"status": "INTERRUPT", "pending_input": pending},
                },
            )
            return EngineOutcome(
                kind=EngineOutcomeKind.WAITING_INPUT,
                pending_input=pending,
            )

        approved = bool((signal.get("payload") or {}).get("approved"))
        if not approved:
            return EngineOutcome(
                kind=EngineOutcomeKind.TERMINAL_FAILURE,
                error_code="APPROVAL_REJECTED",
                message="demo task creation was not approved",
            )
        created = await self.broker.execute(
            run_id=request.envelope.run_id,
            parent_activity_id=request.activity_id,
            fencing_token=request.fencing_token,
            logical_key="demo:create_task",
            tool_name="create_demo_task",
            arguments={"title": "reliability-demo", "approved": True},
            deadline_at_ms=request.envelope.deadline_at,
        )
        if created.status is not ToolResultStatus.SUCCESS:
            return EngineOutcome(
                kind=EngineOutcomeKind.TERMINAL_FAILURE,
                error_code=created.error_code or "DEMO_EFFECT_FAILED",
                message=created.error_message or "demo side effect failed",
            )
        artifact_refs = [created.result_ref] if created.result_ref else []
        await io.checkpoint(
            WorkingState(
                goal="complete the native reliability demonstration",
                confirmed_facts=[
                    {"fact": "lookup-confirmed"},
                    {"fact": "demo-task-created"},
                ],
                artifact_refs=artifact_refs,
            ),
            expected_revision=current_revision,
            engine_state={
                "phase": "FINALIZING",
                "tool_result_ref": created.result_ref,
            },
        )
        await io.emit(
            "text",
            {
                "delta": "可靠性演示已完成：只读查询在两次可重试失败后成功，"
                "approval signal 仅消费一次，幂等副作用已提交，完整证据保存为 Artifact。"
            },
        )
        return EngineOutcome(kind=EngineOutcomeKind.COMPLETED)
