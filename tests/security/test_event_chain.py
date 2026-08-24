"""The event log is tamper-evident: each event commits to the one before it.

qm's own security docs concede that audit records support investigation, not
prevention; buzz chains its log. These tests pin ours: editing a row, removing
a middle row, or truncating the tail is detected by recomputing the chain,
and rows written before the chain existed are hashed once at startup without
papering over a tampered stored hash.
"""

from dataclasses import replace

from multiplayer.db.connection import Database
from multiplayer.db.repositories import EventRepo
from multiplayer.domain.events import EventType, RoomEvent
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.audit import verify_event_chain
from multiplayer.services.service import MultiplayerService

TIMESTAMP = "2026-01-01T00:00:00+00:00"


async def _room_db() -> tuple[Database, MultiplayerService]:
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset())
    await svc.initialize()
    await db.execute(
        "INSERT INTO organizations(org_id, name, slug, created_at) VALUES (?, ?, ?, ?)",
        ("org_1", "Org", "org", TIMESTAMP),
    )
    await db.execute(
        "INSERT INTO workspaces(workspace_id, org_id, name, slug, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("ws_1", "org_1", "Ws", "ws", TIMESTAMP),
    )
    await db.execute(
        "INSERT INTO rooms(room_id, workspace_id, name, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("room_1", "ws_1", "Room", "user_1", TIMESTAMP),
    )
    return db, svc


def _event(note: str) -> RoomEvent:
    return RoomEvent(
        room_id="room_1",
        sequence=0,
        event_type=EventType.ROOM_UPDATED,
        payload={"note": note},
        actor_id="user_1",
        actor_type="user",
    )


async def _append(db: Database, count: int) -> None:
    repo = EventRepo(db)
    for index in range(count):
        await repo.append_with_next_sequence(_event(f"event {index + 1}"))


async def test_an_untampered_chain_verifies():
    db, _ = await _room_db()
    try:
        await _append(db, 3)
        verified, breaks = await verify_event_chain(db)
        assert (verified, breaks) == (3, [])
    finally:
        await db.close()


async def test_an_edited_payload_breaks_the_chain_at_its_row():
    db, _ = await _room_db()
    try:
        await _append(db, 3)
        await db.execute(
            "UPDATE room_events SET payload = ? WHERE room_id = 'room_1' AND sequence = 2",
            ('{"note": "rewritten"}',),
        )
        _, breaks = await verify_event_chain(db)
        assert len(breaks) == 1
        assert breaks[0].sequence == 2
        assert "does not match" in breaks[0].reason
    finally:
        await db.close()


async def test_a_removed_middle_row_breaks_the_chain():
    db, _ = await _room_db()
    try:
        await _append(db, 3)
        await db.execute("DELETE FROM room_events WHERE room_id = 'room_1' AND sequence = 2")
        _, breaks = await verify_event_chain(db)
        assert len(breaks) == 1
        assert "sequence 2 is missing" in breaks[0].reason
    finally:
        await db.close()


async def test_a_truncated_tail_is_caught_by_the_room_counter():
    db, _ = await _room_db()
    try:
        await _append(db, 3)
        await db.execute("DELETE FROM room_events WHERE room_id = 'room_1' AND sequence = 3")
        _, breaks = await verify_event_chain(db)
        assert len(breaks) == 1
        assert "counter reached 3" in breaks[0].reason
    finally:
        await db.close()


async def test_legacy_rows_are_hashed_once_at_startup():
    db, svc = await _room_db()
    try:
        # Rows written before the chain existed: hashes absent.
        for sequence in (1, 2):
            await db.execute(
                "INSERT INTO room_events(event_id, room_id, sequence, event_type, payload, "
                "actor_id, actor_type, timestamp, schema_version) "
                "VALUES (?, 'room_1', ?, 'room.updated', '{}', 'user_1', 'user', ?, 1)",
                (f"evt_legacy_{sequence}", sequence, TIMESTAMP),
            )
            await db.execute(
                "INSERT INTO room_sequences(room_id, seq) VALUES ('room_1', 1) "
                "ON CONFLICT(room_id) DO UPDATE SET seq = seq + 1"
            )
        await svc._backfill_event_chain()
        verified, breaks = await verify_event_chain(db)
        assert (verified, breaks) == (2, [])

        # The chain continues seamlessly past the backfilled rows.
        await EventRepo(db).append_with_next_sequence(_event("post-backfill"))
        verified, breaks = await verify_event_chain(db)
        assert (verified, breaks) == (3, [])
    finally:
        await db.close()


async def test_a_direct_append_with_a_stale_predecessor_fails_loudly():
    db, _ = await _room_db()
    try:
        await _append(db, 1)
        orphan = replace(_event("orphan"), sequence=5)
        try:
            await EventRepo(db).append(orphan)
        except RuntimeError as exc:
            assert "no hashed predecessor" in str(exc)
        else:
            raise AssertionError("append with a missing predecessor must fail")
    finally:
        await db.close()
