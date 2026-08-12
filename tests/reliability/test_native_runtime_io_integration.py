from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from agent.config import AgentSettings
from agent.engine.native_loop.checkpoint import encode_native_checkpoint
from agent.engine.native_loop.engine import NativeLoopAdapter
from agent.engine.native_loop.llm_client import TextDelta, ToolCallReady, TurnEnd
from agent.engine.native_loop.loop import LoopState
from agent.engine.native_loop.messages import Msg, ToolCall
from agent.engine.native_loop.tools import NativeToolContext, ToolRegistry, ToolSpec
from agent.runtime.adapters.brokered_tools import build_runtime_tool_catalog
from agent.runtime.application.tool_broker import PreparedToolExecution
from agent.runtime.domain.errors import RuntimeFault
from agent.runtime.domain.models import (
    CheckpointRecord,
    EngineOutcomeKind,
    RuntimeEnvelope,
    ToolResultEnvelope,
    ToolResultStatus,
    WorkingState,
    sha256_json,
)
from agent.runtime.ports.engine import EngineRunRequest


_RUN_ID = "run_native_runtime_io"
_ACTIVITY_ID = "act_native_runtime_io"


async def _unused_artifact_metadata(_artifact_id: str) -> dict[str, Any]:
    raise AssertionError("these integration tests do not use attachments")


class _UnusedArtifactStore:
    async def read_preview(self, _artifact_id: str) -> Any:
        raise AssertionError("these integration tests do not use attachments")

    async def read_range(self, _artifact_id: str, **_kwargs: Any) -> Any:
        raise AssertionError("these integration tests do not use attachments")


class _RuntimeIO:
    def __init__(
        self,
        broker: Any,
        *,
        block_first_text: bool = False,
        deadline_after_seconds: float | None = None,
        initial_revision: int = 0,
    ) -> None:
        self._broker = broker
        self._revision = initial_revision
        self._deadline = (
            None
            if deadline_after_seconds is None
            else asyncio.get_running_loop().time() + deadline_after_seconds
        )
        self.cancelled = False
        self.block_first_text = block_first_text
        self.text_emit_entered = asyncio.Event()
        self.release_text_emit = asyncio.Event()
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.phases: list[str] = []
        self.checkpoint_states: list[dict[str, Any]] = []
        self.final: tuple[str, str, str] | None = None

    @property
    def tool_broker(self) -> Any:
        return self._broker

    async def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.block_first_text and event_type == "text" and not self.text_emit_entered.is_set():
            self.text_emit_entered.set()
            await self.release_text_emit.wait()
        self.events.append((event_type, dict(payload)))

    async def force_flush(self) -> None:
        return None

    async def abort(self) -> None:
        return None

    async def checkpoint(
        self,
        working_state: WorkingState,
        *,
        expected_revision: int,
        engine_state: dict[str, Any] | None = None,
        events: Any = (),
    ) -> CheckpointRecord:
        assert expected_revision == self._revision
        assert engine_state is not None
        self._revision += 1
        self.phases.append(str(engine_state["phase"]))
        self.checkpoint_states.append(engine_state)
        for draft in events:
            self.events.append((draft.event_type.value, dict(draft.payload or {})))
        return CheckpointRecord(
            checkpoint_id=f"cp_native_{self._revision}",
            run_id=_RUN_ID,
            activity_id=_ACTIVITY_ID,
            revision=self._revision,
            working_state=working_state,
            engine_state=engine_state,
            created_at=2_300_000_000_000 + self._revision,
        )

    async def is_cancelled(self) -> bool:
        return self.cancelled

    def remaining_ms(self) -> int:
        if self._deadline is None:
            return 60_000
        return max(0, int((self._deadline - asyncio.get_running_loop().time()) * 1000))

    def seed_assistant_text(self, _text: str) -> None:
        return None

    def set_final_assistant(
        self, text: str, message_id: str, generation_id: str,
    ) -> None:
        self.final = (text, message_id, generation_id)

    @property
    def final_assistant(self) -> tuple[str, str, str] | None:
        return self.final


