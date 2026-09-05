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

import asyncio
import socket
import threading
import time
from collections.abc import Iterator

import pytest

pytest.importorskip("playwright.sync_api")

import uvicorn
from playwright.sync_api import sync_playwright

from multiplayer.server import create_app

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
    server = _LiveServer(str(tmp_path / "fix-realtime-gap-resync.db"))
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _seed_messages_bypassing_the_rate_limit(
    db_path: str, room_id: str, user_id: str, count: int
) -> None:
    """`POST /messages` is rate limited well below a few hundred calls, so
    seeding a room this size has to go around the HTTP route entirely. A
    second connection to the same sqlite file, on its own event loop in its
    own thread (the live server's own connection belongs to its own loop,
    and sync_playwright owns this thread's), does the writes directly
    through the service layer, the same write path the route itself uses.
    """
    from multiplayer.domain.models import MessageRole
    from multiplayer.manage import open_database
    from multiplayer.realtime.hub import RealtimeHub
    from multiplayer.services.service import MultiplayerService

    async def _seed() -> None:
        db = await open_database(db_path)
        try:
            seeder = MultiplayerService(db, RealtimeHub(), known_users=frozenset({user_id}))
            for i in range(count):
                await seeder.send_message(room_id, MessageRole.HUMAN, user_id, f"seed {i}")
        finally:
            await db.close()

    thread = threading.Thread(target=lambda: asyncio.run(_seed()))
    thread.start()
    thread.join()


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


