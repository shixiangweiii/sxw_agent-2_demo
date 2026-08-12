from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.runtime.application.tool_outputs import (
    a2a_output,
    claude_skill_output,
    plain_json_output,
    project_tool_result_for_model,
    skill_center_output,
)
from agent.runtime.domain.errors import RuntimeFault
from agent.runtime.domain.models import (
    ToolExecutionOutput,
    ToolResultEnvelope,
    ToolResultStatus,
)


def test_plain_json_adapter_does_not_interpret_protocol_aliases() -> None:
    output = plain_json_output({
        "isError": True,
        "errorCode": "NOT_A_PLAIN_PROTOCOL",
        "content": "ordinary user JSON",
    })
    assert output.result.status is ToolResultStatus.SUCCESS
    assert output.result.preview["isError"] is True
    assert plain_json_output(None).result.status is ToolResultStatus.NO_OUTPUT
    typed = ToolExecutionOutput(result=ToolResultEnvelope(
        status=ToolResultStatus.INTERRUPT,
        pending_input={"type": "APPROVAL"},
    ))
    assert plain_json_output(typed) is typed


def test_skill_protocol_adapter_owns_is_error_vocabulary() -> None:
    failure = skill_center_output({
        "isError": True,
        "errorCode": "SKILL_STREAM_TRUNCATED",
        "content": "the first sticky failure",
    }).result
    assert failure.status is ToolResultStatus.FAILURE
    assert failure.error_code == "SKILL_STREAM_TRUNCATED"
    assert failure.error_message == "the first sticky failure"


def test_claude_skill_adapter_requires_its_complete_failure_contract() -> None:
    failure = claude_skill_output({
        "status": "error",
        "isError": True,
        "summary": "sandbox unavailable",
        "error": {"code": "SKILL_SANDBOX_UNAVAILABLE", "message": "sandbox unavailable"},
    }).result
    assert failure.status is ToolResultStatus.FAILURE
    assert failure.error_code == "SKILL_SANDBOX_UNAVAILABLE"

    with pytest.raises(RuntimeFault) as malformed:
        claude_skill_output({"isError": True, "summary": "missing error"})
    assert malformed.value.code == "TOOL_RESULT_CONTRACT_INVALID"


@pytest.mark.parametrize(
    "adapter",
    [plain_json_output, skill_center_output, claude_skill_output, a2a_output],
)
def test_every_protocol_adapter_maps_none_to_no_output(adapter) -> None:
    output = adapter(None)

    assert output.result == ToolResultEnvelope(status=ToolResultStatus.NO_OUTPUT)
    assert output.evidence is None


@pytest.mark.parametrize(
    "value",
    [
        b"bytes-are-not-json",
        ("tuples", "are", "python"),
        {1: "object keys must be strings"},
        {"number": float("nan")},
        {"number": float("inf")},
    ],
)
def test_plain_adapter_rejects_non_strict_json_with_stable_fault(value) -> None:
    with pytest.raises(RuntimeFault) as malformed:
        plain_json_output(value)

    assert malformed.value.code == "TOOL_RESULT_CONTRACT_INVALID"


@pytest.mark.parametrize(
    "field,value",
    [
        ("preview", {"payload": b"not-json"}),
        ("preview", {"number": float("nan")}),
        ("pending_input", {"choices": ("yes", "no")}),
    ],
)
def test_tool_result_dto_rejects_non_json_fields(field, value) -> None:
    payload = {
        "status": (
            ToolResultStatus.INTERRUPT
            if field == "pending_input"
            else ToolResultStatus.SUCCESS
        ),
        field: value,
    }

    with pytest.raises(ValidationError):
        ToolResultEnvelope(**payload)


def test_typed_output_bypass_is_revalidated_by_protocol_adapter() -> None:
    unsafe_result = ToolResultEnvelope.model_construct(
        status=ToolResultStatus.SUCCESS,
        preview={"payload": b"not-json"},
    )
    unsafe_output = ToolExecutionOutput.model_construct(
        result=unsafe_result,
        evidence=None,
    )

    with pytest.raises(RuntimeFault) as malformed:
        plain_json_output(unsafe_output)

    assert malformed.value.code == "TOOL_RESULT_CONTRACT_INVALID"


@pytest.mark.parametrize(
    "result,expected",
    [
        (
            ToolResultEnvelope(
                status=ToolResultStatus.SUCCESS,
                preview={"answer": 42},
            ),
            {"answer": 42},
        ),
        (
            ToolResultEnvelope(
                status=ToolResultStatus.SUCCESS,
                preview={"bounded": True},
                result_ref="a" * 64,
            ),
            {"content": {"bounded": True}, "artifact_ref": "a" * 64},
        ),
        (
            ToolResultEnvelope(
                status=ToolResultStatus.SUCCESS,
                preview="created",
                external_object_id="provider-object-7",
            ),
            {"content": "created", "external_object_id": "provider-object-7"},
        ),
        (
            ToolResultEnvelope(status=ToolResultStatus.NO_OUTPUT),
            {"status": "NO_OUTPUT"},
        ),
        (
            ToolResultEnvelope(
                status=ToolResultStatus.INTERRUPT,
                pending_input={"type": "APPROVAL"},
            ),
            {"interrupt": True, "pending_input": {"type": "APPROVAL"}},
        ),
        (
            ToolResultEnvelope(
                status=ToolResultStatus.FAILURE,
                error_code="TOOL_FAILED",
                error_message="failed",
            ),
            {
                "isError": True,
                "errorCode": "TOOL_FAILED",
                "content": "failed",
                "unknownEffect": False,
            },
        ),
        (
            ToolResultEnvelope(
                status=ToolResultStatus.UNKNOWN,
                error_code="TOOL_EFFECT_UNKNOWN",
                error_message="dispatch outcome unknown",
            ),
            {
                "isError": True,
                "errorCode": "TOOL_EFFECT_UNKNOWN",
                "content": "dispatch outcome unknown",
                "unknownEffect": True,
            },
        ),
    ],
)
def test_model_projection_is_single_and_total_for_current_statuses(
    result: ToolResultEnvelope,
    expected,
) -> None:
    assert project_tool_result_for_model(result) == expected
