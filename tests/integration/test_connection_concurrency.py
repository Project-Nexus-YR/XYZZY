"""A read must not wait behind an unrelated write transaction.

The single-connection model serialized every read behind every write; an
unrelated read was measured blocking 1016 ms. File-backed databases now serve
reads from a WAL reader pool, so these tests pin the properties that model
must keep: reads proceed while a write transaction is open, readers see only
committed state, readers cannot write, and a transaction still reads its own
uncommitted writes. A :memory: database keeps the single-connection path.
"""

import asyncio
import sqlite3

import pytest

from multiplayer.db.connection import Database


async def _connected(path: str) -> Database:
    db = Database(path)
    await db.connect()
    await db.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    await db.execute("INSERT INTO items (name) VALUES (?)", ("committed",))
    return db


async def test_read_completes_while_write_transaction_is_open(tmp_path):
    db = await _connected(str(tmp_path / "pool.db"))
    try:
        async with db.transaction():
            await db.execute("INSERT INTO items (name) VALUES (?)", ("uncommitted",))
            read = asyncio.create_task(db.fetch_all("SELECT name FROM items"))
            try:
                rows = await asyncio.wait_for(read, timeout=2.0)
            except TimeoutError:
                pytest.fail("read blocked behind an open write transaction")
        names = {row["name"] for row in rows}
        assert names == {"committed"}, "reader saw uncommitted writer state"
    finally:
        await db.close()


async def test_read_sees_the_write_once_it_commits(tmp_path):
    db = await _connected(str(tmp_path / "visible.db"))
    try:
        async with db.transaction():
            await db.execute("INSERT INTO items (name) VALUES (?)", ("landed",))
        rows = await db.fetch_all("SELECT name FROM items ORDER BY id")
        assert [row["name"] for row in rows] == ["committed", "landed"]
    finally:
        await db.close()


async def test_a_reader_handed_a_write_fails_loudly(tmp_path):
    db = await _connected(str(tmp_path / "readonly.db"))
    try:
        with pytest.raises(sqlite3.OperationalError):
            await db.fetch_one("INSERT INTO items (name) VALUES ('smuggled') RETURNING id")
        rows = await db.fetch_all("SELECT name FROM items")
        assert [row["name"] for row in rows] == ["committed"]
    finally:
        await db.close()


async def test_a_transaction_reads_its_own_uncommitted_writes(tmp_path):
    db = await _connected(str(tmp_path / "own.db"))
    try:
        async with db.transaction():
            await db.execute("INSERT INTO items (name) VALUES (?)", ("mine",))
            row = await db.fetch_one("SELECT name FROM items WHERE name = ?", ("mine",))
            assert row is not None
    finally:
        await db.close()


async def test_concurrent_reads_all_complete_beyond_the_pool_size(tmp_path):
    db = await _connected(str(tmp_path / "many.db"))
    try:
        reads = [db.fetch_all("SELECT name FROM items") for _ in range(20)]
        results = await asyncio.wait_for(asyncio.gather(*reads), timeout=5.0)
        assert all(rows[0]["name"] == "committed" for rows in results)
    finally:
        await db.close()


async def test_memory_database_keeps_the_single_connection_path():
    db = await _connected(":memory:")
    try:
        row = await db.fetch_one("SELECT name FROM items")
        assert row is not None and row["name"] == "committed"
    finally:
        await db.close()