@dataclass
class _BrokerCounters:
    prepare_calls: int = 0
    execute_calls: int = 0
    execute_prepared_calls: int = 0
    active: int = 0
    peak: int = 0
    completed: int = 0
    cancelled: int = 0


class _TrackingBroker:
    """Minimal Broker port with controllable external execution latency."""

    def __init__(self, *, block_execution: bool = False) -> None:
        self.counters = _BrokerCounters()
        self.execution_gate = asyncio.Event()
        if not block_execution:
            self.execution_gate.set()
        self.peak_reached = asyncio.Event()
        self.all_completed = asyncio.Event()
        self.expected_completions = 0
        self.phase_reader: Any = None
        self.phase_at_first_execution: str | None = None
        self.materialization_results: dict[str, ToolResultEnvelope] = {}
        self.materialized_ids: list[str] = []

    async def prepare_batch(self, *, calls: Any, **_kwargs: Any) -> tuple[Any, ...]:
        self.counters.prepare_calls += 1
        return tuple(
            PreparedToolExecution(
                tool_execution_id=f"tool_runtime_{index}",
                logical_key=call.logical_key,
                tool_name=call.tool_name,
                request_digest=sha256_json(call.arguments),
                arguments=dict(call.arguments),
            )
            for index, call in enumerate(calls)
        )

    async def execute(self, *, logical_key: str, **_kwargs: Any) -> ToolResultEnvelope:
        self.counters.execute_calls += 1
        return await self._execute(logical_key)

    async def execute_prepared(
        self, *, prepared: PreparedToolExecution, **_kwargs: Any,
    ) -> ToolResultEnvelope:
        self.counters.execute_prepared_calls += 1
        return await self._execute(prepared.logical_key)

    async def _execute(self, logical_key: str) -> ToolResultEnvelope:
        if self.phase_at_first_execution is None and self.phase_reader is not None:
            self.phase_at_first_execution = self.phase_reader()
        self.counters.active += 1
        self.counters.peak = max(self.counters.peak, self.counters.active)
        if self.counters.peak >= 2:
            self.peak_reached.set()
        try:
            await self.execution_gate.wait()
        except asyncio.CancelledError:
            self.counters.cancelled += 1
            raise
        finally:
            self.counters.active -= 1
        self.counters.completed += 1
        if self.counters.completed >= self.expected_completions:
            self.all_completed.set()
        return ToolResultEnvelope(
            status=ToolResultStatus.SUCCESS,
            preview={"ok": True, "logical_key": logical_key},
        )

    async def materialize_committed_result(
        self, *, tool_execution_id: str, **_kwargs: Any,
    ) -> ToolResultEnvelope:
        self.materialized_ids.append(tool_execution_id)
        return self.materialization_results[tool_execution_id]


class _ToolTurnClient:
    """One tool turn followed by one final assistant turn."""

    def __init__(
        self,
        calls: list[ToolCall],
        *,
        prefix_text: str | None = None,
    ) -> None:
        self.calls = calls
        self.prefix_text = prefix_text
        self.finish_gate = asyncio.Event()
        self.before_finish = asyncio.Event()
        self.first_stream_closed = asyncio.Event()
        self.stream_count = 0
        self.provider_pulls = 0

    async def stream(self, **_kwargs: Any):
        turn = self.stream_count
        self.stream_count += 1
        if turn > 0:
            self.provider_pulls += 1
            yield TextDelta("final answer")
            self.provider_pulls += 1
            yield TurnEnd(finish_reason="stop")
            return

        try:
            if self.prefix_text is not None:
                self.provider_pulls += 1
                yield TextDelta(self.prefix_text)
            for call in self.calls:
                self.provider_pulls += 1
                yield ToolCallReady(call)
            self.before_finish.set()
            await self.finish_gate.wait()
            self.provider_pulls += 1
            yield TurnEnd(finish_reason="tool_calls")
        finally:
            self.first_stream_closed.set()


