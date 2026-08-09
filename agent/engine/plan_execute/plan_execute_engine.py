"""Gen1 Plan-Execute 引擎：先产出整张计划（规划），再逐步执行（执行）。"""
from __future__ import annotations

from typing import TYPE_CHECKING, AsyncIterator

from agent.engine.base import ReasoningEngine, RunContext, extract_text
from agent.engine.plan_execute.decision_planner import DecisionPlanner
from agent.engine.plan_execute.execution_planner import ExecutionPlanner
from agent.runtime.domain.models import EngineOutcome, EngineOutcomeKind, WorkingState
from agent.stream.event_converters import StreamEvent

if TYPE_CHECKING:
    from agent.context import AgentContext


class PlanExecuteEngine(ReasoningEngine):
    def __init__(self, ctx: "AgentContext") -> None:
        self._planner = DecisionPlanner(ctx.chat)
        self._executor = ExecutionPlanner(ctx)

    async def run_stream(self, ctx: RunContext) -> AsyncIterator[StreamEvent]:
        # Gen1 与 Gen2 的关键差异全在这个方法里：多了一个"规划相"。
        # 注意执行相用的仍然是同一个 ADK 循环（Runner.run_async），
        # 所以两代引擎的区别不是"有没有循环"，而是"循环外面包了什么"。
        query = extract_text(ctx.user_message)

        # 规划相：产出显式计划并以 plan_step 事件流出
        # 这一步是独立的一次 LLM 调用（走轻量 AgentChatClient，不带工具），
        # 计划一旦定下就写进执行相的 instruction，中途不会再改——这就是"计划是铁轨"。
        checkpoint = ctx.engine_checkpoint
        restored = bool(checkpoint and checkpoint.working_state.model_plan)
        if restored:
            plan = [str(item["title"]) for item in checkpoint.working_state.model_plan]
        else:
            plan = await self._planner.plan(query)
            if ctx.runtime_io is not None:
                await ctx.runtime_io.checkpoint(
                    WorkingState(
                        goal=query,
                        model_plan=[
                            {"step": i + 1, "title": step, "status": "planned"}
                            for i, step in enumerate(plan)
                        ],
                        release_fingerprint=ctx.release_fingerprint,
                    ),
                    expected_revision=checkpoint.revision if checkpoint else 0,
                    engine_state={"phase": "EXECUTING_PLAN", "plan": plan},
                )
            # save_checkpoint atomically appends MODEL_PLAN_UPDATED events for a
            # changed WorkingState.model_plan.  Publishing request-local copies
            # here would reintroduce a checkpoint/event crash window.

        # 执行相：ADK Runner 带工具执行，流式转出 text / tool_call / tool_result
        async for se in self._executor.execute(ctx, plan):
            yield se

        yield StreamEvent("plan_step", {"step": len(plan), "total": len(plan), "status": "done"})
        # Event transport completion is not a success signal.  Record the
        # attempt-local outcome only after both planning and execution reached
        # their explicit successful control boundary.
        ctx.engine_outcome = EngineOutcome(kind=EngineOutcomeKind.COMPLETED)
