"""``manage.py user erase``: the operator verb for erasing a user's content.

Covers the acceptance bar for the erasure track: the verb exists and refuses
an unknown id, an author's messages across two rooms come back as markers
everywhere a reader can see them (get_room_state, the export, the chain
itself), a second pass is a no-op, and another user's own data is left alone.
"""

from __future__ import annotations

import json

import pytest

from multiplayer.db.connection import Database
from multiplayer.domain.models import MessageRole, User
from multiplayer.manage import erase_user, open_database
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.audit import verify_event_chain
from multiplayer.services.service import MultiplayerService


async def _seeded_db(path: str) -> tuple[Database, str]:
    """A fresh database with two real users and one workspace both can use."""
    db = await open_database(path)
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset())
    await svc.repos.users.create(User(user_id="alice", display_name="Alice", email="alice@x.com"))
    await svc.repos.users.create(User(user_id="bob", display_name="Bob", email="bob@x.com"))
    org = await svc.create_organization("Org", "org", "alice")
    workspace = await svc.create_workspace(org.org_id, "Ws", "ws", "alice")
    return db, workspace.workspace_id


async def test_erase_refuses_an_unknown_user_id(tmp_path):
    db = await open_database(str(tmp_path / "app.db"))
    try:
        with pytest.raises(ValueError):
            await erase_user(db, "nobody")
    finally:
        await db.close()


async def test_erasing_across_two_rooms_leaves_no_original_text_anywhere(tmp_path):
    db, workspace_id = await _seeded_db(str(tmp_path / "app.db"))
    try:
        svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset())
        room_a = await svc.create_room(workspace_id, "Room A", "alice")
        room_b = await svc.create_room(workspace_id, "Room B", "alice")
        await svc.invite_room_member(room_a.room_id, "bob", "editor", "alice")

        msg_a = await svc.send_message(
            room_a.room_id, MessageRole.HUMAN, "alice", "alice's secret A"
        )
        msg_b = await svc.send_message(
            room_b.room_id, MessageRole.HUMAN, "alice", "alice's secret B"
        )
        bob_msg = await svc.send_message(room_a.room_id, MessageRole.HUMAN, "bob", "bob said this")

        result = await erase_user(db, "alice")
        # Alice's two messages, plus the room name she typed for each of the two
        # rooms she created (room.created's own payload carries "name").
        assert result["redactions"] == 4
        assert set(result["rooms_touched"]) == {room_a.room_id, room_b.room_id}

        for room in (room_a, room_b):
            after = await svc.repos.rooms.get(room.room_id)
            assert after is not None
            assert after.name != room.name
            assert json.loads(after.name)["redacted"] is True

        for room_id in (room_a.room_id, room_b.room_id):
            verified, breaks = await verify_event_chain(db, room_id=room_id)
            assert breaks == [], breaks

        state_a = await svc.get_room_state(room_a.room_id)
        state_b = await svc.get_room_state(room_b.room_id)
        for state in (state_a, state_b):
            for message in state["messages"]:
                if message["message_id"] in (msg_a.message_id, msg_b.message_id):
                    assert "secret" not in message["content"]
                    assert json.loads(message["content"])["redacted"] is True
            for event in state["events_since"]:
                assert "secret" not in json.dumps(event["payload"])

        # Bob's own message, in the same room alice's redacted message lives in,
        # is untouched: erasing alice never touches bob's content.
        bob_after = await svc.repos.messages.get(bob_msg.message_id)
        assert bob_after is not None and bob_after.content == "bob said this"
        bob_user = await svc.repos.users.get("bob")
        assert bob_user is not None and bob_user.display_name == "Bob"

        export_lines_a = [json.loads(line) async for line in svc.export_room_audit(room_a.room_id)]
        assert not any("secret" in line for line in (json.dumps(x) for x in export_lines_a))
        redaction_lines = [line for line in export_lines_a if "redaction" in line]
        assert redaction_lines, "export must surface redaction metadata"
        assert redaction_lines[0]["redaction"]["reason"] == "user erasure"

        alice_after = await svc.repos.users.get("alice")
        assert alice_after is not None
        assert alice_after.display_name == "Erased user"
        assert alice_after.email != "alice@x.com"

        # A second pass changes nothing further.
        second = await erase_user(db, "alice")
        assert second["redactions"] == 0
        assert second["rooms_touched"] == []
    finally:
        await db.close()