class _BlockedClient:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = asyncio.Event()
        self.never = asyncio.Event()

    async def stream(self, **_kwargs: Any):
        self.started.set()
        try:
            await self.never.wait()
            yield TextDelta("unreachable")
        finally:
            self.closed.set()


class _FinalCaptureClient:
    def __init__(self) -> None:
        self.requests: list[list[Msg]] = []

    async def stream(self, *, messages: list[Msg], **_kwargs: Any):
        self.requests.append(messages)
        yield TextDelta("final after restored tool history")
        yield TurnEnd(finish_reason="stop")


class _FinalTextClient:
    def __init__(self, deltas: list[str]) -> None:
        self.deltas = deltas
        self.stream_count = 0

    async def stream(self, **_kwargs: Any):
        self.stream_count += 1
        for delta in self.deltas:
            yield TextDelta(delta)
        yield TurnEnd(finish_reason="stop")


async def _read_only_tool(
    _args: dict[str, Any], _context: NativeToolContext,
) -> dict[str, bool]:
    # The Broker fake owns latency and cancellation in these Adapter tests.
    return {"unused": True}


def _registry() -> ToolRegistry:
    return ToolRegistry([ToolSpec(
        name="calculator",
        description="deterministic read-only test tool",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        run=_read_only_tool,
        concurrency_safe=True,
    )])


def _calls(count: int) -> list[ToolCall]:
    return [
        ToolCall(
            id=f"provider-call-{index}",
            name="calculator",
            arguments=f'{{"value":{index}}}',
        )
        for index in range(count)
    ]


def _settings(
    mode: str,
    *,
    concurrency: int = 2,
    **overrides: Any,
) -> AgentSettings:
    values: dict[str, Any] = {
        "trace_enabled": False,
        "max_loop_iters": 3,
        "native_early_tool_dispatch": mode,
        "native_max_tool_concurrency": concurrency,
        "native_max_tool_calls_per_turn": 16,
        "native_max_tool_calls_per_run": 32,
    }
    values.update(overrides)
    return AgentSettings(_env_file=None, **values)


def _adapter(
    client: Any,
    broker: _TrackingBroker,
    *,
    mode: str = "off",
    concurrency: int = 2,
    with_tools: bool = True,
    **settings_overrides: Any,
) -> NativeLoopAdapter:
    context = SimpleNamespace(
        settings=_settings(mode, concurrency=concurrency, **settings_overrides),
        chat=None,
    )
    registry = _registry() if with_tools else ToolRegistry([])
    return NativeLoopAdapter(
        context=context,  # type: ignore[arg-type] - only settings/chat are Native ports.
        release_fingerprint="release-native-runtime-io",
        artifact_store=_UnusedArtifactStore(),  # type: ignore[arg-type]
        artifact_metadata_loader=_unused_artifact_metadata,
        registry=registry,
        tool_catalog=build_runtime_tool_catalog(registry),
        client=client,
    )


def _request(checkpoint: CheckpointRecord | None = None) -> EngineRunRequest:
    envelope = RuntimeEnvelope(
        request_id="req_native_runtime_io",
        client_request_id="client_native_runtime_io",
        idempotency_key="idem-native-runtime-io",
        conversation_id="conv_native_runtime_io",
        turn_id="turn_native_runtime_io",
        run_id=_RUN_ID,
        principal_id="demo-user",
        agent_id="demo-agent",
        engine="native_loop",
        deadline_at=2_300_000_060_000,
        cancel_token_id="cancel_native_runtime_io",
        release_fingerprint="release-native-runtime-io",
        input_event_id="event_native_runtime_io",
        created_at=2_300_000_000_000,
    )
    return EngineRunRequest(
        envelope=envelope,
        activity_id=_ACTIVITY_ID,
        fencing_token=7,
        attempt=1,
        input_text="use the tools, then answer",
        history=(),
        checkpoint=checkpoint,
        resume_payload=None,
    )


