"""Finding #51 (attribution half): event_redactions.actor_id and the
EVENT_REDACTED event's actor_id were always the constant "system", so an
auditor could never see who actually ran an erasure, even an honest one.

``erase_user`` now takes an ``operator_id`` (default unchanged: the constant
"system", so every existing caller that does not pass one gets exactly the
old behaviour). Given a real operator id, it is recorded on the redaction row
and the announcing event instead of the constant, and an unknown operator id
is refused the same way an unknown user_id already is.

The forgery half of finding #51 (a database writer can fabricate a
byte-identical "honest" redaction with any actor_id at all, since nothing
outside the file can tell an operator from an attacker) is ruled inherent to
an unkeyed chain, not fixed here; SECURITY.md now says so.
"""

from __future__ import annotations

import json

import pytest

from multiplayer.db.connection import Database
from multiplayer.domain.models import DomainError, MessageRole, User
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.erasure import ERASURE_OPERATOR_ID
from multiplayer.services.service import MultiplayerService


async def _seeded_room() -> tuple[Database, MultiplayerService, str]:
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset())
    await svc.initialize()
    await svc.repos.users.create(User(user_id="alice", display_name="Alice", email="a@x.com"))
    await svc.repos.users.create(User(user_id="mod_bob", display_name="Bob", email="b@x.com"))
    org = await svc.create_organization("Org", "org", "alice")
    workspace = await svc.create_workspace(org.org_id, "Ws", "ws", "alice")
    room = await svc.create_room(workspace.workspace_id, "Room", "alice")
    await svc.send_message(room.room_id, MessageRole.HUMAN, "alice", "my secret plan")
    return db, svc, room.room_id


async def test_default_operator_is_unchanged_from_before_this_fix():
    db, svc, room_id = await _seeded_room()
    try:
        await svc.erase_user("alice")
        redaction = await db.fetch_one(
            "SELECT actor_id FROM event_redactions WHERE room_id = ?", (room_id,)
        )
        assert redaction is not None
        assert redaction["actor_id"] == ERASURE_OPERATOR_ID == "system"
    finally:
        await db.close()


async def test_an_explicit_operator_is_recorded_on_the_redaction_and_the_event():
    db, svc, room_id = await _seeded_room()
    try:
        await svc.erase_user("alice", operator_id="mod_bob")
        redaction = await db.fetch_one(
            "SELECT actor_id FROM event_redactions WHERE room_id = ?", (room_id,)
        )
        assert redaction is not None
        assert redaction["actor_id"] == "mod_bob"

        announcement = await db.fetch_one(
            "SELECT actor_id, payload FROM room_events "
            "WHERE room_id = ? AND event_type = 'event.redacted'",
            (room_id,),
        )
        assert announcement is not None
        assert announcement["actor_id"] == "mod_bob"
        payload = json.loads(announcement["payload"])
        # alice's message plus the room name she typed when creating the room.
        assert payload["count"] == 2
        assert "redaction_id" in payload["redactions"][0]
    finally:
        await db.close()


async def test_an_unknown_operator_is_refused():
    db, svc, _room_id = await _seeded_room()
    try:
        with pytest.raises(DomainError):
            await svc.erase_user("alice", operator_id="nobody")
    finally:
        await db.close()
