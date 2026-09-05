"""Connect against a database file that is not a database, and other
storage-layer failures the data track owns."""

import sqlite3
import time

import pytest

from multiplayer.db.connection import Database


@pytest.mark.asyncio
async def test_connect_against_non_database_file_names_the_path(tmp_path, caplog):
    """A truncated or non SQLite file fails on the first PRAGMA, not on
    connect() itself, so the path has to be named from inside the same
    handler that wraps connect() or the operator never sees it in the log.

    This must fail fast: connect()'s retry loop around PRAGMA
    journal_mode=WAL exists for lock contention only, and must not catch a
    corrupt file's error and spin on it for the full thirty-second window."""
    bad_path = tmp_path / "not_a_database.db"
    bad_path.write_text("this is a plain text file, not a sqlite database")

    db = Database(str(bad_path))
    try:
        with caplog.at_level("ERROR"):
            t0 = time.monotonic()
            with pytest.raises(sqlite3.DatabaseError):
                await db.connect()
            assert time.monotonic() - t0 < 5
    finally:
        # A failed connect still started aiosqlite's worker thread; leaving it
        # open keeps the whole pytest process alive at exit.
        await db.close()

    assert any(str(bad_path) in record.getMessage() for record in caplog.records)
