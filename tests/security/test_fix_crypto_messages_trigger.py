"""Finding #50: messages.content had no trigger, so the projection could
diverge from the chained log with nothing at the SQL level noticing.

Migration 050 adds ``messages_reject_content_update``, allowing an UPDATE of
``content`` only when the new value is exactly the redaction marker shape
(``{"redacted": true, "redaction_id": "..."}``) MessageRepo's own erasure path
writes. Any other UPDATE of content, including the erasure path's own legal
one, is exercised here directly against SQLite.
"""

from __future__ import annotations

import sqlite3

import pytest

from multiplayer.db.connection import Database
from multiplayer.domain.models import MessageRole, User
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService


async def _room_with_message() -> tuple[Database, MultiplayerService, str]:
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset())
    await svc.initialize()
    await svc.repos.users.create(User(user_id="alice", display_name="Alice", email="a@x.com"))
    org = await svc.create_organization("Org", "org", "alice")
    workspace = await svc.create_workspace(org.org_id, "Ws", "ws", "alice")
    room = await svc.create_room(workspace.workspace_id, "Room", "alice")
    message = await svc.send_message(room.room_id, MessageRole.HUMAN, "alice", "my secret plan")
    return db, svc, message.message_id


async def test_a_plain_content_edit_is_refused_by_sqlite_itself():
    db, _svc, message_id = await _room_with_message()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(
                "UPDATE messages SET content = ? WHERE message_id = ?",
                ("mallory wrote this instead", message_id),
            )
    finally:
        await db.close()


async def test_a_marker_shaped_edit_is_allowed():
    db, _svc, message_id = await _room_with_message()
    try:
        await db.execute(
            "UPDATE messages SET content = ? WHERE message_id = ?",
            ('{"redacted": true, "redaction_id": "redact_x"}', message_id),
        )
        row = await db.fetch_one("SELECT content FROM messages WHERE message_id = ?", (message_id,))
        assert row is not None
        assert row["content"] == '{"redacted": true, "redaction_id": "redact_x"}'
    finally:
        await db.close()


async def test_the_real_erasure_path_still_works_end_to_end():
    """The trigger must not break the one legitimate writer: MessageRepo's own
    redact_content_in_transaction, exercised here through the real erase_user
    call, the same as every other erasure test."""
    db, svc, message_id = await _room_with_message()
    try:
        await svc.erase_user("alice")
        row = await db.fetch_one("SELECT content FROM messages WHERE message_id = ?", (message_id,))
        assert row is not None
        assert "secret" not in row["content"]
    finally:
        await db.close()
