from __future__ import annotations

from agent.runtime.application.tool_broker import _normalize_tool_result
from agent.runtime.domain.models import ToolResultStatus


def test_legacy_tool_results_map_to_fixed_result_vocabulary():
    failure = _normalize_tool_result({
        "isError": True,
        "errorCode": "SKILL_STREAM_TRUNCATED",
        "content": "the first sticky failure",
    })
    assert failure.status is ToolResultStatus.FAILURE
    assert failure.error_code == "SKILL_STREAM_TRUNCATED"
    assert failure.error_message == "the first sticky failure"

    interrupt = _normalize_tool_result({
        "interrupt": True,
        "pending_input": {"type": "APPROVAL"},
    })
    assert interrupt.status is ToolResultStatus.INTERRUPT
    assert interrupt.pending_input == {"type": "APPROVAL"}

    unknown = _normalize_tool_result({
        "status": "UNKNOWN",
        "error_code": "ACK_LOST",
        "message": "outcome unavailable",
    })
    assert unknown.status is ToolResultStatus.UNKNOWN
    assert unknown.error_code == "ACK_LOST"

    assert _normalize_tool_result(None).status is ToolResultStatus.NO_OUTPUT
    success = _normalize_tool_result({"value": 42})
    assert success.status is ToolResultStatus.SUCCESS
    assert success.preview == {"value": 42}
