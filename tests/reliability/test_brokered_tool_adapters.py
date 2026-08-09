from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from importlib.metadata import version
from types import SimpleNamespace

import pytest
from google.adk.agents import LlmAgent
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.models import BaseLlm, LlmResponse
from google.adk.plugins import BasePlugin
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool
from google.genai import types
from pydantic import PrivateAttr

from agent.config import AgentSettings
from agent.engine.base import RunContext
from agent.engine.loop_tools.task_plan_tool import update_task_plan
from agent.engine.native_loop.tools import NativeToolContext, ToolRegistry, ToolSpec
from agent.runtime.adapters.brokered_tools import (
    AdkToolBatch,
    BrokeredAdkTool,
    broker_adk_tools,
    broker_native_registry,
)
from agent.runtime.adapters.filesystem_artifact import FilesystemArtifactStore
from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.runtime.application.admission import AdmissionService, CreateRunInput
from agent.runtime.application.events import CommittedEventSink
from agent.runtime.application.tool_broker import ToolBroker
from agent.runtime.domain.errors import RuntimeFault
from agent.runtime.domain.models import (
    EventType,
    ReleaseManifest,
    ToolEffectClass,
    ToolResultEnvelope,
    ToolResultStatus,
    WorkingState,
    sha256_json,
)
from agent.runtime.ports.store import ToolExecutionPreparation
from agent.skills.request_context import (
    SkillRequestContext,
    get_request_context,
    reset_request_context,
    set_request_context,
)


@dataclass
class FakeClock:
    value: int = 1_800_000_000_000

    def now_ms(self) -> int:
        return self.value

    def monotonic(self) -> float:
        return self.value / 1000


async def _runtime_context(tmp_path, *, engine: str = "native_loop"):
    clock = FakeClock()
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    release = await store.register_release(
        ReleaseManifest(engine=engine, components={"broker-wrapper-test": "v1"}),
        activate=True,
    )
    admitted = await AdmissionService(
        store,
        clock=clock,
        default_deadline_ms=60_000,
    ).create(
        CreateRunInput(
            client_request_id=str(uuid.uuid4()),
            conversation_id=None,
            principal_id="demo-user",
            agent_id="demo-agent",
            engine=engine,
            text="exercise broker wrapper",
            attachment_refs=(),
            deadline_at=None,
        ),
        idempotency_key=f"wrapper-{engine}-{uuid.uuid4()}",
    )
    claim = await store.claim_next(
        worker_id="wrapper-worker",
        lease_ms=30_000,
        now_ms=clock.now_ms(),
        engines=(engine,),
    )
    assert claim is not None
    activity = await store.mark_activity_running(
        claim.activity.activity_id,
        worker_id="wrapper-worker",
        fencing_token=claim.activity.fencing_token,
        now_ms=clock.now_ms(),
    )
    broker = ToolBroker(
        store,
        FilesystemArtifactStore(tmp_path / "artifacts"),
        clock=clock,
    )
    rc = RunContext(
        run_id=admitted.run.envelope.run_id,
        activity_id=activity.activity_id,
        engine=engine,
        agent_uuid="demo-agent",
        user_id="demo-user",
        session_id=admitted.run.envelope.conversation_id,
        user_message=types.Content(
            role="user",
            parts=[types.Part.from_text(text="exercise broker wrapper")],
        ),
        settings=AgentSettings(_env_file=None),
        deadline_at_ms=admitted.run.envelope.deadline_at,
        tool_broker=broker,
        fencing_token=activity.fencing_token,
        release_fingerprint=release,
    )
    assert admitted.run.envelope.release_fingerprint == release
    return store, rc


def _native_spec(name: str, invoke) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"test {name}",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
        },
        run=invoke,
        concurrency_safe=True,
    )


def _adk_response(*calls: tuple[str, str, dict]) -> SimpleNamespace:
    parts = []
    for framework_id, name, arguments in calls:
        part = types.Part.from_function_call(name=name, args=arguments)
        part.function_call.id = framework_id
        parts.append(part)
    return SimpleNamespace(
        partial=False,
        content=types.Content(role="model", parts=parts),
    )


