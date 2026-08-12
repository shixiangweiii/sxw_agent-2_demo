from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field, replace
from typing import Any

from google.adk.tools import BaseTool, FunctionTool

from agent.engine.base import RunContext
from agent.engine.loop_tools import TASK_PLAN_KEY
from agent.engine.native_loop.tools import NativeToolContext, ToolRegistry, ToolSpec
from agent.runtime.domain.errors import RuntimeFault
from agent.runtime.application.tool_outputs import (
    ToolResultAdapter,
    a2a_output,
    adapter_for_tool,
    claude_skill_output,
    plain_json_output,
    skill_center_output,
)
from agent.runtime.application.tool_catalog import (
    ToolBinding,
    ToolCatalog,
    tool_release_digest,
)
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
    return _manifest_for(
        name,
        release_fingerprint=rc.release_fingerprint or rc.engine,
        deadline_at_ms=rc.deadline_at_ms,
        concurrency_safe=concurrency_safe,
        exclusive_resources=exclusive_resources,
    )


def _manifest_for(
    name: str,
    *,
    release_fingerprint: str,
    deadline_at_ms: int,
    concurrency_safe: bool = False,
    exclusive_resources: tuple[str, ...] = (),
) -> ToolManifest:
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
        release_digest=f"{release_fingerprint}:{name}:v1",
        effect_class=effect,
        # Absolute Run budget is applied independently at dispatch.  A Tool
        # manifest is immutable release semantics, never "remaining time now".
        timeout_seconds=120.0,
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


@dataclass(frozen=True)
class NativeBrokerSession:
    run_id: str
    activity_id: str
    fencing_token: int
    deadline_at_ms: int
    tool_broker: Any
    catalog: ToolCatalog
    early_settlement_capacity: int | None = None
    prepared_by_logical_key: dict[str, Any] = field(
        default_factory=dict,
        compare=False,
        repr=False,
    )
    settlement_turn_by_logical_key: dict[str, Any] = field(
        default_factory=dict,
        compare=False,
        repr=False,
    )
    early_settlement_order_by_turn: dict[int, Any] = field(
        default_factory=dict,
        compare=False,
        repr=False,
    )
    early_prepare_order_by_turn: dict[int, Any] = field(
        default_factory=dict,
        compare=False,
        repr=False,
    )
    prepare_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        compare=False,
        repr=False,
    )


def build_runtime_tool_catalog(
    tools: list[Any] | ToolRegistry,
    *,
    max_bytes: int = 1024 * 1024,
) -> ToolCatalog:
    """Build the one strict post-discovery catalog used by both loop engines."""

    from agent.engine.native_loop.tools import build_registry  # noqa: PLC0415

    registry = tools if isinstance(tools, ToolRegistry) else build_registry(tools)
    bindings: list[ToolBinding] = []
    for spec in registry.all():
        adapter = _native_result_adapter(spec.result_protocol)

        async def execute(
            arguments: dict[str, Any],
            broker_context: Any,
            *,
            tool_spec: ToolSpec = spec,
        ) -> Any:
            native_context = NativeToolContext(
                function_call_id=broker_context.tool_execution_id,
                invocation_id=broker_context.run_id,
                logical_key=str(broker_context.tool_execution_id),
            )
            return await tool_spec.run(arguments, native_context)

        provisional = _manifest_for(
            spec.name,
            release_fingerprint="catalog",
            deadline_at_ms=0,
            concurrency_safe=spec.concurrency_safe,
            exclusive_resources=spec.exclusive_resources,
        )
        policy = provisional.model_dump(mode="json", exclude={"release_digest"})
        policy["implementation"] = spec.implementation
        digest = tool_release_digest(
            name=spec.name,
            description=spec.description,
            parameters=spec.parameters,
            policy=policy,
            executor=execute,
            result_adapter=adapter,
        )
        manifest = provisional.model_copy(update={"release_digest": digest})
        bindings.append(ToolBinding(
            name=spec.name,
            description=spec.description,
            parameters=spec.parameters,
            manifest=manifest,
            executor=execute,
            result_adapter=adapter,
            implementation=spec.implementation or type(spec.run).__qualname__,
        ))
    return ToolCatalog(bindings, max_bytes=max_bytes)


