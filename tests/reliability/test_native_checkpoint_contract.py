from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

import pytest

from agent.engine.native_loop.checkpoint import (
    decode_native_checkpoint,
    encode_native_checkpoint,
)
from agent.engine.native_loop.engine import NativeLoopAdapter
from agent.engine.native_loop.loop import LoopState
from agent.engine.native_loop.messages import Msg, ToolCall, Usage
from agent.engine.native_loop.tools import ToolRegistry
from agent.runtime.adapters.brokered_tools import build_runtime_tool_catalog
from agent.runtime.domain.errors import RuntimeFault
from agent.runtime.domain.models import (
    CheckpointRecord,
    EngineOutcomeKind,
    RuntimeEnvelope,
    WorkingState,
)
from agent.runtime.ports.engine import EngineRunRequest
from agent.config import AgentSettings


def _state(messages: list[Msg], **changes: Any) -> LoopState:
    values: dict[str, Any] = {
        "messages": messages,
        "iters": 1,
        "model_call_count": 1,
        "tool_call_count": sum(
            len(message.tool_calls or []) for message in messages
        ),
        "transition": None,
        "attempted_reactive_compact": 0,
        "compact_failures": 0,
        "compact_cooldown": 0,
        "last_usage": None,
        "tool_state": {},
        "generation_counter": 1,
        "current_message_id": "message-1",
        "current_generation_id": "generation-1",
        "supersedes_generation_id": None,
        "generation_reason": "initial",
        "final_text": None,
        "final_message_id": None,
        "final_generation_id": None,
    }
    values.update(changes)
    return LoopState(**values)


def _call(index: int) -> ToolCall:
    return ToolCall(
        id=f"call-{index}",
        name="read",
        arguments=f'{{"index":{index}}}',
        logical_key=f"native:turn:0:call:{index}",
    )


def _request_payload() -> dict[str, Any]:
    return encode_native_checkpoint(
        _state([Msg(role="user", content="current input")]),
        "MODEL_REQUEST",
    )


def _response_payload() -> dict[str, Any]:
    return encode_native_checkpoint(
        _state([
            Msg(role="user", content="current input"),
            Msg(role="assistant", content="final response"),
        ]),
        "MODEL_RESPONSE_COMMITTED",
    )


def _batch_payload(*, calls: int = 2) -> dict[str, Any]:
    return encode_native_checkpoint(
        _state([
            Msg(role="user", content="current input"),
            Msg(
                role="assistant",
                content=None,
                tool_calls=[_call(index) for index in range(calls)],
            ),
        ]),
        "TOOL_BATCH_COMMITTED",
    )


def _result_payload(*, complete: bool) -> dict[str, Any]:
    calls = [_call(0), _call(1)]
    messages = [
        Msg(role="user", content="current input"),
        Msg(role="assistant", tool_calls=calls),
        Msg(
            role="tool",
            content="result-0",
            tool_call_id=calls[0].id,
            name=calls[0].name,
        ),
    ]
    phase = "TOOL_RESULT_COMMITTED"
    if complete:
        messages.append(Msg(
            role="tool",
            content="result-1",
            tool_call_id=calls[1].id,
            name=calls[1].name,
        ))
        phase = "NEXT_TURN"
    return encode_native_checkpoint(_state(messages), phase)


def _completed_payload() -> dict[str, Any]:
    return encode_native_checkpoint(
        _state(
            [
                Msg(role="user", content="current input"),
                Msg(role="assistant", content="final response"),
            ],
            transition="completed",
            final_text="final response",
            final_message_id="message-1",
            final_generation_id="generation-1",
        ),
        "COMPLETED",
    )


def _assert_invalid(payload: dict[str, Any]) -> None:
    with pytest.raises(RuntimeFault) as raised:
        decode_native_checkpoint(payload, current_input="current input")
    assert raised.value.code == "NATIVE_CHECKPOINT_INVALID"


def _drop_top_level(payload: dict[str, Any]) -> None:
    del payload["generation_reason"]


def _drop_nested_message_field(payload: dict[str, Any]) -> None:
    del payload["messages"][0]["content_source"]


def _add_top_level_field(payload: dict[str, Any]) -> None:
    payload["legacy_phase"] = "request"


def _add_nested_message_field(payload: dict[str, Any]) -> None:
    payload["messages"][0]["legacy_content"] = "fallback"


@pytest.mark.parametrize(
    "mutate",
    [
        _drop_top_level,
        _drop_nested_message_field,
        _add_top_level_field,
        _add_nested_message_field,
    ],
    ids=["missing-top-level", "missing-nested", "extra-top-level", "extra-nested"],
)
def test_native_checkpoint_requires_the_exact_current_field_set(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    payload = _request_payload()
    mutate(payload)
    _assert_invalid(payload)


def test_native_checkpoint_usage_object_has_no_implicit_defaults() -> None:
    payload = encode_native_checkpoint(
        _state(
            [Msg(role="user", content="current input")],
            last_usage=Usage(
                prompt_tokens=3,
                completion_tokens=2,
                total_tokens=5,
                extra={"cached": 1},
            ),
        ),
        "MODEL_REQUEST",
    )
    del payload["last_usage"]["extra"]

    _assert_invalid(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("contract", "native-kernel-v0"),
        ("phase", "LEGACY_TOOL_RUNNING"),
    ],
)
def test_native_checkpoint_rejects_unknown_contract_or_phase(
    field: str,
    value: str,
) -> None:
    payload = _request_payload()
    payload[field] = value
    _assert_invalid(payload)


