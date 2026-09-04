"""verify_event_chain, scoped to one room.

export_room_audit needs to verify only the room it is exporting, not every
room in the database. A room_id filter has to leave every other room's chain
completely unexamined, not merely unreported."""

import pytest

from multiplayer.db.connection import Database
from multiplayer.security.audit import event_chain_hash, verify_event_chain


async def _seed_room(db: Database, room_id: str, event_count: int, *, break_tail: bool) -> None:
    prev_hash = ""
    for sequence in range(1, event_count + 1):
        event_id = f"{room_id}-evt{sequence}"
        event_hash = event_chain_hash(
            prev_hash,
            event_id,
            room_id,
            sequence,
            "message.created",
            "{}",
            "u1",
            "user",
            "2024-01-01T00:00:00",
            1,
        )
        await db.execute(
            "INSERT INTO room_events(event_id, room_id, sequence, event_type, payload, "
            "actor_id, actor_type, timestamp, schema_version, prev_hash, event_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                room_id,
                sequence,
                "message.created",
                "{}",
                "u1",
                "user",
                "2024-01-01T00:00:00",
                1,
                prev_hash,
                event_hash,
            ),
        )
        prev_hash = event_hash
    counter_seq = event_count - 1 if break_tail else event_count
    await db.execute(
        "INSERT INTO room_sequences(room_id, seq) VALUES (?, ?)", (room_id, counter_seq)
    )


@pytest.mark.asyncio
async def test_room_id_filter_scopes_verification_to_one_room(tmp_path):
    db = Database(str(tmp_path / "audit.db"))
    await db.connect()
    await db.execute_script(
        "CREATE TABLE room_events (event_id TEXT PRIMARY KEY, room_id TEXT, sequence INTEGER, "
        "event_type TEXT, payload TEXT, actor_id TEXT, actor_type TEXT, timestamp TEXT, "
        "schema_version INTEGER, prev_hash TEXT, event_hash TEXT);"
        "CREATE TABLE room_sequences (room_id TEXT PRIMARY KEY, seq INTEGER);"
    )
    await _seed_room(db, "roomA", 3, break_tail=False)
    await _seed_room(db, "roomB", 3, break_tail=True)  # counter says 3, log only reaches 2

    verified_a, breaks_a = await verify_event_chain(db, room_id="roomA")
    assert breaks_a == []
    assert verified_a == 3

    verified_all, breaks_all = await verify_event_chain(db)
    assert any(b.room_id == "roomB" for b in breaks_all)
    assert not any(b.room_id == "roomA" for b in breaks_all)

    await db.close()