def register_tool_catalog(broker: Any, catalog: ToolCatalog) -> None:
    """Install one already-validated catalog into Broker; duplicates fail."""

    if getattr(broker, "catalog", None) is not None:
        raise ValueError("ToolBroker already has a ToolCatalog")
    for binding in catalog.all():
        broker.register(
            binding.manifest,
            binding.executor,
            result_adapter=binding.result_adapter,
        )
    broker.catalog = catalog


def native_manifest_for(spec: ToolSpec, session: NativeBrokerSession) -> ToolManifest:
    binding = session.catalog.get(spec.name)
    if binding is None:
        raise RuntimeFault(
            "TOOL_NOT_REGISTERED", f"tool is not registered: {spec.name}",
        )
    if (
        binding.description != spec.description
        or binding.parameter_schema() != spec.parameters
        or binding.result_adapter is not _native_result_adapter(spec.result_protocol)
        or binding.implementation != spec.implementation
        or binding.manifest.exclusive_resources != spec.exclusive_resources
    ):
        raise RuntimeFault(
            "TOOL_CATALOG_MISMATCH",
            "Native ToolSpec disagrees with the activated ToolCatalog",
            500,
            {"tool_name": spec.name},
        )
    return binding.manifest


async def prepare_native_batch(
    session: NativeBrokerSession,
    registry: ToolRegistry,
    calls: tuple[Any, ...] | list[Any],
) -> tuple[Any, ...]:
    """Prepare a full Native batch and publish its slots only after commit.

    ``calls`` are ``ToolBatchCall`` values.  Callers omit manifest_override;
    this boundary derives the frozen manifest from the exact registry spec.
    """

    from agent.runtime.application.tool_broker import ToolBatchCall  # noqa: PLC0415

    prepared_calls: list[ToolBatchCall] = []
    for call in calls:
        if not isinstance(call, ToolBatchCall):
            raise TypeError("Native batch calls must be ToolBatchCall values")
        spec = registry.get(call.tool_name)
        if spec is None:
            raise RuntimeFault(
                "TOOL_NOT_REGISTERED", f"tool is not registered: {call.tool_name}",
            )
        prepared_calls.append(ToolBatchCall(
            logical_key=call.logical_key,
            tool_name=call.tool_name,
            arguments=call.arguments,
            framework_call_id=call.framework_call_id,
            manifest_override=native_manifest_for(spec, session),
        ))
    async with session.prepare_lock:
        prepared = await session.tool_broker.prepare_batch(
            run_id=session.run_id,
            parent_activity_id=session.activity_id,
            fencing_token=session.fencing_token,
            calls=tuple(prepared_calls),
        )
        # Make the entire batch visible to execution only after Broker's atomic
        # transaction succeeded.  A mismatch never publishes a partial map.
        session.prepared_by_logical_key.update({
            item.logical_key: item for item in prepared
        })
        if session.early_settlement_capacity is None:
            begin_native_settlement_batch(
                session,
                [item.logical_key for item in prepared],
            )
        else:
            session.settlement_turn_by_logical_key.update({
                item.logical_key: _native_early_turn(
                    session,
                    item.logical_key,
                    session.early_settlement_order_by_turn,
                )
                for item in prepared
            })
    return prepared


def begin_native_settlement_batch(
    session: NativeBrokerSession,
    calls_or_logical_keys: list[Any] | tuple[Any, ...],
) -> None:
    """Order settlement for exactly the Native calls executed in this pass.

    Recovery prepares the complete historical batch but may execute only its
    still-unpaired suffix, so the adapter calls this again with that subset
    immediately before creating its concurrent tasks.
    """

    from agent.runtime.application.tool_broker import ToolSettlementOrder  # noqa: PLC0415

    logical_keys = [
        str(item) if isinstance(item, str) else str(item.logical_key)
        for item in calls_or_logical_keys
    ]
    if not logical_keys:
        session.settlement_turn_by_logical_key.clear()
        return
    if len(logical_keys) != len(set(logical_keys)):
        raise RuntimeFault(
            "TOOL_REPLAY_MISMATCH",
            "Native batch contains duplicate logical ToolCall slots",
            409,
        )
    missing = [
        logical_key for logical_key in logical_keys
        if logical_key not in session.prepared_by_logical_key
    ]
    if missing:
        raise RuntimeFault(
            "TOOL_BATCH_NOT_PREPARED",
            "Native execution cannot start before the complete batch is prepared",
            409,
            {"logical_keys": missing},
        )
    order = ToolSettlementOrder(len(logical_keys))
    session.settlement_turn_by_logical_key.clear()
    session.settlement_turn_by_logical_key.update({
        logical_key: order.turn(ordinal)
        for ordinal, logical_key in enumerate(logical_keys)
    })


