"""Single current-schema bootstrap and identity verification for SQLite databases.

This project does not migrate databases.  A database either is empty and gets the
one current schema, or it already carries exactly that schema and is accepted, or
it is rejected and the operator deletes it.  There is no incremental migration,
no ALTER path and no compatibility layer for older data.

``schema_meta`` records the identity that was installed.  It must be declared by
the caller's own ``schema.sql`` so that the whole current schema stays in one
readable file.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

import aiosqlite


class SchemaIdentityError(RuntimeError):
    """The on-disk database is not the current schema and must not be touched."""


def schema_checksum(schema_sql: str) -> str:
    return hashlib.sha256(schema_sql.encode("utf-8")).hexdigest()


async def ensure_current_schema(
    conn: aiosqlite.Connection,
    *,
    schema_sql: str,
    schema_version: str,
    db_path: Path,
    label: str,
) -> None:
    """Create the current schema on an empty database, or verify an existing one.

    The whole decision runs inside one ``BEGIN IMMEDIATE``.  Two processes may
    bootstrap the same empty file concurrently: the loser blocks on the write
    lock and then observes the committed schema, so it takes the verify branch.

    The transaction is driven with explicit statements rather than the
    connection's ``isolation_level``, because callers use different connection
    policies.
    """
    expected = schema_checksum(schema_sql)
    await conn.execute("BEGIN IMMEDIATE")
    try:
        if await _user_table_count(conn) == 0:
            for statement in _split_sql(schema_sql):
                await conn.execute(statement)
            await conn.execute(
                """INSERT INTO schema_meta(id,schema_version,schema_checksum,created_at)
                   VALUES (1,?,?,?)""",
                (schema_version, expected, int(time.time() * 1000)),
            )
        else:
            _verify(
                await _read_meta(conn),
                schema_version=schema_version,
                expected_checksum=expected,
                db_path=db_path,
                label=label,
            )
    except BaseException:
        await conn.rollback()
        raise
    await conn.commit()


async def _user_table_count(conn: aiosqlite.Connection) -> int:
    row = await (await conn.execute(
        """SELECT count(*) FROM sqlite_master
           WHERE type='table' AND name NOT LIKE 'sqlite_%'"""
    )).fetchone()
    return int(row[0])


async def _read_meta(conn: aiosqlite.Connection) -> tuple[str, str] | None:
    """Return the recorded (version, checksum), or None when unreadable."""
    try:
        row = await (await conn.execute(
            "SELECT schema_version,schema_checksum FROM schema_meta WHERE id=1"
        )).fetchone()
    except sqlite3.OperationalError:
        # No schema_meta table at all: some other database owns this file.
        return None
    return (str(row[0]), str(row[1])) if row is not None else None


def _verify(
    meta: tuple[str, str] | None,
    *,
    schema_version: str,
    expected_checksum: str,
    db_path: Path,
    label: str,
) -> None:
    if meta is None:
        found = "schema_meta is absent"
        reason = "schema_meta is missing"
    else:
        found_version, found_checksum = meta
        found = f"schema_version={found_version} checksum={found_checksum}"
        if found_version != schema_version:
            reason = "schema_version mismatch"
        elif found_checksum != expected_checksum:
            reason = "checksum mismatch"
        else:
            return
    path = db_path.resolve()
    raise SchemaIdentityError(
        f"{label} database {reason}: {path}\n"
        f"  expected: schema_version={schema_version} checksum={expected_checksum}\n"
        f"  found:    {found}\n"
        "This project does not migrate databases. Delete it and restart:\n"
        f"  rm -f {path} {path}-wal {path}-shm"
    )


def _split_sql(sql: str) -> list[str]:
    """Split a schema file without breaking trigger bodies at inner semicolons.

    ``sqlite3.complete_statement`` wraps ``sqlite3_complete()``, which knows that
    a semicolon inside ``CREATE TRIGGER ... BEGIN ... END;`` does not end the
    statement.  The file must therefore end with a semicolon, not with a comment.
    """
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
        raise SchemaIdentityError("schema file ends with an incomplete SQL statement")
    return statements
