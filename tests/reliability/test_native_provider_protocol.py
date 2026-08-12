from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agent.engine.native_loop.llm_client import (
    NativeLlmClient,
    NativeLlmError,
    ToolCallReady,
    TurnEnd,
)
from agent.runtime.domain.errors import RuntimeFault


class _FakeStream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = iter(chunks)
        self.closed = False

    async def __aenter__(self) -> "_FakeStream":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        self.closed = True

    def __aiter__(self) -> "_FakeStream":
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._chunks)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeCompletions:
    def __init__(self, chunks: list[Any]) -> None:
        self.stream = _FakeStream(chunks)

    async def create(self, **_payload: Any) -> _FakeStream:
        return self.stream


def _client(chunks: list[Any]) -> tuple[NativeLlmClient, _FakeStream]:
    completions = _FakeCompletions(chunks)
    client = object.__new__(NativeLlmClient)
    client._client = SimpleNamespace(  # noqa: SLF001 - raw provider protocol fixture
        chat=SimpleNamespace(completions=completions),
    )
    return client, completions.stream


def _chunk(*choices: Any, usage: Any = None) -> Any:
    return SimpleNamespace(choices=list(choices), usage=usage)


def _choice(
    *,
    content: str | None = None,
    calls: list[Any] | None = None,
    finish_reason: str | None = None,
    index: int | None = 0,
) -> Any:
    return SimpleNamespace(
        index=index,
        finish_reason=finish_reason,
        delta=SimpleNamespace(content=content, tool_calls=calls or []),
    )


def _call(
    index: int,
    *,
    call_id: str | None,
    name: str | None,
    arguments: str | None,
) -> Any:
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


async def _consume(
    chunks: list[Any],
    *,
    allow_early: bool = False,
    max_tool_calls: int = 64,
    max_tool_argument_bytes: int = 64 * 1024,
    max_tool_batch_argument_bytes: int = 256 * 1024,
) -> list[Any]:
    client, stream = _client(chunks)
    try:
        return [
            item
            async for item in client._consume(  # noqa: SLF001 - protocol boundary under test
                {},
                allow_early,
                max_tool_calls=max_tool_calls,
                max_tool_argument_bytes=max_tool_argument_bytes,
                max_tool_batch_argument_bytes=max_tool_batch_argument_bytes,
            )
        ]
    finally:
        assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chunks",
    [
        [],
        [
            _chunk(usage=SimpleNamespace(
                prompt_tokens=3,
                completion_tokens=0,
                total_tokens=3,
            )),
        ],
        [_chunk(_choice(content="partial without finish"))],
    ],
    ids=["silent-eof", "usage-only", "missing-finish"],
)
async def test_native_provider_requires_an_explicit_finish_marker(chunks: list[Any]) -> None:
    with pytest.raises(NativeLlmError) as raised:
        await _consume(chunks)

    assert raised.value.kind == "MODEL_STREAM_INCOMPLETE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chunks",
    [
        [
            _chunk(_choice(content="complete", finish_reason="stop")),
            _chunk(_choice(content="late provider bytes")),
        ],
        [
            _chunk(
                _choice(content="choice zero", index=0),
                _choice(content="choice one", index=1),
            ),
        ],
        [
            _chunk(_choice(
                calls=[
                    _call(0, call_id="call-0", name="read", arguments="{}"),
                    _call(2, call_id="call-2", name="read", arguments="{}"),
                ],
                finish_reason="tool_calls",
            )),
        ],
        [
            _chunk(_choice(calls=[
                _call(0, call_id="call-before", name="read", arguments="{")
            ])),
            _chunk(_choice(
                calls=[_call(0, call_id="call-after", name=None, arguments="}")],
                finish_reason="tool_calls",
            )),
        ],
        [
            _chunk(_choice(calls=[
                _call(0, call_id="call-0", name="read-before", arguments="{")
            ])),
            _chunk(_choice(
                calls=[_call(0, call_id=None, name="read-after", arguments="}")],
                finish_reason="tool_calls",
            )),
        ],
        [
            _chunk(_choice(
                calls=[_call(0, call_id=None, name="read", arguments="{}")],
                finish_reason="tool_calls",
            )),
        ],
        [
            _chunk(_choice(
                calls=[SimpleNamespace(
                    index=None,
                    id="call-0",
                    function=SimpleNamespace(name="read", arguments="{}"),
                )],
                finish_reason="tool_calls",
            )),
        ],
        [
            _chunk(_choice(
                calls=[SimpleNamespace(
                    index=0,
                    id="call-0",
                    function=SimpleNamespace(name="read", arguments={}),
                )],
                finish_reason="tool_calls",
            )),
        ],
    ],
    ids=[
        "choice-after-finish",
        "multiple-choices",
        "non-contiguous-tool-index",
        "tool-id-drift",
        "tool-name-drift",
        "missing-tool-id",
        "missing-tool-index",
        "non-string-tool-arguments",
    ],
)
async def test_native_provider_rejects_contradictory_stream_shapes(
    chunks: list[Any],
) -> None:
    with pytest.raises(NativeLlmError) as raised:
        await _consume(chunks)

    assert raised.value.kind == "MODEL_PROTOCOL_INVALID"


