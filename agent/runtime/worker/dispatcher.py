from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timezone

from agent.runtime.application.coordinator import RunCoordinator
from agent.runtime.domain.errors import RuntimeFault
from agent.runtime.ports.artifact import ArtifactStore
from agent.runtime.ports.clock import Clock, SystemClock
from agent.runtime.ports.store import Claim, RuntimeStore
from common.obs import get_logger, log_kv

logger = get_logger("agent.runtime.worker")

# 捞取 api “接客”后持久化的"run"进行执行
class RuntimeWorker:
    def __init__(
        self,
        *,
        store: RuntimeStore,
        coordinator: RunCoordinator,
        worker_id: str,
        release_map: dict[str, str],
        concurrency: int = 4,
        lease_ms: int = 30_000,
        renew_ms: int = 10_000,
        poll_ms: int = 250,
        shutdown_grace_ms: int = 20_000,
        artifact_store: ArtifactStore | None = None,
        artifact_cleanup_interval_ms: int = 3_600_000,
        artifact_orphan_age_ms: int = 86_400_000,
        clock: Clock | None = None,
    ) -> None:
        self.store = store
        self.coordinator = coordinator
        self.worker_id = worker_id
        self.release_map = release_map
        self.concurrency = concurrency
        self.lease_ms = lease_ms
        self.renew_ms = renew_ms
        self.poll_ms = poll_ms
        self.shutdown_grace_ms = shutdown_grace_ms
        self.artifact_store = artifact_store
        self.artifact_cleanup_interval_ms = artifact_cleanup_interval_ms
        self.artifact_orphan_age_ms = artifact_orphan_age_ms
        self.clock = clock or SystemClock()
        self._stop = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        self._last_heartbeat = 0
        self._last_artifact_cleanup: int | None = None

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        await self.store.heartbeat_worker(
            worker_id=self.worker_id, release_map=self.release_map,
            state="ACTIVE", now_ms=self.clock.now_ms(),
        )
        log_kv(
            logger, logging.INFO, "Worker", "dispatcher started",
            worker_id=self.worker_id, concurrency=self.concurrency,
            lease_ms=self.lease_ms, releases=self.release_map,
        )
        try:
            while not self._stop.is_set():
                now = self.clock.now_ms()
                await self._maintenance(now)
                while len(self._tasks) < self.concurrency and not self._stop.is_set():
                    claim = await self.store.claim_next(
                        worker_id=self.worker_id,
                        lease_ms=self.lease_ms,
                        now_ms=self.clock.now_ms(),
                        engines=tuple(self.release_map),
                    )
                    if claim is None:
                        break
                    task = asyncio.create_task(self._execute(claim))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_ms / 1000)
                except TimeoutError:
                    pass
        finally:
            await self._drain()

    async def run_once(self) -> bool:
        """Deterministic test entrypoint: maintenance + at most one claim."""
        now = self.clock.now_ms()
        await self._maintenance(now)
        claim = await self.store.claim_next(
            worker_id=self.worker_id,
            lease_ms=self.lease_ms,
            now_ms=now,
            engines=tuple(self.release_map),
        )
        if claim is None:
            return False
        await self._execute(claim)
        return True

    async def _maintenance(self, now: int) -> None:
        await self.store.fire_due_timers(now_ms=now)
        await self.store.recover_expired(now_ms=now)
        expire = getattr(self.store, "expire_deadlines", None)
        if expire is not None:
            await expire(now_ms=now)
        if (
            self.artifact_store is not None
            and (
                self._last_artifact_cleanup is None
                or now - self._last_artifact_cleanup
                >= self.artifact_cleanup_interval_ms
            )
        ):
            # Mark the attempt before scanning so a transient filesystem error
            # cannot turn the 250 ms dispatcher loop into a cleanup hot loop.
            self._last_artifact_cleanup = now
            try:
                referenced = await self.store.referenced_artifact_ids()
                result = await self.artifact_store.cleanup_orphans(
                    referenced_artifact_ids=referenced,
                    older_than=datetime.fromtimestamp(
                        (now - self.artifact_orphan_age_ms) / 1000,
                        tz=timezone.utc,
                    ),
                )
                if result.deleted:
                    log_kv(
                        logger, logging.INFO, "ArtifactGC", "orphans reclaimed",
                        deleted=result.deleted,
                        reclaimed_bytes=result.reclaimed_bytes,
                    )
            except Exception as exc:  # housekeeping cannot stop Run dispatch
                log_kv(
                    logger, logging.WARNING, "ArtifactGC", "cleanup failed",
                    error=type(exc).__name__, detail=str(exc),
                )
        if now - self._last_heartbeat >= 5_000:
            await self.store.heartbeat_worker(
                worker_id=self.worker_id, release_map=self.release_map,
                state="ACTIVE", now_ms=now,
            )
            self._last_heartbeat = now

    async def _execute(self, claim: Claim) -> None:
        finished = asyncio.Event()
        renewal = asyncio.create_task(self._renew_lease(claim, finished))
        try:
            await self.coordinator.execute_claim(claim, worker_id=self.worker_id)
        except asyncio.CancelledError:
            raise
        except RuntimeFault as exc:
            log_kv(
                logger, logging.WARNING, "Worker", "attempt rejected",
                run_id=claim.run.envelope.run_id,
                activity_id=claim.activity.activity_id,
                code=exc.code,
            )
        except Exception as exc:  # lease recovery owns the unfinished activity
            log_kv(
                logger, logging.ERROR, "Worker", "attempt crashed",
                run_id=claim.run.envelope.run_id,
                activity_id=claim.activity.activity_id,
                error=type(exc).__name__,
            )
        finally:
            finished.set()
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal

    async def _renew_lease(self, claim: Claim, finished: asyncio.Event) -> None:
        while not finished.is_set():
            try:
                await asyncio.wait_for(finished.wait(), timeout=self.renew_ms / 1000)
                return
            except TimeoutError:
                pass
            now = self.clock.now_ms()
            valid = await self.store.renew_lease(
                claim.activity.activity_id,
                worker_id=self.worker_id,
                fencing_token=claim.activity.fencing_token,
                lease_expires_at=now + self.lease_ms,
                now_ms=now,
            )
            if not valid:
                log_kv(
                    logger, logging.WARNING, "Worker", "lease fence lost",
                    run_id=claim.run.envelope.run_id,
                    activity_id=claim.activity.activity_id,
                    fencing_token=claim.activity.fencing_token,
                )
                return

    async def _drain(self) -> None:
        await self.store.heartbeat_worker(
            worker_id=self.worker_id, release_map=self.release_map,
            state="DRAINING", now_ms=self.clock.now_ms(),
        )
        if self._tasks:
            _, pending = await asyncio.wait(
                self._tasks, timeout=self.shutdown_grace_ms / 1000,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        await self.store.heartbeat_worker(
            worker_id=self.worker_id, release_map=self.release_map,
            state="STOPPED", now_ms=self.clock.now_ms(),
        )
        log_kv(logger, logging.INFO, "Worker", "dispatcher stopped", worker_id=self.worker_id)
