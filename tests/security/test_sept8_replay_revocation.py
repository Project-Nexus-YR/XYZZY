"""A membership revoked during subscription or replay cannot deliver another event."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

import multiplayer.api.routes as routes
from multiplayer.domain.models import MessageRole
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.realtime.websocket import websocket_endpoint
from multiplayer.server import create_app
from multiplayer.services.service import MultiplayerService

OWNER = {"Authorization": "Bearer owner-token"}
GUEST = {"Authorization": "Bearer guest-token"}


@pytest.fixture
def shared_room() -> Iterator[tuple[TestClient, MultiplayerService, str]]:
    app = create_app(":memory:", auth_tokens={"owner-token": "owner", "guest-token": "guest"})
    with TestClient(app) as client:
        room_id = client.post(
            "/api/v1/me/bootstrap",
            headers=OWNER,
            json={"display_name": "Owner", "room_name": "Private"},
        ).json()["room"]["room_id"]
        response = client.post(
            f"/api/v1/rooms/{room_id}/members/invitations",
            headers=OWNER,
            json={"user_id": "guest", "role": "viewer"},
        )
        assert response.status_code == 200
        assert routes._svc is not None
        yield client, routes._svc, room_id


def test_revocation_between_authorization_and_subscribe_denies_connection(
    shared_room, monkeypatch
) -> None:
    client, svc, room_id = shared_room
    original = svc.hub.subscribe

    async def subscribe_after_removal(*args: Any, **kwargs: Any):
        await svc.remove_room_member(room_id, "guest", "owner")
        return await original(*args, **kwargs)

    monkeypatch.setattr(svc.hub, "subscribe", subscribe_after_removal)
    with client.websocket_connect(f"/ws?room_id={room_id}", headers=GUEST) as socket:
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 4403
    assert asyncio.run(svc.hub.subscriber_count()) == 0


def test_revocation_while_reading_replay_denies_even_a_freshly_written_event(
    shared_room, monkeypatch
) -> None:
    client, svc, room_id = shared_room
    original = svc.get_room_events

    async def read_after_removal(*args: Any, **kwargs: Any):
        await svc.remove_room_member(room_id, "guest", "owner")
        await svc.send_message(room_id, MessageRole.HUMAN, "owner", "After removal")
        return await original(*args, **kwargs)

    monkeypatch.setattr(svc, "get_room_events", read_after_removal)
    with client.websocket_connect(
        f"/ws?room_id={room_id}&last_sequence=0", headers=GUEST
    ) as socket:
        assert socket.receive_json()["type"] == "connected"
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 4403
    assert asyncio.run(svc.hub.subscriber_count()) == 0


def test_revocation_between_events_stops_the_rest_of_the_replay_page(
    shared_room, monkeypatch
) -> None:
    client, svc, room_id = shared_room
    original = WebSocket.send_json
    removed = False

    async def remove_after_first_event(self, data, *args, **kwargs):
        nonlocal removed
        await original(self, data, *args, **kwargs)
        if data.get("type") == "room_event" and not removed:
            removed = True
            await svc.remove_room_member(room_id, "guest", "owner")

    monkeypatch.setattr(WebSocket, "send_json", remove_after_first_event)
    with client.websocket_connect(
        f"/ws?room_id={room_id}&last_sequence=0", headers=GUEST
    ) as socket:
        assert socket.receive_json()["type"] == "connected"
        assert socket.receive_json()["event_type"] == "room.created"
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 4403
    assert asyncio.run(svc.hub.subscriber_count()) == 0


def test_member_replays_the_complete_page_and_receives_live_events(shared_room) -> None:
    client, _svc, room_id = shared_room
    with client.websocket_connect(
        f"/ws?room_id={room_id}&last_sequence=0", headers=GUEST
    ) as socket:
        assert socket.receive_json()["type"] == "connected"
        assert socket.receive_json()["event_type"] == "room.created"
        assert socket.receive_json()["event_type"] == "user.invited_room"
        response = client.post(
            f"/api/v1/rooms/{room_id}/messages", headers=OWNER, json={"content": "Live"}
        )
        assert response.status_code == 200
        assert socket.receive_json()["payload"]["content"] == "Live"


def test_revocation_discards_room_events_queued_before_the_close_marker(
    shared_room, monkeypatch
) -> None:
    client, svc, room_id = shared_room
    original = WebSocket.send_json
    removed = False

    async def queue_then_remove(self, data, *args, **kwargs):
        nonlocal removed
        await original(self, data, *args, **kwargs)
        if data.get("type") == "room_event" and not removed:
            removed = True
            await svc.send_message(room_id, MessageRole.HUMAN, "owner", "Still queued")
            await svc.remove_room_member(room_id, "guest", "owner")

    monkeypatch.setattr(WebSocket, "send_json", queue_then_remove)
    with client.websocket_connect(f"/ws?room_id={room_id}", headers=GUEST) as socket:
        assert socket.receive_json()["type"] == "connected"
        response = client.post(
            f"/api/v1/rooms/{room_id}/messages", headers=OWNER, json={"content": "First"}
        )
        assert response.status_code == 200
        assert socket.receive_json()["payload"]["content"] == "First"
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 4403


def test_extra_subscription_rechecks_membership_after_registration(shared_room, monkeypatch):
    client, svc, room_id = shared_room
    workspace_id = client.get(f"/api/v1/rooms/{room_id}", headers=OWNER).json()["workspace_id"]
    extra_id = client.post(
        f"/api/v1/workspaces/{workspace_id}/rooms", headers=OWNER, json={"name": "Extra"}
    ).json()["room_id"]
    assert (
        client.post(
            f"/api/v1/rooms/{extra_id}/members/invitations",
            headers=OWNER,
            json={"user_id": "guest", "role": "viewer"},
        ).status_code
        == 200
    )
    original = svc.hub.subscribe

    async def subscribe_after_removal(subscribed_room, user_id, **kwargs):
        if subscribed_room == extra_id:
            await svc.remove_room_member(extra_id, "guest", "owner")
        return await original(subscribed_room, user_id, **kwargs)

    monkeypatch.setattr(svc.hub, "subscribe", subscribe_after_removal)
    with client.websocket_connect(f"/ws?room_id={room_id}", headers=GUEST) as socket:
        assert socket.receive_json()["type"] == "connected"
        socket.send_json({"type": "subscribe", "room_id": extra_id})
        with pytest.raises(WebSocketDisconnect) as closed:
            while True:
                # The still-authorized primary subscription may carry the
                # sidebar removal notice before this socket is closed.
                assert socket.receive_json() == {"type": "room_removed", "room_id": extra_id}
        assert closed.value.code == 4403
    assert asyncio.run(svc.hub.subscriber_count()) == 0


@pytest.mark.asyncio
async def test_failed_live_membership_check_closes_and_releases_a_silent_peer() -> None:
    class Socket:
        query_params = {"room_id": "room"}
        headers = {"authorization": "Bearer local-test"}

        def __init__(self) -> None:
            self.connected = asyncio.Event()
            self.never_received = asyncio.Event()
            self.frames: list[dict[str, Any]] = []
            self.closes: list[int] = []

        async def accept(self, **_kwargs) -> None:
            return None

        async def send_json(self, frame: dict[str, Any]) -> None:
            self.frames.append(frame)
            if frame["type"] == "connected":
                self.connected.set()

        async def close(self, *, code: int, reason: str) -> None:
            self.closes.append(code)
            # A silent peer does not acknowledge the close. The handler must
            # still stop its receive loop and release its own subscription.

        async def receive_text(self) -> str:
            await self.never_received.wait()
            return '{"type": "ping"}'

    class Auth:
        async def authenticate(self, _authorization):
            return SimpleNamespace(user_id="member")

    class Policy:
        fail = False

        async def require(self, *_args) -> None:
            if self.fail:
                raise OSError("membership database unavailable")

    socket = Socket()
    hub = RealtimeHub()
    policy = Policy()
    handler = asyncio.create_task(websocket_endpoint(socket, hub, Auth(), policy, None))
    try:
        await asyncio.wait_for(socket.connected.wait(), timeout=1)
        policy.fail = True
        await hub.broadcast_to_room(
            "room", {"type": "room_event", "room_id": "room", "sequence": 1}
        )
        await asyncio.wait_for(handler, timeout=1)
        assert socket.closes == [1011]
        assert [frame["type"] for frame in socket.frames] == ["connected"]
        assert await hub.subscriber_count() == 0
    finally:
        handler.cancel()
        await asyncio.gather(handler, return_exceptions=True)


def test_presence_cleanup_failure_releases_every_socket_subscription(shared_room, monkeypatch):
    client, svc, room_id = shared_room
    workspace_id = client.get(f"/api/v1/rooms/{room_id}", headers=OWNER).json()["workspace_id"]
    extra_id = client.post(
        f"/api/v1/workspaces/{workspace_id}/rooms", headers=OWNER, json={"name": "Extra"}
    ).json()["room_id"]
    assert (
        client.post(
            f"/api/v1/rooms/{extra_id}/members/invitations",
            headers=OWNER,
            json={"user_id": "guest", "role": "viewer"},
        ).status_code
        == 200
    )

    async def unavailable_presence(_user_id: str, _room_id: str) -> None:
        raise ConnectionError("Redis presence is unavailable")

    monkeypatch.setattr(svc.presence, "user_left", unavailable_presence)
    with client.websocket_connect(f"/ws?room_id={room_id}", headers=GUEST) as socket:
        assert socket.receive_json()["type"] == "connected"
        socket.send_json({"type": "subscribe", "room_id": extra_id})
        assert socket.receive_json() == {"type": "subscribed", "room_id": extra_id}
    assert asyncio.run(svc.hub.subscriber_count()) == 0
