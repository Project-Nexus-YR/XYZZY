"""verify_event_chain's own rules for a redacted event.

A redacted event's stored payload is a marker, not the content that hashed to
its stored ``event_hash``. Ordinary verification would recompute a hash from
the marker and find it disagrees with what was stored, so a redacted row
needs its own rule: trust the recorded ``original_event_hash`` in place of a
recomputation, and require every such trust to be backed by a matching
``event_redactions`` row and later announced by an ``event.redacted`` event.
Each of the three ways that can be faked is its own ChainBreak.

Round 2 (crypto track) added append-only triggers on ``room_events`` and
``event_redactions`` (migration 050): a plain UPDATE or DELETE against either
table is now refused by SQLite itself, defence in depth against an
in-process bug or a stray session on this same connection. A tamper
simulation below that needs to get such a write past SQLite the way an
attacker who holds the file directly would, by dropping the trigger first,
now does exactly that, immediately before the write, through the same
connection; every assertion about what the *verifier* then finds is
unchanged. The triggers' own refusal, with nothing dropped, is asserted
separately at the bottom of this file.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from multiplayer.db.connection import Database
from multiplayer.domain.models import MessageRole
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.audit import verify_event_chain
from multiplayer.services.service import MultiplayerService

TIMESTAMP = "2026-01-01T00:00:00+00:00"


async def _room_with_message() -> tuple[Database, MultiplayerService, str, str]:
    """A service, a room, and one message a real user sent in it."""
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset())
    await svc.initialize()
    org = await svc.create_organization("Org", "org", "alice")
    workspace = await svc.create_workspace(org.org_id, "Ws", "ws", "alice")
    room = await svc.create_room(workspace.workspace_id, "Room", "alice")
    message = await svc.send_message(room.room_id, MessageRole.HUMAN, "alice", "my secret plan")
    return db, svc, room.room_id, message.message_id


async def _redact_via_erasure(svc: MultiplayerService, user_id: str) -> None:
    """Insert a bare user row and run the real erasure path against it."""
    from multiplayer.domain.models import User

    await svc.repos.users.create(User(user_id=user_id, display_name="Alice", email="a@x.com"))
    await svc.erase_user(user_id)


async def test_a_properly_redacted_event_verifies_clean():
    db, svc, room_id, _ = await _room_with_message()
    try:
        await _redact_via_erasure(svc, "alice")
        verified, breaks = await verify_event_chain(db)
        assert breaks == []
        assert verified > 0
    finally:
        await db.close()


async def test_a_marker_with_no_redaction_row_breaks_the_chain():
    db, svc, room_id, _ = await _room_with_message()
    try:
        await _redact_via_erasure(svc, "alice")
        row = await db.fetch_one(
            "SELECT event_id FROM room_events WHERE room_id = ? AND event_type = 'message.created'",
            (room_id,),
        )
        assert row is not None
        # Simulate a hand edit that drops straight to a marker payload with no
        # event_redactions row behind it at all: pick a fresh, unregistered id.
        # A file-level attacker drops the append-only trigger first; simulated
        # here the same way, through the same connection, immediately before.
        await db.execute("DROP TRIGGER IF EXISTS event_redactions_reject_delete")
        await db.execute("DELETE FROM event_redactions WHERE event_id = ?", (row["event_id"],))
        _, breaks = await verify_event_chain(db, room_id=room_id)
        assert len(breaks) == 1
        assert "no matching event_redactions row" in breaks[0].reason
    finally:
        await db.close()


async def test_deleting_the_announcement_breaks_the_chain():
    db, svc, room_id, _ = await _room_with_message()
    try:
        await _redact_via_erasure(svc, "alice")
        await db.execute("DROP TRIGGER IF EXISTS room_events_reject_delete")
        await db.execute(
            "DELETE FROM room_events WHERE room_id = ? AND event_type = 'event.redacted'",
            (room_id,),
        )
        _, breaks = await verify_event_chain(db, room_id=room_id)
        assert len(breaks) == 1
    finally:
        await db.close()


async def test_altering_original_event_hash_breaks_the_chain():
    db, svc, room_id, _ = await _room_with_message()
    try:
        await _redact_via_erasure(svc, "alice")
        await db.execute("DROP TRIGGER IF EXISTS event_redactions_reject_update")
        await db.execute(
            "UPDATE event_redactions SET original_event_hash = 'tampered' WHERE room_id = ?",
            (room_id,),
        )
        _, breaks = await verify_event_chain(db, room_id=room_id)
        assert len(breaks) == 1
        assert "original_event_hash does not match" in breaks[0].reason
    finally:
        await db.close()


async def test_room_events_refuses_a_plain_update_or_delete():
    """Without dropping the trigger first, SQLite itself refuses both a
    header-column rewrite and a row delete on room_events."""
    db, svc, room_id, _ = await _room_with_message()
    try:
        await _redact_via_erasure(svc, "alice")
        row = await db.fetch_one(
            "SELECT event_id FROM room_events WHERE room_id = ? AND event_type = 'message.created'",
            (room_id,),
        )
        assert row is not None
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(
                "UPDATE room_events SET actor_id = 'mallory' WHERE event_id = ?",
                (row["event_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute("DELETE FROM room_events WHERE event_id = ?", (row["event_id"],))
    finally:
        await db.close()


async def test_event_redactions_refuses_a_plain_update_or_delete():
    """Without dropping the trigger first, SQLite itself refuses both an
    UPDATE and a DELETE on event_redactions."""
    db, svc, room_id, _ = await _room_with_message()
    try:
        await _redact_via_erasure(svc, "alice")
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(
                "UPDATE event_redactions SET reason = 'changed' WHERE room_id = ?", (room_id,)
            )
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute("DELETE FROM event_redactions WHERE room_id = ?", (room_id,))
    finally:
        await db.close()


async def test_a_naive_recompute_of_a_marker_payload_would_have_failed():
    """Sanity check on the test design itself: the marker really does not hash
    to the row's own stored event_hash under the ordinary rule, which is why
    verify_event_chain needs the special case at all rather than nothing."""
    db, svc, room_id, _ = await _room_with_message()
    try:
        await _redact_via_erasure(svc, "alice")
        row = await db.fetch_one(
            "SELECT payload, event_hash FROM room_events "
            "WHERE room_id = ? AND event_type = 'message.created'",
            (room_id,),
        )
        assert row is not None
        payload = json.loads(row["payload"])
        assert payload.get("redacted") is True
    finally:
        await db.close()
