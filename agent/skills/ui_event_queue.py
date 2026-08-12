"""每请求 UI 事件队列（contextvar）：技能流式展示帧经此汇入 agent SSE。

对应原项目 `single_loop/ui_event_queue.py`：工具在执行中把展示帧推入队列，引擎并发 drain。
"""
from __future__ import annotations

import asyncio
import contextvars
from typing import Awaitable, Callable, Optional

from agent.stream.event_converters import StreamEvent

_queue_var: contextvars.ContextVar[Optional["asyncio.Queue[StreamEvent]"]] = contextvars.ContextVar(
    "skill_ui_queue", default=None,
)
_sink_var: contextvars.ContextVar[
    Optional[Callable[[StreamEvent], Awaitable[None]]]
] = contextvars.ContextVar("skill_ui_sink", default=None)


def set_ui_queue(
    queue: "asyncio.Queue[StreamEvent]",
) -> "contextvars.Token[Optional[asyncio.Queue[StreamEvent]]]":
    return _queue_var.set(queue)


def reset_ui_queue(token: "contextvars.Token[Optional[asyncio.Queue[StreamEvent]]]") -> None:
    _queue_var.reset(token)


def get_ui_queue() -> Optional["asyncio.Queue[StreamEvent]"]:
    return _queue_var.get()


def set_ui_sink(
    sink: Callable[[StreamEvent], Awaitable[None]],
) -> contextvars.Token[Optional[Callable[[StreamEvent], Awaitable[None]]]]:
    """Install Native's direct awaited Runtime sink for the current attempt."""
    return _sink_var.set(sink)


def reset_ui_sink(
    token: contextvars.Token[Optional[Callable[[StreamEvent], Awaitable[None]]]],
) -> None:
    _sink_var.reset(token)


async def emit_skill_event(event: StreamEvent) -> None:
    """Await Native's durable sink, else use the ADK attempt-local queue."""
    sink = _sink_var.get()
    if sink is not None:
        await sink(event)
        return
    queue = _queue_var.get()
    if queue is not None:
        await queue.put(event)