async def _assert_no_native_tasks() -> None:
    await asyncio.sleep(0)
    leaked = [
        task
        for task in asyncio.all_tasks()
        if not task.done()
        and (
            task.get_name() == "native-provider-pull"
            or task.get_name().startswith("native-early-tool-")
        )
    ]
    assert leaked == []


@pytest.mark.asyncio
async def test_native_adapter_runtime_emit_backpressures_provider_and_tool_dispatch() -> None:
    broker = _TrackingBroker()
    client = _ToolTurnClient(_calls(1), prefix_text="committed before next pull")
    client.finish_gate.set()
    broker.expected_completions = 1
    io = _RuntimeIO(broker, block_first_text=True)
    broker.phase_reader = lambda: io.phases[-1]
    adapter = _adapter(client, broker)

    execution = asyncio.create_task(adapter.execute(_request(), io))
    await asyncio.wait_for(io.text_emit_entered.wait(), timeout=1)

    # MODEL_REQUEST + generation-start are durable, but the first text commit
    # is blocked.  The adapter must not pull another provider item, PREPARE a
    # ToolCall, or make external execution reachable across this barrier.
    await asyncio.sleep(0.05)
    assert io.phases == ["MODEL_REQUEST"]
    assert client.provider_pulls == 1
    assert broker.counters.prepare_calls == 0
    assert broker.counters.execute_calls == 0
    assert broker.counters.execute_prepared_calls == 0
    assert not execution.done()

    io.release_text_emit.set()
    outcome = await asyncio.wait_for(execution, timeout=2)
    assert outcome.kind is EngineOutcomeKind.COMPLETED
    assert broker.counters.prepare_calls == 1
    assert broker.counters.execute_prepared_calls == 1
    assert broker.phase_at_first_execution == "TOOL_BATCH_COMMITTED"
    assert io.final is not None and io.final[0] == "final answer"
    await _assert_no_native_tasks()


@pytest.mark.asyncio
async def test_native_adapter_preexisting_cancel_prevents_model_reservation_and_request() -> None:
    broker = _TrackingBroker()
    client = _BlockedClient()
    io = _RuntimeIO(broker)
    io.cancelled = True
    adapter = _adapter(client, broker, with_tools=False)

    outcome = await asyncio.wait_for(adapter.execute(_request(), io), timeout=1)

    assert outcome.kind is EngineOutcomeKind.CANCELLED
    assert io.phases == []
    assert not client.started.is_set()
    await _assert_no_native_tasks()


@pytest.mark.asyncio
async def test_native_adapter_expired_deadline_prevents_model_reservation_and_request() -> None:
    broker = _TrackingBroker()
    client = _BlockedClient()
    io = _RuntimeIO(broker, deadline_after_seconds=0)
    adapter = _adapter(client, broker, with_tools=False)

    outcome = await asyncio.wait_for(adapter.execute(_request(), io), timeout=1)

    assert outcome.kind is EngineOutcomeKind.TERMINAL_FAILURE
    assert outcome.error_code == "DEADLINE_EXCEEDED"
    assert io.phases == []
    assert not client.started.is_set()
    await _assert_no_native_tasks()


@pytest.mark.asyncio
async def test_native_adapter_cancel_closes_blocked_provider_stream() -> None:
    broker = _TrackingBroker()
    client = _BlockedClient()
    io = _RuntimeIO(broker)
    adapter = _adapter(client, broker, with_tools=False)

    execution = asyncio.create_task(adapter.execute(_request(), io))
    await asyncio.wait_for(client.started.wait(), timeout=1)
    io.cancelled = True

    outcome = await asyncio.wait_for(execution, timeout=2)
    assert outcome.kind is EngineOutcomeKind.CANCELLED
    await asyncio.wait_for(client.closed.wait(), timeout=1)
    await _assert_no_native_tasks()


