"""合并两个异步源：后台消费 Runner 事件流 + 实时 drain 技能 UI 队列。

技能工具在执行中经 `emit_skill_event` 推入同一队列 → `skill_event` 实时穿插在 Runner 事件之间，
而不必等工具调用整体返回（真实「流式技能输出」的核心机制）。
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any, AsyncIterator, Callable

from agent.asyncio_utils import await_with_deferred_cancellation
from agent.skills.ui_event_queue import reset_ui_queue, set_ui_queue
from agent.stream.event_converters import StreamEvent

_SENTINEL: object = object()
logger = logging.getLogger(__name__)


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
        except asyncio.CancelledError:
            # 客户端断开/上游关闭生成器属于正常取消，不应伪装成 error SSE。
            raise
        except Exception as exc:  # noqa: BLE001 - Runner 异常转 error 事件，保证收口
            await queue.put(StreamEvent("error", {"message": str(exc)}))
        finally:
            queue.put_nowait(_SENTINEL)

    task = asyncio.create_task(pump())
    primary_failed = False
    try:
        while True:
            item = await queue.get()
            if item is _SENTINEL:
                break
            yield item
    except BaseException:
        primary_failed = True
        raise
    finally:
        async def close_sources() -> None:
            if not task.done():
                task.cancel()
            # pump 取消是关闭协议的一部分，不生成 error 事件。
            with suppress(asyncio.CancelledError):
                await task
            close = getattr(runner_events, "aclose", None)
            if close is not None:
                await close()

        def log_close_error(exc: BaseException) -> None:
            logger.warning(
                "runner stream cleanup failed after cancellation: %s",
                type(exc).__name__,
            )

        try:
            await await_with_deferred_cancellation(
                close_sources(),
                on_error_after_cancel=log_close_error,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 已有主异常优先于关闭异常
            if not primary_failed:
                raise
            logger.warning(
                "runner stream cleanup failed while preserving primary error: %s",
                type(exc).__name__,
            )
        finally:
            reset_ui_queue(token)
