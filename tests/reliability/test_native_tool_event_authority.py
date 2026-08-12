from __future__ import annotations

from agent.engine.native_loop import executor
from agent.engine.native_loop.loop import LoopConfig, NativeLoop
from agent.engine.native_loop.messages import Msg, ToolCall
from agent.engine.native_loop.tools import ToolRegistry, ToolSpec
from agent.runtime.adapters.adk_engines import _broker_owns_tool_projection


async def _never_run(_args, _context):
    raise AssertionError("classification must not dispatch a tool")


def _loop() -> NativeLoop:
    registry = ToolRegistry([
        ToolSpec(
            name="known",
            description="known tool",
            parameters={"type": "object", "properties": {}},
            run=_never_run,
        )
    ])
    return NativeLoop(
        client=object(),  # type: ignore[arg-type]
        registry=registry,
        system_instruction="test",
        config=LoopConfig(
            max_iters=1,
            hard_cap=2,
            max_tool_concurrency=1,
            early_tool_dispatch="off",
            tool_result_max_chars=1000,
            context_window_tokens=1000,
            compact_buffer_tokens=100,
            compact_preserve_units=1,
        ),
    )


def _outcome(call: ToolCall, response: dict[str, str]) -> executor.ToolOutcome:
    return executor.ToolOutcome(
        call=call,
        message=Msg(role="tool", content="result", tool_call_id=call.id, name=call.name),
        response=response,
        ok=False,
    )


def test_native_only_suppresses_tool_projections_owned_by_broker() -> None:
    loop = _loop()
    valid = ToolCall(id="call-valid", name="known", arguments='{"value":1}')
    invalid = ToolCall(id="call-invalid", name="known", arguments="[1,2]")
    unknown = ToolCall(id="call-unknown", name="missing", arguments="{}")

    valid_call = loop._call_events(valid)[0]  # noqa: SLF001 - exact adapter contract
    valid_result = loop._result_events(_outcome(valid, {"ok": "true"}))[0]  # noqa: SLF001
    invalid_call = loop._call_events(invalid)[0]  # noqa: SLF001
    invalid_result = loop._result_events(
        _outcome(invalid, {"error": "ToolArgumentsParseError"})
    )[0]  # noqa: SLF001
    unknown_call = loop._call_events(unknown)[0]  # noqa: SLF001
    unknown_result = loop._result_events(
        _outcome(unknown, {"error": "NoSuchTool"})
    )[0]  # noqa: SLF001

    broker = object()
    assert _broker_owns_tool_projection(valid_call, broker)
    assert _broker_owns_tool_projection(valid_result, broker)
    assert not _broker_owns_tool_projection(invalid_call, broker)
    assert not _broker_owns_tool_projection(invalid_result, broker)
    assert not _broker_owns_tool_projection(unknown_call, broker)
    assert not _broker_owns_tool_projection(unknown_result, broker)
    assert not _broker_owns_tool_projection(valid_call, None)
