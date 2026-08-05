"""Agent 运行时内部的 asyncio 生命周期辅助函数。"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

_T = TypeVar("_T")
_ErrorCallback = Callable[[BaseException], None]


async def await_with_deferred_cancellation(
    awaitable: Awaitable[_T],
    *,
    on_error_after_cancel: _ErrorCallback | None = None,
) -> _T:
    """等待目标任务收口，并把调用方取消延迟到目标任务完成后再传播。

    `asyncio.shield` 只保护被等待的任务不被取消，调用方仍可能反复收到
    `CancelledError`。这里持续等待同一个任务，避免第二次取消打断关键清理；
    若清理同时失败，取消保持为主异常，清理异常通过回调旁路记录。
    """
    task = asyncio.ensure_future(awaitable)
    deferred_cancel: asyncio.CancelledError | None = None

    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            # 目标任务主动取消时直接按其结果传播；否则是调用方取消，继续等待。
            if task.cancelled():
                break
            if deferred_cancel is None:
                deferred_cancel = exc
        except BaseException:
            # 目标任务已经失败，下面统一通过 task.result() 取得准确结果。
            if not task.done():
                raise
            break

    try:
        result = task.result()
    except BaseException as exc:
        if deferred_cancel is None:
            raise
        if on_error_after_cancel is not None:
            try:
                on_error_after_cancel(exc)
            except BaseException:
                # 旁路日志不得覆盖调用方取消。
                pass
        raise deferred_cancel

    if deferred_cancel is not None:
        raise deferred_cancel
    return result
