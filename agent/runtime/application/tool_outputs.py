"""Protocol-specific adapters into the one internal Tool execution output.

The Broker never interprets framework-shaped dictionaries.  Each external
protocol is normalized exactly once, at the adapter that owns that protocol.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent.runtime.domain.errors import RuntimeFault
from agent.runtime.domain.models import (
    ToolExecutionOutput,
    ToolResultEnvelope,
    ToolResultStatus,
    require_json_value,
)

ToolResultAdapter = Callable[[Any], ToolExecutionOutput]


def plain_json_output(value: Any) -> ToolExecutionOutput:
    """Adapt builtin/plain Python results without recognizing protocol aliases."""

    if isinstance(value, ToolExecutionOutput):
        return _validated_typed_output(value, protocol="plain")
    if isinstance(value, ToolResultEnvelope):
        return _output_from_result(value, protocol="plain")
    if value is None:
        return ToolExecutionOutput(
            result=ToolResultEnvelope(status=ToolResultStatus.NO_OUTPUT)
        )
    _require_json_value(value, protocol="plain")
    return ToolExecutionOutput(
        result=ToolResultEnvelope(status=ToolResultStatus.SUCCESS, preview=value)
    )


def skill_center_output(value: Any) -> ToolExecutionOutput:
    """Adapt SelectedSkillTool's current response vocabulary."""

    if isinstance(value, ToolExecutionOutput):
        return _validated_typed_output(value, protocol="skill-center")
    if value is None:
        return ToolExecutionOutput(
            result=ToolResultEnvelope(status=ToolResultStatus.NO_OUTPUT)
        )
    if not isinstance(value, dict):
        raise _contract_fault("Skill result must be an object", protocol="skill-center")
    _require_json_value(value, protocol="skill-center")
    if value.get("isError") is True:
        code = _required_error_code(value.get("errorCode"), protocol="skill-center")
        message = _required_error_message(value.get("content"), protocol="skill-center")
        return ToolExecutionOutput(result=ToolResultEnvelope(
            status=ToolResultStatus.FAILURE,
            error_code=code,
            error_message=message,
        ))
    return ToolExecutionOutput(
        result=ToolResultEnvelope(status=ToolResultStatus.SUCCESS, preview=value)
    )


def claude_skill_output(value: Any) -> ToolExecutionOutput:
    """Adapt Claude Skill's SkillCallResult wire object."""

    if isinstance(value, ToolExecutionOutput):
        return _validated_typed_output(value, protocol="claude-skill")
    if value is None:
        return ToolExecutionOutput(
            result=ToolResultEnvelope(status=ToolResultStatus.NO_OUTPUT)
        )
    if not isinstance(value, dict):
        raise _contract_fault("Claude Skill result must be an object", protocol="claude-skill")
    _require_json_value(value, protocol="claude-skill")
    if value.get("isError") is True:
        error = value.get("error")
        if not isinstance(error, dict):
            raise _contract_fault(
                "Claude Skill failure requires an error object",
                protocol="claude-skill",
            )
        return ToolExecutionOutput(result=ToolResultEnvelope(
            status=ToolResultStatus.FAILURE,
            error_code=_required_error_code(error.get("code"), protocol="claude-skill"),
            error_message=_required_error_message(
                error.get("message") or value.get("summary"),
                protocol="claude-skill",
            ),
        ))
    if value.get("status") != "success" or value.get("isError") is not False:
        raise _contract_fault(
            "Claude Skill success must declare status=success and isError=false",
            protocol="claude-skill",
        )
    return ToolExecutionOutput(
        result=ToolResultEnvelope(status=ToolResultStatus.SUCCESS, preview=value)
    )


def a2a_output(value: Any) -> ToolExecutionOutput:
    """Adapt the local A2A bridge result; remote failures are explicit."""

    if isinstance(value, ToolExecutionOutput):
        return _validated_typed_output(value, protocol="a2a")
    if value is None:
        return ToolExecutionOutput(
            result=ToolResultEnvelope(status=ToolResultStatus.NO_OUTPUT)
        )
    _require_json_value(value, protocol="a2a")
    if isinstance(value, dict) and "error" in value:
        return ToolExecutionOutput(result=ToolResultEnvelope(
            status=ToolResultStatus.FAILURE,
            error_code="A2A_EXECUTION_FAILED",
            error_message=_required_error_message(value.get("error"), protocol="a2a"),
        ))
    return ToolExecutionOutput(
        result=ToolResultEnvelope(status=ToolResultStatus.SUCCESS, preview=value)
    )


def adapter_for_tool(tool: Any) -> ToolResultAdapter:
    """Select an adapter by the concrete protocol-owning tool class."""

    # Imports are deliberately local: application contracts must not make
    # Runtime startup import optional ADK/Skill implementations eagerly.
    from agent.claude_skill.claude_skill_tool import ClaudeSkillTool  # noqa: PLC0415
    from agent.skills.selected_skill_tool import SelectedSkillTool  # noqa: PLC0415

    if isinstance(tool, SelectedSkillTool):
        return skill_center_output
    if isinstance(tool, ClaudeSkillTool):
        return claude_skill_output
    try:
        from google.adk.tools.agent_tool import AgentTool  # noqa: PLC0415
    except ImportError:  # pragma: no cover - Worker pins ADK
        AgentTool = ()  # type: ignore[assignment,misc]
    if isinstance(tool, AgentTool):
        protocol = getattr(tool, "result_protocol", None)
        if protocol == "plain":
            return plain_json_output
        if protocol == "a2a":
            return a2a_output
        raise ValueError(
            f"AgentTool {getattr(tool, 'name', type(tool).__name__)} must "
            "explicitly declare result_protocol=plain|a2a"
        )
    return plain_json_output


def _require_json_value(value: Any, *, protocol: str) -> None:
    try:
        require_json_value(value)
    except ValueError as exc:
        raise _contract_fault(
            "tool result is not deterministic JSON",
            protocol=protocol,
        ) from exc


def _validated_typed_output(
    value: ToolExecutionOutput,
    *,
    protocol: str,
) -> ToolExecutionOutput:
    try:
        ToolExecutionOutput.model_validate(value.model_dump(mode="python"))
    except Exception as exc:
        raise _contract_fault(
            "typed tool result is not strict JSON-safe ToolExecutionOutput",
            protocol=protocol,
        ) from exc
    return value


def _output_from_result(
    value: ToolResultEnvelope,
    *,
    protocol: str,
) -> ToolExecutionOutput:
    try:
        result = ToolResultEnvelope.model_validate(value.model_dump(mode="python"))
        return ToolExecutionOutput(result=result)
    except Exception as exc:
        raise _contract_fault(
            "typed tool result is not strict JSON-safe ToolResultEnvelope",
            protocol=protocol,
        ) from exc


def _required_error_code(value: Any, *, protocol: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _contract_fault("failure requires a non-empty error code", protocol=protocol)
    return value.strip()[:128]


def _required_error_message(value: Any, *, protocol: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _contract_fault("failure requires a non-empty error message", protocol=protocol)
    return value.strip()[:8192]


def _contract_fault(message: str, *, protocol: str) -> RuntimeFault:
    return RuntimeFault(
        "TOOL_RESULT_CONTRACT_INVALID",
        message,
        500,
        {"protocol": protocol},
    )


__all__ = [
    "ToolResultAdapter",
    "a2a_output",
    "adapter_for_tool",
    "claude_skill_output",
    "plain_json_output",
    "skill_center_output",
]
