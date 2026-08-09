"""aiosqlite connection policy and checksum-verified migrations."""
from __future__ import annotations

import hashlib
import asyncio
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite


class SchemaCompatibilityError(RuntimeError):
    pass


class RuntimeDatabase:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self._migrations_dir = Path(__file__).with_name("migrations")

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

    async def migrate(self) -> None:
        migration_files = sorted(self._migrations_dir.glob("[0-9][0-9][0-9]_*.sql"))
        known = {int(path.name.split("_", 1)[0]): path for path in migration_files}
        conn = await self.connect()
        try:
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    applied_at INTEGER NOT NULL
                ) STRICT"""
            )
            rows = await self._migration_rows(conn)
            self._verify_rows(rows, known)
            applied = {int(row["version"]) for row in rows}
            for version, path in known.items():
                if version in applied:
                    continue
                sql = path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                import time  # local to keep the adapter's module surface small
                try:
                    await conn.execute("BEGIN IMMEDIATE")
                    # API and Worker may bootstrap the same empty database.  The
                    # write lock must be acquired before checking this version a
                    # second time; another process may have applied it while we
                    # were waiting.
                    existing = await (await conn.execute(
                        "SELECT name,checksum FROM schema_migrations WHERE version=?",
                        (version,),
                    )).fetchone()
                    if existing is not None:
                        if existing["checksum"] != checksum:
                            raise SchemaCompatibilityError(
                                f"migration checksum mismatch: {path.name}"
                            )
                        await conn.commit()
                        continue
                    for statement in self._split_sql(sql):
                        await conn.execute(statement)
                    await conn.execute(
                        """INSERT INTO schema_migrations(version,name,checksum,applied_at)
                           VALUES (?,?,?,?)""",
                        (version, path.name, checksum, int(time.time() * 1000)),
                    )
                    await conn.commit()
                except BaseException:
                    await conn.rollback()
                    raise
            self._verify_rows(await self._migration_rows(conn), known)
        finally:
            await conn.close()

    @staticmethod
    async def _migration_rows(conn: aiosqlite.Connection) -> list[aiosqlite.Row]:
        return list(await (await conn.execute(
            "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
        )).fetchall())

    @staticmethod
    def _verify_rows(rows: list[aiosqlite.Row], known: dict[int, Path]) -> None:
        for row in rows:
            path = known.get(int(row["version"]))
            if path is None:
                raise SchemaCompatibilityError(
                    f"database schema version {row['version']} is newer than this runtime"
                )
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            if checksum != row["checksum"]:
                raise SchemaCompatibilityError(
                    f"migration checksum mismatch: {path.name}"
                )

    @staticmethod
    def _split_sql(sql: str) -> list[str]:
        """Split a migration without breaking trigger bodies at inner semicolons."""
        statements: list[str] = []
        buffer = ""
        for line in sql.splitlines(keepends=True):
            buffer += line
            if sqlite3.complete_statement(buffer):
                statement = buffer.strip()
                if statement:
                    statements.append(statement)
                buffer = ""
        if buffer.strip():
            raise SchemaCompatibilityError("migration contains an incomplete SQL statement")
        return statements

    async def verify(self) -> None:
        """Re-run migration checksum/version validation without changing known schema."""
        await self.migrate()
        async with self.read() as conn:
            row = await (await conn.execute("PRAGMA integrity_check")).fetchone()
            if row is None or row[0] != "ok":
                raise SchemaCompatibilityError(f"SQLite integrity_check failed: {row[0] if row else 'empty'}")
