from __future__ import annotations

from google.genai import types

from agent.config import AgentSettings
from agent.engine.base import RunContext
from agent.engine.native_loop.tools import ToolRegistry, ToolSpec
from agent.runtime.adapters.brokered_tools import (
    NativeBrokerSession,
    build_brokered_native_registry,
    build_runtime_tool_catalog,
)


async def _unused(_args, _context):  # pragma: no cover - classification only
    return None


def _spec(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
        run=_unused,
        concurrency_safe=True,
    )


def test_native_early_concurrency_requires_reviewed_read_only_effect() -> None:
    rc = RunContext(
        run_id="run_boundary",
        activity_id="act_boundary",
        engine="native_loop",
        agent_uuid="demo-agent",
        user_id="demo-user",
        session_id="attempt-local",
        user_message=types.Content(
            role="user", parts=[types.Part.from_text(text="test")],
        ),
        settings=AgentSettings(_env_file=None),
        deadline_at_ms=2_000_000_000_000,
        tool_broker=object(),
        fencing_token=1,
        release_fingerprint="release-v1",
    )
    source = ToolRegistry([
            _spec("deep_translate"),       # explicitly reviewed READ_ONLY Skill
            _spec("new_unreviewed_skill"), # defaults to UNKNOWN_EFFECT
        ])
    registry = build_brokered_native_registry(
        source,
        NativeBrokerSession(
            run_id=rc.run_id,
            activity_id=rc.activity_id,
            fencing_token=rc.fencing_token,
            deadline_at_ms=rc.deadline_at_ms,
            tool_broker=rc.tool_broker,
            catalog=build_runtime_tool_catalog(source),
        ),
    )

    assert registry.get("deep_translate").concurrency_safe is True
    assert registry.get("new_unreviewed_skill").concurrency_safe is False
