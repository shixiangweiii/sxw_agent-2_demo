from __future__ import annotations

from tests.reliability.support.runtime_releases import activate_test_release

import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from agent.config import AgentSettings
from agent.engine.native_loop.engine import NativeLoopAdapter
from agent.engine.native_loop.llm_client import TextDelta, ToolCallReady, TurnEnd
from agent.engine.native_loop.messages import ToolCall
from agent.engine.native_loop.tools import NativeToolContext, ToolRegistry, ToolSpec
from agent.runtime.adapters.filesystem_artifact import FilesystemArtifactStore
from agent.runtime.adapters.brokered_tools import build_runtime_tool_catalog
from agent.runtime.adapters.sqlite import RuntimeDatabase, SqliteRuntimeStore
from agent.runtime.application.admission import AdmissionService, CreateRunInput
from agent.runtime.application.coordinator import EngineRegistry, RunCoordinator
from agent.runtime.application.tool_broker import ToolBroker
from agent.runtime.domain.models import EventType, ReleaseManifest, RunStatus


@dataclass
class _Clock:
    value: int = 2_350_000_000_000

    def now_ms(self) -> int:
        return self.value

    def monotonic(self) -> float:
        return self.value / 1000

    def advance(self, milliseconds: int) -> None:
        self.value += milliseconds


class _NoJitter:
    def uniform(self, _low: float, _high: float) -> float:
        return 1.0


class _CrashAfterCompletedCheckpointStore(SqliteRuntimeStore):
    """Commit COMPLETED, then emulate process loss before terminal commit."""

    def __init__(self, database: RuntimeDatabase, *, crash_once: bool) -> None:
        super().__init__(database)
        self.crash_once = crash_once
        self.crashed = False

    async def save_checkpoint(self, **kwargs: Any):
        checkpoint = await super().save_checkpoint(**kwargs)
        phase = (kwargs.get("engine_state") or {}).get("phase")
        if self.crash_once and not self.crashed and phase == "COMPLETED":
            self.crashed = True
            raise OSError("fault injection after durable COMPLETED checkpoint")
        return checkpoint


class _IntermediateToolFinalClient:
    def __init__(self) -> None:
        self.stream_calls = 0

    async def stream(self, **_kwargs: Any):
        turn = self.stream_calls
        self.stream_calls += 1
        if turn == 0:
            yield TextDelta("intermediate text that is not the final assistant")
            yield ToolCallReady(ToolCall(
                id="provider-calculator-call",
                name="calculator",
                arguments='{"value":7}',
            ))
            yield TurnEnd(finish_reason="tool_calls")
            return
        if turn == 1:
            yield TextDelta("authoritative final answer")
            yield TurnEnd(finish_reason="stop")
            return
        raise AssertionError("COMPLETED recovery must not issue another model request")


async def _calculator(
    args: dict[str, Any], _context: NativeToolContext,
) -> dict[str, int]:
    return {"doubled": int(args["value"]) * 2}


async def _unused_artifact_metadata(_artifact_id: str) -> dict[str, Any]:
    raise AssertionError("final recovery scenario has no attachments")


