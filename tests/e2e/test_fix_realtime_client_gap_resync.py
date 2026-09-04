"""Finding 26 (medium), client half: fanout.py's docstring used to claim
"the client already reconciles gaps against the room event log" — false.
web/js/socket.js's `handleRealtimeEvent` only ever advanced `state.lastSequence`
forward (`Math.max`); it never compared a delivered sequence against what it
expected next, so a live socket that missed an event (a lossy cross-process
publish, see realtime/fanout.py) kept silently accepting whatever arrived
next, with a permanent hole in between that nothing healed until an
unrelated reconnect.

`handleRealtimeEvent` now detects a gap (a delivered sequence that is not
`lastSequence + 1`, for the room this socket is actually subscribed to) and
resolves it once: a `resync_request` sent to the server (see
websocket.py's handling and RealtimeHub.record_sequence_gap) and a
`loadState()` reload from the room event log, the single source of truth.

Runs a real Chromium against the real app (same pattern as
test_web_client.py) and calls `handleRealtimeEvent` directly with a
synthetic gapped event, since manufacturing an actual dropped Redis publish
is out of reach for a single-process test — the function under test is the
one this fix changed, and its exported shape makes that a faithful,
minimal-fixture way to drive it. `state` itself is read via a fresh dynamic
import of state.js in every `page.evaluate`/`wait_for_function` call rather
than through a `window.state` global — app.js's own debug bridge (a
different track's file) exposes only `roomId`/`lastSequence`/etc., not the
whole state object, and this fix does not touch app.js to add one.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest

pytest.importorskip("playwright.sync_api")

import uvicorn
from playwright.sync_api import sync_playwright

from multiplayer.server import create_app

from .test_web_client import _enter_demo_workspace, _require_chromium  # noqa: F401


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _LiveServer:
    def __init__(self, db_path: str) -> None:
        self.port = _free_port()
        app = create_app(db_path, demo=True)
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

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
    server = _LiveServer(str(tmp_path / "fix-realtime-gap-resync.db"))
    server.start()
    try:
        yield server
    finally:
        server.stop()


def test_a_sequence_gap_triggers_exactly_one_resync(live_server: _LiveServer) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)

        # Count fresh /state fetches from here on, so the gap-triggered
        # reload is unambiguous rather than inferred from a race with the
        # one loadState() _enter_demo_workspace already caused.
        page.evaluate(
            "() => { window.__stateFetches = 0; "
            "const orig = window.fetch.bind(window); "
            "window.fetch = (...args) => { "
            "  if (String(args[0]).includes('/state?')) window.__stateFetches++; "
            "  return orig(...args); "
            "}; }"
        )

        result = page.evaluate(
            """
            async () => {
              const socketModule = await import('/static/js/socket.js');
              const { state } = await import('/static/js/state.js');
              const roomId = state.roomId;
              const before = state.lastSequence;
              // A gap of +5 past the last sequence this tab has actually
              // seen: exactly what a dropped cross-process publish looks
              // like from this socket's point of view.
              socketModule.handleRealtimeEvent({
                type: 'room_event', event_type: 'message.created', room_id: roomId,
                sequence: before + 5, actor_id: 'owner', actor_type: 'user',
                timestamp: new Date().toISOString(),
                payload: {message_id: 'm1', role: 'human', sender_id: 'owner', content: 'hi'},
              });
              return { before, resyncRequestedRightAfter: state.resyncRequested };
            }
            """
        )
        assert result["resyncRequestedRightAfter"] is True

        page.wait_for_function("() => window.__stateFetches >= 1", timeout=5000)
        page.wait_for_function(
            """
            async () => {
              const { state } = await import('/static/js/state.js');
              return state.resyncRequested === false;
            }
            """,
            timeout=5000,
        )

        fetch_count_after_one_gap = page.evaluate("() => window.__stateFetches")

        # A second, in-order event right after must not fire a second resync.
        page.evaluate(
            """
            async () => {
              const socketModule = await import('/static/js/socket.js');
              const { state } = await import('/static/js/state.js');
              socketModule.handleRealtimeEvent({
                type: 'room_event', event_type: 'message.created', room_id: state.roomId,
                sequence: state.lastSequence + 1, actor_id: 'owner', actor_type: 'user',
                timestamp: new Date().toISOString(),
                payload: {message_id: 'm2', role: 'human', sender_id: 'owner', content: 'hi2'},
              });
            }
            """
        )
        page.wait_for_timeout(200)
        assert page.evaluate("() => window.__stateFetches") == fetch_count_after_one_gap

        browser.close()