def test_native_checkpoint_rejects_an_illegal_message_role() -> None:
    payload = _request_payload()
    payload["messages"][0]["role"] = "system"
    _assert_invalid(payload)


@pytest.mark.parametrize("identity", ["id", "logical_key"])
def test_native_checkpoint_rejects_duplicate_tool_call_identity(identity: str) -> None:
    payload = _batch_payload()
    calls = payload["messages"][-1]["tool_calls"]
    calls[1][identity] = calls[0][identity]
    _assert_invalid(payload)


def test_native_checkpoint_rejects_an_orphan_tool_result() -> None:
    payload = _request_payload()
    payload["messages"].append({
        "role": "tool",
        "content": "orphan",
        "content_source": "INLINE",
        "tool_calls": [],
        "tool_call_id": "missing-call",
        "name": "read",
        "is_error": False,
        "kind": "normal",
        "tool_execution_id": None,
    })
    _assert_invalid(payload)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {**_response_payload(), "phase": "MODEL_REQUEST"},
            id="model-request-with-response-tail",
        ),
        pytest.param(
            {**_request_payload(), "phase": "MODEL_RESPONSE_COMMITTED"},
            id="model-response-with-user-tail",
        ),
        pytest.param(
            {**_response_payload(), "phase": "TOOL_BATCH_COMMITTED"},
            id="tool-batch-without-calls",
        ),
        pytest.param(
            {**_batch_payload(), "phase": "TOOL_RESULT_COMMITTED"},
            id="tool-result-phase-without-result",
        ),
        pytest.param(
            {**_result_payload(complete=False), "phase": "NEXT_TURN"},
            id="next-turn-with-incomplete-batch",
        ),
    ],
)
def test_native_checkpoint_rejects_phase_message_contradictions(
    payload: dict[str, Any],
) -> None:
    _assert_invalid(deepcopy(payload))


def test_model_response_checkpoint_requires_non_empty_text_or_tool_calls() -> None:
    payload = _response_payload()
    payload["messages"][-1]["content"] = None
    _assert_invalid(payload)


def test_completed_checkpoint_round_trips_exact_final_generation() -> None:
    state, phase = decode_native_checkpoint(
        _completed_payload(),
        current_input="current input",
    )

    assert phase == "COMPLETED"
    assert state.final_text == "final response"
    assert state.final_message_id == "message-1"
    assert state.final_generation_id == "generation-1"


@pytest.mark.asyncio
async def test_completed_checkpoint_restore_returns_without_requesting_the_model() -> None:
    class _NeverModel:
        tool_call_block_complete = False

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, **_kwargs: Any) -> Any:
            self.calls += 1
            raise AssertionError("COMPLETED recovery must not request the provider")
            yield  # pragma: no cover - retain async-generator shape

    class _RuntimeIO:
        tool_broker = object()

        def __init__(self) -> None:
            self.final_assistant: tuple[str, str, str] | None = None
            self.checkpoints = 0

        def set_final_assistant(
            self,
            text: str,
            message_id: str,
            generation_id: str,
        ) -> None:
            self.final_assistant = (text, message_id, generation_id)

        async def checkpoint(self, *_args: Any, **_kwargs: Any) -> Any:
            self.checkpoints += 1
            raise AssertionError("COMPLETED recovery must not write another checkpoint")

    async def _unused_metadata(_artifact_id: str) -> dict[str, Any]:
        raise AssertionError("checkpoint recovery has no attachment to load")

    settings = AgentSettings(_env_file=None, trace_enabled=False)
    context = type("Context", (), {"settings": settings, "chat": None})()
    model = _NeverModel()
    registry = ToolRegistry([])
    adapter = NativeLoopAdapter(
        context=context,  # type: ignore[arg-type] - narrow adapter fixture
        release_fingerprint="release-native-current",
        artifact_store=object(),  # type: ignore[arg-type] - never read in this fixture
        artifact_metadata_loader=_unused_metadata,
        registry=registry,
        tool_catalog=build_runtime_tool_catalog(registry),
        client=model,  # type: ignore[arg-type] - strict no-call provider spy
    )
    checkpoint = CheckpointRecord(
        checkpoint_id="checkpoint-1",
        run_id="run-1",
        activity_id="activity-1",
        revision=7,
        working_state=WorkingState(goal="current input"),
        engine_state=_completed_payload(),
        created_at=1,
    )
    request = EngineRunRequest(
        envelope=RuntimeEnvelope(
            request_id="request-1",
            client_request_id="client-request-1",
            idempotency_key="idempotency-1",
            conversation_id="conversation-1",
            turn_id="turn-1",
            run_id="run-1",
            principal_id="principal-1",
            agent_id="agent-1",
            engine="native_loop",
            deadline_at=2_000_000_000_000,
            cancel_token_id="cancel-1",
            release_fingerprint="release-native-current",
            input_event_id="input-event-1",
            created_at=1,
        ),
        activity_id="activity-1",
        fencing_token=3,
        attempt=2,
        input_text="current input",
        history=(),
        checkpoint=checkpoint,
        resume_payload=None,
    )
    io = _RuntimeIO()

    outcome = await adapter.execute(request, io)  # type: ignore[arg-type]

    assert outcome.kind is EngineOutcomeKind.COMPLETED
    assert model.calls == 0
    assert io.checkpoints == 0
    assert io.final_assistant == (
        "final response",
        "message-1",
        "generation-1",
    )
