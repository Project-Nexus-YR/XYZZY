"""Finding 44: a task cancelled while awaiting ``BEGIN IMMEDIATE`` inside
``Database.transaction()`` must not leave the connection wedged in an open
transaction with the lock already released.

``BEGIN IMMEDIATE`` can complete on SQLite's side before the cancellation is
delivered back to the coroutine awaiting it, and that await sat outside the
try/except that would otherwise roll it back: the outer ``finally`` only
released the process-local lock, so every later ``transaction()`` call on
this connection then failed with "cannot start a transaction within a
transaction" until the process restarted.
"""

from __future__ import annotations

import asyncio

import pytest

from multiplayer.db.connection import Database


@pytest.mark.asyncio
async def test_a_cancel_parked_on_begin_immediate_does_not_wedge_the_connection(
    tmp_path,
) -> None:
    db_path = tmp_path / "app.db"
    holder = Database(str(db_path))
    await holder.connect()
    victim = Database(str(db_path))
    await victim.connect()
    try:
        holder_ready = asyncio.Event()
        release_holder = asyncio.Event()

        async def hold_the_write_lock() -> None:
            async with holder.transaction():
                await holder.execute("CREATE TABLE IF NOT EXISTS t(x)")
                holder_ready.set()
                await release_holder.wait()

        holder_task = asyncio.create_task(hold_the_write_lock())
        await holder_ready.wait()

        async def blocked_transaction() -> None:
            async with victim.transaction():
                pass  # pragma: no cover - never reached, the lock is held

        victim_task = asyncio.create_task(blocked_transaction())
        # Give victim_task a real chance to reach and start waiting on its own
        # BEGIN IMMEDIATE, which cannot proceed while the holder has the lock.
        await asyncio.sleep(0.05)
        victim_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await victim_task

        release_holder.set()
        await holder_task

        # The connection this cancellation landed on must still take a fresh
        # transaction normally, not fail as already being inside one.
        async with victim.transaction():
            await victim.execute("INSERT INTO t VALUES (1)")
        row = await victim.fetch_one("SELECT COUNT(*) AS n FROM t")
        assert row is not None and row["n"] == 1
    finally:
        await holder.close()
        await victim.close()
