"""Production Native EngineAdapter, directly bound to durable RuntimeIO.

The reusable ``NativeLoop`` kernel remains Runtime-independent for Claude Skill
sub-runners.  This adapter alone owns canonical history/attachments, the strict
checkpoint codec, commit-before-pull model streaming and mandatory Tool Broker
coordination.  It deliberately does not implement ``ReasoningEngine`` and does
not use the ADK queue/merge transport.
"""
from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agent.engine.loop_tools import LOOP_INSTRUCTION, TASK_PLAN_KEY
from agent.engine.loop_tools.catalog import collect_loop_tools
from agent.engine.native_loop import executor
from agent.engine.native_loop.checkpoint import (
    NativeCheckpointPhase,
    decode_native_checkpoint,
    encode_native_checkpoint,
)
from agent.engine.native_loop.llm_client import NativeLlmClient, get_shared_client
from agent.engine.native_loop.loop import (
    T_COMPLETED,
    LoopConfig,
    LoopState,
    NativeLoop,
    NativeRunCancelled,
)
from agent.engine.native_loop.messages import Msg, ToolCall
from agent.engine.native_loop.tools import ToolRegistry, build_registry
from agent.runtime.adapters.brokered_tools import (
    NativeBrokerSession,
    begin_native_settlement_batch,
    build_brokered_native_registry,
    prepare_native_batch,
)
from agent.runtime.application.tool_broker import ToolBatchCall
from agent.runtime.application.tool_outputs import project_tool_result_for_model
from agent.runtime.domain.artifact import MAX_HTTP_RANGE_BYTES
from agent.runtime.domain.errors import RuntimeFault, raise_if_ownership_lost
from agent.runtime.domain.models import (
    EngineOutcome,
    EngineOutcomeKind,
    EventType,
    ToolResultStatus,
    WorkingState,
    canonical_json,
    stable_id,
)
from agent.runtime.ports.artifact import ArtifactStore
from agent.runtime.ports.engine import EngineRunRequest, RuntimeIO
from agent.runtime.ports.store import EventDraft
from agent.skills.request_context import (
    SkillRequestContext,
    reset_request_context,
    set_request_context,
)
from agent.skills.ui_event_queue import reset_ui_sink, set_ui_sink
from agent.stream.event_converters import StreamEvent

if TYPE_CHECKING:
    from agent.context import AgentContext
    from agent.runtime.application.tool_catalog import ToolCatalog

_HARD_CAP_MARGIN = 2
_LARGE_CHECKPOINT_RESULT_BYTES = 8 * 1024
_CONTROL_POLL_SECONDS = 0.1


@dataclass(frozen=True)
class _StreamEnvelope:
    """One bounded hand-off from the attempt-level stream/control tasks."""

    event: StreamEvent | None = None
    error: BaseException | None = None
    exhausted: bool = False


def collect_native_tools(ctx: "AgentContext", *, run_engine: str = "native_loop") -> list[Any]:
    """Build the final loop tool surface; Worker validates it before release."""
    return collect_loop_tools(ctx, run_engine=run_engine)


