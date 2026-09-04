"""Finding #52: a marker payload with an extra key used to still verify clean.

``_redaction_marker``'s docstring always said a marker is exactly
``{"redacted": true, "redaction_id": "..."}``, but the code only checked that
those two keys were present and correct, not that nothing else rode along.
Combined with finding #15's original hash-only trust, an attacker could add
any key at all (a fake "content", a different "message_id") to a redacted row
and the chain still verified clean, and the export would echo it. This fix
makes both ``audit.py::_redaction_marker`` and
``erasure.py::_is_redaction_marker`` require the set of keys to be exactly
``{"redacted", "redaction_id"}``.
"""

from __future__ import annotations

import json

from multiplayer.db.connection import Database
from multiplayer.domain.models import MessageRole, User
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.audit import verify_event_chain
from multiplayer.services.erasure import _is_redaction_marker
from multiplayer.services.service import MultiplayerService


async def test_a_marker_with_an_extra_key_fails_the_ordinary_hash_check():
    """Once the extra key stops the marker fast path, the row falls through to
    the normal recompute, which fails: the stored event_hash was computed over
    the original message text, not this edited-in-place payload, so the chain
    breaks exactly where it should for any other unexplained payload rewrite.
    """
    db = Database(":memory:")
    await db.connect()
    try:
        svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset())
        await svc.initialize()
        await svc.repos.users.create(User(user_id="alice", display_name="Alice", email="a@x.com"))
        org = await svc.create_organization("Org", "org", "alice")
        workspace = await svc.create_workspace(org.org_id, "Ws", "ws", "alice")
        room = await svc.create_room(workspace.workspace_id, "Room", "alice")
        await svc.send_message(room.room_id, MessageRole.HUMAN, "alice", "my secret plan")
        await svc.erase_user("alice")
        row = await db.fetch_one(
            "SELECT event_id, payload FROM room_events "
            "WHERE room_id = ? AND event_type = 'message.created'",
            (room.room_id,),
        )
        assert row is not None
        marker = json.loads(row["payload"])
        marker["content"] = "mallory wrote this instead"
        await db.execute(
            "UPDATE room_events SET payload = ? WHERE event_id = ?",
            (json.dumps(marker), row["event_id"]),
        )
        _, breaks = await verify_event_chain(db, room_id=room.room_id)
        assert len(breaks) == 1
        assert "does not match the recomputed chain" in breaks[0].reason
    finally:
        await db.close()


def test_is_redaction_marker_rejects_an_extra_key():
    exact = {"redacted": True, "redaction_id": "redact_1"}
    with_extra = {"redacted": True, "redaction_id": "redact_1", "content": "leak"}
    assert _is_redaction_marker(exact) is True
    assert _is_redaction_marker(with_extra) is False
