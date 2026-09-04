"""Finding 71 (low): last_sequence was parsed with ``str.isdigit()``. A
Unicode digit (e.g. the superscript two, U+00B2) is ``isdigit() is True`` but
``int()`` rejects it, so an authenticated caller could trigger an unhandled
``ValueError`` inside the ASGI app (a 500 at the handshake) with a
one-character query string. A negative or alphabetic cursor fell through to
``replay_cursor = None`` silently: no replay, no error — the opposite of
what the same parameter does on ``GET /rooms/{id}/state``
(``Query(0, ge=0)``), which rejects it.

websocket.py now parses with ``int()`` directly and closes 4400 on anything
that is not a non-negative integer, rather than raising or silently
ignoring it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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


def test_unicode_digit_cursor_closes_4400_instead_of_raising() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, OWNER, "Cursor room")
        # U+00B2 SUPERSCRIPT TWO: str.isdigit() is True, int() raises ValueError.
        with pytest.raises(WebSocketDisconnect) as disconnect:
            with client.websocket_connect(
                f"/ws?room_id={room_id}&last_sequence=²", headers=OWNER
            ) as websocket:
                websocket.receive_json()
    assert disconnect.value.code == 4400


def test_negative_cursor_closes_4400_instead_of_silently_replaying_nothing() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, OWNER, "Cursor room")
        with pytest.raises(WebSocketDisconnect) as disconnect:
            with client.websocket_connect(
                f"/ws?room_id={room_id}&last_sequence=-1", headers=OWNER
            ) as websocket:
                websocket.receive_json()
    assert disconnect.value.code == 4400


def test_alphabetic_cursor_closes_4400() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, OWNER, "Cursor room")
        with pytest.raises(WebSocketDisconnect) as disconnect:
            with client.websocket_connect(
                f"/ws?room_id={room_id}&last_sequence=abc", headers=OWNER
            ) as websocket:
                websocket.receive_json()
    assert disconnect.value.code == 4400


def test_valid_zero_cursor_still_replays_normally() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, OWNER, "Cursor room")
        with client.websocket_connect(
            f"/ws?room_id={room_id}&last_sequence=0", headers=OWNER
        ) as websocket:
            assert websocket.receive_json()["type"] == "connected"
            replayed = websocket.receive_json()
            assert replayed["type"] == "room_event"
            assert replayed["sequence"] == 1
