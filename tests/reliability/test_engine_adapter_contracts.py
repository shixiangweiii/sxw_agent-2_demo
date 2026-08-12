from __future__ import annotations

from tests.reliability.support.runtime_releases import activate_test_release

import uuid
from dataclasses import dataclass
from importlib.metadata import version
from typing import Any

import pytest
from google.adk.models import BaseLlm, LlmResponse
from google.genai import types
from pydantic import PrivateAttr

from agent.config import AgentSettings
from agent.context import AgentContext
from agent.runtime.adapters.filesystem_artifact import FilesystemArtifactStore
from agent.runtime.adapters.adk_engines import AdkEngineAdapter
from agent.runtime.adapters.brokered_tools import (
    build_runtime_tool_catalog,
    register_tool_catalog,
)
from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.runtime.application.admission import AdmissionService, CreateRunInput
from agent.runtime.application.coordinator import EngineRegistry, RunCoordinator
from agent.runtime.application.tool_broker import ToolBroker
from agent.runtime.domain.errors import AttemptOwnershipLost, RuntimeFault
from agent.runtime.domain.models import EventType, ReleaseManifest, RunStatus
from agent.runtime.worker.dispatcher import RuntimeWorker
from agent.claude_skill.execution_coordinator import SkillExecutionCoordinator
from agent.stream.event_converters import StreamEvent


@dataclass
class FakeClock:
    value: int = 2_300_000_000_000

    def now_ms(self) -> int:
        return self.value

    def monotonic(self) -> float:
        return self.value / 1000


class FakeAdkTextModel(BaseLlm):
    """Real ADK BaseLlm contract with deterministic, network-free SSE output."""

    _answer: str = PrivateAttr(default="")
    _calls: int = PrivateAttr(default=0)

    async def generate_content_async(self, llm_request, stream=False):
        self._calls += 1
        content = types.Content(
            role="model",
            parts=[types.Part.from_text(text=self._answer)],
        )
        # ADK 2.6.2 emits partial text for streaming plus a non-partial aggregate
        # response.  Runtime only commits the partial delta, preventing duplicate
        # output while ADK still sees its normal final model response.
        yield LlmResponse(content=content, partial=True)
        yield LlmResponse(
            content=content,
            partial=False,
            finish_reason=types.FinishReason.STOP,
        )


class FakeChat:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, **_kwargs: Any) -> str:
        self.calls.append((system, user))
        return "直接回答用户问题"


class FakeAdkToolModel(BaseLlm):
    """One Tool call followed by a final answer for real ADK boundary tests."""

    _tool_name: str = PrivateAttr(default="controlled_tool")
    _calls: int = PrivateAttr(default=0)

    async def generate_content_async(self, llm_request, stream=False):
        self._calls += 1
        if self._calls == 1:
            part = types.Part.from_function_call(name=self._tool_name, args={"value": 1})
            part.function_call.id = "framework-control-call"
            yield LlmResponse(
                content=types.Content(role="model", parts=[part]),
                partial=False,
                finish_reason=types.FinishReason.STOP,
            )
            return
        yield LlmResponse(
            content=types.Content(
                role="model", parts=[types.Part.from_text(text="recovered answer")],
            ),
            partial=False,
            finish_reason=types.FinishReason.STOP,
        )


def _context(answer: str) -> tuple[AgentContext, FakeAdkTextModel, FakeChat]:
    model = FakeAdkTextModel(model="fake-adk-text")
    model._answer = answer
    chat = FakeChat()
    settings = AgentSettings(
        _env_file=None,
        max_loop_iters=3,
        sub_agent_engine="native",
        trace_enabled=False,
    )
    return (
        AgentContext(
            settings=settings,
            llm=model,  # type: ignore[arg-type] - BaseLlm is the official ADK port.
            chat=chat,  # type: ignore[arg-type] - deterministic AgentChatClient port.
            tools=[],
            skill_coordinator=SkillExecutionCoordinator(1),
        ),
        model,
        chat,
    )


async def _unused_artifact_metadata(_artifact_id: str) -> dict[str, Any]:
    raise AssertionError("no-tool/no-attachment contract smoke must not load artifacts")


async def _admit(store, clock, *, engine: str, key: str):
    return (await AdmissionService(
        store, clock=clock, default_deadline_ms=60_000,
    ).create(
        CreateRunInput(
            client_request_id=str(uuid.uuid4()),
            conversation_id=None,
            principal_id="demo-user",
            agent_id="demo-agent",
            engine=engine,
            text=f"{engine} deterministic text request",
            attachment_refs=(),
            deadline_at=None,
        ),
        idempotency_key=key,
    )).run


