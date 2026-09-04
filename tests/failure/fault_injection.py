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

import sqlite3
from typing import Any

from multiplayer.db.connection import Database


class FaultInjectingDatabase(Database):
    """Raises ``sqlite3.OperationalError`` on the Nth call to ``execute``.

    Calls are counted from one. A count of zero or less never raises, which
    is useful for a dry run that only needs to know how many calls a given
    action takes.
    """

    def __init__(self, path: str = ":memory:", *, fail_on_execute: int) -> None:
        super().__init__(path)
        self.fail_on_execute = fail_on_execute
        self.execute_count = 0

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        self.execute_count += 1
        if self.execute_count == self.fail_on_execute:
            raise sqlite3.OperationalError("database or disk is full")
        return await super().execute(sql, params)
