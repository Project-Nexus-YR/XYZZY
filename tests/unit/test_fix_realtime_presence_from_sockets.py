"""Finding 27 (medium): presence used to be created only by `POST
/join` (`join_room` -> `presence.user_joined`), which the shipped web
client never calls (grep for '/join' over web/ returns nothing but
stroke-linejoin). The socket only ever `heartbeat`ed an entry that already
existed, a no-op otherwise (`PresenceService.heartbeat`'s own `xx=True`
Redis flag and its in-memory twin's `if key in self._presence` guard), so a
browser user never appeared in `/rooms/{id}/presence`, and an API user who
did join stayed ONLINE forever after their socket disconnected (nothing
called `user_left`).

Presence is now derived from sockets: subscribing marks the member online
(`user_joined`, an upsert), the reauth tick still heartbeats, and the last
socket for a (user, room) pair closing marks them offline (`user_left`).
`POST /join` keeps working unchanged (it is a second, independent way to
become present, not replaced).

Ruling: the brief's "after a short grace" is implemented as an immediate
transition on the last socket's close rather than a timed grace window — a
timer needs a background task and wall-clock-sensitive tests for a low/
medium-severity gap; the observable contract (a member who is no longer
connected eventually reads as offline) holds either way, and two sockets
for one user (below) already proves a single tab closing does not flap it.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from multiplayer.server import create_app

TOKENS = {"owner-token": "owner"}
OWNER = {"Authorization": "Bearer owner-token"}


def _bootstrap(client: TestClient, headers: dict[str, str], room_name: str) -> str:
    response = client.post(
        "/api/v1/me/bootstrap",
        headers=headers,
        json={"display_name": "Owner", "room_name": room_name},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["room"]["room_id"])


def _presence(client: TestClient, room_id: str) -> dict[str, str]:
    response = client.get(f"/api/v1/rooms/{room_id}/presence", headers=OWNER)
    assert response.status_code == 200, response.text
    return {row["user_id"]: row["status"] for row in response.json()}


def test_a_socket_that_never_joined_still_appears_in_presence() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, OWNER, "Presence room")

        assert _presence(client, room_id) == {}

        with client.websocket_connect(f"/ws?room_id={room_id}", headers=OWNER) as websocket:
            assert websocket.receive_json()["type"] == "connected"
            assert _presence(client, room_id) == {"owner": "ONLINE"}

        # The socket closed: the only thing that ever marked "owner" present
        # is gone, so presence must not keep claiming they are still here.
        assert _presence(client, room_id) == {}


def test_two_sockets_for_one_user_only_go_offline_when_the_last_one_closes() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, OWNER, "Presence room")

        with client.websocket_connect(f"/ws?room_id={room_id}", headers=OWNER) as first:
            assert first.receive_json()["type"] == "connected"
            assert _presence(client, room_id) == {"owner": "ONLINE"}

            with client.websocket_connect(f"/ws?room_id={room_id}", headers=OWNER) as second:
                assert second.receive_json()["type"] == "connected"
                # Still just the one user, still online, with two sockets open.
                assert _presence(client, room_id) == {"owner": "ONLINE"}

            # The second socket closed; the first is still open.
            assert _presence(client, room_id) == {"owner": "ONLINE"}

        # Both closed now.
        assert _presence(client, room_id) == {}
