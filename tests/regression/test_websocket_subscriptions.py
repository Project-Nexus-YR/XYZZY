"""Extra WebSocket room subscriptions: drained, released, and scoped to the
socket that created them.

Before this fix, a "subscribe" message dropped the returned subscription on
the floor (nothing ever read its queue and nothing released it on close), and
"unsubscribe" released every subscription the user held to that room across
every one of their sockets. Mirrors the fixture and client style of
tests/regression/test_reconnect.py, driven through the real app the way
tests/security/test_room_membership.py drives its websocket tests.
"""

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import multiplayer.api.routes as routes_mod
from multiplayer.domain.models import MessageRole
from multiplayer.server import create_app

TOKENS = {"owner-token": "owner"}
OWNER = {"Authorization": "Bearer owner-token"}


def _bootstrap(client: TestClient, room_name: str) -> str:
    """The first room. Bootstrap is idempotent per user, so a second call
    with a different room_name still returns this same room, so extra rooms
    need `_extra_room` instead.
    """
    response = client.post(
        "/api/v1/me/bootstrap",
        headers=OWNER,
        json={"display_name": "Owner", "room_name": room_name},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["room"]["room_id"])


def _extra_room(client: TestClient, name: str) -> str:
    workspaces = client.get(
        f"/api/v1/organizations/{_org_id(client)}/workspaces", headers=OWNER
    ).json()
    workspace_id = workspaces[0]["workspace_id"]
    response = client.post(
        f"/api/v1/workspaces/{workspace_id}/rooms", headers=OWNER, json={"name": name}
    )
    assert response.status_code == 200, response.text
    return str(response.json()["room_id"])


def _org_id(client: TestClient) -> str:
    orgs = client.get("/api/v1/me/context", headers=OWNER).json()["organizations"]
    return str(orgs[0]["org_id"])


def test_extra_room_subscriptions_are_all_drained() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, "Primary")
        extra1 = _extra_room(client, "Extra one")
        extra2 = _extra_room(client, "Extra two")
        svc = routes_mod._svc
        assert svc is not None

        with client.websocket_connect(f"/ws?room_id={room_id}", headers=OWNER) as websocket:
            assert websocket.receive_json()["type"] == "connected"
            websocket.send_json({"type": "subscribe", "room_id": extra1})
            assert websocket.receive_json() == {"type": "subscribed", "room_id": extra1}
            websocket.send_json({"type": "subscribe", "room_id": extra2})
            assert websocket.receive_json() == {"type": "subscribed", "room_id": extra2}

            asyncio.run(svc.send_message(extra1, MessageRole.HUMAN, "owner", "into extra one"))
            first = websocket.receive_json()
            assert first["type"] == "room_event"
            assert first["payload"]["content"] == "into extra one"

            asyncio.run(svc.send_message(extra2, MessageRole.HUMAN, "owner", "into extra two"))
            second = websocket.receive_json()
            assert second["type"] == "room_event"
            assert second["payload"]["content"] == "into extra two"


def test_abnormal_disconnect_releases_every_subscription(monkeypatch) -> None:
    """A dropped connection (a raise inside the receive loop, standing in for
    a client that vanishes without a close frame) must still release the
    primary and every extra subscription, not just the primary.
    """
    import starlette.websockets as starlette_ws

    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, "Primary")
        extra1 = _extra_room(client, "Extra one")
        extra2 = _extra_room(client, "Extra two")
        svc = routes_mod._svc
        assert svc is not None
        baseline = asyncio.run(svc.hub.subscriber_count())

        original_receive_text = starlette_ws.WebSocket.receive_text
        calls = {"n": 0}

        async def flaky_receive_text(self):
            calls["n"] += 1
            if calls["n"] > 2:
                raise RuntimeError("simulated dropped connection")
            return await original_receive_text(self)

        monkeypatch.setattr(starlette_ws.WebSocket, "receive_text", flaky_receive_text)

        with client.websocket_connect(f"/ws?room_id={room_id}", headers=OWNER) as websocket:
            assert websocket.receive_json()["type"] == "connected"
            websocket.send_json({"type": "subscribe", "room_id": extra1})
            assert websocket.receive_json() == {"type": "subscribed", "room_id": extra1}
            websocket.send_json({"type": "subscribe", "room_id": extra2})
            assert websocket.receive_json() == {"type": "subscribed", "room_id": extra2}
            websocket.send_json({"type": "ping"})
            with pytest.raises(WebSocketDisconnect):
                websocket.receive_json()

        assert asyncio.run(svc.hub.subscriber_count()) == baseline
        assert asyncio.run(svc.hub.room_subscriber_count(room_id)) == 0
        assert asyncio.run(svc.hub.room_subscriber_count(extra1)) == 0
        assert asyncio.run(svc.hub.room_subscriber_count(extra2)) == 0


def test_unsubscribe_only_releases_this_sockets_subscription() -> None:
    """Two sockets for the same user share a room. One unsubscribes; the
    other must keep receiving that room's events.
    """
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, "Shared room")
        svc = routes_mod._svc
        assert svc is not None

        with (
            client.websocket_connect(f"/ws?room_id={room_id}", headers=OWNER) as ws_a,
            client.websocket_connect(f"/ws?room_id={room_id}", headers=OWNER) as ws_b,
        ):
            assert ws_a.receive_json()["type"] == "connected"
            assert ws_b.receive_json()["type"] == "connected"

            ws_a.send_json({"type": "unsubscribe", "room_id": room_id})
            assert ws_a.receive_json() == {"type": "unsubscribed", "room_id": room_id}

            asyncio.run(svc.send_message(room_id, MessageRole.HUMAN, "owner", "still here"))

            event = ws_b.receive_json()
            assert event["type"] == "room_event"
            assert event["payload"]["content"] == "still here"

            assert asyncio.run(svc.hub.room_subscriber_count(room_id)) == 1


def test_repeated_subscribe_neither_duplicates_events_nor_leaks() -> None:
    """The same extra room subscribed twice on one socket is one subscription:
    a broadcast arrives once, and the count the hub holds for this socket is
    two (the primary plus the extra), not three.
    """
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, "Primary")
        extra = _extra_room(client, "Extra")
        svc = routes_mod._svc
        assert svc is not None
        baseline = asyncio.run(svc.hub.subscriber_count())

        with client.websocket_connect(f"/ws?room_id={room_id}", headers=OWNER) as websocket:
            assert websocket.receive_json()["type"] == "connected"
            for _ in range(2):
                websocket.send_json({"type": "subscribe", "room_id": extra})
                assert websocket.receive_json() == {"type": "subscribed", "room_id": extra}
            assert asyncio.run(svc.hub.subscriber_count()) == baseline + 2

            asyncio.run(svc.send_message(extra, MessageRole.HUMAN, "owner", "once"))
            asyncio.run(svc.send_message(room_id, MessageRole.HUMAN, "owner", "then primary"))
            first = websocket.receive_json()
            assert first["payload"]["content"] == "once"
            second = websocket.receive_json()
            assert second["payload"]["content"] == "then primary", "extra room event arrived twice"

        assert asyncio.run(svc.hub.subscriber_count()) == baseline
