"""Gen1 Plan-Execute 引擎：先产出整张计划（规划），再逐步执行（执行）。"""
from __future__ import annotations

from typing import TYPE_CHECKING, AsyncIterator

from agent.engine.base import ReasoningEngine, RunContext, extract_text
from agent.engine.plan_execute.decision_planner import DecisionPlanner
from agent.engine.plan_execute.execution_planner import ExecutionPlanner
from agent.stream.event_converters import StreamEvent

if TYPE_CHECKING:
    from agent.context import AgentContext


class PlanExecuteEngine(ReasoningEngine):
    def __init__(self, ctx: "AgentContext") -> None:
        self._planner = DecisionPlanner(ctx.chat)
        self._executor = ExecutionPlanner(ctx)

    async def run_stream(self, ctx: RunContext) -> AsyncIterator[StreamEvent]:
        query = extract_text(ctx.user_message)

        # 规划相：产出显式计划并以 plan_step 事件流出
        plan = await self._planner.plan(query)
        for i, step in enumerate(plan):
            yield StreamEvent("plan_step", {
                "step": i + 1, "total": len(plan), "title": step, "status": "planned",
            })

        # 执行相：ADK Runner 带工具执行，流式转出 text / tool_call / tool_result
        async for se in self._executor.execute(ctx, plan):
            yield se

        yield StreamEvent("plan_step", {"step": len(plan), "total": len(plan), "status": "done"})
        yield StreamEvent("done", {"finish_reason": "stop"})
