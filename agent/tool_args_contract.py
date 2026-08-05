"""工具参数防护的共享契约。

模型层用 sentinel 把无法安全恢复的非对象参数转换成合法 FunctionCall；
插件层再在真实工具分发前短路。任务计划参数也在这里统一规范化，避免
模型恢复、工具写状态和 SSE 事件探测使用不同规则。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

TOOL_ARGS_PARSE_ERROR_KEY = "__sxw_agent_tool_args_parse_error_v1__"

_STEP_TEXT_KEYS = ("content", "tasks", "task", "description")
_RUNNING_STATUSES = {"in_progress", "running"}
_DONE_STATUSES = {"completed", "done"}


class TaskPlanArgumentsError(ValueError):
    """update_task_plan 参数无法无歧义地规范化。"""


def build_tool_args_parse_error(error_type: str) -> dict[str, Any]:
    """构造不包含原始参数的版本化 sentinel。"""
    return {
        TOOL_ARGS_PARSE_ERROR_KEY: {
            "errorType": error_type,
        },
    }


def get_tool_args_parse_error(tool_args: Any) -> Optional[dict[str, Any]]:
    """若参数包含工具解析 sentinel，返回其安全元信息。"""
    if not isinstance(tool_args, Mapping):
        return None
    marker = tool_args.get(TOOL_ARGS_PARSE_ERROR_KEY)
    return dict(marker) if isinstance(marker, Mapping) else None


def normalize_task_plan_args(raw_args: Any) -> dict[str, Any]:
    """把计划参数规范化为 ``steps`` + ``current_step``。

    支持公开对象形态，也支持部分模型偶发生成的顶层数组。数组中的步骤
    必须全为字符串或全为对象，拒绝混合类型和无法识别的空内容。
    """
    if isinstance(raw_args, Mapping):
        if "steps" not in raw_args:
            raise TaskPlanArgumentsError("缺少 steps")
        steps, statuses = _normalize_steps(raw_args["steps"])
        if "current_step" not in raw_args:
            raise TaskPlanArgumentsError("缺少 current_step")
        current_step = _normalize_current_step(raw_args["current_step"], len(steps))
    elif isinstance(raw_args, list):
        steps, statuses = _normalize_steps(raw_args)
        current_step = _infer_current_step(statuses, len(steps))
    else:
        raise TaskPlanArgumentsError("计划参数必须是对象或数组")

    return {"steps": steps, "current_step": current_step}


def _normalize_steps(raw_steps: Any) -> tuple[list[str], Optional[list[str]]]:
    if not isinstance(raw_steps, list) or not raw_steps:
        raise TaskPlanArgumentsError("steps 必须是非空数组")

    if all(isinstance(item, str) for item in raw_steps):
        steps = [_normalize_step_text(item) for item in raw_steps]
        return steps, None

    if all(isinstance(item, Mapping) for item in raw_steps):
        steps: list[str] = []
        statuses: list[str] = []
        for item in raw_steps:
            text = next(
                (
                    value
                    for key in _STEP_TEXT_KEYS
                    if isinstance((value := item.get(key)), str) and value.strip()
                ),
                None,
            )
            if text is None:
                raise TaskPlanArgumentsError("计划步骤对象缺少可识别的标题")
            steps.append(_normalize_step_text(text))
            status = item.get("status")
            statuses.append(status.strip().lower() if isinstance(status, str) else "")
        return steps, statuses

    raise TaskPlanArgumentsError("steps 不允许混合字符串和对象")


def _normalize_step_text(value: str) -> str:
    text = value.strip()
    if not text:
        raise TaskPlanArgumentsError("计划步骤不能为空")
    return text


def _normalize_current_step(value: Any, step_count: int) -> int:
    if isinstance(value, bool):
        raise TaskPlanArgumentsError("current_step 不能是布尔值")
    if isinstance(value, int):
        current_step = value
    elif isinstance(value, str) and value.strip().isdigit():
        current_step = int(value.strip())
    else:
        raise TaskPlanArgumentsError("current_step 必须是整数或数字字符串")
    if not 1 <= current_step <= step_count + 1:
        raise TaskPlanArgumentsError("current_step 超出计划范围")
    return current_step


def _infer_current_step(statuses: Optional[list[str]], step_count: int) -> int:
    if statuses is None:
        return 1
    for index, status in enumerate(statuses, start=1):
        if status in _RUNNING_STATUSES:
            return index
    for index, status in enumerate(statuses, start=1):
        if status not in _DONE_STATUSES:
            return index
    return step_count + 1
