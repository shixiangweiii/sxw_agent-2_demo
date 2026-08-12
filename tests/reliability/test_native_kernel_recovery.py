from __future__ import annotations

import pytest

from agent.engine.native_loop import executor
from agent.engine.native_loop.checkpoint import (
    decode_native_checkpoint,
    encode_native_checkpoint,
)
from agent.engine.native_loop.llm_client import TextDelta, ToolCallReady, TurnEnd
from agent.engine.native_loop.loop import LoopConfig, LoopState, NativeLoop
from agent.engine.native_loop.messages import Msg, ToolCall
from agent.engine.native_loop.tools import NativeToolContext, ToolRegistry, ToolSpec
from agent.runtime.domain.errors import AttemptOwnershipLost


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
        early_tool_dispatch="off",
        tool_result_max_chars=8_000,
        context_window_tokens=32_000,
        compact_buffer_tokens=4_000,
        compact_preserve_units=4,
    )


@pytest.mark.asyncio
async def test_native_loop_requires_exactly_one_initial_state_source() -> None:
    loop = NativeLoop(
        client=_ScriptedClient([]),
        registry=ToolRegistry([]),
        system_instruction="test",
        config=_config(),
    )
    state = LoopState(messages=[Msg(role="user", content="resume")])

    with pytest.raises(ValueError, match="exactly one"):
        await anext(loop.run())
    with pytest.raises(ValueError, match="exactly one"):
        await anext(loop.run(state.messages, initial_state=state))


@pytest.mark.asyncio
async def test_native_executor_propagates_attempt_ownership_loss_unchanged() -> None:
    lost = AttemptOwnershipLost("ACTIVITY_LEASE_EXPIRED", "lease expired")

    async def lose_ownership(_args: dict, _context: NativeToolContext):
        raise lost

    registry = ToolRegistry([ToolSpec(
        name="ownership_probe",
        description="raise attempt ownership loss",
        parameters={"type": "object", "properties": {}},
        run=lose_ownership,
    )])

    with pytest.raises(AttemptOwnershipLost) as raised:
        await executor.execute_one(
            ToolCall(id="ownership-call", name="ownership_probe", arguments="{}"),
            registry,
            invocation_id="run-ownership",
            state={},
        )
    assert raised.value is lost


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("crash_phase", "effects_before_restart"),
    [
        ("MODEL_RESPONSE_COMMITTED", 0),
        ("TOOL_BATCH_COMMITTED", 0),
        ("TOOL_RESULT_COMMITTED", 1),
        ("NEXT_TURN", 1),
    ],
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

    async def crash_checkpoint(state, phase, _events):
        durable.clear()
        durable.update(encode_native_checkpoint(state, phase))
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

    restored, phase = decode_native_checkpoint(
        dict(durable), current_input="create one task",
    )
    assert phase == crash_phase
    tool_call = next(
        call
        for message in restored.messages
        for call in (message.tool_calls or [])
    )
    assert tool_call.logical_key == "native:turn:0:call:0"

    phases: list[str] = []

    async def keep_checkpoint(state, current_phase, _events):
        phases.append(current_phase)
        durable.clear()
        durable.update(encode_native_checkpoint(state, current_phase))

    final_client = _ScriptedClient([TextDelta("done"), TurnEnd(finish_reason="stop")])
    resumed = NativeLoop(
        client=final_client,
        registry=registry,
        system_instruction="test",
        config=_config(),
        checkpoint=keep_checkpoint,
    )
    events = []
    async for event in resumed.run(initial_state=restored):
        events.append(event)

    assert effects == 1
    assert "".join(event.data["delta"] for event in events if event.event == "text") == "done"
    assert phases[-1] == "COMPLETED"
    if crash_phase == "MODEL_RESPONSE_COMMITTED":
        assert phases[0] == "TOOL_BATCH_COMMITTED"
    if crash_phase == "TOOL_RESULT_COMMITTED":
        assert phases[0] == "NEXT_TURN"
    assert any(message.role == "tool" for message in final_client.requests[0])