@pytest.mark.asyncio
async def test_rel_30_two_real_adk_adapters_share_explicit_outcome_contract(
    tmp_path, monkeypatch,
):
    """Admission -> Worker -> real build_engine -> terminal, without network/tools."""
    assert version("google-adk") == "2.6.2"

    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    releases = await store.activate_current_releases(
        [
            ReleaseManifest(engine=engine, components={"rel-030": "fake-llm-v1"})
            for engine in ("plan_execute", "agent_loop", "native_loop")
        ],
    )
    clock = FakeClock()
    artifact_store = FilesystemArtifactStore(tmp_path / "artifacts")

    import agent.runtime.adapters.adk_engines as adk_module

    real_build_engine = adk_module.build_engine
    built: dict[str, str] = {}

    def tracked_build_engine(context, engine):
        instance = real_build_engine(context, engine)
        built[engine] = type(instance).__name__
        return instance

    monkeypatch.setattr(adk_module, "build_engine", tracked_build_engine)

    adapters = {}
    models: dict[str, FakeAdkTextModel] = {}
    chats: dict[str, FakeChat] = {}
    runs = {}
    for engine in ("plan_execute", "agent_loop"):
        context, model, chat = _context(f"answer:{engine}")
        models[engine] = model
        chats[engine] = chat
        adapters[engine] = AdkEngineAdapter(
            engine=engine,
            context=context,
            release_fingerprint=releases[engine],
            artifact_store=artifact_store,
            artifact_metadata_loader=_unused_artifact_metadata,
            tool_broker=None,
        )
        runs[engine] = await _admit(
            store, clock, engine=engine, key=f"rel-030-{engine}",
        )

    worker = RuntimeWorker(
        store=store,
        coordinator=RunCoordinator(
            store,
            EngineRegistry(adapters),
            clock=clock,
            event_flush_bytes=1,
        ),
        worker_id="rel-030-worker",
        release_map=releases,
        concurrency=1,
        clock=clock,
    )
    for _ in range(2):
        assert await worker.run_once() is True
    assert await worker.run_once() is False

    assert built == {
        "plan_execute": "PlanExecuteEngine",
        "agent_loop": "AgentLoopEngine",
    }
    assert models["plan_execute"]._calls == 1
    assert models["agent_loop"]._calls == 1
    assert len(chats["plan_execute"].calls) == 1
    assert not chats["agent_loop"].calls

    for engine, run in runs.items():
        persisted = await store.get_run(run.envelope.run_id)
        assert persisted.terminal_status is RunStatus.SUCCEEDED
        events = await store.list_events(run.envelope.run_id, visibility=None)
        assert sum(e.event_type is EventType.RUN_TERMINATED for e in events) == 1
        assert sum(
            e.event_type is EventType.ASSISTANT_MESSAGE_COMMITTED for e in events
        ) == 1
        assert any(
            e.event_type is EventType.OUTPUT_DELTA_COMMITTED
            and e.payload == {"delta": f"answer:{engine}"}
            for e in events
        )
        assert not any(
            e.event_type is EventType.MODEL_MESSAGE_COMMITTED
            and "engine_error" in (e.payload or {})
            for e in events
        )
        # EventType has no done/error terminal projection; success exists
        # only because the per-attempt engine_outcome reached Coordinator.
        assert all(e.event_type.value not in {"done", "error"} for e in events)


@pytest.mark.asyncio
async def test_rel_30_text_then_eof_without_explicit_outcome_fails_closed(
    tmp_path, monkeypatch,
):
    """A plausible text stream is insufficient to infer SUCCEEDED from EOF."""
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    release = await activate_test_release(store,
        ReleaseManifest(engine="agent_loop", components={"rel-030": "missing-outcome"}),
    )
    clock = FakeClock()
    context, _model, _chat = _context("unused")

    class EofOnlyReasoningEngine:
        async def run_stream(self, _rc):
            yield StreamEvent("text", {"delta": "looks complete but is not authoritative"})

    import agent.runtime.adapters.adk_engines as adk_module

    monkeypatch.setattr(
        adk_module,
        "build_engine",
        lambda _context, _engine: EofOnlyReasoningEngine(),
    )
    adapter = AdkEngineAdapter(
        engine="agent_loop",
        context=context,
        release_fingerprint=release,
        artifact_store=FilesystemArtifactStore(tmp_path / "artifacts"),
        artifact_metadata_loader=_unused_artifact_metadata,
        tool_broker=None,
    )
    run = await _admit(store, clock, engine="agent_loop", key="rel-030-eof")
    worker = RuntimeWorker(
        store=store,
        coordinator=RunCoordinator(
            store,
            EngineRegistry({"agent_loop": adapter}),
            clock=clock,
            event_flush_bytes=1,
        ),
        worker_id="rel-030-eof-worker",
        release_map={"agent_loop": release},
        concurrency=1,
        clock=clock,
    )
    assert await worker.run_once() is True

    persisted = await store.get_run(run.envelope.run_id)
    assert persisted.terminal_status is RunStatus.FAILED
    assert persisted.terminal_payload["code"] == "ENGINE_OUTCOME_MISSING"
    events = await store.list_events(run.envelope.run_id, visibility=None)
    assert any(e.event_type is EventType.OUTPUT_DELTA_COMMITTED for e in events)
    assert not any(
        e.event_type is EventType.ASSISTANT_MESSAGE_COMMITTED for e in events
    )
    assert sum(e.event_type is EventType.RUN_TERMINATED for e in events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["plan_execute", "agent_loop"])
