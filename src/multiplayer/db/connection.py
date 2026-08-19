"""Database connection and query helpers using aiosqlite."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite

log = logging.getLogger(__name__)


class Database:
    """Async SQLite database wrapper with transaction support."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        try:
            self._db = await aiosqlite.connect(self._path)
        except Exception:
            log.exception("Failed to connect to database at %s", self._path)
            raise
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=30000")
        await self._db.execute("PRAGMA foreign_keys=ON")

    async def close(self) -> None:
        if self._db:
            try:
                await self._db.close()
            except Exception:
                log.warning("Error closing database", exc_info=True)
            finally:
                self._db = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database not connected")
        return self._db

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Execute a block in an explicit transaction. Rolls back on error."""
        await self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            await self.conn.execute("COMMIT")
        except Exception:
            await self.conn.execute("ROLLBACK")
            raise

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Cursor:
        return await self.conn.execute(sql, params)

    async def executemany(self, sql: str, params: list[tuple[Any, ...]]) -> None:
        await self.conn.executemany(sql, params)

    async def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        cursor = await self.execute(sql, params)
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        cursor = await self.execute(sql, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def commit(self) -> None:
        await self.conn.commit()

    async def execute_script(self, script: str) -> None:
        await self.conn.executescript(script)


def serialize_datetime(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def serialize_dict(d: dict[str, Any]) -> str:
    return json.dumps(d, sort_keys=True, default=str)


def deserialize_datetime(s: str | None) -> datetime | None:
    if s is None:
        return None
    return datetime.fromisoformat(s)


def deserialize_dict(s: str) -> dict[str, Any]:
    return json.loads(s)
