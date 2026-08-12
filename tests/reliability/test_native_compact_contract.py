from __future__ import annotations

from typing import Any

import pytest

from agent.engine.native_loop import compact
from agent.engine.native_loop.llm_client import (
    ContextOverflowError,
    TextDelta,
    TurnEnd,
)
from agent.engine.native_loop.loop import LoopConfig, LoopState, NativeLoop
from agent.engine.native_loop.messages import (
    KIND_COMPACT_SUMMARY,
    Msg,
    ToolCall,
    Usage,
    atomic_units,
)
from agent.engine.native_loop.tools import ToolRegistry


class _SummaryChat:
    def __init__(self, response: str = "<summary>durable facts</summary>") -> None:
        self.response = response
        self.requests: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> str:
        self.requests.append(dict(kwargs))
        return self.response


class _FailingSummaryChat:
    async def complete(self, **_kwargs: Any) -> str:
        raise TimeoutError("summary provider unavailable")


def _history() -> list[Msg]:
    call = ToolCall(
        id="call-1",
        name="lookup",
        arguments='{"query":"fact"}',
        logical_key="native:turn:0:call:0",
    )
    return [
        Msg(role="user", content="old request"),
        Msg(role="assistant", tool_calls=[call]),
        Msg(role="tool", tool_call_id=call.id, name=call.name, content="old result"),
        Msg(role="user", content="latest request"),
    ]


@pytest.mark.asyncio
async def test_compact_replaces_prefix_without_splitting_tool_atomic_unit() -> None:
    chat = _SummaryChat()
    compacted = await compact.compact(
        _history(), chat, preserve_units=2, trigger="proactive",
    )

    assert compacted is not None
    assert compacted[0].kind == KIND_COMPACT_SUMMARY
    assert [message.role for message in compacted] == [
        "user", "assistant", "tool", "user",
    ]
    assert compacted[1].tool_calls
    assert compacted[2].tool_call_id == compacted[1].tool_calls[0].id
    assert [(unit.start, unit.end) for unit in atomic_units(compacted)] == [
        (0, 0), (1, 2), (3, 3),
    ]
    assert chat.requests[0]["max_tokens"] == 4096


@pytest.mark.asyncio
async def test_compact_failure_or_empty_summary_does_not_mutate_history() -> None:
    original = _history()

    assert await compact.compact(
        original, _FailingSummaryChat(), preserve_units=1, trigger="proactive",
    ) is None
    assert await compact.compact(
        original, _SummaryChat("<summary>   </summary>"),
        preserve_units=1,
        trigger="proactive",
    ) is None
    assert [message.content for message in original] == [
        "old request", None, "old result", "latest request",
    ]


def test_compact_estimation_accounts_for_fixed_overhead_and_hides_image_bytes() -> None:
    messages = [Msg(role="user", content=[
        {"type": "text", "text": "inspect"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,TOP-SECRET-BASE64"},
        },
    ])]
    without_overhead = compact.decide(
        messages,
        None,
        context_window_tokens=100,
        buffer_tokens=10,
        fixed_overhead_chars=0,
    )
    with_overhead = compact.decide(
        messages,
        Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        context_window_tokens=100,
        buffer_tokens=10,
        fixed_overhead_chars=150,
    )

    assert with_overhead.tokens > without_overhead.tokens
    assert with_overhead.estimated is False
    rendered = compact.render_history(messages)
    assert "[图片]" in rendered
    assert "TOP-SECRET-BASE64" not in rendered


@pytest.mark.asyncio
async def test_reactive_compact_retries_without_publishing_duplicate_text() -> None:
    class _OverflowThenFinal:
        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, **_kwargs: Any):
            self.calls += 1
            if self.calls == 1:
                raise ContextOverflowError("context window exceeded")
            yield TextDelta("final after compact")
            yield TurnEnd(finish_reason="stop")

    client = _OverflowThenFinal()
    chat = _SummaryChat()
    checkpoint_events: list[tuple[str, tuple[Any, ...]]] = []

    async def checkpoint(_state: LoopState, phase: str, events: tuple[Any, ...]) -> None:
        checkpoint_events.append((phase, events))

    loop = NativeLoop(
        client=client,  # type: ignore[arg-type] - deterministic protocol fixture
        registry=ToolRegistry([]),
        system_instruction="test",
        chat=chat,  # type: ignore[arg-type] - narrow complete() fixture
        checkpoint=checkpoint,
        config=LoopConfig(
            max_iters=4,
            hard_cap=6,
            max_tool_concurrency=1,
            early_tool_dispatch="off",
            tool_result_max_chars=8_000,
            context_window_tokens=32_000,
            compact_buffer_tokens=4_000,
            compact_preserve_units=1,
        ),
    )
    messages = [
        Msg(role="user", content="old question"),
        Msg(role="assistant", content="old answer"),
        Msg(role="user", content="current question"),
    ]

    events = [event async for event in loop.run(messages)]

    assert client.calls == 2
    assert [event.data["delta"] for event in events if event.event == "text"] == [
        "final after compact",
    ]
    starts = [
        event
        for phase, checkpointed in checkpoint_events
        if phase == "MODEL_REQUEST"
        for event in checkpointed
    ]
    assert [event.data["reason"] for event in starts] == [
        "initial", "reactive_compact",
    ]
    assert loop.stop_reason == "completed"