async def _run_scenario(base, *, crash_once: bool) -> dict[str, Any]:
    base.mkdir()
    clock = _Clock()
    store = _CrashAfterCompletedCheckpointStore(
        RuntimeDatabase(base / "runtime.db"),
        crash_once=crash_once,
    )
    await store.initialize()
    release = await activate_test_release(store,
        ReleaseManifest(
            engine="native_loop",
            components={"native-final-recovery-test": "current"},
        ),
    )
    run = (await AdmissionService(
        store,
        clock=clock,
        default_deadline_ms=60_000,
    ).create(
        CreateRunInput(
            client_request_id=str(uuid.uuid4()),
            conversation_id=None,
            principal_id="demo-user",
            agent_id="demo-agent",
            engine="native_loop",
            text="calculate, then give only the final answer",
            attachment_refs=(),
            deadline_at=None,
        ),
        idempotency_key=f"native-final-recovery-{crash_once}",
    )).run

    client = _IntermediateToolFinalClient()
    settings = AgentSettings(
        _env_file=None,
        trace_enabled=False,
        max_loop_iters=3,
        native_early_tool_dispatch="off",
        native_max_tool_concurrency=2,
    )
    context = SimpleNamespace(settings=settings, chat=None)
    registry = ToolRegistry([ToolSpec(
        name="calculator",
        description="double one integer",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        run=_calculator,
        concurrency_safe=True,
    )])
    artifact_store = FilesystemArtifactStore(base / "artifacts")
    broker = ToolBroker(store, artifact_store, clock=clock)
    adapter = NativeLoopAdapter(
        context=context,  # type: ignore[arg-type] - Native uses settings/chat only.
        release_fingerprint=release,
        artifact_store=artifact_store,
        artifact_metadata_loader=_unused_artifact_metadata,
        registry=registry,
        tool_catalog=build_runtime_tool_catalog(registry),
        client=client,
    )
    coordinator = RunCoordinator(
        store,
        EngineRegistry({"native_loop": adapter}),
        clock=clock,
        random_source=_NoJitter(),
        event_flush_bytes=1,
        tool_broker=broker,
    )

    claim = await store.claim_next(
        worker_id="native-final-worker-1",
        lease_ms=30_000,
        now_ms=clock.now_ms(),
        release_map={"native_loop": release},
    )
    assert claim is not None
    first_status = await coordinator.execute_claim(
        claim, worker_id="native-final-worker-1",
    )

    if crash_once:
        assert store.crashed is True
        assert first_status is RunStatus.WAITING_RETRY
        checkpoint = await store.latest_checkpoint(run.envelope.run_id)
        assert checkpoint is not None
        assert checkpoint.engine_state is not None
        assert checkpoint.engine_state["phase"] == "COMPLETED"
        before_recovery = await store.list_events(
            run.envelope.run_id, visibility=None,
        )
        assert not any(
            event.event_type is EventType.ASSISTANT_MESSAGE_COMMITTED
            for event in before_recovery
        )

        clock.advance(1_000)
        assert await store.fire_due_timers(now_ms=clock.now_ms()) == 1
        recovery_claim = await store.claim_next(
            worker_id="native-final-worker-2",
            lease_ms=30_000,
            now_ms=clock.now_ms(),
            release_map={"native_loop": release},
        )
        assert recovery_claim is not None
        recovered_status = await coordinator.execute_claim(
            recovery_claim, worker_id="native-final-worker-2",
        )
        assert recovered_status is RunStatus.SUCCEEDED
    else:
        assert first_status is RunStatus.SUCCEEDED

    persisted = await store.get_run(run.envelope.run_id)
    assert persisted.terminal_status is RunStatus.SUCCEEDED
    events = await store.list_events(run.envelope.run_id, visibility=None)
    assistants = [
        event.payload
        for event in events
        if event.event_type is EventType.ASSISTANT_MESSAGE_COMMITTED
    ]
    deltas = [
        str((event.payload or {}).get("delta", ""))
        for event in events
        if event.event_type is EventType.OUTPUT_DELTA_COMMITTED
    ]
    # History compilation intentionally excludes the current Run.  Admit the
    # next turn in the same conversation and verify what that turn receives.
    followup = (await AdmissionService(
        store,
        clock=clock,
        default_deadline_ms=60_000,
    ).create(
        CreateRunInput(
            client_request_id=str(uuid.uuid4()),
            conversation_id=run.envelope.conversation_id,
            principal_id="demo-user",
            agent_id="demo-agent",
            engine="native_loop",
            text="follow up",
            attachment_refs=(),
            deadline_at=None,
        ),
        idempotency_key=f"native-final-followup-{crash_once}",
    )).run
    history = await store.compile_history(followup.envelope.run_id)
    return {
        "assistants": assistants,
        "deltas": deltas,
        "history": history,
        "stream_calls": client.stream_calls,
        "terminal_count": sum(
            event.event_type is EventType.RUN_TERMINATED for event in events
        ),
    }


@pytest.mark.asyncio
async def test_native_completed_checkpoint_recovery_commits_only_the_same_final_assistant(
    tmp_path,
) -> None:
    normal = await _run_scenario(tmp_path / "normal", crash_once=False)
    recovered = await _run_scenario(tmp_path / "recovered", crash_once=True)

    for result in (normal, recovered):
        assert len(result["assistants"]) == 1
        assert result["assistants"][0]["text"] == "authoritative final answer"
        assert result["terminal_count"] == 1
        assert result["stream_calls"] == 2
        assert "".join(result["deltas"]) == (
            "intermediate text that is not the final assistant"
            "authoritative final answer"
        )
        assert [item["role"] for item in result["history"]] == ["user", "assistant"]
        assert result["history"][-1]["text"] == "authoritative final answer"

    # Generation ids are Run-scoped, but semantic authority is identical.
    assert normal["assistants"][0]["text"] == recovered["assistants"][0]["text"]
