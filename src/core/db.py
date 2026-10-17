"""Async SQLite access (WAL) with a single serialised connection.

Ultron Prime is a single-operator application, so one connection guarded by an ``asyncio.Lock``
is simpler and safer than a pool: a transaction can never interleave with another coroutine's
statements. Rows come back as plain dictionaries.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from src.core.events import AgentEvent, parse_event, utcnow_iso

SCHEMA_PATH = Path(__file__).resolve().parent / "memory" / "schema.sql"

Row = dict[str, Any]
Params = Sequence[Any] | dict[str, Any]


def new_id(prefix: str) -> str:
    """Return a sortable-enough random identifier such as ``msg_1a2b3c4d5e6f7a8b``."""
    return f"{prefix}_{secrets.token_hex(8)}"


def to_json(value: Any) -> str:
    """Serialise ``value`` to compact JSON."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def from_json(raw: str | None, default: Any = None) -> Any:
    """Parse JSON that may be missing."""
    if raw is None or raw == "":
        return default
    return json.loads(raw)


class Tx:
    """Statement helpers valid inside ``Database.transaction()``."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def execute(self, sql: str, params: Params = ()) -> int:
        """Run one statement and return ``lastrowid``."""
        cursor = await self._conn.execute(sql, params)
        rowid = cursor.lastrowid or 0
        await cursor.close()
        return rowid

    async def executemany(self, sql: str, seq: Iterable[Params]) -> None:
        """Run one statement for each parameter set."""
        await self._conn.executemany(sql, seq)

    async def fetchone(self, sql: str, params: Params = ()) -> Row | None:
        """Return the first row or ``None``."""
        cursor = await self._conn.execute(sql, params)
        row = await cursor.fetchone()
        await cursor.close()
        return dict(row) if row is not None else None

    async def fetchall(self, sql: str, params: Params = ()) -> list[Row]:
        """Return all rows."""
        cursor = await self._conn.execute(sql, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return [dict(r) for r in rows]

    async def scalar(self, sql: str, params: Params = ()) -> Any:
        """Return the first column of the first row, or ``None``."""
        row = await self.fetchone(sql, params)
        return next(iter(row.values())) if row else None


class Database:
    """The application database."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Open the connection, enable WAL and foreign keys, and apply the schema."""
        if self._conn is not None:
            return
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        await conn.commit()
        self._conn = conn

    async def close(self) -> None:
        """Close the connection."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        """Return the live connection or raise if not connected."""
        if self._conn is None:
            raise RuntimeError("database is not connected")
        return self._conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Tx]:
        """Hold the lock, run statements, and commit (or roll back on error)."""
        async with self._lock:
            conn = self.conn
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield Tx(conn)
            except BaseException:
                await conn.rollback()
                raise
            else:
                await conn.commit()

    async def execute(self, sql: str, params: Params = ()) -> int:
        """Run one statement in its own transaction."""
        async with self.transaction() as tx:
            return await tx.execute(sql, params)

    async def fetchone(self, sql: str, params: Params = ()) -> Row | None:
        """Return the first row or ``None``."""
        async with self._lock:
            return await Tx(self.conn).fetchone(sql, params)

    async def fetchall(self, sql: str, params: Params = ()) -> list[Row]:
        """Return all rows."""
        async with self._lock:
            return await Tx(self.conn).fetchall(sql, params)

    async def scalar(self, sql: str, params: Params = ()) -> Any:
        """Return the first column of the first row."""
        async with self._lock:
            return await Tx(self.conn).scalar(sql, params)

    async def audit(self, event: str, actor: str, payload: dict[str, Any] | None = None) -> None:
        """Append an audit-log entry (never include memory content in the payload)."""
        await self.execute(
            "INSERT INTO audit_log(id, event, actor, payload_json, created_at) VALUES (?,?,?,?,?)",
            (new_id("aud"), event, actor, to_json(payload or {}), utcnow_iso()),
        )


class SqliteTurnEventSink:
    """``TurnEventSink`` backed by the ``turn_events`` table."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def append(self, event: AgentEvent) -> None:
        """Persist one sequenced event."""
        await self._db.execute(
            "INSERT INTO turn_events(turn_id, seq, type, payload_json, created_at) "
            "VALUES (?,?,?,?,?)",
            (event.turn_id, event.seq, event.type, event.model_dump_json(), event.ts),
        )

    async def read_after(self, turn_id: str, after_seq: int) -> list[AgentEvent]:
        """Return stored events after ``after_seq`` in order."""
        rows = await self._db.fetchall(
            "SELECT type, payload_json FROM turn_events WHERE turn_id=? AND seq>? ORDER BY seq",
            (turn_id, after_seq),
        )
        return [parse_event(r["type"], r["payload_json"]) for r in rows]

    async def last_seq(self, turn_id: str) -> int:
        """Return the highest stored sequence for the turn."""
        value = await self._db.scalar(
            "SELECT COALESCE(MAX(seq), 0) FROM turn_events WHERE turn_id=?", (turn_id,)
        )
        return int(value or 0)

    async def purge_older_than(self, hours: float) -> int:
        """Delete events of finished turns older than ``hours``; returns rows removed."""
        async with self._db.transaction() as tx:
            await tx.execute(
                "DELETE FROM turn_events WHERE turn_id IN ("
                " SELECT id FROM turns WHERE status != 'running' "
                " AND finished_at < strftime('%Y-%m-%dT%H:%M:%f', 'now', ?))",
                (f"-{hours} hours",),
            )
            return int(await tx.scalar("SELECT changes()") or 0)
