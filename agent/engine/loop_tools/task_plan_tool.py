"""TaskPlanTool：把"计划"做成工具，驱动 Agent-Loop 的续推与终止判定。"""
from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext

from agent.engine.loop_tools import TASK_PLAN_KEY, has_open_steps
from agent.tool_args_contract import normalize_task_plan_args

__all__ = ["TASK_PLAN_KEY", "has_open_steps", "update_task_plan"]


def update_task_plan(steps: list[str], current_step: int, tool_context: ToolContext) -> dict[str, Any]:
    """登记或更新当前任务的执行计划与进度（多步任务应先调用本工具规划，再逐步执行）。

    Args:
        steps: 计划的步骤标题列表（按顺序执行）。
        current_step: 当前正在执行的步骤序号（从 1 开始）；全部完成时传 len(steps)+1。
    """
    normalized = normalize_task_plan_args({"steps": steps, "current_step": current_step})
    plan = {"steps": normalized["steps"], "current": normalized["current_step"]}
    tool_context.state[TASK_PLAN_KEY] = plan
    all_done = plan["current"] > len(plan["steps"])
    return {
        "steps": plan["steps"],
        "current": plan["current"],
        "all_done": all_done,
        "note": "计划已登记。请继续执行未完成步骤；全部完成后给出最终答案。",
    }
