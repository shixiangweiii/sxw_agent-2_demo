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


class _PumpFailure:
    def __init__(self, error: BaseException) -> None:
        self.error = error


async def merge_runner_events(
    runner_events: AsyncIterator[Any],
    convert: Callable[[Any], list[StreamEvent]],
) -> AsyncIterator[StreamEvent]:
    # 设计要点：把"两个并发的事件来源"汇成一条队列，再由本生成器单点吐出。
    #   来源 A：ADK Runner 事件流（模型文本、工具调用、工具结果）——由下面的 pump 任务搬运；
    #   来源 B：技能/沙箱工具在"一次工具调用内部"推进来的展示帧——由工具自己 put。
    # 没有它的话，技能执行期间前端会长时间静默，直到工具整体返回才一次性看到结果。
    queue: "asyncio.Queue[Any]" = asyncio.Queue()
    token = set_ui_queue(queue)   # 必须在 create_task 之前设置：子任务复制当前 context 才能拿到队列

    # pump：后台把 Runner 事件搬进队列。跑在独立 task 里，才能与来源 B 真正并发。
    async def pump() -> None:
        try:
            async for event in runner_events:
                for se in convert(event):        # convert = 引擎传进来的事件翻译器
                    await queue.put(se)
        except asyncio.CancelledError:
            # 客户端断开/上游关闭生成器属于正常取消，不应伪装成 error SSE。
            raise
        except Exception as exc:  # noqa: BLE001 - 把原异常交给 EngineAdapter 分类
            # 不再把 Runner 失败伪装成普通流事件。否则生成器会“正常”结束，
            # 上层无法区分真实完成与异常后 EOF。
            await queue.put(_PumpFailure(exc))
        finally:
            # 哨兵是消费侧唯一的"结束信号"。用 put_nowait 而非 await put：
            # 取消场景下 await 可能挂住，导致消费侧永远等不到结束。
            queue.put_nowait(_SENTINEL)

    task = asyncio.create_task(pump())
    primary_failed = False
    try:
        # 消费侧：谁先入队就先吐谁，于是 skill_event 能实时穿插在 tool_call / text 之间。
        while True:
            item = await queue.get()
            if item is _SENTINEL:
                break
            if isinstance(item, _PumpFailure):
                raise item.error
            yield item
    except BaseException:
        primary_failed = True
        raise
    finally:
        # 收口顺序很讲究：先停 pump 并等它真的退出，再关 Runner 事件流。
        # 反过来（先关流）会让 pump 在一个已关闭的生成器上迭代而报错。
        async def close_sources() -> None:
            if not task.done():
                task.cancel()
            # pump 取消是关闭协议的一部分，不生成 error 事件。
            with suppress(asyncio.CancelledError):
                await task
            close = getattr(runner_events, "aclose", None)
            if close is not None:
                await close()   # 触发 ADK 生成器的 finally → 在途工具/沙箱得以清理

        def log_close_error(exc: BaseException) -> None:
            logger.warning(
                "runner stream cleanup failed after cancellation: %s",
                type(exc).__name__,
            )

        try:
            # 为什么不能直接 await close_sources()：客户端断开时本协程已处于"被取消"状态，
            # 裸 await 会被再次抛入的 CancelledError 打断，清理做到一半就中止。
            # await_with_deferred_cancellation 会持续等清理完成，再把取消重新抛出去。
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