@pytest.mark.asyncio
async def test_native_adapter_deadline_closes_blocked_provider_stream() -> None:
    broker = _TrackingBroker()
    client = _BlockedClient()
    io = _RuntimeIO(broker, deadline_after_seconds=0.15)
    adapter = _adapter(client, broker, with_tools=False)

    outcome = await asyncio.wait_for(adapter.execute(_request(), io), timeout=2)
    assert outcome.kind is EngineOutcomeKind.TERMINAL_FAILURE
    assert outcome.error_code == "DEADLINE_EXCEEDED"
    await asyncio.wait_for(client.closed.wait(), timeout=1)
    await _assert_no_native_tasks()


@pytest.mark.asyncio
async def test_native_adapter_off_waits_for_finish_then_bounds_read_only_concurrency() -> None:
    call_count = 6
    broker = _TrackingBroker(block_execution=True)
    broker.expected_completions = call_count
    client = _ToolTurnClient(_calls(call_count))
    io = _RuntimeIO(broker)
    broker.phase_reader = lambda: io.phases[-1]
    adapter = _adapter(client, broker, mode="off", concurrency=2)

    execution = asyncio.create_task(adapter.execute(_request(), io))
    await asyncio.wait_for(client.before_finish.wait(), timeout=1)
    await asyncio.sleep(0.05)
    assert broker.counters.prepare_calls == 0
    assert broker.counters.execute_calls == 0
    assert broker.counters.execute_prepared_calls == 0

    client.finish_gate.set()
    await asyncio.wait_for(broker.peak_reached.wait(), timeout=1)
    assert broker.counters.prepare_calls == 1
    assert broker.counters.peak == 2
    assert broker.counters.active == 2
    assert broker.phase_at_first_execution == "TOOL_BATCH_COMMITTED"

    broker.execution_gate.set()
    outcome = await asyncio.wait_for(execution, timeout=2)
    assert outcome.kind is EngineOutcomeKind.COMPLETED
    assert broker.counters.execute_calls == 0
    assert broker.counters.execute_prepared_calls == call_count
    assert broker.counters.completed == call_count
    assert broker.counters.peak <= 2
    await _assert_no_native_tasks()


@pytest.mark.asyncio
async def test_native_adapter_experimental_uses_fixed_workers_and_has_no_hanging_tasks() -> None:
    call_count = 6
    broker = _TrackingBroker(block_execution=True)
    broker.expected_completions = call_count
    client = _ToolTurnClient(_calls(call_count))
    io = _RuntimeIO(broker)
    adapter = _adapter(
        client,
        broker,
        mode="experimental_heuristic",
        concurrency=2,
    )

    execution = asyncio.create_task(adapter.execute(_request(), io))
    await asyncio.wait_for(client.before_finish.wait(), timeout=1)
    await asyncio.wait_for(broker.peak_reached.wait(), timeout=1)

    # The experiment may execute reviewed READ_ONLY calls before finish, but
    # it is constrained by fixed workers.  Every call first establishes its
    # durable stable slot; no direct/unprepared Broker execution is allowed.
    assert broker.counters.prepare_calls >= 2
    assert broker.counters.execute_calls == 0
    assert broker.counters.execute_prepared_calls >= 2
    assert broker.counters.peak == 2
    assert broker.counters.active == 2

    broker.execution_gate.set()
    await asyncio.wait_for(broker.all_completed.wait(), timeout=1)
    assert broker.counters.completed == call_count
    assert broker.counters.prepare_calls == call_count
    client.finish_gate.set()

    outcome = await asyncio.wait_for(execution, timeout=2)
    assert outcome.kind is EngineOutcomeKind.COMPLETED
    assert broker.counters.prepare_calls == call_count + 1
    assert broker.counters.execute_calls == 0
    assert broker.counters.execute_prepared_calls == call_count
    assert broker.counters.peak <= 2
    assert broker.counters.active == 0
    await _assert_no_native_tasks()


