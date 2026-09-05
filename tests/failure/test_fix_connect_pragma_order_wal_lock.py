"""Finding (final critic, seam 5): two server processes booting the same
fresh database file must both start, not have the loser die with
``sqlite3.OperationalError: database is locked``.

``connect()`` used to run ``PRAGMA journal_mode=WAL`` before ``PRAGMA
busy_timeout=30000``. On a brand-new file that first statement takes SQLite's
one-time WAL-conversion exclusive lock, so a second process connecting at the
same moment could hit that lock before its own busy_timeout was in effect and
fail immediately instead of waiting.
"""

from __future__ import annotations

import asyncio

import pytest

from multiplayer.db.connection import Database


@pytest.mark.asyncio
async def test_two_processes_connecting_a_fresh_file_concurrently_both_succeed(
    tmp_path,
) -> None:
    db_path = tmp_path / "fresh.db"
    db1 = Database(str(db_path))
    db2 = Database(str(db_path))
    try:
        await asyncio.gather(db1.connect(), db2.connect())
    finally:
        await db1.close()
        await db2.close()