@pytest.mark.asyncio
async def test_native_model_request_reservation_is_not_refunded_after_crash() -> None:
    durable: dict = {}

    async def crash_after_reservation(state, phase, _events):
        durable.clear()
        durable.update(encode_native_checkpoint(state, phase))
        if phase == "MODEL_REQUEST":
            raise RuntimeError("fault injection after model-call reservation")

    first_client = _ScriptedClient([TextDelta("unreachable"), TurnEnd(finish_reason="stop")])
    first = NativeLoop(
        client=first_client,
        registry=ToolRegistry([]),
        system_instruction="test",
        config=_config(),
        checkpoint=crash_after_reservation,
    )

    with pytest.raises(RuntimeError, match="model-call reservation"):
        async for _ in first.run([Msg(role="user", content="answer once")]):
            pass

    assert first_client.requests == []
    restored, phase = decode_native_checkpoint(
        durable,
        current_input="answer once",
    )
    assert phase == "MODEL_REQUEST"
    assert restored.model_call_count == 1
    assert restored.resume_from_model_request is True

    reservations: list[int] = []

    async def keep_checkpoint(state, current_phase, _events):
        if current_phase == "MODEL_REQUEST":
            reservations.append(state.model_call_count)

    final_client = _ScriptedClient([TextDelta("done"), TurnEnd(finish_reason="stop")])
    resumed = NativeLoop(
        client=final_client,
        registry=ToolRegistry([]),
        system_instruction="test",
        config=_config(),
        checkpoint=keep_checkpoint,
    )
    async for _ in resumed.run(initial_state=restored):
        pass

    assert reservations == [2]
    assert len(final_client.requests) == 1


@pytest.mark.asyncio
async def test_native_model_call_hard_cap_stops_before_another_provider_request() -> None:
    config = _config()
    client = _ScriptedClient([TextDelta("unreachable"), TurnEnd(finish_reason="stop")])
    loop = NativeLoop(
        client=client,
        registry=ToolRegistry([]),
        system_instruction="test",
        config=config,
    )
    state = LoopState(
        messages=[Msg(role="user", content="already exhausted")],
        iters=config.hard_cap,
        model_call_count=config.hard_cap,
        generation_counter=config.hard_cap,
    )

    events = [event async for event in loop.run(initial_state=state)]

    assert loop.error_code == "RUN_MODEL_CALL_LIMIT"
    assert client.requests == []
    assert [event.data["code"] for event in events if event.event == "error"] == [
        "RUN_MODEL_CALL_LIMIT",
    ]


def test_native_checkpoint_references_current_multimodal_input_instead_of_copying_base64():
    state = LoopState(
        messages=[Msg(role="user", content=[
            {"type": "text", "text": "inspect"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,SECRET"}},
        ])],
        iters=1,
        model_call_count=1,
        generation_counter=1,
        current_message_id="message-1",
        current_generation_id="generation-1",
        transition=None,
        attempted_reactive_compact=0,
        compact_failures=0,
        compact_cooldown=0,
        last_usage=None,
        tool_state={},
    )
    serialized = encode_native_checkpoint(state, "MODEL_REQUEST")
    assert serialized["messages"][0]["content_source"] == "CURRENT_INPUT"
    assert serialized["messages"][0]["content"] is None
    assert "SECRET" not in str(serialized)


def test_native_checkpoint_rematerializes_large_artifact_tool_result():
    state = LoopState(
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
                tool_execution_id="tool_" + "a" * 32,
            ),
        ],
        iters=1,
        model_call_count=1,
        generation_counter=1,
        tool_call_count=1,
        current_message_id="message-1",
        current_generation_id="generation-1",
        transition="next_turn",
        attempted_reactive_compact=0,
        compact_failures=0,
        compact_cooldown=0,
        last_usage=None,
        tool_state={},
    )
    serialized = encode_native_checkpoint(state, "NEXT_TURN")
    assert "SENSITIVE-LARGE-SLICE" not in str(serialized)
    restored, _ = decode_native_checkpoint(
        serialized, current_input="fallback",
    )
    assert [message.role for message in restored.messages] == ["assistant", "tool"]
    assert restored.messages[1].content is None
    assert restored.messages[1].tool_execution_id == "tool_" + "a" * 32
    assert restored.messages[0].tool_calls[0].logical_key == "native:turn:0:call:0"