@pytest.mark.asyncio
async def test_native_wrapper_replays_committed_slot_without_redispatch(tmp_path):
    store, rc = await _runtime_context(tmp_path)
    calls: list[tuple[dict, str]] = []

    async def calculator(args, context):
        calls.append((dict(args), context.function_call_id))
        return {"doubled": args["value"] * 2}

    registry = broker_native_registry(
        ToolRegistry([_native_spec("calculator", calculator)]),
        rc,
    )
    wrapped = registry.get("calculator")
    assert wrapped is not None
    logical_key = "native:model:0:call:0"

    first = await wrapped.run(
        {"value": 7},
        NativeToolContext(
            function_call_id="native-framework-first",
            invocation_id=rc.run_id,
            logical_key=logical_key,
        ),
    )
    replay = await wrapped.run(
        {"value": 7},
        NativeToolContext(
            # Framework ids may differ after process recovery; logical slot is authority.
            function_call_id="native-framework-after-restart",
            invocation_id=rc.run_id,
            logical_key=logical_key,
        ),
    )

    assert first == replay == {"doubled": 14}
    assert calls == [({"value": 7}, "native-framework-first")]
    events = await store.list_events(rc.run_id, visibility=None)
    tool_calls = [event for event in events if event.event_type is EventType.TOOL_CALL_COMMITTED]
    tool_results = [event for event in events if event.event_type is EventType.TOOL_RESULT_COMMITTED]
    assert len(tool_calls) == len(tool_results) == 1
    assert tool_calls[0].payload["logical_key"] == logical_key
    execution = await store.get_tool_execution(tool_calls[0].tool_execution_id)
    assert execution["effect_status"] == "COMMITTED"
    assert execution["attempt"] == 1


@pytest.mark.asyncio
async def test_native_wrapper_preserves_external_only_success_on_committed_replay(tmp_path):
    _store, rc = await _runtime_context(tmp_path)
    calls = 0

    async def create_external(_args, _context):
        nonlocal calls
        calls += 1
        return ToolResultEnvelope(
            status=ToolResultStatus.SUCCESS,
            external_object_id="provider-object-42",
        )

    wrapped = broker_native_registry(
        ToolRegistry([_native_spec("calculator", create_external)]), rc,
    ).get("calculator")
    assert wrapped is not None
    context = NativeToolContext(
        function_call_id="native-external-first",
        invocation_id=rc.run_id,
        logical_key="native:model:external:0",
    )

    first = await wrapped.run({"value": 1}, context)
    replay = await wrapped.run({"value": 1}, context)

    assert first == replay == {
        "content": None,
        "external_object_id": "provider-object-42",
    }
    assert calls == 1


@pytest.mark.asyncio
async def test_adk_wrapper_does_not_overwrite_preview_external_object_key(tmp_path):
    store, rc = await _runtime_context(tmp_path, engine="agent_loop")

    class ExternalResultBroker:
        def __init__(self):
            self.store = store
            self.clock = rc.tool_broker.clock

        async def execute(self, **_kwargs):
            return ToolResultEnvelope(
                status=ToolResultStatus.SUCCESS,
                preview={"external_object_id": "user-preview-value", "value": 7},
                external_object_id="provider-authority-value",
            )

    rc.tool_broker = ExternalResultBroker()

    async def calculator(value: int) -> dict[str, int]:
        """Return a value; the fake Broker supplies the committed projection."""
        return {"value": value}

    batch = AdkToolBatch(rc)
    wrapped = broker_adk_tools([FunctionTool(calculator)], rc, batch=batch)[0]
    await batch.prepare_model_response(
        _adk_response(("adk-external-0", "calculator", {"value": 7})),
        turn_ordinal=0,
    )
    projected = await wrapped.run_async(
        args={"value": 7},
        tool_context=SimpleNamespace(function_call_id="adk-external-0"),
    )

    assert projected == {
        "content": {"external_object_id": "user-preview-value", "value": 7},
        "external_object_id": "provider-authority-value",
    }


