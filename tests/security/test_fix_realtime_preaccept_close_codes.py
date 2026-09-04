"""Finding 25 (medium): the three pre-accept rejections (4400 no room_id,
4401 no/bad credential, 4403 forbidden room) used to close the socket
*before* accepting the WebSocket upgrade. Starlette's TestClient still hands
that back to the caller as a `WebSocketDisconnect` carrying the code, which
is why the existing security tests (test_cookie_auth.py, kept unchanged
below) never caught this: they only ever exercised the TestClient's own
transport. A real client cannot: rejecting an upgrade closes the HTTP
handshake with a 403, which every real WebSocket client (browsers included)
reports as `onclose {code: 1006, reason: ''}` — indistinguishable from "the
server is unreachable".

websocket.py now accepts the socket first (no room state is sent before any
of the three checks below), then closes with the real code, so a real
client reads it. This test proves it against an actual uvicorn/websockets
server on a free port, not TestClient.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
import websockets
from websockets.exceptions import ConnectionClosed

from multiplayer.server import create_app

TOKENS = {"owner-token": "owner", "alex-token": "alex"}
OWNER = {"Authorization": "Bearer owner-token"}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _LiveServer:
    def __init__(self, db_path: str) -> None:
        self.port = _free_port()
        app = create_app(db_path, auth_tokens=TOKENS)
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/ws"

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if getattr(self.server, "started", False):
                return
            time.sleep(0.05)
        raise RuntimeError("live server did not start within 20s")

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def live_server(tmp_path) -> Iterator[_LiveServer]:
    server = _LiveServer(str(tmp_path / "fix-realtime-close-codes.db"))
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _bootstrap_room(base_url: str) -> str:
    response = httpx.post(
        f"{base_url}/api/v1/me/bootstrap",
        headers=OWNER,
        json={"display_name": "Owner", "room_name": "Real server room"},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["room"]["room_id"])


async def _handshake_close_code(uri: str, headers: dict[str, str] | None = None) -> int:
    """Connect for real and return the close code the server sends.

    A pre-accept rejection would instead raise `InvalidStatus` (the
    handshake itself fails with an HTTP 403) before a socket ever opens —
    exactly the defect this test exists to catch, so that exception is
    allowed to propagate rather than being caught here.
    """
    async with websockets.connect(uri, additional_headers=headers or {}, open_timeout=5) as ws:
        try:
            await asyncio.wait_for(ws.recv(), timeout=5)
        except ConnectionClosed as exc:
            assert exc.rcvd is not None
            return exc.rcvd.code
        raise AssertionError("expected the server to close the socket, got a message instead")


def test_missing_room_id_closes_4400_on_a_real_client(live_server: _LiveServer) -> None:
    code = asyncio.run(_handshake_close_code(f"{live_server.ws_url}?room_id=", headers=dict(OWNER)))
    assert code == 4400


def test_missing_credential_closes_4401_on_a_real_client(live_server: _LiveServer) -> None:
    room_id = _bootstrap_room(live_server.base_url)
    code = asyncio.run(_handshake_close_code(f"{live_server.ws_url}?room_id={room_id}"))
    assert code == 4401


def test_forbidden_room_closes_4403_on_a_real_client(live_server: _LiveServer) -> None:
    room_id = _bootstrap_room(live_server.base_url)
    code = asyncio.run(
        _handshake_close_code(
            f"{live_server.ws_url}?room_id={room_id}",
            headers={"Authorization": "Bearer alex-token"},
        )
    )
    assert code == 4403


def test_valid_credential_still_connects_on_a_real_client(live_server: _LiveServer) -> None:
    room_id = _bootstrap_room(live_server.base_url)

    async def _connect() -> dict[str, object]:
        async with websockets.connect(
            f"{live_server.ws_url}?room_id={room_id}",
            additional_headers=dict(OWNER),
            open_timeout=5,
        ) as ws:
            import json

            return dict(json.loads(await asyncio.wait_for(ws.recv(), timeout=5)))

    frame = asyncio.run(_connect())
    assert frame["type"] == "connected"
