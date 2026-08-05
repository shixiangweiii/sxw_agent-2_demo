"""Claude Skill 调用的资源门控；只治理执行，不承担任务编排。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Iterable

from agent.asyncio_utils import await_with_deferred_cancellation

logger = logging.getLogger(__name__)


@dataclass
class _InvocationGate:
    """writer 排他的异步读写门：只有全部为 parallel-safe 时才共享。"""

    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    readers: int = 0
    writer: bool = False
    waiting_writers: int = 0
    users: int = 0

    async def acquire(self, *, shared: bool) -> None:
        async with self.condition:
            if shared:
                # writer 优先，防止持续 safe 调用让 non-safe 调用饥饿。
                while self.writer or self.waiting_writers:
                    await self.condition.wait()
                self.readers += 1
                return

            self.waiting_writers += 1
            try:
                while self.writer or self.readers:
                    await self.condition.wait()
                self.writer = True
            finally:
                self.waiting_writers -= 1
                self.condition.notify_all()

    async def release(self, *, shared: bool) -> None:
        async with self.condition:
            if shared:
                self.readers -= 1
            else:
                self.writer = False
            self.condition.notify_all()


class SkillExecutionCoordinator:
    """进程级并发上限 + invocation 读写门 + 命名独占资源锁。"""

    def __init__(self, max_parallel_calls: int) -> None:
        self._global = asyncio.Semaphore(max(1, max_parallel_calls))
        self._registry_guard = asyncio.Lock()
        self._invocation_gates: dict[str, _InvocationGate] = {}
        self._resource_locks: dict[str, asyncio.Lock] = {}

    async def _claim_invocation_gate(self, invocation_id: str) -> _InvocationGate:
        async with self._registry_guard:
            gate = self._invocation_gates.get(invocation_id)
            if gate is None:
                gate = _InvocationGate()
                self._invocation_gates[invocation_id] = gate
            gate.users += 1
            return gate

    async def _release_invocation_gate(
        self,
        invocation_id: str,
        gate: _InvocationGate,
    ) -> None:
        async with self._registry_guard:
            gate.users -= 1
            if gate.users == 0 and self._invocation_gates.get(invocation_id) is gate:
                self._invocation_gates.pop(invocation_id, None)

    async def _get_resource_locks(self, resources: Iterable[str]) -> list[asyncio.Lock]:
        names = sorted(set(resources))
        async with self._registry_guard:
            return [self._resource_locks.setdefault(name, asyncio.Lock()) for name in names]

    async def _release_lease(
        self,
        *,
        parent_invocation_id: str,
        invocation_gate: _InvocationGate,
        shared: bool,
        gate_acquired: bool,
        resource_locks: list[asyncio.Lock],
        global_acquired: bool,
    ) -> None:
        if global_acquired:
            self._global.release()
        for lock in reversed(resource_locks):
            lock.release()
        try:
            if gate_acquired:
                await invocation_gate.release(shared=shared)
        finally:
            await self._release_invocation_gate(parent_invocation_id, invocation_gate)

    @staticmethod
    async def _await_release(
        awaitable: Awaitable[Any],
        *,
        suppress_errors: bool,
    ) -> None:
        """等待租约完整释放，并保持既有主异常或取消优先。"""

        def log_release_error(exc: BaseException) -> None:
            logger.warning("skill lease cleanup failed: %s", type(exc).__name__)

        try:
            await await_with_deferred_cancellation(
                awaitable,
                on_error_after_cancel=log_release_error,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not suppress_errors:
                raise
            logger.warning("skill lease cleanup failed: %s", type(exc).__name__)

    @asynccontextmanager
    async def acquire(
        self,
        *,
        parent_invocation_id: str,
        parallel_safe: bool,
        exclusive_resources: tuple[str, ...],
    ) -> AsyncIterator[None]:
        """按元数据取得执行租约；排队时间由外层调用总超时约束。"""
        shared = parallel_safe and not exclusive_resources
        invocation_gate = await self._claim_invocation_gate(parent_invocation_id)
        gate_acquired = False
        resource_locks: list[asyncio.Lock] = []
        global_acquired = False
        primary_failed = False
        try:
            await invocation_gate.acquire(shared=shared)
            gate_acquired = True

            for lock in await self._get_resource_locks(exclusive_resources):
                await lock.acquire()
                resource_locks.append(lock)

            await self._global.acquire()
            global_acquired = True
            yield
        except BaseException:
            primary_failed = True
            raise
        finally:
            await self._await_release(
                self._release_lease(
                    parent_invocation_id=parent_invocation_id,
                    invocation_gate=invocation_gate,
                    shared=shared,
                    gate_acquired=gate_acquired,
                    resource_locks=resource_locks,
                    global_acquired=global_acquired,
                ),
                suppress_errors=primary_failed,
            )