@pytest.mark.asyncio
async def test_adk_plan_tool_persists_runtime_working_state_once(tmp_path):
    store, rc = await _runtime_context(tmp_path, engine="agent_loop")
    clock = FakeClock()
    sink = CommittedEventSink(
        store,
        run_id=rc.run_id,
        activity_id=rc.activity_id,
        fencing_token=rc.fencing_token,
        deadline_at_ms=rc.deadline_at_ms,
        clock=clock,
    )
    rc.runtime_io = sink
    rc.runtime_working_state = WorkingState(
        goal="make a plan",
        release_fingerprint=rc.release_fingerprint,
    )
    batch = AdkToolBatch(rc)
    wrapped = broker_adk_tools(
        [FunctionTool(update_task_plan)], rc, batch=batch,
    )[0]
    tool_context = SimpleNamespace(function_call_id="adk-plan-0", state={})
    arguments = {"steps": ["inspect", "answer"], "current_step": 1}
    await batch.prepare_model_response(
        _adk_response(("adk-plan-0", "update_task_plan", arguments)),
        turn_ordinal=0,
    )

    first = await wrapped.run_async(args=arguments, tool_context=tool_context)
    replay = await wrapped.run_async(args=arguments, tool_context=tool_context)
    await sink.close()

    assert first == replay
    checkpoint = await store.latest_checkpoint(rc.run_id)
    assert checkpoint is not None
    assert checkpoint.revision == 1
    assert checkpoint.working_state.model_plan == [
        {"step": 1, "title": "inspect", "status": "running"},
        {"step": 2, "title": "answer", "status": "planned"},
    ]
    events = await store.list_events(rc.run_id, visibility=None)
    plan_events = [event for event in events if event.event_type is EventType.MODEL_PLAN_UPDATED]
    assert [event.payload["title"] for event in plan_events] == ["inspect", "answer"]


@pytest.mark.asyncio
async def test_native_wrapper_fails_closed_when_stable_slot_changes(tmp_path):
    _store, rc = await _runtime_context(tmp_path)
    calculator_calls = 0
    stats_calls = 0

    async def calculator(args, _context):
        nonlocal calculator_calls
        calculator_calls += 1
        return args["value"] * 2

    async def text_stats(args, _context):
        nonlocal stats_calls
        stats_calls += 1
        return args["value"]

    registry = broker_native_registry(
        ToolRegistry(
            [
                _native_spec("calculator", calculator),
                _native_spec("text_stats", text_stats),
            ]
        ),
        rc,
    )
    calculator_tool = registry.get("calculator")
    text_stats_tool = registry.get("text_stats")
    assert calculator_tool is not None and text_stats_tool is not None
    context = NativeToolContext(
        function_call_id="native-call-0",
        invocation_id=rc.run_id,
        logical_key="native:model:0:call:0",
    )
    await calculator_tool.run({"value": 3}, context)

    with pytest.raises(RuntimeFault) as changed_arguments:
        await calculator_tool.run({"value": 4}, context)
    assert changed_arguments.value.code == "TOOL_REPLAY_MISMATCH"

    with pytest.raises(RuntimeFault) as changed_tool:
        await text_stats_tool.run({"value": 3}, context)
    assert changed_tool.value.code == "TOOL_REPLAY_MISMATCH"
    assert calculator_calls == 1
    assert stats_calls == 0


