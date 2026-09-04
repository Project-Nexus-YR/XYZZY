"""Finding 29: get_next_sequence hands each racing caller a distinct number.

The docstring already claimed the increment was atomic and the counter
strictly monotonic, but the increment and the read were two separate
statements, so two callers racing here could both read the value back after
a third caller's increment had already moved it on, and hand out the same
number twice. This proves the two are now one atomic statement (an UPDATE
with RETURNING, the same shape append_with_next_sequence_in_transaction
already uses beside it), by gathering many concurrent callers and asserting
every number they got back is distinct.

``append_batch`` is also named in this finding. It has no caller anywhere in
this repository (a grep for it outside its own definition returns nothing),
so it is deleted rather than fixed: nothing exercises the docstring's claim,
and nothing would notice its absence but a test that keeps it alive.
"""

from __future__ import annotations

import asyncio

import pytest

from multiplayer.db.connection import Database
from multiplayer.db.repositories import EventRepo


@pytest.fixture
async def repo():
    db = Database(":memory:")
    await db.connect()
    await db.execute("CREATE TABLE room_sequences(room_id TEXT PRIMARY KEY, seq INTEGER)")
    await db.commit()
    yield EventRepo(db)
    await db.close()


@pytest.mark.asyncio
async def test_concurrent_callers_never_get_the_same_sequence(repo: EventRepo) -> None:
    results = await asyncio.gather(*[repo.get_next_sequence("r1") for _ in range(20)])
    assert len(set(results)) == len(results), f"a sequence number was handed out twice: {results}"
    assert sorted(results) == list(range(1, 21))


def test_append_batch_has_been_deleted() -> None:
    assert not hasattr(EventRepo, "append_batch"), "dead code with no caller anywhere"
