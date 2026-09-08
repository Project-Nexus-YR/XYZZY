"""Workspace membership changes fence live delivery and concurrent room creation."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import multiplayer.api.routes as routes
from multiplayer.db.connection import Database
from multiplayer.domain.models import MessageRole
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.server import create_app
from multiplayer.services.service import MultiplayerService


class _RemoteFanout:
    """Deliver the revocations a different process would receive from Redis."""

    def __init__(self, remote: RealtimeHub) -> None:
        self.remote = remote
        self.messages: list[dict[str, Any]] = []

    async def publish(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        if message["kind"] == "revoke":
            await self.remote.revoke_room_access(
                message["user_id"], message["room_id"], publish=False
            )
        elif message["kind"] == "room_event":
            await self.remote.broadcast_to_room(message["room_id"], message["event"])


@pytest.mark.asyncio
async def test_workspace_removal_revokes_local_and_remote_rooms_before_broadcast() -> None:
    db = Database(":memory:")
    await db.connect()
    remote = RealtimeHub()
    fanout = _RemoteFanout(remote)
    hub = RealtimeHub(fanout)
    svc = MultiplayerService(db, hub, known_users=frozenset({"owner", "guest"}))
    try:
        await svc.initialize()
        org = await svc.create_organization("Org", "org", "owner")
        workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
        rooms = [
            await svc.create_room(workspace.workspace_id, name, "owner")
            for name in ("Local room", "Remote only room")
        ]
        for room in rooms:
            await svc.invite_room_member(room.room_id, "guest", "editor", "owner")
        local_guest = await hub.subscribe(rooms[0].room_id, "guest")
        remote_guest = await remote.subscribe(rooms[1].room_id, "guest")
        owner = await hub.subscribe(rooms[0].room_id, "owner")
        fanout.messages.clear()

        await svc.remove_workspace_member(workspace.workspace_id, "guest", "owner")

        assert await hub.get_user_rooms("guest") == set()
        assert await remote.get_user_rooms("guest") == set()
        assert local_guest.queue.get_nowait()["type"] == "access_revoked"
        assert remote_guest.queue.get_nowait()["type"] == "access_revoked"
        assert owner.queue.get_nowait()["event_type"] == "workspace.member_removed"
        assert {
            message["room_id"] for message in fanout.messages if message["kind"] == "revoke"
        } == {room.room_id for room in rooms}

        for room in rooms:
            await svc.send_message(room.room_id, MessageRole.HUMAN, "owner", "After removal")
        assert local_guest.queue.empty()
        assert remote_guest.queue.empty()
        assert owner.queue.get_nowait()["payload"]["content"] == "After removal"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_presence_failure_cannot_interrupt_multiroom_workspace_removal(monkeypatch) -> None:
    db = Database(":memory:")
    await db.connect()
    hub = RealtimeHub()
    svc = MultiplayerService(db, hub, known_users=frozenset({"owner", "guest"}))
    try:
        await svc.initialize()
        org = await svc.create_organization("Org", "org", "owner")
        workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
        rooms = [
            await svc.create_room(workspace.workspace_id, name, "owner")
            for name in ("First", "Second")
        ]
        for room in rooms:
            await svc.invite_room_member(room.room_id, "guest", "editor", "owner")
        other_workspace = await svc.create_workspace(org.org_id, "Other", "other", "owner")
        other_room = await svc.create_room(other_workspace.workspace_id, "Other", "owner")
        await svc.invite_room_member(other_room.room_id, "guest", "editor", "owner")
        guests = [await hub.subscribe(room.room_id, "guest") for room in rooms]
        owners = [await hub.subscribe(room.room_id, "owner") for room in rooms]
        remaining_socket = await hub.subscribe(other_room.room_id, "guest")
        attempted_cleanup: list[str] = []

        async def unavailable_presence(_user_id: str, room_id: str) -> None:
            attempted_cleanup.append(room_id)
            raise ConnectionError("Redis presence is unavailable")

        monkeypatch.setattr(svc.presence, "user_left", unavailable_presence)
        await svc.remove_workspace_member(workspace.workspace_id, "guest", "owner")

        assert await svc.repos.workspaces.get_member(workspace.workspace_id, "guest") is None
        assert await hub.get_user_rooms("guest") == {other_room.room_id}
        assert set(attempted_cleanup) == {room.room_id for room in rooms}
        for room, guest, owner in zip(rooms, guests, owners, strict=True):
            assert await svc.repos.room_members.get(room.room_id, "guest") is None
            assert guest.queue.get_nowait()["type"] == "access_revoked"
            assert guest.queue.empty()
            event = owner.queue.get_nowait()
            assert event["event_type"] == "workspace.member_removed"
            assert event["room_id"] == room.room_id
            assert owner.queue.empty()
        notices = [remaining_socket.queue.get_nowait() for _room in rooms]
        assert {notice["room_id"] for notice in notices} == {room.room_id for room in rooms}
        assert all(notice["type"] == "room_removed" for notice in notices)
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["remove", "leave"])
async def test_presence_failure_cannot_interrupt_room_membership_events(
    monkeypatch, operation: str
) -> None:
    db = Database(":memory:")
    await db.connect()
    hub = RealtimeHub()
    svc = MultiplayerService(db, hub, known_users=frozenset({"owner", "guest"}))
    try:
        await svc.initialize()
        org = await svc.create_organization("Org", "org", "owner")
        workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
        room = await svc.create_room(workspace.workspace_id, "Shared", "owner")
        await svc.invite_room_member(room.room_id, "guest", "editor", "owner")
        guest = await hub.subscribe(room.room_id, "guest")
        owner = await hub.subscribe(room.room_id, "owner")

        async def unavailable_presence(_user_id: str, _room_id: str) -> None:
            raise ConnectionError("Redis presence is unavailable")

        monkeypatch.setattr(svc.presence, "user_left", unavailable_presence)
        if operation == "remove":
            await svc.remove_room_member(room.room_id, "guest", "owner")
            expected = "user.removed_room"
        else:
            await svc.leave_room(room.room_id, "guest")
            expected = "user.left_room"

        assert await svc.repos.room_members.get(room.room_id, "guest") is None
        assert await hub.get_user_rooms("guest") == set()
        assert guest.queue.get_nowait()["type"] == "access_revoked"
        assert owner.queue.get_nowait()["event_type"] == expected
    finally:
        await db.close()


def test_room_creation_rechecks_membership_after_route_authorization(monkeypatch) -> None:
    owner = {"Authorization": "Bearer owner-token"}
    guest = {"Authorization": "Bearer guest-token"}
    app = create_app(":memory:", auth_tokens={"owner-token": "owner", "guest-token": "guest"})
    with TestClient(app) as client:
        bootstrap = client.post(
            "/api/v1/me/bootstrap",
            headers=owner,
            json={"display_name": "Owner", "room_name": "Shared"},
        ).json()
        workspace_id = bootstrap["workspace"]["workspace_id"]
        room_id = bootstrap["room"]["room_id"]
        invite = client.post(
            f"/api/v1/rooms/{room_id}/members/invitations",
            headers=owner,
            json={"user_id": "guest", "role": "editor"},
        )
        assert invite.status_code == 200
        svc = routes._svc
        assert svc is not None
        original = svc.create_room

        async def remove_after_route_check(*args: Any, **kwargs: Any):
            await svc.remove_workspace_member(workspace_id, "guest", "owner")
            return await original(*args, **kwargs)

        monkeypatch.setattr(svc, "create_room", remove_after_route_check)
        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/rooms",
            headers=guest,
            json={"name": "Must not exist"},
        )
        assert response.status_code == 403
        remaining = client.get(f"/api/v1/workspaces/{workspace_id}/rooms", headers=owner).json()
        assert [room["room_id"] for room in remaining] == [room_id]

        monkeypatch.setattr(svc, "create_room", original)
        permitted = client.post(
            f"/api/v1/workspaces/{workspace_id}/rooms",
            headers=owner,
            json={"name": "Owner's room"},
        )
        assert permitted.status_code == 200
