from __future__ import annotations

from google.genai import types

from agent.config import AgentSettings
from agent.engine.base import RunContext
from agent.engine.loop_tools import TASK_PLAN_KEY
from agent.engine.loop_tools.task_plan_tool import update_task_plan
from agent.engine.native_loop.tools import NativeToolContext, ToolRegistry, from_function
from agent.runtime.adapters.brokered_tools import broker_native_registry
from agent.runtime.domain.models import ToolResultEnvelope, ToolResultStatus


class _CommittedReplayBroker:
    async def execute(self, **_kwargs) -> ToolResultEnvelope:
        return ToolResultEnvelope(
            status=ToolResultStatus.SUCCESS,
            preview={
                "steps": ["inspect", "answer"],
                "current": 2,
                "all_done": False,
            },
        )


async def test_native_committed_plan_replay_rebuilds_request_local_mirror() -> None:
    rc = RunContext(
        run_id="run_plan_replay",
        activity_id="act_plan_replay",
        engine="native_loop",
        agent_uuid="demo-agent",
        user_id="demo-user",
        session_id="attempt-local",
        user_message=types.Content(
            role="user", parts=[types.Part.from_text(text="plan")],
        ),
        settings=AgentSettings(_env_file=None),
        deadline_at_ms=2_000_000_000_000,
        tool_broker=_CommittedReplayBroker(),
        fencing_token=3,
        release_fingerprint="release-v1",
    )
    registry = broker_native_registry(
        ToolRegistry([from_function(update_task_plan)]), rc,
    )
    wrapped = registry.get("update_task_plan")
    assert wrapped is not None
    tool_context = NativeToolContext(
        function_call_id="provider-id-after-restart",
        invocation_id="attempt-after-restart",
        logical_key="native:turn:0:call:0",
        state={},
    )

    await wrapped.run(
        {"steps": ["inspect", "answer"], "current_step": 2},
        tool_context,
    )
    assert tool_context.state[TASK_PLAN_KEY] == {
        "steps": ["inspect", "answer"],
        "current": 2,
    }