@pytest.mark.asyncio
async def test_adk_function_tool_wrapper_reuses_prepared_turn_call_slot(tmp_path):
    store, rc = await _runtime_context(tmp_path, engine="agent_loop")
    calls = 0
    observed_contexts: list[tuple[str, str]] = []

    async def calculator(value: int) -> dict[str, int]:
        """Return a deterministic calculation."""

        nonlocal calls
        calls += 1
        request_context = get_request_context()
        observed_contexts.append(
            (request_context.idempotency_key, request_context.activity_id)
        )
        return {"tripled": value * 3}

    original = FunctionTool(calculator)
    batch = AdkToolBatch(rc)
    wrapped_tools = broker_adk_tools([original], rc, batch=batch)
    assert len(wrapped_tools) == 1
    wrapped = wrapped_tools[0]
    assert isinstance(wrapped, BrokeredAdkTool)
    assert wrapped.name == original.name
    assert wrapped._get_declaration() == original._get_declaration()  # noqa: SLF001

    # A pure FunctionTool does not require a full ADK invocation context. The wrapper only
    # consumes function_call_id, whose stable callback slot is what this contract exercises.
    tool_context = SimpleNamespace(function_call_id="adk-framework-call-42")
    await batch.prepare_model_response(
        _adk_response(("adk-framework-call-42", "calculator", {"value": 5})),
        turn_ordinal=0,
    )
    context_token = set_request_context(SkillRequestContext(
        agent_uuid="demo-agent",
        user_id="demo-user",
        session_id=rc.session_id,
        run_id=rc.run_id,
        activity_id=rc.activity_id,
        idempotency_key="run-level-key-must-not-reach-tool",
    ))
    try:
        first = await wrapped.run_async(args={"value": 5}, tool_context=tool_context)
        # A recovered whole ADK invocation can receive a different provider id.
        # Rebuild the attempt-local correlation map while retaining the same
        # stable turn/call identity in Runtime Store.
        recovered_batch = AdkToolBatch(rc)
        recovered_wrapped = broker_adk_tools(
            [original], rc, batch=recovered_batch,
        )[0]
        await recovered_batch.prepare_model_response(
            _adk_response(("adk-framework-after-restart", "calculator", {"value": 5})),
            turn_ordinal=0,
        )
        replay = await recovered_wrapped.run_async(
            args={"value": 5},
            tool_context=SimpleNamespace(
                function_call_id="adk-framework-after-restart",
            ),
        )
        assert get_request_context().idempotency_key == "run-level-key-must-not-reach-tool"
    finally:
        reset_request_context(context_token)

    assert first == replay == {"tripled": 15}
    assert calls == 1
    assert len(observed_contexts) == 1
    assert observed_contexts[0][0].startswith("tool_")
    assert observed_contexts[0][1].startswith("act_")
    assert observed_contexts[0][1] != rc.activity_id
    events = await store.list_events(rc.run_id, visibility=None)
    tool_call = next(
        event for event in events if event.event_type is EventType.TOOL_CALL_COMMITTED
    )
    assert tool_call.payload["logical_key"] == "adk:turn:0:call:0"
    assert tool_call.payload["framework_call_id"] == "adk-framework-call-42"
    assert tool_call.payload["name"] == "calculator"
    execution = await store.get_tool_execution(tool_call.tool_execution_id)
    assert execution["effect_status"] == "COMMITTED"
    assert execution["attempt"] == 1