class NativeLoopAdapter:
    """Direct ``EngineAdapter.execute(request, RuntimeIO)`` implementation."""

    name = "native_loop"

    def __init__(
        self,
        *,
        context: "AgentContext",
        release_fingerprint: str,
        artifact_store: ArtifactStore,
        artifact_metadata_loader: Any,
        registry: ToolRegistry,
        tool_catalog: "ToolCatalog",
        client: NativeLlmClient | None = None,
    ) -> None:
        self.context = context
        self.release_fingerprint = release_fingerprint
        self.artifact_store = artifact_store
        self.artifact_metadata_loader = artifact_metadata_loader
        self.registry = registry
        self.tool_catalog = tool_catalog
        self.client = client or get_shared_client(context.settings)
        settings = context.settings
        if (
            settings.native_early_tool_dispatch == "provider_block_complete"
            and not getattr(self.client, "tool_call_block_complete", False)
        ):
            raise RuntimeFault(
                "EARLY_DISPATCH_CAPABILITY_UNAVAILABLE",
                "the configured provider has no ToolCall block-complete signal",
                500,
            )

    async def execute(self, request: EngineRunRequest, io: RuntimeIO) -> EngineOutcome:
        settings = self.context.settings
        broker = io.tool_broker

        initial_messages = await self._compile_input(request)
        if request.checkpoint is None:
            state = LoopState(messages=initial_messages)
            restored_phase: NativeCheckpointPhase | None = None
        else:
            if request.checkpoint.engine_state is None:
                raise RuntimeFault(
                    "NATIVE_CHECKPOINT_INVALID",
                    "native checkpoint is missing engine_state",
                    409,
                )
            current_input = initial_messages[-1].content
            state, restored_phase = decode_native_checkpoint(
                request.checkpoint.engine_state,
                current_input=current_input,
            )
            await self._materialize_ledger_results(state, request, broker)

        checkpoint_revision = request.checkpoint.revision if request.checkpoint else 0
        session = NativeBrokerSession(
            run_id=request.envelope.run_id,
            activity_id=request.activity_id,
            fencing_token=request.fencing_token,
            deadline_at_ms=request.envelope.deadline_at,
            tool_broker=broker,
            catalog=self.tool_catalog,
            early_settlement_capacity=(
                settings.native_max_tool_calls_per_turn
                if settings.native_early_tool_dispatch == "experimental_heuristic"
                else None
            ),
        )
        brokered_registry = build_brokered_native_registry(self.registry, session)

        async def persist(
            current: LoopState,
            phase: str,
            stream_events: tuple[StreamEvent, ...],
        ) -> None:
            nonlocal checkpoint_revision
            raw = encode_native_checkpoint(
                current,
                phase,  # type: ignore[arg-type] - encoder rejects unknown values
                large_result_bytes=_LARGE_CHECKPOINT_RESULT_BYTES,
            )
            size = len(canonical_json(raw).encode("utf-8"))
            if size > settings.native_max_checkpoint_bytes:
                raise RuntimeFault(
                    "CHECKPOINT_TOO_LARGE",
                    "native checkpoint exceeds the configured UTF-8 byte limit",
                    409,
                    {"limit": settings.native_max_checkpoint_bytes, "actual": size},
                )
            plan = current.tool_state.get(TASK_PLAN_KEY)
            steps = plan.get("steps", []) if isinstance(plan, dict) else []
            cursor = plan.get("current", 1) if isinstance(plan, dict) else 1
            model_plan = [
                {
                    "step": index + 1,
                    "title": str(title),
                    "status": (
                        "done"
                        if index + 1 < cursor
                        else "running"
                        if index + 1 == cursor
                        else "planned"
                    ),
                }
                for index, title in enumerate(steps)
            ]
            drafts = tuple(self._checkpoint_event(event, request) for event in stream_events)
            try:
                saved = await io.checkpoint(
                    WorkingState(
                        goal=request.input_text,
                        model_plan=model_plan,
                        budget={
                            "model_call_count": current.model_call_count,
                            "tool_call_count": current.tool_call_count,
                        },
                    ),
                    expected_revision=checkpoint_revision,
                    engine_state=raw,
                    events=drafts,
                )
            except RuntimeFault as exc:
                raise_if_ownership_lost(exc)
                raise
            checkpoint_revision = saved.revision

        async def prepare_batch(calls: list[ToolCall], _state: LoopState) -> None:
            batch: list[ToolBatchCall] = []
            for call in calls:
                arguments, error = executor.parse_arguments(call)
                if error is not None or arguments is None:
                    raise RuntimeError("validated Native ToolCall unexpectedly became invalid")
                batch.append(ToolBatchCall(
                    logical_key=call.logical_key,
                    tool_name=call.name,
                    arguments=arguments,
                    framework_call_id=call.id,
                ))
            try:
                await prepare_native_batch(session, self.registry, batch)
            except RuntimeFault as exc:
                raise_if_ownership_lost(exc)
                raise

        async def execute_one(call: ToolCall, current: LoopState) -> executor.ToolOutcome:
            try:
                outcome = await executor.execute_one(
                    call,
                    brokered_registry,
                    invocation_id=request.envelope.run_id,
                    state=current.tool_state,
                )
            except RuntimeFault as exc:
                raise_if_ownership_lost(exc)
                raise
            prepared = session.prepared_by_logical_key.get(call.logical_key)
            execution_id = (
                prepared.tool_execution_id
                if prepared is not None
                else stable_id("tool", request.envelope.run_id, call.logical_key)
            )
            outcome.tool_execution_id = execution_id
            outcome.message.tool_execution_id = execution_id
            outcome.project_event = False
            return outcome

        async def run_calls(
            calls: list[ToolCall], current: LoopState, max_concurrency: int,
        ) -> AsyncIterator[executor.ToolOutcome]:
            begin_native_settlement_batch(session, calls)
            async for outcome in executor.run_calls(
                calls,
                brokered_registry,
                invocation_id=request.envelope.run_id,
                state=current.tool_state,
                max_concurrency=max_concurrency,
            ):
                prepared = session.prepared_by_logical_key.get(outcome.call.logical_key)
                execution_id = (
                    prepared.tool_execution_id
                    if prepared is not None
                    else stable_id(
                        "tool", request.envelope.run_id, outcome.call.logical_key,
                    )
                )
                outcome.tool_execution_id = execution_id
                outcome.message.tool_execution_id = execution_id
                outcome.project_event = False
                yield outcome

        async def probe_control() -> None:
            if io.remaining_ms() <= 0:
                raise TimeoutError
            if await io.is_cancelled():
                raise NativeRunCancelled

        # A kill after MODEL_RESPONSE_COMMITTED but before COMPLETED must not
        # issue another model request. Finish the exact persisted final response.
        if restored_phase == "MODEL_RESPONSE_COMMITTED":
            final = state.messages[-1] if state.messages else None
            if final is not None and final.role == "assistant" and not final.tool_calls:
                text = final.content if isinstance(final.content, str) else ""
                if not text.strip():
                    raise RuntimeFault(
                        "NATIVE_CHECKPOINT_INVALID",
                        "MODEL_RESPONSE_COMMITTED contains an empty final response",
                        409,
                    )
                state.final_text = text
                state.final_message_id = state.current_message_id
                state.final_generation_id = state.current_generation_id
                state.transition = T_COMPLETED
                await persist(state, "COMPLETED", ())
                io.set_final_assistant(
                    text, state.current_message_id, state.current_generation_id,
                )
                return EngineOutcome(kind=EngineOutcomeKind.COMPLETED)

        if restored_phase == "COMPLETED":
            assert state.final_text and state.final_message_id and state.final_generation_id
            io.set_final_assistant(
                state.final_text, state.final_message_id, state.final_generation_id,
            )
            return EngineOutcome(kind=EngineOutcomeKind.COMPLETED)

        loop = NativeLoop(
            client=self.client,
            registry=brokered_registry,
            system_instruction=LOOP_INSTRUCTION,
            chat=self.context.chat,
            checkpoint=persist,
            prepare_tool_batch=prepare_batch,
            run_tool_calls=run_calls,
            execute_tool=execute_one,
            control_probe=probe_control,
            message_id_factory=lambda ordinal: stable_id(
                "msg", request.envelope.run_id, f"native:model:{ordinal}",
            ),
            config=LoopConfig(
                max_iters=settings.max_loop_iters,
                hard_cap=settings.max_loop_iters + _HARD_CAP_MARGIN,
                max_tool_concurrency=settings.native_max_tool_concurrency,
                early_tool_dispatch=settings.native_early_tool_dispatch,
                tool_result_max_chars=settings.native_tool_result_max_chars,
                context_window_tokens=settings.context_window_tokens,
                compact_buffer_tokens=settings.compact_buffer_tokens,
                compact_preserve_units=settings.compact_preserve_units,
                max_tool_calls_per_turn=settings.native_max_tool_calls_per_turn,
                max_tool_calls_per_run=settings.native_max_tool_calls_per_run,
                max_tool_argument_bytes=settings.native_max_tool_argument_bytes,
                max_tool_batch_argument_bytes=settings.native_max_tool_batch_argument_bytes,
                max_model_output_bytes=settings.native_max_model_output_bytes,
            ),
        )

        request_token = set_request_context(SkillRequestContext(
            agent_uuid=request.envelope.agent_id,
            user_id=request.envelope.principal_id,
            session_id=request.envelope.run_id,
            text=request.input_text,
            user={"userId": request.envelope.principal_id},
            run_id=request.envelope.run_id,
            activity_id=request.activity_id,
            deadline_at_ms=request.envelope.deadline_at,
            idempotency_key=request.envelope.idempotency_key,
        ))

        async def direct_skill_sink(event: StreamEvent) -> None:
            await io.emit(event.event, event.data)

        ui_token = set_ui_sink(direct_skill_sink)
        stream = loop.run(initial_state=state)
        cancelled = False

        stream_ready = asyncio.Event()
        stream_acknowledged = asyncio.Event()
        stop_pulling = asyncio.Event()
        stream_slot: _StreamEnvelope | None = None
        control_error: BaseException | None = None

        async def pump_stream() -> None:
            nonlocal stream_slot
            try:
                while not stop_pulling.is_set():
                    try:
                        event = await anext(stream)
                    except StopAsyncIteration:
                        stream_slot = _StreamEnvelope(exhausted=True)
                        stream_ready.set()
                        return
                    stream_acknowledged.clear()
                    stream_slot = _StreamEnvelope(event=event)
                    stream_ready.set()
                    await stream_acknowledged.wait()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                stream_slot = _StreamEnvelope(error=exc)
                stream_ready.set()

        async def watch_cancel() -> None:
            nonlocal control_error
            try:
                while not stop_pulling.is_set():
                    if await io.is_cancelled():
                        control_error = NativeRunCancelled()
                        stop_pulling.set()
                        pump_task.cancel()
                        stream_ready.set()
                        return
                    await asyncio.sleep(_CONTROL_POLL_SECONDS)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                control_error = exc
                stop_pulling.set()
                pump_task.cancel()
                stream_ready.set()

        pump_task = asyncio.create_task(pump_stream(), name="native-stream-pump")
        cancel_task = asyncio.create_task(watch_cancel(), name="native-cancel-watch")

        try:
            async with asyncio.timeout(max(0.001, io.remaining_ms() / 1000)):
                while True:
                    await stream_ready.wait()
                    if control_error is not None:
                        raise control_error
                    envelope = stream_slot
                    assert envelope is not None
                    stream_slot = None
                    stream_ready.clear()
                    if envelope.error is not None:
                        raise envelope.error
                    if envelope.exhausted:
                        break
                    event = envelope.event
                    assert event is not None
                    emitted = False
                    try:
                        # RuntimeIO may aggregate text in memory, but the pump
                        # cannot pull another provider item until this awaited
                        # admission returns. Checkpoint/non-text/close remains
                        # the durable flush boundary.
                        await io.emit(event.event, event.data)
                        emitted = True
                    finally:
                        if not emitted:
                            stop_pulling.set()
                        stream_acknowledged.set()
        except NativeRunCancelled:
            cancelled = True
        except TimeoutError:
            return EngineOutcome(
                kind=EngineOutcomeKind.TERMINAL_FAILURE,
                error_code="DEADLINE_EXCEEDED",
                message="native attempt exceeded the absolute Run deadline",
            )
        except RuntimeFault as exc:
            raise_if_ownership_lost(exc)
            raise
        finally:
            stop_pulling.set()
            pump_task.cancel()
            cancel_task.cancel()
            await asyncio.gather(pump_task, cancel_task, return_exceptions=True)
            try:
                await stream.aclose()
            finally:
                reset_ui_sink(ui_token)
                reset_request_context(request_token)

        if cancelled:
            return EngineOutcome(kind=EngineOutcomeKind.CANCELLED)
        if loop.stop_reason != T_COMPLETED:
            if loop.error_code == "MODEL_STREAM_INCOMPLETE":
                return EngineOutcome(
                    kind=EngineOutcomeKind.RETRYABLE_FAILURE,
                    error_code=loop.error_code,
                    message="provider stream ended before its explicit finish marker",
                )
            return EngineOutcome(
                kind=EngineOutcomeKind.TERMINAL_FAILURE,
                error_code=loop.error_code or "NATIVE_LOOP_ABORTED",
                message=f"native loop stopped: {loop.stop_reason or 'unknown'}",
            )
        if not state.final_text or not state.final_message_id or not state.final_generation_id:
            return EngineOutcome(
                kind=EngineOutcomeKind.TERMINAL_FAILURE,
                error_code="MODEL_EMPTY_FINAL_RESPONSE",
                message="native loop completed without a semantic final assistant",
            )
        io.set_final_assistant(
            state.final_text, state.final_message_id, state.final_generation_id,
        )
        return EngineOutcome(kind=EngineOutcomeKind.COMPLETED)

    async def _compile_input(self, request: EngineRunRequest) -> list[Msg]:
        messages = [
            Msg(role=str(item["role"]), content=str(item.get("text", "")))
            for item in request.history
        ]
        blocks: list[dict[str, Any]] = []
        if request.input_text:
            blocks.append({"type": "text", "text": request.input_text})
        for artifact_id in request.envelope.attachment_refs:
            metadata = await self.artifact_metadata_loader(artifact_id)
            media_type = metadata.get("media_type") or "application/octet-stream"
            if media_type.startswith("image/"):
                raw = await self._read_all(
                    artifact_id, total_size=int(metadata["size_bytes"]),
                )
                encoded = base64.b64encode(raw).decode("ascii")
                blocks.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{media_type};base64,{encoded}"},
                })
            else:
                preview = await self.artifact_store.read_preview(artifact_id)
                suffix = (
                    ""
                    if preview.end_exclusive >= preview.total_size
                    else "\n[preview truncated; call read_artifact with an offset for more]"
                )
                blocks.append({
                    "type": "text",
                    "text": (
                        f"[Attachment {artifact_id} media_type={media_type} "
                        f"bytes=0-{preview.end_exclusive}/{preview.total_size}]\n"
                        + preview.data.decode("utf-8", errors="replace")
                        + suffix
                    ),
                })
        has_image = any(block.get("type") == "image_url" for block in blocks)
        content: Any = (
            blocks
            if has_image
            else "\n".join(
                str(block.get("text", ""))
                for block in blocks
                if block.get("type") == "text"
            )
        )
        messages.append(Msg(role="user", content=content))
        return messages

    async def _materialize_ledger_results(
        self,
        state: LoopState,
        request: EngineRunRequest,
        broker: Any,
    ) -> None:
        """Rematerialize every referenced result in-place, not just the tail."""
        for message in state.messages:
            if (
                message.role != "tool"
                or message.content is not None
                or not message.tool_execution_id
            ):
                continue
            try:
                result = await broker.materialize_committed_result(
                    tool_execution_id=message.tool_execution_id,
                    parent_activity_id=request.activity_id,
                    deadline_at_ms=request.envelope.deadline_at,
                )
            except RuntimeFault as exc:
                raise_if_ownership_lost(exc)
                raise
            projected = project_tool_result_for_model(result)
            message.content = executor._to_text(projected)  # noqa: SLF001
            message.is_error = result.status not in {
                ToolResultStatus.SUCCESS,
                ToolResultStatus.NO_OUTPUT,
            }

    @staticmethod
    def _checkpoint_event(event: StreamEvent, request: EngineRunRequest) -> EventDraft:
        event_map = {
            "output_generation_started": EventType.OUTPUT_GENERATION_STARTED,
            "tool_call": EventType.TOOL_CALL_COMMITTED,
            "tool_result": EventType.TOOL_RESULT_COMMITTED,
            "plan_step": EventType.MODEL_PLAN_UPDATED,
            "skill_event": EventType.SKILL_UI_FRAME_COMMITTED,
        }
        try:
            event_type = event_map[event.event]
        except KeyError as exc:
            raise RuntimeError(
                f"event {event.event!r} is not allowed in a checkpoint transaction"
            ) from exc
        return EventDraft(
            event_type,
            dict(event.data),
            activity_id=request.activity_id,
            producer="native-engine",
        )

    async def _read_all(self, artifact_id: str, *, total_size: int) -> bytes:
        if total_size < 0:
            raise ValueError("artifact metadata size cannot be negative")
        chunks: list[bytes] = []
        offset = 0
        while offset < total_size:
            part = await self.artifact_store.read_range(
                artifact_id,
                start=offset,
                end_exclusive=min(total_size, offset + MAX_HTTP_RANGE_BYTES),
            )
            chunks.append(part.data)
            offset = part.end_exclusive
        if offset != total_size:
            raise RuntimeError("artifact metadata size changed during materialization")
        return b"".join(chunks)


__all__ = [
    "NativeLoopAdapter",
    "collect_native_tools",
]
