"""计划事件探测：把模型对 update_task_plan 的调用翻译成 SSE plan_step 事件。"""
from __future__ import annotations

from typing import Any, Optional

from agent.stream.event_converters import StreamEvent

PLAN_TOOL = "update_task_plan"


def _parts(event: Any) -> Any:
    content = getattr(event, "content", None)
    return getattr(content, "parts", None) if content else None


def plan_events_from(event: Any) -> Optional[list[StreamEvent]]:
    """若该 ADK 事件是对 update_task_plan 的调用，返回对应的 plan_step 事件列表；否则 None。"""
    parts = _parts(event)
    if not parts:
        return None
    for part in parts:
        fc = getattr(part, "function_call", None)
        if fc is not None and getattr(fc, "name", "") == PLAN_TOOL:
            args = dict(getattr(fc, "args", None) or {})
            steps = args.get("steps") or []
            current = int(args.get("current_step", 1))
            events: list[StreamEvent] = []
            for i, step in enumerate(steps):
                n = i + 1
                status = "done" if n < current else ("running" if n == current else "planned")
                events.append(StreamEvent("plan_step", {
                    "step": n, "total": len(steps), "title": step, "status": status,
                }))
            return events
    return None


def is_plan_tool_response(event: Any) -> bool:
    """该事件是否为 update_task_plan 的工具结果（用于在流里抑制其噪音）。"""
    parts = _parts(event)
    if not parts:
        return False
    for part in parts:
        fr = getattr(part, "function_response", None)
        if fr is not None and getattr(fr, "name", "") == PLAN_TOOL:
            return True
    return False