@pytest.mark.asyncio
async def test_adk_complete_batch_flushes_text_and_prepares_all_slots_before_execution(
    tmp_path,
):
    store, rc = await _runtime_context(tmp_path, engine="plan_execute")
    sink = CommittedEventSink(
        store,
        run_id=rc.run_id,
        activity_id=rc.activity_id,
        fencing_token=rc.fencing_token,
        deadline_at_ms=rc.deadline_at_ms,
        clock=rc.tool_broker.clock,
    )
    rc.runtime_io = sink
    executed: list[str] = []

    async def calculator(value: int) -> dict[str, int]:
        """Return a deterministic calculation."""
        executed.append("calculator")
        return {"value": value * 2}

    async def text_stats(value: int) -> dict[str, int]:
        """Return a deterministic statistic."""
        executed.append("text_stats")
        return {"value": value}

    batch = AdkToolBatch(rc)
    wrapped = broker_adk_tools(
        [FunctionTool(calculator), FunctionTool(text_stats)],
        rc,
        batch=batch,
    )
    await sink.emit("text", {"delta": "committed before calls"})
    await batch.prepare_model_response(
        _adk_response(
            ("framework-a", "calculator", {"value": 2}),
            ("framework-b", "text_stats", {"value": 3}),
        ),
        turn_ordinal=4,
    )

    assert executed == []
    events = await store.list_events(rc.run_id, visibility=None)
    delta = next(event for event in events if event.event_type is EventType.OUTPUT_DELTA_COMMITTED)
    calls = [event for event in events if event.event_type is EventType.TOOL_CALL_COMMITTED]
    assert [event.payload["logical_key"] for event in calls] == [
        "adk:turn:4:call:0",
        "adk:turn:4:call:1",
    ]
    assert delta.seq < calls[0].seq < calls[1].seq
    for event in calls:
        execution = await store.get_tool_execution(event.tool_execution_id)
        assert execution["effect_status"] == "PREPARED"

    # Deliberately enter the wrappers in reverse order: provider ids only
    # correlate callbacks; callback scheduling never assigns stable identity.
    results = await asyncio.gather(
        wrapped[1].run_async(
            args={"value": 3},
            tool_context=SimpleNamespace(function_call_id="framework-b"),
        ),
        wrapped[0].run_async(
            args={"value": 2},
            tool_context=SimpleNamespace(function_call_id="framework-a"),
        ),
    )
    await sink.close()
    assert results == [{"value": 3}, {"value": 4}]
    assert sorted(executed) == ["calculator", "text_stats"]


@pytest.mark.asyncio
async def test_store_batch_replay_mismatch_rolls_back_every_new_slot(tmp_path):
    store, rc = await _runtime_context(tmp_path, engine="agent_loop")
    original = {"value": 7}
    await store.prepare_tool_execution(
        run_id=rc.run_id,
        parent_activity_id=rc.activity_id,
        fencing_token=rc.fencing_token,
        logical_key="adk:turn:0:call:1",
        tool_name="calculator",
        release_digest="calculator:v1",
        effect_class=ToolEffectClass.READ_ONLY,
        request_digest=sha256_json(original),
        request=original,
        now_ms=rc.tool_broker.clock.now_ms(),
    )
    before = await store.list_events(rc.run_id, visibility=None)

    with pytest.raises(RuntimeFault) as mismatch:
        await store.prepare_tool_execution_batch(
            run_id=rc.run_id,
            parent_activity_id=rc.activity_id,
            fencing_token=rc.fencing_token,
            preparations=(
                ToolExecutionPreparation(
                    logical_key="adk:turn:0:call:0",
                    tool_name="text_stats",
                    release_digest="text_stats:v1",
                    effect_class=ToolEffectClass.READ_ONLY.value,
                    request_digest=sha256_json({"value": 1}),
                    request={"value": 1},
                    framework_call_id="new-first-slot",
                ),
                ToolExecutionPreparation(
                    logical_key="adk:turn:0:call:1",
                    tool_name="calculator",
                    release_digest="calculator:v1",
                    effect_class=ToolEffectClass.READ_ONLY.value,
                    request_digest=sha256_json({"value": 8}),
                    request={"value": 8},
                    framework_call_id="changed-existing-slot",
                ),
            ),
            now_ms=rc.tool_broker.clock.now_ms(),
        )
    assert mismatch.value.code == "TOOL_REPLAY_MISMATCH"
    after = await store.list_events(rc.run_id, visibility=None)
    assert [(event.seq, event.event_type) for event in after] == [
        (event.seq, event.event_type) for event in before
    ]
    assert [
        event.payload["logical_key"]
        for event in after
        if event.event_type is EventType.TOOL_CALL_COMMITTED
    ] == ["adk:turn:0:call:1"]


