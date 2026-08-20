"""Database connection and query helpers using aiosqlite."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import aiosqlite

log = logging.getLogger(__name__)


class Database:
    """Async SQLite database wrapper with transaction support."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        self._db: aiosqlite.Connection | None = None
        # SQLite transactions belong to the connection, not to an asyncio task.
        # Every operation therefore passes through one ownership gate, while an
        # explicit transaction holds the gate for its complete lifetime.
        self._connection_lock = asyncio.Lock()
        self._transaction_owner: asyncio.Task[Any] | None = None

    async def connect(self) -> None:
        try:
            # Single-statement writes are independently durable. Multi-statement
            # units use transaction(), which provides task-scoped ownership.
            self._db = await aiosqlite.connect(self._path, isolation_level=None)
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
                async with self._connection_lock:
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
        """Run a transaction with exclusive, task-scoped connection ownership."""
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("database transaction requires an asyncio task")
        if self._transaction_owner is owner:
            raise RuntimeError("nested database transactions are not supported")

        await self._connection_lock.acquire()
        self._transaction_owner = owner
        try:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                await self.conn.execute("COMMIT")
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
        finally:
            self._transaction_owner = None
            self._connection_lock.release()

    def _owns_transaction(self) -> bool:
        return asyncio.current_task() is self._transaction_owner

    @property
    def owns_current_transaction(self) -> bool:
        """Whether the calling task currently owns this connection's transaction."""
        return self._owns_transaction()

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Cursor:
        if self._owns_transaction():
            return await self.conn.execute(sql, params)
        async with self._connection_lock:
            return await self.conn.execute(sql, params)

    async def executemany(self, sql: str, params: list[tuple[Any, ...]]) -> None:
        if self._owns_transaction():
            await self.conn.executemany(sql, params)
            return
        async with self._connection_lock:
            await self.conn.executemany(sql, params)

    async def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        if self._owns_transaction():
            cursor = await self.conn.execute(sql, params)
            try:
                row = await cursor.fetchone()
            finally:
                await cursor.close()
        else:
            async with self._connection_lock:
                cursor = await self.conn.execute(sql, params)
                try:
                    row = await cursor.fetchone()
                finally:
                    await cursor.close()
        if row is None:
            return None
        return dict(row)

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if self._owns_transaction():
            cursor = await self.conn.execute(sql, params)
            try:
                rows = await cursor.fetchall()
            finally:
                await cursor.close()
        else:
            async with self._connection_lock:
                cursor = await self.conn.execute(sql, params)
                try:
                    rows = await cursor.fetchall()
                finally:
                    await cursor.close()
        return [dict(row) for row in rows]

    async def commit(self) -> None:
        # An inner repository method must never end its caller's transaction.
        if self._owns_transaction():
            return
        async with self._connection_lock:
            await self.conn.commit()

    async def execute_script(self, script: str) -> None:
        if self._owns_transaction():
            await self.conn.executescript(script)
            return
        async with self._connection_lock:
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
    return cast(dict[str, Any], json.loads(s))