@pytest.mark.asyncio
async def test_native_adapter_experimental_cancel_awaits_workers_and_provider() -> None:
    broker = _TrackingBroker(block_execution=True)
    client = _ToolTurnClient(_calls(6))
    io = _RuntimeIO(broker)
    adapter = _adapter(
        client,
        broker,
        mode="experimental_heuristic",
        concurrency=2,
    )

    execution = asyncio.create_task(adapter.execute(_request(), io))
    await asyncio.wait_for(client.before_finish.wait(), timeout=1)
    await asyncio.wait_for(broker.peak_reached.wait(), timeout=1)
    io.cancelled = True

    outcome = await asyncio.wait_for(execution, timeout=2)
    assert outcome.kind is EngineOutcomeKind.CANCELLED
    await asyncio.wait_for(client.first_stream_closed.wait(), timeout=1)
    assert broker.counters.peak == 2
    assert broker.counters.active == 0
    assert broker.counters.cancelled == 2
    await _assert_no_native_tasks()


@pytest.mark.asyncio
async def test_native_adapter_rematerializes_every_large_result_across_prior_turns() -> None:
    execution_ids = [f"tool_large_result_{index}" for index in range(3)]
    messages: list[Msg] = [Msg(role="user", content="inspect three sources")]
    for index, execution_id in enumerate(execution_ids):
        call = ToolCall(
            id=f"provider-historical-{index}",
            name="calculator",
            arguments=f'{{"value":{index}}}',
            logical_key=f"native:turn:{index}:call:0",
        )
        messages.extend([
            Msg(role="assistant", tool_calls=[call]),
            Msg(
                role="tool",
                name=call.name,
                tool_call_id=call.id,
                content=f"large-original-{index}:" + "x" * 9_000,
                tool_execution_id=execution_id,
            ),
        ])
    state = LoopState(
        messages=messages,
        iters=3,
        model_call_count=3,
        tool_call_count=3,
        transition="next_turn",
        generation_counter=3,
        current_message_id="historical-message-2",
        current_generation_id="historical-generation-2",
        generation_reason="next_turn",
    )
    encoded = encode_native_checkpoint(state, "NEXT_TURN")
    encoded_tools = [
        message for message in encoded["messages"] if message["role"] == "tool"
    ]
    assert len(encoded_tools) == 3
    assert all(message["content_source"] == "LEDGER_RESULT" for message in encoded_tools)
    assert all(message["content"] is None for message in encoded_tools)
    assert "large-original" not in str(encoded)

    checkpoint = CheckpointRecord(
        checkpoint_id="cp_large_historical_results",
        run_id=_RUN_ID,
        activity_id=_ACTIVITY_ID,
        revision=11,
        working_state=WorkingState(goal="inspect three sources"),
        engine_state=encoded,
        created_at=2_300_000_000_011,
    )
    broker = _TrackingBroker()
    for index, execution_id in enumerate(execution_ids):
        broker.materialization_results[execution_id] = ToolResultEnvelope(
            status=ToolResultStatus.SUCCESS,
            preview=f"restored-result-{index}:" + "z" * 9_000,
            result_ref=f"{index + 1:064x}",
        )
    client = _FinalCaptureClient()
    io = _RuntimeIO(broker, initial_revision=checkpoint.revision)
    adapter = _adapter(client, broker)

    outcome = await adapter.execute(_request(checkpoint), io)

    assert outcome.kind is EngineOutcomeKind.COMPLETED
    assert broker.materialized_ids == execution_ids
    assert broker.counters.prepare_calls == 0
    assert broker.counters.execute_calls == 0
    assert broker.counters.execute_prepared_calls == 0

    # Every historical assistant call still has exactly one adjacent result in
    # the provider request.  Recovery scans the whole checkpoint, not its tail.
    restored = [message for message in client.requests[0] if message.role != "system"]
    expected_call_ids = [f"provider-historical-{index}" for index in range(3)]
    assert [
        call.id
        for message in restored
        for call in (message.tool_calls or [])
    ] == expected_call_ids
    restored_tools = [message for message in restored if message.role == "tool"]
    assert [message.tool_call_id for message in restored_tools] == expected_call_ids
    assert all(message.content for message in restored_tools)
    assert all(
        f"restored-result-{index}" in str(message.content)
        for index, message in enumerate(restored_tools)
    )

    # The next MODEL_REQUEST checkpoint immediately collapses all three large
    # values back to ledger refs; restart safety does not copy Artifact bytes.
    model_request = next(
        raw for raw in io.checkpoint_states if raw["phase"] == "MODEL_REQUEST"
    )
    persisted_tools = [
        message for message in model_request["messages"] if message["role"] == "tool"
    ]
    assert len(persisted_tools) == 3
    assert all(message["content_source"] == "LEDGER_RESULT" for message in persisted_tools)
    assert all(message["content"] is None for message in persisted_tools)
    assert "restored-result" not in str(model_request)