@pytest.mark.asyncio
async def test_adk_prepared_slot_fails_closed_on_name_or_args_drift(tmp_path):
    _store, rc = await _runtime_context(tmp_path, engine="agent_loop")
    calls = 0

    async def calculator(value: int) -> int:
        """Return a deterministic calculation."""
        nonlocal calls
        calls += 1
        return value * 2

    async def text_stats(value: int) -> int:
        """Return a deterministic statistic."""
        nonlocal calls
        calls += 1
        return value

    batch = AdkToolBatch(rc)
    calculator_tool, stats_tool = broker_adk_tools(
        [FunctionTool(calculator), FunctionTool(text_stats)], rc, batch=batch,
    )
    await batch.prepare_model_response(
        _adk_response(("framework-slot", "calculator", {"value": 3})),
        turn_ordinal=0,
    )
    context = SimpleNamespace(function_call_id="framework-slot")

    with pytest.raises(RuntimeFault) as changed_arguments:
        await calculator_tool.run_async(args={"value": 4}, tool_context=context)
    assert changed_arguments.value.code == "TOOL_REPLAY_MISMATCH"

    with pytest.raises(RuntimeFault) as changed_tool:
        await stats_tool.run_async(args={"value": 3}, tool_context=context)
    assert changed_tool.value.code == "TOOL_REPLAY_MISMATCH"
    assert calls == 0


@pytest.mark.asyncio
async def test_adk_262_aggregate_after_model_precedes_every_tool_callback():
    """Pin the private ordering assumption used by durable batch preparation."""
    assert version("google-adk") == "2.6.2"
    order: list[str] = []

    def function_part(name: str, arguments: dict, framework_id: str) -> types.Part:
        part = types.Part.from_function_call(name=name, args=arguments)
        part.function_call.id = framework_id
        return part

    class TwoTurnModel(BaseLlm):
        _calls: int = PrivateAttr(default=0)

        async def generate_content_async(self, llm_request, stream=False):
            self._calls += 1
            if self._calls == 1:
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[types.Part.from_text(text="preface")],
                    ),
                    partial=True,
                )
                yield LlmResponse(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part.from_text(text="preface"),
                            function_part("first_tool", {"value": 1}, "framework-1"),
                            function_part("second_tool", {"value": 2}, "framework-2"),
                        ],
                    ),
                    partial=False,
                    finish_reason=types.FinishReason.STOP,
                )
                return
            yield LlmResponse(
                content=types.Content(
                    role="model", parts=[types.Part.from_text(text="done")],
                ),
                partial=False,
                finish_reason=types.FinishReason.STOP,
            )

    class OrderPlugin(BasePlugin):
        def __init__(self) -> None:
            super().__init__(name="adk_order_contract")

        async def after_model_callback(self, *, callback_context, llm_response):
            if llm_response.partial:
                return None
            names = [
                part.function_call.name
                for part in llm_response.content.parts
                if part.function_call is not None
            ]
            order.append("after_model:" + ",".join(names))
            return None

        async def before_tool_callback(self, *, tool, tool_args, tool_context):
            order.append(f"before_tool:{tool.name}")
            return None

    async def first_tool(value: int) -> dict[str, int]:
        """Return the first value."""
        order.append("execute:first_tool")
        return {"value": value}

    async def second_tool(value: int) -> dict[str, int]:
        """Return the second value."""
        order.append("execute:second_tool")
        return {"value": value}

    sessions = InMemorySessionService()
    await sessions.create_session(app_name="adk-contract", user_id="u", session_id="s")
    runner = Runner(
        app_name="adk-contract",
        agent=LlmAgent(
            name="order_agent",
            model=TwoTurnModel(model="fake-order-model"),
            tools=[first_tool, second_tool],
        ),
        session_service=sessions,
        plugins=[OrderPlugin()],
    )
    async for _event in runner.run_async(
        user_id="u",
        session_id="s",
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text="call both")],
        ),
        run_config=RunConfig(
            streaming_mode=StreamingMode.SSE,
            max_llm_calls=3,
        ),
    ):
        pass

    assert order[0] == "after_model:first_tool,second_tool"
    first_tool_boundary = min(
        index
        for index, marker in enumerate(order)
        if marker.startswith(("before_tool:", "execute:"))
    )
    assert first_tool_boundary > 0
