"""Final critic, seam 3 (medium; probe scratchpad/critic-tmp/final3/eventslog2_probe.py):
a demo room seeded with 600 messages, then a page reload. The client used to
open its socket before any snapshot existed, so it subscribed with
`last_sequence=0` and the server replayed the whole log (634 frames on a room
this size, 100,000 on a larger one). Each replayed `message.created` ran
socket.js's `refreshUnread()`, one `GET /rooms/{id}/read-cursor` per message:
610 requests, 514 answered 429, the `/state` fetch among them failed, the
token was rate limited, and a live message never rendered.

The fix has two parts, both in the client. auth.js's `showWorkspace()` now
awaits `loadState()` before calling `connectWS()`, the same order
`switchRoom` (rooms.js) already used, so the socket's own subscribe cursor
is the snapshot's real head, never 0 on a room that has one. messages.js's
`refreshUnread()` is now scheduled through a debounced `scheduleRefreshUnread()`
that coalesces a burst of events into one request after the burst settles,
and defers instead of firing while a backfill (`state.loadStatePromise`) is
still in flight.

This drives a real Chromium against the real app (same pattern as
test_web_client.py and test_fix_realtime_client_gap_resync.py): seeds 600
messages through the service layer (bypassing the message-post rate limit),
reloads the page, and asserts on the reload's own network traffic: no
socket opened with `last_sequence=0`, fewer than 10 read-cursor requests,
zero 429 responses, plus that a message posted after the reload still
renders live within five seconds.
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

from .test_fix_realtime_client_gap_resync import _seed_messages_bypassing_the_rate_limit
from .test_web_client import (  # noqa: F401
    _enter_demo_workspace,
    _require_chromium,
    _wait_for_socket_connected,
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _LiveServer:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
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
    server = _LiveServer(str(tmp_path / "f4-pageload.db"))
    server.start()
    try:
        yield server
    finally:
        server.stop()


def test_page_reload_does_not_replay_and_storm_read_cursor(live_server: _LiveServer) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        room_id, user_id = page.evaluate(
            """
            async () => {
              const { state } = await import('/static/js/state.js');
              return [state.roomId, state.userId];
            }
            """
        )

        seed_count = 600
        _seed_messages_bypassing_the_rate_limit(live_server.db_path, room_id, user_id, seed_count)

        # Recorded only from here on, so a legitimate last_sequence=0 socket
        # opened earlier (the room genuinely had no messages yet) is not
        # confused with the reload under test.
        ws_urls: list[str] = []
        page.on("websocket", lambda ws: ws_urls.append(ws.url))
        statuses_429: list[str] = []
        read_cursor_gets: list[int] = []

        def record_response(response: object) -> None:
            url = response.url  # type: ignore[attr-defined]
            if response.status == 429:  # type: ignore[attr-defined]
                statuses_429.append(url)
            if "/read-cursor" in url and response.request.method == "GET":  # type: ignore[attr-defined]
                read_cursor_gets.append(response.status)  # type: ignore[attr-defined]

        page.on("response", record_response)

        page.reload()
        page.wait_for_selector("#app-main", state="visible", timeout=10000)
        page.wait_for_function(
            "() => document.getElementById('agents-panel').innerHTML.trim().length > 0",
            timeout=10000,
        )
        _wait_for_socket_connected(page)
        # Let any backfill/replay burst finish landing before counting.
        page.wait_for_timeout(3000)

        marker = "after the page reload"
        post_status, _posted = page.evaluate(
            """
            async (args) => {
                const [roomId, content] = args;
                const r = await fetch(`/api/v1/rooms/${roomId}/messages`, {
                    method: 'POST',
                    headers: {
                        Authorization: 'Bearer demo',
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({content})
                });
                return [r.status, await r.json()];
            }
            """,
            [room_id, marker],
        )
        assert post_status == 200
        page.wait_for_function(
            "(m) => document.body.textContent.includes(m)",
            arg=marker,
            timeout=5000,
        )

        browser.close()

    zero_cursor_sockets = [url for url in ws_urls if "last_sequence=0" in url]
    assert not zero_cursor_sockets, (
        f"a reload socket opened with last_sequence=0 on a {seed_count}-message "
        f"room that already has a snapshot: {zero_cursor_sockets}"
    )
    assert len(read_cursor_gets) < 10, (
        f"{len(read_cursor_gets)} read-cursor requests after a page reload on a "
        f"{seed_count}-message room; a burst of replayed events fetching the "
        "cursor once each storms the endpoint instead of coalescing"
    )
    assert not statuses_429, statuses_429


def test_live_posture_change_survives_a_reload_as_one_line(live_server: _LiveServer) -> None:
    """A live membership or posture event used to render twice: once as the
    keyed synthetic line a reload's own reconcile now builds from
    events_since, once more as a second, keyless copy the live handler
    appended after that same reload settled. The duplicate persisted until
    an unrelated later reconcile happened to sweep the keyless half away.

    Changes the room's posture live, waits for the line to land, reloads,
    and asserts exactly one line survives both.
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        room_id = page.evaluate("roomId")

        patch_status, _body = page.evaluate(
            """
            async (roomId) => {
                const r = await fetch(`/api/v1/rooms/${roomId}/posture`, {
                    method: 'PATCH',
                    headers: {
                        Authorization: 'Bearer demo',
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({posture: 'STRICT'})
                });
                return [r.status, await r.json()];
            }
            """,
            room_id,
        )
        assert patch_status == 200

        posture_line = "Channel posture is now strict"
        page.wait_for_function(
            "(m) => document.body.textContent.includes(m)",
            arg=posture_line,
            timeout=5000,
        )
        # Lets the live event's own reload (loadStateOrShowReconnecting)
        # fully settle before this counts, so the count reflects the steady
        # state rather than a moment mid-reconcile.
        page.wait_for_timeout(500)

        def count_posture_lines() -> int:
            return page.evaluate(
                "(m) => Array.from(document.querySelectorAll('#messages .msg.system'))"
                ".filter(el => el.textContent.includes(m)).length",
                posture_line,
            )

        count_before_reload = count_posture_lines()

        page.reload()
        page.wait_for_selector("#app-main", state="visible", timeout=10000)
        page.wait_for_function(
            "() => document.getElementById('agents-panel').innerHTML.trim().length > 0",
            timeout=10000,
        )
        _wait_for_socket_connected(page)
        page.wait_for_function(
            "(m) => document.body.textContent.includes(m)",
            arg=posture_line,
            timeout=5000,
        )
        count_after_reload = count_posture_lines()

        browser.close()

    assert count_before_reload == 1, (
        f"expected exactly one line for the live posture change before any "
        f"reload, found {count_before_reload}"
    )
    assert count_after_reload == 1, (
        f"expected exactly one line for the posture change to survive a "
        f"reload, found {count_after_reload}"
    )


def test_reload_on_a_room_past_the_event_cap_shows_the_recent_line_not_the_oldest(
    live_server: _LiveServer,
) -> None:
    """A room past the event cap used to page events_since from sequence 1
    forward, so a reload always showed the room's own earliest system lines
    (the demo seed's own membership invites) and never a change that had
    just happened past the cap. get_room_state now windows a fresh connect
    (last_sequence 0) around the recent room instead.
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        room_id, user_id = page.evaluate(
            """
            async () => {
              const { state } = await import('/static/js/state.js');
              return [state.roomId, state.userId];
            }
            """
        )

        seed_count = 600
        _seed_messages_bypassing_the_rate_limit(live_server.db_path, room_id, user_id, seed_count)

        patch_status, _body = page.evaluate(
            """
            async (roomId) => {
                const r = await fetch(`/api/v1/rooms/${roomId}/posture`, {
                    method: 'PATCH',
                    headers: {
                        Authorization: 'Bearer demo',
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({posture: 'STRICT'})
                });
                return [r.status, await r.json()];
            }
            """,
            room_id,
        )
        assert patch_status == 200

        page.reload()
        page.wait_for_selector("#app-main", state="visible", timeout=10000)
        page.wait_for_function(
            "() => document.getElementById('agents-panel').innerHTML.trim().length > 0",
            timeout=10000,
        )
        _wait_for_socket_connected(page)

        posture_line = "Channel posture is now strict"
        page.wait_for_function(
            "(m) => document.body.textContent.includes(m)",
            arg=posture_line,
            timeout=5000,
        )
        page_text = page.evaluate("() => document.body.textContent")

        browser.close()

    assert posture_line in page_text
    # The demo seed's own membership invites happened well before the 600
    # seeded messages; a window this far past the cap that still started
    # from sequence 1 would show them here instead of the posture change
    # that just happened at the head.
    assert "was invited as" not in page_text
