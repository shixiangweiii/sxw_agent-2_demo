from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.genai import types

from agent.config import AgentSettings
from agent.engine.base import RunContext
from agent.engine.native_loop.engine import _restore_state, _serialize_state
from agent.engine.native_loop.llm_client import TextDelta, ToolCallReady, TurnEnd
from agent.engine.native_loop.loop import LoopConfig, NativeLoop
from agent.engine.native_loop.messages import Msg, ToolCall
from agent.engine.native_loop.tools import NativeToolContext, ToolRegistry, ToolSpec


class _ScriptedClient:
    def __init__(self, items):
        self.items = items
        self.requests: list[list[Msg]] = []

    async def stream(self, *, messages, tools=None, allow_early_tool_dispatch=True, **_kwargs):
        self.requests.append(messages)
        for item in self.items:
            yield item


def _config() -> LoopConfig:
    return LoopConfig(
        max_iters=4,
        hard_cap=6,
        max_tool_concurrency=2,
        streaming_tool_exec=True,
        tool_result_max_chars=8_000,
        context_window_tokens=32_000,
        compact_buffer_tokens=4_000,
        compact_preserve_units=4,
    )


def _run_context(checkpoint) -> RunContext:
    return RunContext(
        run_id="run_native_recovery",
        activity_id="act_native_recovery",
        engine="native_loop",
        agent_uuid="demo-agent",
        user_id="demo-user",
        session_id="conv-native-recovery",
        user_message=types.Content(
            role="user", parts=[types.Part.from_text(text="create one task")],
        ),
        settings=AgentSettings(_env_file=None),
        release_fingerprint="release-native-v1",
        engine_checkpoint=checkpoint,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("crash_phase", "effects_before_restart"),
    [("TOOL_BATCH_COMMITTED", 0), ("NEXT_TURN", 1)],
)
async def test_native_kernel_recovers_durable_tool_boundary_without_duplicate_effect(
    crash_phase, effects_before_restart,
):
    effects = 0

    async def create_task(args: dict, _context: NativeToolContext):
        nonlocal effects
        effects += 1
        return {"task_id": f"task-{args['title']}"}

    registry = ToolRegistry([ToolSpec(
        name="create_task",
        description="create one idempotent demo task",
        parameters={
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
        run=create_task,
        concurrency_safe=False,
    )])
    durable: dict = {}

    async def crash_checkpoint(state, phase):
        durable.clear()
        durable.update(_serialize_state(state, phase))
        if phase == crash_phase:
            raise RuntimeError("fault injection: worker killed")

    first_client = _ScriptedClient([
        ToolCallReady(ToolCall(
            id="provider-call-before-kill",
            name="create_task",
            arguments='{"title":"once"}',
        )),
        TurnEnd(finish_reason="tool_calls"),
    ])
    first = NativeLoop(
        client=first_client,
        registry=registry,
        system_instruction="test",
        config=_config(),
        checkpoint=crash_checkpoint,
    )
    with pytest.raises(RuntimeError, match="worker killed"):
        async for _ in first.run([Msg(role="user", content="create one task")]):
            pass
    assert effects == effects_before_restart

    checkpoint = SimpleNamespace(engine_state=dict(durable), revision=1)
    restored, phase = _restore_state(
        _run_context(checkpoint), [Msg(role="user", content="fallback")],
    )
    assert phase == crash_phase
    tool_call = next(
        call
        for message in restored.messages
        for call in (message.tool_calls or [])
    )
    assert tool_call.logical_key == "native:turn:0:call:0"

    phases: list[str] = []

    async def keep_checkpoint(state, current_phase):
        phases.append(current_phase)
        durable.clear()
        durable.update(_serialize_state(state, current_phase))

    final_client = _ScriptedClient([TextDelta("done"), TurnEnd(finish_reason="stop")])
    resumed = NativeLoop(
        client=final_client,
        registry=registry,
        system_instruction="test",
        config=_config(),
        checkpoint=keep_checkpoint,
    )
    events = []
    async for event in resumed.run(restored.messages, initial_state=restored):
        events.append(event)

    assert effects == 1
    assert "".join(event.data["delta"] for event in events if event.event == "text") == "done"
    assert phases[-1] == "COMPLETED"
    assert any(message.role == "tool" for message in final_client.requests[0])


def test_native_checkpoint_references_current_multimodal_input_instead_of_copying_base64():
    state = SimpleNamespace(
        messages=[Msg(role="user", content=[
            {"type": "text", "text": "inspect"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,SECRET"}},
        ])],
        iters=1,
        transition=None,
        attempted_reactive_compact=0,
        compact_failures=0,
        compact_cooldown=0,
        last_usage=None,
        tool_state={},
    )
    serialized = _serialize_state(state, "MODEL_REQUEST")
    assert serialized["messages"][0]["content"] == {"$runtime_current_input": True}
    assert "SECRET" not in str(serialized)


def test_native_checkpoint_rematerializes_large_artifact_tool_result():
    state = SimpleNamespace(
        messages=[
            Msg(
                role="assistant",
                tool_calls=[ToolCall(
                    id="read-call",
                    name="read_artifact",
                    arguments='{"artifact_id":"abc","offset":0}',
                    logical_key="native:turn:0:call:0",
                )],
            ),
            Msg(
                role="tool",
                name="read_artifact",
                tool_call_id="read-call",
                content="SENSITIVE-LARGE-SLICE" * 1_000,
            ),
        ],
        iters=1,
        transition="next_turn",
        attempted_reactive_compact=0,
        compact_failures=0,
        compact_cooldown=0,
        last_usage=None,
        tool_state={},
    )
    serialized = _serialize_state(state, "NEXT_TURN")
    assert "SENSITIVE-LARGE-SLICE" not in str(serialized)
    restored, _ = _restore_state(
        _run_context(SimpleNamespace(engine_state=serialized, revision=1)),
        [Msg(role="user", content="fallback")],
    )
    assert [message.role for message in restored.messages] == ["assistant"]
    assert restored.messages[0].tool_calls[0].logical_key == "native:turn:0:call:0"
