from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any

from google.adk.tools import BaseTool, FunctionTool

from agent.engine.base import RunContext
from agent.engine.loop_tools import TASK_PLAN_KEY
from agent.engine.native_loop.tools import NativeToolContext, ToolRegistry, ToolSpec
from agent.runtime.domain.errors import RuntimeFault
from agent.runtime.domain.models import ToolEffectClass, ToolManifest, ToolResultEnvelope, ToolResultStatus
from agent.runtime.domain.models import WorkingState, sha256_json
from agent.runtime.ports.store import ToolExecutionPreparation
from agent.skills.request_context import (
    get_request_context,
    reset_request_context,
    set_request_context,
)

_READ_ONLY = frozenset({
    "calculator", "get_weather", "simulate_unstable_operation", "knowledge_search",
    "tool_search", "translate", "text_stats", "researcher", "read_artifact",
    # Current, reviewed demo Skill/A2A/Claude-SKILL entries. New names remain UNKNOWN.
    "deep_translate", "query_weather", "math_expert", "claude_skill_data_analysis",
})


def manifest_for(name: str, rc: RunContext, *, concurrency_safe: bool = False,
                 exclusive_resources: tuple[str, ...] = ()) -> ToolManifest:
    if name == "update_task_plan":
        effect = ToolEffectClass.IDEMPOTENT_EFFECT
        supports_idempotency = True
    elif name in _READ_ONLY:
        effect = ToolEffectClass.READ_ONLY
        supports_idempotency = False
    else:
        effect = ToolEffectClass.UNKNOWN_EFFECT
        supports_idempotency = False
    return ToolManifest(
        name=name,
        release_digest=f"{rc.release_fingerprint or rc.engine}:{name}:v1",
        effect_class=effect,
        timeout_seconds=max(
            0.001,
            min(120.0, (rc.deadline_at_ms - int(time.time() * 1000)) / 1000),
        ) if rc.deadline_at_ms else 120.0,
        max_attempts=2 if effect in {ToolEffectClass.READ_ONLY, ToolEffectClass.IDEMPOTENT_EFFECT} else 1,
        supports_idempotency=supports_idempotency,
        result_policy="ARTIFACT_BOUNDED_READ" if name == "read_artifact" else "INLINE_OR_ARTIFACT",
        # A framework/frontmatter concurrency declaration is insufficient on
        # its own: only reviewed READ_ONLY tools may run in an early/concurrent
        # batch.  New Skill/A2A tools default to UNKNOWN_EFFECT and therefore
        # wait for the complete durable ToolCall batch and execute serially.
        concurrency_safe=(
            concurrency_safe and effect is ToolEffectClass.READ_ONLY
        ),
        exclusive_resources=exclusive_resources,
    )