def _native_early_turn(
    session: NativeBrokerSession,
    logical_key: str,
    orders: dict[int, Any],
) -> Any | None:
    """Return the experimental turn gate for one streamed stable slot.

    Early calls are a contiguous safe prefix, but they become visible one at a
    time before the provider closes the batch.  Allocate a bounded order per
    model turn so external work may overlap while every durable settlement
    still waits for the model call ordinal.
    """

    capacity = session.early_settlement_capacity
    if capacity is None:
        return None
    match = re.fullmatch(r"native:turn:(\d+):call:(\d+)", logical_key)
    if match is None:
        raise RuntimeFault(
            "TOOL_REPLAY_MISMATCH",
            "experimental Native dispatch requires a canonical stable slot",
            409,
            {"logical_key": logical_key},
        )
    turn_ordinal = int(match.group(1))
    call_ordinal = int(match.group(2))
    if call_ordinal >= capacity:
        raise RuntimeFault(
            "TOOL_CALL_LIMIT_EXCEEDED",
            "experimental Native ToolCall ordinal exceeds the frozen turn limit",
            409,
            {"logical_key": logical_key, "limit": capacity},
        )
    from agent.runtime.application.tool_broker import ToolSettlementOrder  # noqa: PLC0415

    order = orders.get(turn_ordinal)
    if order is None:
        order = ToolSettlementOrder(capacity)
        orders[turn_ordinal] = order
    return order.turn(call_ordinal)


def _native_early_settlement_turn(
    session: NativeBrokerSession,
    logical_key: str,
) -> Any | None:
    return _native_early_turn(
        session,
        logical_key,
        session.early_settlement_order_by_turn,
    )


def _native_early_prepare_turn(
    session: NativeBrokerSession,
    logical_key: str,
) -> Any | None:
    return _native_early_turn(
        session,
        logical_key,
        session.early_prepare_order_by_turn,
    )


