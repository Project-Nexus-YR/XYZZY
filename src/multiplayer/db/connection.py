"""Database connection and query helpers using aiosqlite."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

_READER_POOL_SIZE = 4


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
        # Under WAL, read connections proceed while the write connection holds
        # a transaction. A :memory: database is private to its one connection,
        # so it stays on the single-connection path.
        self._readers: asyncio.Queue[aiosqlite.Connection] | None = None
        self._all_readers: list[aiosqlite.Connection] = []
        self._reader_open_lock = asyncio.Lock()

    def _is_memory(self) -> bool:
        return ":memory:" in self._path or "mode=memory" in self._path

    async def connect(self) -> None:
        try:
            # Single-statement writes are independently durable. Multi-statement
            # units use transaction(), which provides task-scoped ownership.
            self._db = await aiosqlite.connect(self._path, isolation_level=None)
            self._db.row_factory = aiosqlite.Row
            # sqlite3 opens lazily: a corrupt, truncated, or non-SQLite file
            # passes connect() and only fails on the first real statement, so
            # that failure has to stay inside this handler to name the path.
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA busy_timeout=30000")
            await self._db.execute("PRAGMA foreign_keys=ON")
        except Exception:
            log.exception("Failed to connect to database at %s", self._path)
            raise
        if not self._is_memory():
            self._readers = asyncio.Queue()

    @asynccontextmanager
    async def _reader(self) -> AsyncIterator[aiosqlite.Connection]:
        if self._readers is None:
            raise RuntimeError("Database not connected")
        readers = self._readers
        if readers.empty() and len(self._all_readers) < _READER_POOL_SIZE:
            async with self._reader_open_lock:
                if readers.empty() and len(self._all_readers) < _READER_POOL_SIZE:
                    conn = await aiosqlite.connect(self._path, isolation_level=None)
                    conn.row_factory = aiosqlite.Row
                    await conn.execute("PRAGMA busy_timeout=30000")
                    await conn.execute("PRAGMA foreign_keys=ON")
                    # A read connection that is handed a write must fail loudly,
                    # not race the writer.
                    await conn.execute("PRAGMA query_only=ON")
                    self._all_readers.append(conn)
                    readers.put_nowait(conn)
        conn = await readers.get()
        try:
            yield conn
        finally:
            readers.put_nowait(conn)

    async def close(self) -> None:
        self._readers = None
        for reader in self._all_readers:
            try:
                await reader.close()
            except Exception:
                log.warning("Error closing read connection", exc_info=True)
        self._all_readers = []
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
        """Run a transaction with exclusive, task-scoped connection ownership.

        A task cancelled while awaiting ``BEGIN IMMEDIATE`` used to leave the
        connection sitting inside an open transaction with the lock already
        released: the statement can finish on aiosqlite's own worker thread
        after the cancellation has already unwound this coroutine, and that
        await sat outside the try/except below that would otherwise roll it
        back. Reading ``in_transaction`` right here cannot see that: the
        worker thread may still be mid-call, so the flag has not flipped yet.
        The outer finally instead unconditionally queues a ``ROLLBACK`` behind
        whatever the pending call was — aiosqlite's worker thread runs one
        call at a time in submission order, so this one only runs once that
        call has actually finished one way or another, and rolls back exactly
        when there turns out to be something to roll back. A ``ROLLBACK`` with
        no open transaction is the harmless, expected case, so any failure
        here is swallowed rather than replacing whatever this block is really
        unwinding from.
        """
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
            with suppress(Exception):
                await self.conn.execute("ROLLBACK")
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
        elif self._readers is not None:
            async with self._reader() as conn:
                cursor = await conn.execute(sql, params)
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
        elif self._readers is not None:
            async with self._reader() as conn:
                cursor = await conn.execute(sql, params)
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


def deserialize_datetime(s: str | None) -> datetime | None:
    if s is None:
        return None
    return datetime.fromisoformat(s)