class AdkToolBatch:
    """Bridge ADK's aggregated model callback to durable ToolCall slots.

    ADK 2.6.2 awaits ``after_model_callback`` for the final non-partial
    response before it creates parallel tool tasks.  We use the ordered call
    batch in that response to derive stable ``turn + call`` slots.  Provider
    function-call ids are retained only to correlate the later callback with
    its already prepared slot.
    """

    def __init__(self, rc: RunContext) -> None:
        if rc.tool_broker is None:
            raise ValueError("ADK ToolCall batching requires a ToolBroker")
        self._rc = rc
        self._manifests: dict[str, ToolManifest] = {}
        self._active_slots: dict[str, tuple[str, str, str]] = {}
        self._prepared_turns: dict[int, tuple[tuple[str, str, str], ...]] = {}
        self._lock = asyncio.Lock()

    def register(self, manifest: ToolManifest) -> None:
        prior = self._manifests.get(manifest.name)
        if prior is not None and prior != manifest:
            raise ValueError(f"duplicate ADK tool manifest: {manifest.name}")
        self._manifests[manifest.name] = manifest

    async def prepare_model_response(self, llm_response: Any, *, turn_ordinal: int) -> None:
        if getattr(llm_response, "partial", False):
            return
        calls: list[Any] = []
        for part in getattr(getattr(llm_response, "content", None), "parts", None) or []:
            function_call = getattr(part, "function_call", None)
            if function_call is not None:
                calls.append(function_call)
        if not calls:
            return

        preparations: list[ToolExecutionPreparation] = []
        signature: list[tuple[str, str, str]] = []
        active_slots: dict[str, tuple[str, str, str]] = {}
        for call_ordinal, function_call in enumerate(calls):
            framework_id = str(getattr(function_call, "id", None) or "")
            if not framework_id:
                raise RuntimeFault(
                    "ADK_TOOL_BATCH_INVALID",
                    "ADK function_call.id is required for attempt-local correlation",
                    409,
                )
            if framework_id in active_slots:
                raise RuntimeFault(
                    "ADK_TOOL_BATCH_INVALID",
                    "ADK returned duplicate function_call ids in one batch",
                    409,
                    {"framework_call_id": framework_id},
                )
            tool_name = str(getattr(function_call, "name", "") or "")
            manifest = self._manifests.get(tool_name)
            if manifest is None:
                raise RuntimeFault(
                    "TOOL_NOT_REGISTERED",
                    f"tool is not registered: {tool_name}",
                )
            arguments = getattr(function_call, "args", None) or {}
            if not isinstance(arguments, dict):
                raise RuntimeFault(
                    "ADK_TOOL_BATCH_INVALID",
                    "ADK function_call args must be a normalized object",
                    409,
                )
            request = dict(arguments)
            request_digest = sha256_json(request)
            logical_key = f"adk:turn:{turn_ordinal}:call:{call_ordinal}"
            preparations.append(ToolExecutionPreparation(
                logical_key=logical_key,
                tool_name=tool_name,
                release_digest=manifest.release_digest,
                effect_class=manifest.effect_class.value,
                request_digest=request_digest,
                request=request,
                supports_reconcile=manifest.supports_reconcile,
                framework_call_id=framework_id,
            ))
            slot = (logical_key, tool_name, request_digest)
            active_slots[framework_id] = slot
            signature.append(slot)

        async with self._lock:
            frozen_signature = tuple(signature)
            prior = self._prepared_turns.get(turn_ordinal)
            if prior is not None:
                if prior != frozen_signature:
                    raise RuntimeFault(
                        "TOOL_REPLAY_MISMATCH",
                        "an ADK turn changed its complete ToolCall batch",
                        409,
                        {"turn_ordinal": turn_ordinal},
                    )
                self._active_slots = active_slots
                return

            # All text streamed before the aggregate function-call response
            # becomes committed before any TOOL_CALL_COMMITTED event appears.
            if self._rc.runtime_io is not None:
                await self._rc.runtime_io.force_flush()
            await self._rc.tool_broker.store.prepare_tool_execution_batch(
                run_id=self._rc.run_id,
                parent_activity_id=self._rc.activity_id,
                fencing_token=self._rc.fencing_token,
                preparations=preparations,
                now_ms=self._rc.tool_broker.clock.now_ms(),
            )
            # Publish correlation only after the whole SQLite transaction
            # commits.  Tool callbacks can never observe a half-prepared batch.
            self._prepared_turns[turn_ordinal] = frozen_signature
            self._active_slots = active_slots

    async def resolve(
        self,
        *,
        framework_call_id: str | None,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        correlation_id = str(framework_call_id or "")
        request_digest = sha256_json(arguments)
        async with self._lock:
            slot = self._active_slots.get(correlation_id)
        if slot is None:
            raise RuntimeFault(
                "ADK_TOOL_BATCH_NOT_PREPARED",
                "ADK attempted tool execution before its complete batch was committed",
                409,
                {"framework_call_id": correlation_id or None},
            )
        logical_key, expected_name, expected_digest = slot
        if expected_name != tool_name or expected_digest != request_digest:
            raise RuntimeFault(
                "TOOL_REPLAY_MISMATCH",
                "a prepared ADK tool slot changed name or normalized request",
                409,
                {"logical_key": logical_key},
            )
        return logical_key


class BrokeredAdkTool(BaseTool):
    def __init__(
        self,
        tool: BaseTool,
        rc: RunContext,
        batch: AdkToolBatch,
        manifest: ToolManifest,
    ) -> None:
        super().__init__(
            name=tool.name,
            description=tool.description,
            is_long_running=getattr(tool, "is_long_running", False),
        )
        self._tool = tool
        self._rc = rc
        self._batch = batch
        self._manifest = manifest

    def _get_declaration(self):
        return self._tool._get_declaration()  # noqa: SLF001 - delegate an ADK tool contract

    async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:
        framework_id = getattr(tool_context, "function_call_id", None)
        logical_key = await self._batch.resolve(
            framework_call_id=framework_id,
            tool_name=self.name,
            arguments=args,
        )

        async def invoke(arguments: dict[str, Any], context: Any) -> Any:
            return await _with_tool_request_context(
                context,
                self._tool.run_async(args=arguments, tool_context=tool_context),
            )

        result = await self._rc.tool_broker.execute(
            run_id=self._rc.run_id,
            parent_activity_id=self._rc.activity_id,
            fencing_token=getattr(self._rc, "fencing_token", 0),
            logical_key=logical_key,
            tool_name=self.name,
            arguments=args,
            deadline_at_ms=self._rc.deadline_at_ms,
            manifest_override=self._manifest,
            executor_override=invoke,
        )
        if self.name == "update_task_plan":
            await _persist_adk_plan(self._rc, result)
        return _model_result(result)


def broker_adk_tools(
    tools: list[Any],
    rc: RunContext,
    *,
    batch: AdkToolBatch | None = None,
) -> list[Any]:
    if rc.tool_broker is None:
        return tools
    batch = batch or AdkToolBatch(rc)
    out: list[Any] = []
    for tool in tools:
        base = tool if isinstance(tool, BaseTool) else FunctionTool(tool)
        manifest = manifest_for(
            base.name,
            rc,
            concurrency_safe=bool(getattr(base, "concurrency_safe", False)),
            exclusive_resources=tuple(getattr(base, "exclusive_resources", ()) or ()),
        )
        batch.register(manifest)
        out.append(BrokeredAdkTool(base, rc, batch, manifest))
    return out


def broker_native_registry(registry: ToolRegistry, rc: RunContext) -> ToolRegistry:
    if rc.tool_broker is None:
        return registry
    wrapped: list[ToolSpec] = []
    for original in registry.all():
        manifest = manifest_for(
            original.name, rc,
            concurrency_safe=original.concurrency_safe,
            exclusive_resources=original.exclusive_resources,
        )

        async def run(
            args: dict[str, Any], context: NativeToolContext, *, spec: ToolSpec = original,
            tool_manifest: ToolManifest = manifest,
        ) -> Any:
            async def invoke(arguments: dict[str, Any], broker_context: Any) -> Any:
                return await _with_tool_request_context(
                    broker_context, spec.run(arguments, context),
                )

            result = await rc.tool_broker.execute(
                run_id=rc.run_id,
                parent_activity_id=rc.activity_id,
                fencing_token=getattr(rc, "fencing_token", 0),
                logical_key=context.logical_key or f"native:call:{context.function_call_id}",
                tool_name=spec.name,
                arguments=args,
                deadline_at_ms=rc.deadline_at_ms,
                manifest_override=tool_manifest,
                executor_override=invoke,
            )
            if spec.name == "update_task_plan":
                _restore_native_plan_mirror(context, result)
            return _model_result(result)

        wrapped.append(ToolSpec(
            name=original.name,
            description=original.description,
            parameters=original.parameters,
            run=run,
            concurrency_safe=manifest.concurrency_safe,
            exclusive_resources=original.exclusive_resources,
        ))
    return ToolRegistry(wrapped)


def _model_result(result: ToolResultEnvelope) -> Any:
    if result.status in {ToolResultStatus.SUCCESS, ToolResultStatus.NO_OUTPUT}:
        # Provider object identity is part of a successful durable Tool result,
        # not just ledger metadata.  Keep user preview under ``content`` when
        # adding envelope metadata so a user-owned key can never be overwritten.
        if result.result_ref or result.external_object_id:
            projected = {"content": result.preview}
            if result.result_ref:
                projected["artifact_ref"] = result.result_ref
            if result.external_object_id:
                projected["external_object_id"] = result.external_object_id
            return projected
        return result.preview if result.preview is not None else {"status": "NO_OUTPUT"}
    if result.status is ToolResultStatus.INTERRUPT:
        return {"interrupt": True, "pending_input": result.pending_input}
    return {
        "isError": True,
        "errorCode": result.error_code or result.status,
        "content": result.error_message or "tool execution failed",
        "unknownEffect": result.status is ToolResultStatus.UNKNOWN,
    }


def _restore_native_plan_mirror(
    context: NativeToolContext,
    result: ToolResultEnvelope,
) -> None:
    """Rebuild the native request-local mirror from a committed Tool result.

    The first execution mutates ``context.state`` inside ``update_task_plan``.
    After a crash between ToolExecution commit and kernel checkpoint, Broker
    replay correctly skips that function.  Reapplying its already-committed
    result here keeps the mirror deterministic; the following Runtime
    checkpoint remains the sole durable WorkingState authority.
    """
    if result.status is not ToolResultStatus.SUCCESS or not isinstance(result.preview, dict):
        return
    steps = result.preview.get("steps")
    current = result.preview.get("current")
    if not isinstance(steps, list) or not isinstance(current, int):
        return
    context.state[TASK_PLAN_KEY] = {
        "steps": [str(step) for step in steps],
        "current": current,
    }


async def _with_tool_request_context(broker_context: Any, awaitable: Any) -> Any:
    """Expose the stable ToolExecution identity to Skill/A2A request adapters."""
    current = get_request_context()
    if current is None:
        return await awaitable
    token = set_request_context(replace(
        current,
        activity_id=broker_context.tool_activity_id,
        deadline_at_ms=broker_context.deadline_at_ms,
        idempotency_key=broker_context.idempotency_key,
    ))
    try:
        return await awaitable
    finally:
        reset_request_context(token)


async def _persist_adk_plan(rc: RunContext, result: ToolResultEnvelope) -> None:
    """Move update_task_plan authority from ADK state into Runtime WorkingState."""
    if (
        rc.runtime_io is None
        or result.status is not ToolResultStatus.SUCCESS
        or not isinstance(result.preview, dict)
    ):
        return
    steps = result.preview.get("steps")
    current = result.preview.get("current")
    if not isinstance(steps, list) or not isinstance(current, int):
        return
    model_plan = [
        {
            "step": index + 1,
            "title": str(title),
            "status": (
                "done" if index + 1 < current
                else "running" if index + 1 == current
                else "planned"
            ),
        }
        for index, title in enumerate(steps)
    ]
    async with rc.runtime_checkpoint_lock:
        state = rc.runtime_working_state
        if not isinstance(state, WorkingState):
            state = WorkingState(
                goal="",
                release_fingerprint=rc.release_fingerprint,
            )
        if state.model_plan == model_plan:
            return
        state = state.model_copy(update={"model_plan": model_plan})
        saved = await rc.runtime_io.checkpoint(
            state,
            expected_revision=rc.runtime_checkpoint_revision,
            engine_state={
                "contract": "adk-coarse-v1",
                "phase": "MODEL_PLAN_UPDATED",
            },
        )
        rc.runtime_checkpoint_revision = saved.revision
        rc.runtime_working_state = saved.working_state
