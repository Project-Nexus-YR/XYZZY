"""Round 7: erasing a user must not leave their room name or display name
sitting in someone else's notification inbox.

``notifications.title``/``body`` used to be filed under "kept_by_ruling"
alongside genuinely shared infrastructure (templates, org/workspace names).
An invitation notice names the erased person, not a group, so it belongs in
the "redacted" bucket like every other free-text column an erased user's own
action populated: ``title='You were invited to #alice-room-name'``,
``body='Invited as editor by Alice'``.
"""

from __future__ import annotations

from multiplayer.db.connection import Database
from multiplayer.domain.models import User
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.audit import verify_event_chain
from multiplayer.services.service import MultiplayerService


async def _room_with_invite() -> tuple[Database, MultiplayerService, str, str]:
    """Alice creates a room and invites Bob; the invite notification names
    both the room and Alice by their display names."""
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset())
    await svc.initialize()
    await svc.repos.users.create(User(user_id="alice", display_name="Alice", email="a@x.com"))
    await svc.repos.users.create(User(user_id="bob", display_name="Bob", email="b@x.com"))
    org = await svc.create_organization("Org", "org", "alice")
    workspace = await svc.create_workspace(org.org_id, "Ws", "ws", "alice")
    room = await svc.create_room(workspace.workspace_id, "alice-room-name", "alice")
    await svc.invite_room_member(room.room_id, "bob", "editor", "alice")
    return db, svc, room.room_id, "alice"


async def _all_notification_text(db: Database) -> str:
    rows = await db.fetch_all("SELECT * FROM notifications")
    return " ".join(f"{r['title']} {r['body']}" for r in rows)


async def test_erasing_the_inviter_scrubs_the_invitees_notification():
    db, svc, _room_id, alice = await _room_with_invite()
    try:
        before = await _all_notification_text(db)
        assert "alice-room-name" in before
        assert "Alice" in before

        await svc.erase_user(alice)

        after = await _all_notification_text(db)
        assert "alice-room-name" not in after
        assert "Alice" not in after

        verified, breaks = await verify_event_chain(db)
        assert breaks == []
        assert verified > 0
    finally:
        await db.close()


async def test_a_second_erase_is_idempotent():
    db, svc, _room_id, alice = await _room_with_invite()
    try:
        await svc.erase_user(alice)
        after_first = await _all_notification_text(db)

        result = await svc.erase_user(alice)

        after_second = await _all_notification_text(db)
        assert after_second == after_first
        assert result["redactions"] == 0
        assert result["rooms_touched"] == []

        verified, breaks = await verify_event_chain(db)
        assert breaks == []
    finally:
        await db.close()


async def test_erasing_the_recipient_scrubs_their_own_notification():
    """The erased user need not be the inviter: their own inbox row (the
    "subject" half of the ruling) is scrubbed even when they authored no
    event in that room at all."""
    db, svc, _room_id, _alice = await _room_with_invite()
    try:
        await svc.erase_user("bob")

        after = await _all_notification_text(db)
        assert "alice-room-name" not in after

        verified, breaks = await verify_event_chain(db)
        assert breaks == []
    finally:
        await db.close()
