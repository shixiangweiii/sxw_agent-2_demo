"""合并两个异步源：后台消费 Runner 事件流 + 实时 drain 技能 UI 队列。

技能工具在执行中经 `emit_skill_event` 推入同一队列 → `skill_event` 实时穿插在 Runner 事件之间，
而不必等工具调用整体返回（真实「流式技能输出」的核心机制）。
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Callable

from agent.skills.ui_event_queue import reset_ui_queue, set_ui_queue
from agent.stream.event_converters import StreamEvent

_SENTINEL: object = object()


async def merge_runner_events(
    runner_events: AsyncIterator[Any],
    convert: Callable[[Any], list[StreamEvent]],
) -> AsyncIterator[StreamEvent]:
    queue: "asyncio.Queue[Any]" = asyncio.Queue()
    token = set_ui_queue(queue)   # 必须在 create_task 之前设置：子任务复制当前 context 才能拿到队列

    async def pump() -> None:
        try:
            async for event in runner_events:
                for se in convert(event):
                    await queue.put(se)
        except Exception as exc:  # noqa: BLE001 - Runner 异常转 error 事件，保证收口
            await queue.put(StreamEvent("error", {"message": str(exc)}))
        finally:
            await queue.put(_SENTINEL)

    task = asyncio.create_task(pump())
    try:
        while True:
            item = await queue.get()
            if item is _SENTINEL:
                break
            yield item
    finally:
        if not task.done():
            await task
        reset_ui_queue(token)
