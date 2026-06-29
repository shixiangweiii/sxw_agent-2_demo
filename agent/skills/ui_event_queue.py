"""每请求 UI 事件队列（contextvar）：技能流式展示帧经此汇入 agent SSE。

对应原项目 `single_loop/ui_event_queue.py`：工具在执行中把展示帧推入队列，引擎并发 drain。
"""
from __future__ import annotations

import asyncio
import contextvars
from typing import Optional

from agent.stream.event_converters import StreamEvent

_queue_var: contextvars.ContextVar[Optional["asyncio.Queue[StreamEvent]"]] = contextvars.ContextVar(
    "skill_ui_queue", default=None,
)


def set_ui_queue(
    queue: "asyncio.Queue[StreamEvent]",
) -> "contextvars.Token[Optional[asyncio.Queue[StreamEvent]]]":
    return _queue_var.set(queue)


def reset_ui_queue(token: "contextvars.Token[Optional[asyncio.Queue[StreamEvent]]]") -> None:
    _queue_var.reset(token)


def get_ui_queue() -> Optional["asyncio.Queue[StreamEvent]"]:
    return _queue_var.get()


async def emit_skill_event(event: StreamEvent) -> None:
    """把技能展示帧推入当前请求的 UI 队列；队列未设置（未接引擎）则静默丢弃。"""
    queue = _queue_var.get()
    if queue is not None:
        await queue.put(event)
