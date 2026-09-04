"""Finding #15: a redacted row's header used to be a free rewrite slot.

Before this fix, verify_event_chain trusted a marker row's event_hash against
event_redactions.original_event_hash and never recomputed anything from the
row's own event_type/actor_id/actor_type/timestamp/schema_version. Since a
redaction never touches those columns, an attacker with write access to the
file could rewrite them on an already-redacted row and the chain still
verified clean: the row's event_hash never changed, and nothing else was ever
compared against it.

The fix stores a header snapshot hash in event_redactions at redaction time
and has verify_event_chain recompute it from the row's live columns. Each
test below edits exactly one of those columns by hand, the way attack B in
the round 2 audit did, and expects a ChainBreak instead of a clean verify.

Round 2 (crypto track) also added append-only triggers on room_events and
event_redactions (migration 050), so a direct UPDATE like these needs the
same trigger dropped first that a file-level attacker would drop, through
the same connection, immediately before the tamper: that is what each
"DROP TRIGGER IF EXISTS" line below does. It changes nothing about what the
verifier is then asked to catch.
"""

from __future__ import annotations

from multiplayer.db.connection import Database
from multiplayer.domain.models import MessageRole, User
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.audit import event_chain_hash, verify_event_chain
from multiplayer.services.service import MultiplayerService


async def _redacted_room() -> tuple[Database, str, str]:
    """A room with one of alice's messages already erased."""
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset())
    await svc.initialize()
    await svc.repos.users.create(User(user_id="alice", display_name="Alice", email="a@x.com"))
    org = await svc.create_organization("Org", "org", "alice")
    workspace = await svc.create_workspace(org.org_id, "Ws", "ws", "alice")
    room = await svc.create_room(workspace.workspace_id, "Room", "alice")
    await svc.send_message(room.room_id, MessageRole.HUMAN, "alice", "my secret plan")
    await svc.erase_user("alice")
    row = await db.fetch_one(
        "SELECT event_id FROM room_events WHERE room_id = ? AND event_type = 'message.created'",
        (room.room_id,),
    )
    assert row is not None
    return db, room.room_id, str(row["event_id"])


async def test_editing_a_marker_rows_actor_id_breaks_the_chain():
    db, room_id, event_id = await _redacted_room()
    try:
        await db.execute("DROP TRIGGER IF EXISTS room_events_reject_identity_update")
        await db.execute(
            "UPDATE room_events SET actor_id = 'mallory' WHERE event_id = ?", (event_id,)
        )
        _, breaks = await verify_event_chain(db, room_id=room_id)
        assert len(breaks) == 1
        assert "header" in breaks[0].reason
    finally:
        await db.close()


async def test_editing_a_marker_rows_actor_type_breaks_the_chain():
    db, room_id, event_id = await _redacted_room()
    try:
        await db.execute("DROP TRIGGER IF EXISTS room_events_reject_identity_update")
        await db.execute(
            "UPDATE room_events SET actor_type = 'agent' WHERE event_id = ?", (event_id,)
        )
        _, breaks = await verify_event_chain(db, room_id=room_id)
        assert len(breaks) == 1
        assert "header" in breaks[0].reason
    finally:
        await db.close()


async def test_editing_a_marker_rows_timestamp_breaks_the_chain():
    db, room_id, event_id = await _redacted_room()
    try:
        await db.execute("DROP TRIGGER IF EXISTS room_events_reject_identity_update")
        await db.execute(
            "UPDATE room_events SET timestamp = '2099-01-01T00:00:00+00:00' WHERE event_id = ?",
            (event_id,),
        )
        _, breaks = await verify_event_chain(db, room_id=room_id)
        assert len(breaks) == 1
        assert "header" in breaks[0].reason
    finally:
        await db.close()


async def test_editing_a_marker_rows_event_type_breaks_the_chain():
    db, room_id, event_id = await _redacted_room()
    try:
        await db.execute("DROP TRIGGER IF EXISTS room_events_reject_identity_update")
        await db.execute(
            "UPDATE room_events SET event_type = 'message.edited' WHERE event_id = ?", (event_id,)
        )
        _, breaks = await verify_event_chain(db, room_id=room_id)
        assert len(breaks) == 1
        assert "header" in breaks[0].reason
    finally:
        await db.close()


async def test_editing_a_marker_rows_schema_version_breaks_the_chain():
    db, room_id, event_id = await _redacted_room()
    try:
        await db.execute("DROP TRIGGER IF EXISTS room_events_reject_identity_update")
        await db.execute(
            "UPDATE room_events SET schema_version = 99 WHERE event_id = ?", (event_id,)
        )
        _, breaks = await verify_event_chain(db, room_id=room_id)
        assert len(breaks) == 1
        assert "header" in breaks[0].reason
    finally:
        await db.close()


async def test_tampering_the_stored_header_hash_breaks_the_chain():
    """A rewrite of the record's own header_hash is caught the same way the
    original_event_hash tamper already was: the two are independent checks."""
    db, room_id, event_id = await _redacted_room()
    try:
        await db.execute("DROP TRIGGER IF EXISTS event_redactions_reject_update")
        await db.execute(
            "UPDATE event_redactions SET header_hash = 'tampered' WHERE event_id = ?", (event_id,)
        )
        _, breaks = await verify_event_chain(db, room_id=room_id)
        assert len(breaks) == 1
        assert "header" in breaks[0].reason
    finally:
        await db.close()


async def test_tampering_the_event_redacted_announcement_breaks_the_chain():
    """The EVENT_REDACTED event's payload now carries the header_hash and
    original_event_hash it announces. A rewrite of that claim is only
    reachable by also recomputing the announcing event's own hash (the same
    "attack D" shape the round 2 audit already treats as inherent to an
    unkeyed chain, reproduced by hand with the module's own public
    event_chain_hash), which is exactly the scenario this binding is for:
    even a self-consistent forgery of the announcement is caught, because it
    no longer matches the redaction record it claims to name.
    """
    db, room_id, event_id = await _redacted_room()
    try:
        redaction = await db.fetch_one(
            "SELECT redaction_id FROM event_redactions WHERE event_id = ?", (event_id,)
        )
        assert redaction is not None
        row = await db.fetch_one(
            "SELECT * FROM room_events WHERE room_id = ? AND event_type = 'event.redacted'",
            (room_id,),
        )
        assert row is not None
        forged_payload = (
            '{{"redactions": [{{"redaction_id": "{}", '
            '"header_hash": "tampered", "original_event_hash": "tampered"}}], "count": 1}}'
        ).format(redaction["redaction_id"])
        forged_hash = event_chain_hash(
            str(row["prev_hash"]),
            str(row["event_id"]),
            room_id,
            int(row["sequence"]),
            str(row["event_type"]),
            forged_payload,
            str(row["actor_id"]),
            str(row["actor_type"]),
            str(row["timestamp"]),
            int(row["schema_version"]),
        )
        await db.execute("DROP TRIGGER IF EXISTS room_events_reject_hash_rewrite")
        await db.execute(
            "UPDATE room_events SET payload = ?, event_hash = ? WHERE event_id = ?",
            (forged_payload, forged_hash, str(row["event_id"])),
        )
        _, breaks = await verify_event_chain(db, room_id=room_id)
        assert len(breaks) == 1
        assert "does not match" in breaks[0].reason
    finally:
        await db.close()
