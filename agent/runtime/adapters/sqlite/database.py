"""aiosqlite connection policy and current-schema bootstrap for ``runtime.db``."""
from __future__ import annotations

import asyncio
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from common.sqlite_schema import ensure_current_schema


class RuntimeDatabase:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self._schema_path = Path(__file__).with_name("schema.sql")

    async def connect(self) -> aiosqlite.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        try:
            # Set the lock wait before WAL negotiation.  SQLite's journal_mode
            # PRAGMA can still return SQLITE_BUSY immediately on a brand-new DB,
            # so retry that bootstrap-only race within the same configured bound.
            await conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
            deadline = time.monotonic() + self.busy_timeout_ms / 1000
            while True:
                try:
                    await conn.execute("PRAGMA journal_mode = WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                        raise
                    await asyncio.sleep(0.01)
            await conn.execute("PRAGMA synchronous = FULL")
            await conn.execute("PRAGMA foreign_keys = ON")
            return conn
        except BaseException:
            await conn.close()
            raise

    @asynccontextmanager
    async def read(self) -> AsyncIterator[aiosqlite.Connection]:
        conn = await self.connect()
        try:
            yield conn
        finally:
            await conn.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        conn = await self.connect()
        try:
            await conn.execute("BEGIN IMMEDIATE")
            yield conn
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
        finally:
            await conn.close()

    async def ensure_schema(self) -> None:
        """Install the current schema on an empty file, or verify an existing one.

        There is no migration path: a database carrying anything other than the
        current schema is rejected and must be deleted by the operator.
        """
        conn = await self.connect()
        try:
            await ensure_current_schema(
                conn,
                schema_bytes=self._schema_path.read_bytes(),
                db_path=self.path,
                label="runtime",
            )
        finally:
            await conn.close()