@pytest.mark.asyncio
async def test_native_provider_accepts_standard_argument_fragment_append_when_early_is_off() -> None:
    items = await _consume([
        _chunk(_choice(calls=[
            _call(0, call_id="call-0", name="read", arguments='{"offset":')
        ])),
        _chunk(_choice(
            calls=[_call(0, call_id=None, name=None, arguments="1}")],
            finish_reason="tool_calls",
        )),
    ])

    assert len(items) == 2
    assert isinstance(items[0], ToolCallReady)
    assert items[0].call.arguments == '{"offset":1}'
    assert isinstance(items[1], TurnEnd)
    assert items[1].finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_experimental_early_dispatch_fails_closed_on_later_argument_fragment() -> None:
    client, stream = _client([
        _chunk(_choice(calls=[
            _call(0, call_id="call-0", name="read", arguments="{}"),
            _call(1, call_id="call-1", name="read", arguments="{"),
        ])),
        _chunk(_choice(
            calls=[_call(0, call_id=None, name=None, arguments=" ")],
            finish_reason="tool_calls",
        )),
    ])
    response = client._consume({}, True)  # noqa: SLF001 - protocol boundary under test

    first = await anext(response)
    assert isinstance(first, ToolCallReady)
    assert first.call.id == "call-0"
    with pytest.raises(RuntimeFault) as raised:
        await anext(response)

    assert raised.value.code == "TOOL_REPLAY_MISMATCH"
    assert stream.closed


@pytest.mark.asyncio
async def test_usage_only_trailer_after_finish_is_not_mistaken_for_late_choice_data() -> None:
    usage = SimpleNamespace(
        prompt_tokens=7,
        completion_tokens=2,
        total_tokens=9,
    )
    items = await _consume([
        _chunk(_choice(content="done", finish_reason="stop")),
        _chunk(usage=usage),
    ])

    assert isinstance(items[-1], TurnEnd)
    assert items[-1].usage is not None
    assert items[-1].usage.total_tokens == 9


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limits", "chunks", "expected"),
    [
        (
            {"max_tool_calls": 1},
            [_chunk(_choice(calls=[
                _call(0, call_id="call-0", name="read", arguments="{}"),
                _call(1, call_id="call-1", name="read", arguments="{}"),
            ], finish_reason="tool_calls"))],
            "TOOL_CALL_LIMIT_EXCEEDED",
        ),
        (
            {"max_tool_argument_bytes": 4},
            [_chunk(_choice(
                calls=[_call(0, call_id="call-0", name="read", arguments='{"x":1}')],
                finish_reason="tool_calls",
            ))],
            "TOOL_ARGUMENTS_TOO_LARGE",
        ),
        (
            {"max_tool_argument_bytes": 16, "max_tool_batch_argument_bytes": 8},
            [_chunk(_choice(calls=[
                _call(0, call_id="call-0", name="read", arguments='{"x":1}'),
                _call(1, call_id="call-1", name="read", arguments='{"y":2}'),
            ], finish_reason="tool_calls"))],
            "TOOL_BATCH_TOO_LARGE",
        ),
    ],
    ids=["call-count", "single-arguments", "batch-arguments"],
)
async def test_native_provider_enforces_tool_resource_limits(
    limits: dict[str, int], chunks: list[Any], expected: str,
) -> None:
    with pytest.raises(NativeLlmError) as raised:
        await _consume(chunks, **limits)

    assert raised.value.kind == expected
