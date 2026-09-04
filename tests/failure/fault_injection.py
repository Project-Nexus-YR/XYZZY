"""A database that fails partway through, for tests that need to see what a
real storage error leaves behind.

Nothing in the suite otherwise drives a multi-statement write through a
storage failure, so no test can tell an atomic partial write from a silent
one. ``FaultInjectingDatabase`` makes that failure reproducible: it counts
every call to ``execute`` and raises on the one the caller names, the same
error class and message SQLite itself raises when the volume underneath it
is full.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from multiplayer.db.connection import Database


class FaultInjectingDatabase(Database):
    """Raises ``sqlite3.OperationalError`` on the Nth call to ``execute``, or
    on the COMMIT of the Nth ``transaction()`` block when ``fail_on_commit``
    names it.

    Both counters are counted from one; zero or less never raises, which is
    useful for a dry run that only needs to know how many calls or
    transactions a given action takes. ``fail_on_commit`` is separate from
    ``fail_on_execute`` because ``Database.transaction()`` issues
    ``BEGIN``/``COMMIT``/``ROLLBACK`` straight against the raw connection
    rather than through ``self.execute``, so no execute count ever reaches
    them.
    """

    def __init__(
        self,
        path: str = ":memory:",
        *,
        fail_on_execute: int = 0,
        fail_on_commit: int = 0,
    ) -> None:
        super().__init__(path)
        self.fail_on_execute = fail_on_execute
        self.execute_count = 0
        self.fail_on_commit = fail_on_commit
        self.transaction_count = 0
        # The SQL of the call the fault actually landed on, so a test can
        # assert *which* statement it faulted rather than trusting a
        # hard-coded ordinal that a future execute added earlier in the
        # transaction would silently move onto a different statement.
        self.faulted_sql: str | None = None

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        self.execute_count += 1
        if self.execute_count == self.fail_on_execute:
            self.faulted_sql = sql
            raise sqlite3.OperationalError("database or disk is full")
        return await super().execute(sql, params)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        self.transaction_count += 1
        if self.transaction_count != self.fail_on_commit:
            async with super().transaction():
                yield
            return

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
                raise sqlite3.OperationalError("database or disk is full")
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
        finally:
            self._transaction_owner = None
            self._connection_lock.release()