def test_a_stale_handshake_cursor_resets_before_reconnecting_and_recovers(
    live_server: _LiveServer,
) -> None:
    """Round 2, finding 1 (high), client half of the stale-cursor fix
    (websocket.py's `GET /ws?last_sequence=N` beyond the room's head, now
    closed 4408): that server change's own comment claimed the client's
    existing gap-resync handler would "reload the snapshot, reconnect on a
    fresh cursor". It did not. `state.lastSequence` only ever rises
    (`Math.max` in loadStateImpl and handleRealtimeEvent), and `onclose`
    special-cased only 4403 and 4401, so a 4408 fell to the default branch:
    reconnect with the same poisoned cursor, get the same close, once a
    second, forever, `presence.user_joined`/`user_left` flapping every
    cycle. Confirmed with a real uvicorn server and a real Chromium
    (`browser_probe.py`, round 2 critic): 19 sockets and 18 full snapshot
    fetches in 20 seconds, `#ws-status` never reaching connected.

    `onclose` now resets `state.lastSequence` to 0 on a 4408 before
    scheduling the reconnect: 0 is always a valid cursor (never beyond any
    room's head), so the next handshake succeeds, and the reconnect's own
    `onopen` reload raises the cursor back up from there.

    Forces the scenario directly rather than waiting for a real cross-
    process gap: poisons `state.lastSequence` past the room's actual head,
    then closes the live socket and reconnects, so the very next handshake
    is the stale one the server rejects.
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        room_id = page.evaluate("roomId")

        # Every WebSocket this page opens from here on is counted, and every
        # close code any of them receives is recorded, without relying on
        # the app's own onclose (the very thing under test) to report it.
        page.evaluate(
            """
            () => {
              window.__wsOpens = 0;
              window.__closeCodes = [];
              const NativeWS = window.WebSocket;
              function SpyWS(url, protocols) {
                window.__wsOpens++;
                const sock = protocols === undefined
                  ? new NativeWS(url) : new NativeWS(url, protocols);
                sock.addEventListener('close', (e) => window.__closeCodes.push(e.code));
                return sock;
              }
              SpyWS.prototype = NativeWS.prototype;
              window.WebSocket = SpyWS;
            }
            """
        )

        # Poison the stored cursor past the room's head, then force a fresh
        # handshake with it: closeSocket() first, the same call switchRoom
        # and a channel switch already use to retire a socket without
        # onclose treating it as an unexpected drop.
        page.evaluate(
            """
            async () => {
              const { state } = await import('/static/js/state.js');
              const socketModule = await import('/static/js/socket.js');
              state.lastSequence = state.lastSequence + 1000000;
              socketModule.closeSocket(state.ws);
              socketModule.connectWS();
            }
            """
        )

        page.wait_for_function("() => window.__closeCodes.includes(4408)", timeout=5000)
        # The reconnect this triggers is on a 1 second base backoff (see
        # WS_MAX_DELAY / wsReconnectDelay in socket.js); give it real room
        # rather than a tight timeout that would flag a slow-but-honest
        # single retry as a failure.
        page.wait_for_function(
            """
            async () => {
              const { state } = await import('/static/js/state.js');
              return window.__wsOpens >= 2 && state.ws && state.ws.readyState === 1;
            }
            """,
            timeout=10000,
        )
        _wait_for_socket_connected(page)

        # A unique marker searched for in the page's own text, not a count of
        # `.msg` elements: the demo seed data includes agents that post their
        # own follow-up messages on their own schedule, so a plain "more `.msg`
        # nodes than before" check is racing an unrelated source of churn and
        # was observed to flake on exactly that (a background post landing,
        # then a reconcile removing a now-superseded one, netting no change or
        # even a decrease). Content is unambiguous regardless of what else the
        # room does around it.
        marker = "after the stale-cursor reset"
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
        # A generous budget: the reconnect's own onopen just kicked off a
        # loadState() reload (see loadStateOrShowReconnecting in socket.js),
        # and a message posted while that fetch is still in flight is only
        # guaranteed to land once it settles and the buffered event replays,
        # not necessarily inside the same tick as the POST resolving.
        page.wait_for_function(
            "(m) => document.body.textContent.includes(m)",
            arg=marker,
            timeout=10000,
        )

        # Let the reconnect settle for a few more seconds: a client still
        # looping would have opened several more sockets by now (the
        # pre-fix probe opened 19 in 20 seconds), a recovered one opens
        # none.
        page.wait_for_timeout(3000)
        assert page.evaluate("() => window.__wsOpens") < 3

        browser.close()


def test_a_stale_handshake_cursor_on_a_big_room_reconnects_on_the_head_not_zero(
    live_server: _LiveServer,
) -> None:
    """Round 3, finding 3 (medium): resetting the stored cursor to 0 on a
    4408 (the round 2 fix) is honest but expensive on a room with real
    history: 0 replays every event in the room from scratch. The round 2
    critic's `browser3000_probe.py` measured this on a 3034 event room: a
    217 second replay of 2523 events when the doomed socket's own snapshot
    fetch won the race, and a renderer crash still 1049 events short of the
    head when a slow `/state` lost that race and the reconnect carried 0.

    The fix: the room state route now returns `latest_sequence`, the room's
    real head (see `MultiplayerService.get_room_state`), not the capped
    `events_since` watermark the client already used for
    `state.lastSequence`. `onclose` on a 4408 now waits for a fresh snapshot
    (`loadStateOrShowReconnecting()`) before reconnecting at all, and takes
    its cursor from that snapshot's `latest_sequence` rather than racing
    whichever fetch happens to land first.

    Seeds 600 events (through the service directly; `POST /messages` is
    rate limited far below that) and asserts the reconnect replays well
    under that many events and closes exactly once with 4408, rather than
    reopening on 0 and replaying the room from scratch.
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

        # Every WebSocket construction, every close code, and every
        # room_event frame delivered from here on is counted, so "how much
        # got replayed" is measured directly rather than inferred from
        # timing.
        page.evaluate(
            """
            () => {
              window.__wsOpens = 0;
              window.__closeCodes = [];
              window.__roomEventFrames = 0;
              const NativeWS = window.WebSocket;
              function SpyWS(url, protocols) {
                window.__wsOpens++;
                const sock = protocols === undefined
                  ? new NativeWS(url) : new NativeWS(url, protocols);
                sock.addEventListener('close', (e) => window.__closeCodes.push(e.code));
                sock.addEventListener('message', (e) => {
                  try {
                    if (JSON.parse(e.data).type === 'room_event') window.__roomEventFrames++;
                  } catch (_err) { /* not JSON, not a room_event either */ }
                });
                return sock;
              }
              SpyWS.prototype = NativeWS.prototype;
              window.WebSocket = SpyWS;
            }
            """
        )

        # Poison the stored cursor past the room's now-600-events head, then
        # force a fresh handshake with it, same as the smaller-room test above.
        page.evaluate(
            """
            async () => {
              const { state } = await import('/static/js/state.js');
              const socketModule = await import('/static/js/socket.js');
              state.lastSequence = state.lastSequence + 1000000;
              socketModule.closeSocket(state.ws);
              socketModule.connectWS();
            }
            """
        )

        page.wait_for_function("() => window.__closeCodes.includes(4408)", timeout=5000)
        page.wait_for_function(
            """
            async () => {
              const { state } = await import('/static/js/state.js');
              return window.__wsOpens >= 2 && state.ws && state.ws.readyState === 1;
            }
            """,
            timeout=15000,
        )
        _wait_for_socket_connected(page)
        # Let any in-flight backfill/replay finish landing before counting.
        page.wait_for_timeout(2000)

        close_codes = page.evaluate("() => window.__closeCodes")
        assert close_codes.count(4408) == 1, close_codes
        replayed = page.evaluate("() => window.__roomEventFrames")
        assert replayed < 100, (
            f"replayed {replayed} room_event frames recovering from a stale cursor "
            f"on a {seed_count}-event room; a fix that reconnects on the head should "
            "replay nothing, a regression back to reconnecting on 0 replays the room "
            "seeded 600, not the tens actually posted before/around the poison"
        )

        browser.close()