class AdkToolBatch:
    """Bridge ADK's aggregated model callback to durable ToolCall slots.

    ADK 2.6.2 awaits ``after_model_callback`` for the final non-partial
    response before it creates parallel tool tasks.  We use the ordered call
    batch in that response to derive stable ``turn + call`` slots.  Provider
    function-call ids are retained only to correlate the later callback with
    its already prepared slot.
    """

    def __init__(self, rc: RunContext, *, catalog: ToolCatalog | None = None) -> None:
        if rc.tool_broker is None:
            raise ValueError("ADK ToolCall batching requires a ToolBroker")
        self._rc = rc
        self._catalog = catalog or getattr(rc.tool_broker, "catalog", None)
        self._manifests: dict[str, ToolManifest] = {}
        self._active_slots: dict[str, tuple[str, str, str]] = {}
        self._prepared_turns: dict[int, tuple[tuple[str, str, str], ...]] = {}
        self._lock = asyncio.Lock()

    def register(self, manifest: ToolManifest) -> None:
        if self._catalog is not None:
            binding = self._catalog.get(manifest.name)
            if binding is None:
                raise ValueError(f"ADK tool is absent from ToolCatalog: {manifest.name}")
            manifest = binding.manifest
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
            from agent.runtime.application.tool_broker import ToolBatchCall  # noqa: PLC0415

            await self._rc.tool_broker.prepare_batch(
                run_id=self._rc.run_id,
                parent_activity_id=self._rc.activity_id,
                fencing_token=self._rc.fencing_token,
                calls=tuple(
                    ToolBatchCall(
                        logical_key=item.logical_key,
                        tool_name=item.tool_name,
                        arguments=item.request,
                        framework_call_id=item.framework_call_id,
                        manifest_override=self._manifests[item.tool_name],
                    )
                    for item in preparations
                ),
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
        result_adapter: ToolResultAdapter,
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
        self._result_adapter = result_adapter

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
            result_adapter_override=self._result_adapter,
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
        catalog = getattr(rc.tool_broker, "catalog", None)
        if catalog is not None:
            binding = catalog.get(base.name)
            if binding is None:
                raise ValueError(f"ADK tool is absent from ToolCatalog: {base.name}")
            # The catalog was built from this exact post-discovery surface.
            # Re-check the public declaration here so an adapter cannot drift.
            from agent.engine.native_loop.tools import (
                from_adk_tool,
                from_function,
            )  # noqa: PLC0415
            projected = (
                from_function(tool)
                if callable(tool) and not hasattr(tool, "run_async")
                else from_adk_tool(tool)
            )
            if (
                binding.description != projected.description
                or binding.parameter_schema() != projected.parameters
            ):
                raise ValueError(f"ADK tool declaration disagrees with catalog: {base.name}")
            manifest = binding.manifest
        batch.register(manifest)
        out.append(BrokeredAdkTool(
            base, rc, batch, manifest, adapter_for_tool(base),
        ))
    return out


def build_brokered_native_registry(
    registry: ToolRegistry,
    session: NativeBrokerSession,
) -> ToolRegistry:
    """Bind the Runtime-independent Native registry to durable Broker slots."""

    if session.tool_broker is None:
        raise ValueError("Native Runtime execution requires a ToolBroker")
    wrapped: list[ToolSpec] = []
    for original in registry.all():
        manifest = native_manifest_for(original, session)
        result_adapter = _native_result_adapter(original.result_protocol)

        async def run(
            args: dict[str, Any], context: NativeToolContext, *, spec: ToolSpec = original,
            tool_manifest: ToolManifest = manifest,
            adapter: ToolResultAdapter = result_adapter,
        ) -> Any:
            async def invoke(arguments: dict[str, Any], broker_context: Any) -> Any:
                return await _with_tool_request_context(
                    broker_context, spec.run(arguments, context),
                )

            logical_key = context.logical_key or f"native:call:{context.function_call_id}"
            prepared = session.prepared_by_logical_key.get(logical_key)
            if prepared is not None and (
                prepared.tool_name != spec.name
                or prepared.request_digest != sha256_json(args)
            ):
                raise RuntimeFault(
                    "TOOL_REPLAY_MISMATCH",
                    "Native stable slot changed tool name or arguments",
                    409,
                    {"logical_key": logical_key},
                )
            if prepared is None:
                if session.early_settlement_capacity is None:
                    raise RuntimeFault(
                        "TOOL_BATCH_NOT_PREPARED",
                        "Native execution cannot start before batch PREPARE",
                        409,
                        {"logical_key": logical_key},
                    )
                prepare_turn = _native_early_prepare_turn(session, logical_key)
                assert prepare_turn is not None
                try:
                    await prepare_turn.acquire()
                    from agent.runtime.application.tool_broker import (  # noqa: PLC0415
                        ToolBatchCall,
                    )
                    prepared = (
                        await prepare_native_batch(
                            session,
                            registry,
                            [ToolBatchCall(
                                logical_key=logical_key,
                                tool_name=spec.name,
                                arguments=args,
                                framework_call_id=context.function_call_id,
                            )],
                        )
                    )[0]
                    prepare_turn.complete()
                except BaseException as exc:
                    prepare_turn.abort(exc)
                    raise
            result = await session.tool_broker.execute_prepared(
                prepared=prepared,
                parent_activity_id=session.activity_id,
                fencing_token=session.fencing_token,
                deadline_at_ms=session.deadline_at_ms,
                manifest_override=tool_manifest,
                executor_override=invoke,
                result_adapter_override=adapter,
                settlement_turn=session.settlement_turn_by_logical_key.pop(
                    logical_key,
                    None,
                ),
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
            result_protocol=original.result_protocol,
            implementation=original.implementation,
        ))
    return ToolRegistry(wrapped)


def _native_result_adapter(protocol: str) -> ToolResultAdapter:
    adapters = {
        "plain": plain_json_output,
        "skill-center": skill_center_output,
        "claude-skill": claude_skill_output,
        "a2a": a2a_output,
    }
    try:
        return adapters[protocol]
    except KeyError as exc:
        raise ValueError(f"unsupported Native tool result protocol: {protocol}") from exc


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
