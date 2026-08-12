from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.adk.tools import BaseTool

from agent.engine.native_loop.tools import ToolRegistry, ToolSpec, build_registry
from agent.runtime.adapters.brokered_tools import (
    NativeBrokerSession,
    build_brokered_native_registry,
    build_runtime_tool_catalog,
)
from agent.runtime.application.tool_catalog import ToolBinding, ToolCatalog
from agent.runtime.application.tool_outputs import plain_json_output
from agent.runtime.domain.models import ToolEffectClass, ToolManifest


async def _run(_arguments, _context):
    return {"ok": True}


def _binding(name: str = "one") -> ToolBinding:
    return ToolBinding(
        name=name,
        description="strict tool",
        parameters={"type": "object", "properties": {}},
        manifest=ToolManifest(
            name=name,
            release_digest="a" * 64,
            effect_class=ToolEffectClass.READ_ONLY,
            timeout_seconds=1,
        ),
        executor=_run,
        result_adapter=plain_json_output,
        implementation="tests.strict_tool",
    )


def test_catalog_rejects_duplicate_names_and_invalid_schema() -> None:
    with pytest.raises(ValueError, match="duplicate tool name"):
        ToolCatalog([_binding(), _binding()])

    with pytest.raises(ValueError, match="root type must be object"):
        ToolBinding(
            name="bad",
            description="bad schema",
            parameters={"type": "string"},
            manifest=_binding("bad").manifest,
            executor=_run,
            result_adapter=plain_json_output,
            implementation="tests.bad",
        )

    with pytest.raises(ValueError, match="catalog exceeds"):
        ToolCatalog([_binding()], max_bytes=1)


def test_catalog_snapshot_is_immutable_and_digest_covers_schema() -> None:
    first = ToolCatalog([_binding()])
    snapshot = first.snapshot()
    snapshot[0]["parameters"]["properties"]["injected"] = {"type": "string"}
    assert first.snapshot()[0]["parameters"]["properties"] == {}

    changed = ToolBinding(
        name="one",
        description="strict tool",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
        manifest=_binding().manifest,
        executor=_run,
        result_adapter=plain_json_output,
        implementation="tests.strict_tool",
    )
    assert ToolCatalog([changed]).digest != first.digest


def test_native_registry_duplicate_and_adaptation_failures_are_fatal() -> None:
    spec = ToolSpec(
        name="duplicate",
        description="duplicate",
        parameters={"type": "object", "properties": {}},
        run=_run,
    )
    with pytest.raises(ValueError, match="duplicate tool name"):
        ToolRegistry([spec, spec])

    class BrokenDeclarationTool(BaseTool):
        def __init__(self) -> None:
            super().__init__(name="broken", description="broken declaration")

        def _get_declaration(self):
            raise RuntimeError("catalog corruption")

        async def run_async(self, *, args, tool_context):
            return None

    with pytest.raises(RuntimeError, match="catalog corruption"):
        build_registry([BrokenDeclarationTool()])


def test_runtime_catalog_builds_one_strict_public_surface() -> None:
    def sample(value: str) -> dict[str, str]:
        """Return the value.

        Args:
            value: input value.
        """
        return {"value": value}

    catalog = build_runtime_tool_catalog([sample])
    assert len(catalog) == 1
    binding = catalog.require("sample")
    assert binding.description == "Return the value."
    assert binding.parameters == {
        "type": "object",
        "properties": {"value": {"type": "string", "description": "input value."}},
        "required": ("value",),
    }
    assert binding.parameter_schema()["required"] == ["value"]
    assert len(binding.manifest.release_digest) == 64


def test_catalog_binding_accepts_nested_schema_arrays_in_native_registry() -> None:
    spec = ToolSpec(
        name="required_input",
        description="require one input",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        run=_run,
        implementation="tests.required_input",
    )
    registry = ToolRegistry([spec])
    catalog = build_runtime_tool_catalog(registry)
    bound = build_brokered_native_registry(
        registry,
        NativeBrokerSession(
            run_id="run-current",
            activity_id="activity-current",
            fencing_token=1,
            deadline_at_ms=2_000_000_000_000,
            tool_broker=object(),
            catalog=catalog,
        ),
    )

    rebound = bound.get("required_input")
    assert rebound is not None
    assert rebound.parameters["required"] == ["value"]
    assert rebound.implementation == "tests.required_input"