def test_a_failed_snapshot_after_4408_retries_with_backoff_not_a_bare_reconnect(
    live_server: _LiveServer,
) -> None:
    """Round 4, finding 1 (medium): the round 3 fix waits for a fresh
    snapshot before reconnecting on a 4408, but the branch that runs when
    that snapshot fetch itself fails (a 500, a 429 from the rate limiter, a
    network blip) reconnected anyway, on the 0 it had just written, with no
    backoff: exactly the full-replay trap the branch exists to avoid. The
    round 3 critic's browser600_probe.py failall measured this on a
    634 event room: 634 room_event frames replayed in 11 seconds, then a
    429 storm from the per-message read-cursor PUTs the replay itself
    triggered.

    The fix never reconnects until loadState() actually resolves: a
    rejected fetch schedules a retry through the same backoff a plain
    reconnect uses (see wsReconnectAttempts/wsReconnectDelay in socket.js)
    rather than opening a new socket immediately on a stale 0.

    Fails the first two `/state` fetches after the poison, then lets every
    later one through. Two, not one: the first fetch is the doomed socket's
    own onopen call, and the 4408 handler's own loadState() call dedupes
    onto that same in-flight promise (round 5, finding 3, fixed the case
    where only that first fetch fails and the deduped follow-up succeeds,
    which now resolves immediately with no backoff at all, correctly, so a
    test that only failed the first fetch would no longer exercise a
    backoff wait at all). Failing the follow-up too forces a genuine
    rejection, so this still exercises the backoff this test is named for:
    no second WebSocket is constructed while both are failing, only once a
    later retry's snapshot actually lands.
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

        page.evaluate(
            """
            () => {
              window.__wsOpens = 0;
              window.__closeCodes = [];
              window.__roomEventFrames = 0;
              const NativeWS = window.WebSocket;
              function SpyWS(url, protocols) {
                window.__wsOpens++;
                const sock = protocols === undefined
                  ? new NativeWS(url) : new NativeWS(url, protocols);
                sock.addEventListener('close', (e) => window.__closeCodes.push(e.code));
                sock.addEventListener('message', (e) => {
                  try {
                    if (JSON.parse(e.data).type === 'room_event') window.__roomEventFrames++;
                  } catch (_err) { /* not JSON, not a room_event either */ }
                });
                return sock;
              }
              SpyWS.prototype = NativeWS.prototype;
              window.WebSocket = SpyWS;
            }
            """
        )

        # Fail the first two /state fetches issued from here on (the doomed
        # socket's own onopen call and the 4408 handler's deduped follow-up,
        # see the docstring above), then let every later fetch through,
        # including the retry this fix adds.
        failed_count = {"n": 0}

        def fail_first_two_state_fetches(route: object) -> None:
            if failed_count["n"] < 2:
                failed_count["n"] += 1
                route.fulfill(status=500, content_type="application/json", body="{}")  # type: ignore[attr-defined]
            else:
                route.continue_()  # type: ignore[attr-defined]

        page.route(f"**/api/v1/rooms/{room_id}/state*", fail_first_two_state_fetches)

        page.evaluate(
            """
            async () => {
              const { state } = await import('/static/js/state.js');
              const socketModule = await import('/static/js/socket.js');
              state.lastSequence = state.lastSequence + 1000000;
              socketModule.closeSocket(state.ws);
              socketModule.connectWS();
            }
            """
        )

        page.wait_for_function("() => window.__closeCodes.includes(4408)", timeout=5000)
        # Both failing fetches resolve fast (local 500s, no artificial
        # delay); what has to hold for a while is the backoff before the
        # next retry, not the two failures themselves. Give both their own
        # round trip, then check no second socket exists while the backoff
        # is still pending.
        page.wait_for_timeout(400)
        assert failed_count["n"] == 2, (
            f"only {failed_count['n']} of 2 expected /state fetches failed before "
            "the 400ms check ran; the timing assumption behind this test's "
            "no-premature-socket assertion below no longer holds"
        )
        assert page.evaluate("() => window.__wsOpens") == 1, (
            "a second WebSocket was constructed before any snapshot fetch "
            "succeeded: the failure branch reconnected on 0 instead of "
            "waiting out the same backoff a plain reconnect uses"
        )

        page.wait_for_function(
            """
            async () => {
              const { state } = await import('/static/js/state.js');
              return window.__wsOpens >= 2 && state.ws && state.ws.readyState === 1;
            }
            """,
            timeout=15000,
        )
        _wait_for_socket_connected(page)
        page.wait_for_timeout(2000)

        close_codes = page.evaluate("() => window.__closeCodes")
        assert close_codes.count(4408) == 1, close_codes
        replayed = page.evaluate("() => window.__roomEventFrames")
        assert replayed < 100, (
            f"replayed {replayed} room_event frames recovering from a stale cursor "
            f"whose first two snapshot fetches failed on a {seed_count}-event room; "
            "a fix that retries with backoff and reconnects on the head should "
            "still replay nothing once it does land"
        )

        browser.close()