@pytest.mark.parametrize("failure_kind", ["runtime", "ownership", "ordinary"])
async def test_real_adk_adapter_control_fault_and_ordinary_error_boundaries(
    tmp_path, engine, failure_kind,
) -> None:
    """Both ADK engines preserve control faults and still consume ordinary failures."""

    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / f"{engine}-{failure_kind}.db"))
    await store.initialize()
    release = await activate_test_release(
        store,
        ReleaseManifest(engine=engine, components={"adk-tool-fault": failure_kind}),
    )
    run = await _admit(
        store, FakeClock(), engine=engine, key=f"tool-{engine}-{failure_kind}",
    )
    activity = await store.claim_next(
        release_map={engine: release},
        worker_id="control-worker",
        lease_ms=30_000,
        now_ms=FakeClock().now_ms(),
    )
    assert activity is not None
    running = await store.mark_activity_running(
        activity.activity.activity_id,
        worker_id="control-worker",
        fencing_token=activity.activity.fencing_token,
        now_ms=FakeClock().now_ms(),
    )

    async def calculator(value: int) -> dict[str, int]:
        """Exercise the Tool failure boundary without network access."""

        if failure_kind == "runtime":
            raise RuntimeFault("EVIDENCE_CONTRACT_INVALID", "invalid provenance", 500)
        if failure_kind == "ownership":
            raise AttemptOwnershipLost(
                "ACTIVITY_LEASE_EXPIRED", "replacement Worker owns this attempt",
            )
        raise ValueError("ordinary tool failure")

    context, _text_model, chat = _context("unused")
    model = FakeAdkToolModel(model=f"fake-{engine}-{failure_kind}-tool")
    model._tool_name = "calculator"
    context.llm = model  # type: ignore[assignment]
    context.tools = [calculator]
    if engine == "plan_execute":
        async def fixed_plan(*_args, **_kwargs):
            return "调用工具"

        chat.complete = fixed_plan  # type: ignore[method-assign]

    from agent.engine.loop_tools.catalog import collect_loop_tools

    catalog_tools = (
        collect_loop_tools(context, run_engine=engine)
        if engine == "agent_loop"
        else list(context.tools)
    )
    broker = ToolBroker(
        store,
        FilesystemArtifactStore(tmp_path / f"{engine}-{failure_kind}-tool-artifacts"),
        clock=FakeClock(),
    )
    register_tool_catalog(broker, build_runtime_tool_catalog(catalog_tools))

    adapter = AdkEngineAdapter(
        engine=engine,
        context=context,
        release_fingerprint=release,
        artifact_store=FilesystemArtifactStore(
            tmp_path / f"{engine}-{failure_kind}-artifacts",
        ),
        artifact_metadata_loader=_unused_artifact_metadata,
        tool_broker=broker,
    )

    from agent.runtime.application.events import CommittedEventSink

    sink = CommittedEventSink(
        store,
        run_id=run.envelope.run_id,
        activity_id=running.activity_id,
        fencing_token=running.fencing_token,
        deadline_at_ms=run.envelope.deadline_at,
        clock=FakeClock(),
        tool_broker=broker,
    )
    from agent.runtime.ports.engine import EngineRunRequest

    request = EngineRunRequest(
        envelope=run.envelope,
        activity_id=running.activity_id,
        fencing_token=running.fencing_token,
        attempt=running.attempt,
        input_text=f"{engine} deterministic text request",
        history=(),
        checkpoint=None,
        resume_payload=None,
    )
    if failure_kind in {"runtime", "ownership"}:
        expected_type = RuntimeFault if failure_kind == "runtime" else AttemptOwnershipLost
        expected_code = (
            "EVIDENCE_CONTRACT_INVALID"
            if failure_kind == "runtime"
            else "ACTIVITY_LEASE_EXPIRED"
        )
        with pytest.raises(expected_type) as raised:
            await adapter.execute(request, sink)
        assert raised.value.code == expected_code
        assert model._calls == 1
    else:
        outcome = await adapter.execute(request, sink)
        assert outcome.kind.value == "COMPLETED"
        # The ordinary failure was projected as a function response, allowing
        # the same real ADK loop to request and commit its final model answer.
        assert model._calls == 2


@pytest.mark.asyncio
async def test_adk_adapter_does_not_guess_arbitrary_runtime_error_is_control_fault() -> None:
    from agent.runtime.adapters.adk_engines import _runtime_control_fault

    wrapper = RuntimeError("ordinary ADK framework error")
    wrapper.__cause__ = ValueError("ordinary tool failure")
    assert _runtime_control_fault(wrapper) is None