@pytest.mark.asyncio
async def test_native_adapter_enforces_model_output_limit_in_utf8_bytes() -> None:
    broker = _TrackingBroker()
    client = _FinalTextClient(["你", "好"])
    io = _RuntimeIO(broker)
    adapter = _adapter(
        client,
        broker,
        with_tools=False,
        native_max_model_output_bytes=5,
    )

    outcome = await adapter.execute(_request(), io)

    assert outcome.kind is EngineOutcomeKind.TERMINAL_FAILURE
    assert outcome.error_code == "MODEL_OUTPUT_LIMIT_EXCEEDED"
    assert [payload["delta"] for event, payload in io.events if event == "text"] == ["你"]
    assert io.final is None
    await _assert_no_native_tasks()


@pytest.mark.asyncio
async def test_native_adapter_rejects_oversized_checkpoint_before_provider_dispatch() -> None:
    broker = _TrackingBroker()
    client = _FinalTextClient(["unreachable"])
    io = _RuntimeIO(broker)
    adapter = _adapter(
        client,
        broker,
        with_tools=False,
        native_max_checkpoint_bytes=1,
    )

    with pytest.raises(RuntimeFault) as raised:
        await adapter.execute(_request(), io)

    assert raised.value.code == "CHECKPOINT_TOO_LARGE"
    assert client.stream_count == 0
    assert io.phases == []
    assert broker.counters.prepare_calls == 0
    await _assert_no_native_tasks()


@pytest.mark.asyncio
async def test_native_adapter_enforces_cumulative_per_run_tool_call_limit_after_restore() -> None:
    historical_call = ToolCall(
        id="provider-historical-0",
        name="calculator",
        arguments='{"value":0}',
        logical_key="native:turn:0:call:0",
    )
    state = LoopState(
        messages=[
            Msg(role="user", content="first turn"),
            Msg(role="assistant", tool_calls=[historical_call]),
            Msg(
                role="tool",
                name=historical_call.name,
                tool_call_id=historical_call.id,
                content='{"ok":true}',
            ),
        ],
        iters=1,
        model_call_count=1,
        tool_call_count=1,
        transition="next_turn",
        generation_counter=1,
        current_message_id="historical-message-0",
        current_generation_id="historical-generation-0",
        generation_reason="initial",
    )
    checkpoint = CheckpointRecord(
        checkpoint_id="cp_per_run_tool_limit",
        run_id=_RUN_ID,
        activity_id=_ACTIVITY_ID,
        revision=4,
        working_state=WorkingState(goal="use the tools, then answer"),
        engine_state=encode_native_checkpoint(state, "NEXT_TURN"),
        created_at=2_300_000_000_004,
    )
    broker = _TrackingBroker()
    client = _ToolTurnClient(_calls(1))
    io = _RuntimeIO(broker, initial_revision=checkpoint.revision)
    adapter = _adapter(
        client,
        broker,
        native_max_tool_calls_per_run=1,
    )

    outcome = await adapter.execute(_request(checkpoint), io)

    assert outcome.kind is EngineOutcomeKind.TERMINAL_FAILURE
    assert outcome.error_code == "TOOL_CALL_LIMIT_EXCEEDED"
    assert broker.counters.prepare_calls == 0
    assert broker.counters.execute_calls == 0
    assert broker.counters.execute_prepared_calls == 0
    await _assert_no_native_tasks()
