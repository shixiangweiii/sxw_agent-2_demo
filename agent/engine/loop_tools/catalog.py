"""One public Tool surface shared by the ADK and Native loop engines."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent.engine.loop_tools import resolve_sub_agent_engine
from agent.engine.loop_tools.task_plan_tool import update_task_plan
from agent.engine.loop_tools.tool_search_tool import build_deferred_tools, tool_search

if TYPE_CHECKING:
    from agent.context import AgentContext


def collect_loop_tools(
    context: "AgentContext",
    *,
    run_engine: str,
) -> list[Any]:
    """Return the complete, ordered loop catalog for one engine release.

    The sub-runner implementation may differ, but both variants intentionally
    expose the same name, description and parameter schema.  Worker startup
    compares their normalized declarations before activating any release.
    """

    tools: list[Any] = list(context.tools)
    tools.append(update_task_plan)
    tools.append(tool_search)
    tools.extend(build_deferred_tools())
    sub_engine = resolve_sub_agent_engine(context.settings.sub_agent_engine, run_engine)
    if sub_engine == "adk":
        from agent.engine.loop_tools.sub_agent_tool import build_sub_agent_tool  # noqa: PLC0415

        tools.append(build_sub_agent_tool(context.llm))
    else:
        from agent.engine.native_loop.sub_agent import build_researcher_tool  # noqa: PLC0415

        tools.append(build_researcher_tool(context.chat))
    return tools


__all__ = ["collect_loop_tools"]
