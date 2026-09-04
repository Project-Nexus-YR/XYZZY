"""Executing browser coverage for web/index.html.

Every other e2e file reads this client as text and asserts a substring is
present, so a JavaScript syntax error, a dead handler, or a defeated escape
all ship with a green suite because nothing ever parses or runs the file.
This module drives a real Chromium against the real app instead: it starts
the app on a free port with the sign-in-free demo path enabled, loads the
served page, and asserts on what actually renders and runs.

Skips cleanly (no browsers installed, no Playwright) so a CI leg without
browsers stays green; the browser install step is a different track's job.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("playwright.sync_api")

import uvicorn
from playwright.sync_api import Dialog, Page, sync_playwright

from multiplayer.server import create_app

_MALICIOUS_AGENT_NAME = "',alert(document.domain),'"


@pytest.fixture(scope="module", autouse=True)
def _require_chromium() -> Iterator[None]:
    """Skip this whole module cleanly when no Chromium build is installed.

    pytest.importorskip above only covers the playwright package itself
    being importable; a machine with the package installed but no browser
    downloaded (`playwright install chromium` never run) would otherwise
    fail every single test in this module with a launch ERROR instead of
    one clean module-level skip.
    """
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:
            pytest.skip(f"Chromium is not installed for Playwright: {exc}")
        else:
            browser.close()
    yield


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _LiveServer:
    """Runs the real ASGI app with uvicorn on a background thread.

    The demo workspace is seeded once at startup by the app itself (see
    MultiplayerService.seed_demo_workspace), so this needs no fixture data
    of its own: one decision brief, its ontology, and its agents are already
    there once the health check answers.
    """

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
    server = _LiveServer(str(tmp_path / "web-client-e2e.db"))
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _enter_demo_workspace(page: Page, base_url: str) -> None:
    page.goto(base_url)
    page.click("#setup-demo-button", timeout=10000)
    page.wait_for_selector("#app-main", state="visible", timeout=10000)
    # loadState() populates #agents-panel as part of entering the workspace;
    # wait for it rather than for a fixed delay.
    page.wait_for_function(
        "document.getElementById('agents-panel').innerHTML.trim().length > 0",
        timeout=10000,
    )


def test_page_loads_with_zero_console_errors(live_server: _LiveServer) -> None:
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.on(
            "console",
            lambda msg: errors.append(msg.text) if msg.type == "error" else None,
        )
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        _enter_demo_workspace(page, live_server.base_url)
        browser.close()
    assert errors == []


def test_ontology_panel_renders_seeded_nodes(live_server: _LiveServer) -> None:
    """The demo workspace publishes a Decision Brief on startup, which
    materializes the ontology the seed relies on downstream. renderOntology
    should turn that into at least one .ontology-node, not the empty state."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        page.click("[aria-label='Workspace details']")
        page.wait_for_function(
            "document.getElementById('ontology-tree').children.length > 0",
            timeout=10000,
        )
        node_count = page.eval_on_selector_all(".ontology-node", "els => els.length")
        empty_state = page.query_selector("#ontology-tree .ontology-empty")
        browser.close()
    assert node_count > 0
    assert empty_state is None


def test_malicious_agent_name_does_not_execute_when_agents_panel_renders(
    live_server: _LiveServer,
) -> None:
    """Finding 9: an editor-chosen agent name used to break out of the Remove
    button's onclick JS string once the browser HTML-decoded escHtml's output.
    A dialog firing here means the injected call ran with the viewer's origin;
    with the fix (data-agent-name plus this.dataset), the name is read back as
    a decoded string only, never re-parsed as code, and no dialog fires.
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)

        # Plant the payload the way an editor would, through the real API.
        templates = page.evaluate(
            """
                async () => {
                    const r = await fetch('/api/v1/agent-templates', {
                        headers: {Authorization: 'Bearer demo'}
                    });
                    return r.json();
                }
                """
        )
        template_id = templates[0]["template_id"]
        # roomId is a top-level `let`, so it lives in the page's global lexical
        # scope, not as a `window.roomId` property; page.evaluate runs in that
        # same scope and can read it as a bare identifier.
        room_id = page.evaluate("roomId")
        spawn_status = page.evaluate(
            """
            async ([roomId, templateId, name]) => {
                const r = await fetch(`/api/v1/rooms/${roomId}/agents`, {
                    method: 'POST',
                    headers: {
                        Authorization: 'Bearer demo',
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({template_id: templateId, name})
                });
                return r.status;
            }
            """,
            [room_id, template_id, _MALICIOUS_AGENT_NAME],
        )
        assert spawn_status == 200, f"planting the agent failed with status {spawn_status}"
        page.evaluate("loadState()")
        page.wait_for_function(
            "document.querySelectorAll('#agents-panel .card').length > 0",
            timeout=10000,
        )

        dialogs: list[Dialog] = []
        page.on("dialog", lambda dialog: (dialogs.append(dialog), dialog.dismiss()))

        # Open the Agents panel (People and permissions -> Workspace records)
        # the way an admin browsing the room would, then click Remove on the
        # planted agent specifically: two other, harmless seeded agents also
        # render a Remove button, and the escaped payload text still shows up
        # in the card's title either way, fixed or not.
        page.click("[aria-label='Workspace details']")
        page.click(".records summary")
        planted_card = page.locator("#agents-panel .card", has_text="alert(document.domain)")
        remove_button = planted_card.locator("button:has-text('Remove')")
        remove_button.wait_for(state="visible", timeout=10000)
        remove_button.click()
        page.wait_for_timeout(500)

        browser.close()
    assert dialogs == []


def test_task_created_during_in_flight_snapshot_fetch_is_not_lost(
    live_server: _LiveServer,
) -> None:
    """Finding 38: a room_event that lands while a snapshot fetch is already
    in flight used to vanish. Round one fixed the case where the event's own
    handler applies directly (a message) or where the racing loadState() call
    is the only one in flight; a second loadState() call made during replay
    (or made directly while the original fetch is still pending) still just
    deduped onto that same stale promise and never re-fetched, so a task
    created mid-fetch rendered nothing until some unrelated later refresh.

    This reproduces the exact seam: the real /state response is fetched but
    held back (not fulfilled) until a task has been created and the resulting
    room_event has been buffered, so the snapshot handed to the page is
    provably the one that predates the task.
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        room_id = page.evaluate("roomId")

        state_route_seen = {"done": False}
        spawn_status_holder: dict[str, int] = {}

        def hold_state_response(route: object) -> None:
            request_url = route.request.url  # type: ignore[attr-defined]
            if "/state" not in request_url or state_route_seen["done"]:
                route.continue_()  # type: ignore[attr-defined]
                return
            state_route_seen["done"] = True
            # A regression here must fail the test, not wedge Playwright's
            # dispatch loop waiting on a request nobody ever answers: whatever
            # happens below, this route gets a response (the held one once
            # fetched, or otherwise let through) before this handler returns.
            response = None
            try:
                # Let the real request run now, while the task does not exist
                # yet, so the response this holds is provably the pre-task
                # snapshot.
                response = route.fetch()  # type: ignore[attr-defined]
                spawn_status_holder["status"] = page.evaluate(
                    """
                    async (roomId) => {
                        const r = await fetch(`/api/v1/rooms/${roomId}/tasks`, {
                            method: 'POST',
                            headers: {
                                Authorization: 'Bearer demo',
                                'Content-Type': 'application/json'
                            },
                            body: JSON.stringify({title: 'Round 2 task'})
                        });
                        return r.status;
                    }
                    """,
                    room_id,
                )
                # Confirm the room_event actually landed and was buffered
                # while this fetch was still in flight, the precondition the
                # finding names, before releasing the stale response. Bounded
                # well under the suite's own budget: this either lands in
                # under a second or the fix is absent and it never will.
                page.wait_for_function(
                    "typeof pendingEventsDuringLoad !== 'undefined' "
                    "&& pendingEventsDuringLoad.some(e => e.event_type === 'task.created')",
                    timeout=3000,
                )
            finally:
                if response is not None:
                    route.fulfill(response=response)  # type: ignore[attr-defined]
                else:
                    route.continue_()  # type: ignore[attr-defined]

        page.route("**/api/v1/rooms/*/state*", hold_state_response)
        sequence_before = page.evaluate("lastSequence")
        page.evaluate("loadState()")
        page.wait_for_function(
            "document.querySelectorAll('#tasks-panel .card[data-task-id]').length > 0",
            timeout=10000,
        )
        task_titles = page.eval_on_selector_all(
            "#tasks-panel .card[data-task-id] .title", "els => els.map(e => e.textContent)"
        )
        sequence_after = page.evaluate("lastSequence")
        browser.close()

    assert spawn_status_holder.get("status") == 200
    assert any("Round 2 task" in title for title in task_titles), task_titles
    assert sequence_after >= sequence_before


def test_switch_room_delivers_a_message_posted_during_the_snapshot_fetch(
    live_server: _LiveServer,
) -> None:
    """Finding 39 (borrowed from realtime): switchRoom used to fire
    `connectWS(); await loadState();` with no ordering between them. Two
    things follow from that, and this test checks both:

    1. connectWS reads the global `lastSequence` to build the socket's
       `last_sequence` query param (the same field name the /state fetch
       already sends). Firing connectWS before the snapshot resolves means
       that cursor is still whatever it was reset to at the top of
       switchRoom (0), not where the snapshot actually left off, regardless
       of anything that self-heals via the socket's own onopen refresh. This
       is the part that actually distinguishes the old ordering from the
       fixed one: seed the target room with a message before ever switching
       to it, then assert the subscribe URL's cursor is at least that
       message's sequence, not 0.
    2. The literal race the finding names: hold the target room's real
       /state response back with page.route, post a second message while it
       is held, release it, and check that message still renders and the
       cursor does not regress. In this codebase that half turns out to
       self-heal even on the old ordering (the socket's own onopen already
       triggers a fresh loadState(), and round 2's dirty-flag fix means that
       refresh always actually runs) — kept here as a regression net, not as
       the discriminating half.
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        workspace_id = page.evaluate("workspaceId")

        create_status, new_room = page.evaluate(
            """
            async (workspaceId) => {
                const r = await fetch(`/api/v1/workspaces/${workspaceId}/rooms`, {
                    method: 'POST',
                    headers: {
                        Authorization: 'Bearer demo',
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({name: 'round-2-room', description: ''})
                });
                return [r.status, await r.json()];
            }
            """,
            workspace_id,
        )
        assert create_status == 200, new_room
        new_room_id = new_room["room_id"]

        # Seed one message before ever switching to this room, so its
        # snapshot's final sequence is provably not 0 by the time switchRoom
        # runs: a subscribe cursor of 0 can only mean it was read before the
        # snapshot settled, not that the room happened to be empty.
        seed_status, seeded_message = page.evaluate(
            """
            async (roomId) => {
                const r = await fetch(`/api/v1/rooms/${roomId}/messages`, {
                    method: 'POST',
                    headers: {
                        Authorization: 'Bearer demo',
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({content: 'Seed message'})
                });
                return [r.status, await r.json()];
            }
            """,
            new_room_id,
        )
        assert seed_status == 200, seeded_message
        seeded_sequence = seeded_message["sequence"]
        page.evaluate("refreshRooms()")

        ws_urls: list[str] = []
        page.on("websocket", lambda ws: ws_urls.append(ws.url))

        held = {"seen": False}
        post_result_holder: dict[str, int] = {}

        def hold_new_room_state(route: object) -> None:
            request_url = route.request.url  # type: ignore[attr-defined]
            if f"/rooms/{new_room_id}/state" not in request_url or held["seen"]:
                route.continue_()  # type: ignore[attr-defined]
                return
            held["seen"] = True
            # Same discipline as hold_state_response above: this route always
            # gets answered, so a failure here fails the test instead of
            # wedging the browser on a request nobody ever responds to.
            response = None
            try:
                # The real request runs now, before the second message
                # exists, so the response this holds back is provably the
                # earlier snapshot (seed message only).
                response = route.fetch()  # type: ignore[attr-defined]
                status, sequence = page.evaluate(
                    """
                    async (roomId) => {
                        const r = await fetch(`/api/v1/rooms/${roomId}/messages`, {
                            method: 'POST',
                            headers: {
                                Authorization: 'Bearer demo',
                                'Content-Type': 'application/json'
                            },
                            body: JSON.stringify({content: 'Round 2 switch-room message'})
                        });
                        const body = await r.json();
                        return [r.status, body.sequence];
                    }
                    """,
                    new_room_id,
                )
                post_result_holder["status"] = status
                post_result_holder["sequence"] = sequence
            finally:
                if response is not None:
                    route.fulfill(response=response)  # type: ignore[attr-defined]
                else:
                    route.continue_()  # type: ignore[attr-defined]

        page.route("**/api/v1/rooms/*/state*", hold_new_room_state)
        page.evaluate("(targetRoomId) => switchRoom(targetRoomId)", new_room_id)
        page.wait_for_function(
            "Array.from(document.querySelectorAll('#messages .msg .bubble'))"
            ".some(el => el.textContent.includes('Round 2 switch-room message'))",
            timeout=10000,
        )
        sequence_after = page.evaluate("lastSequence")
        browser.close()

    new_room_ws_urls = [url for url in ws_urls if new_room_id in url and "/ws" in url]
    assert new_room_ws_urls, ws_urls
    subscribe_query = parse_qs(urlparse(new_room_ws_urls[0]).query)
    subscribed_last_sequence = int(subscribe_query["last_sequence"][0])
    assert subscribed_last_sequence >= seeded_sequence

    assert post_result_holder.get("status") == 200
    # The second message's own sequence, from the room it was posted in,
    # must not be ahead of this tab's cursor for that same room: nothing
    # between them was skipped.
    assert sequence_after >= post_result_holder["sequence"]


def _create_room_with_messages(page, workspace_id: str, name: str, contents: list[str]) -> dict:
    """Create a room and post each of contents to it in order, through the
    live API. Returns {"room_id": ..., "max_sequence": ...} using the last
    message's own sequence as the room's max, so the caller has ground truth
    for what a subscribe cursor or a read-cursor PUT for this room should be
    once everything settles.
    """
    room_id = page.evaluate(
        """
        async ([workspaceId, name]) => {
            const r = await fetch(`/api/v1/workspaces/${workspaceId}/rooms`, {
                method: 'POST',
                headers: {
                    Authorization: 'Bearer demo',
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({name, description: ''})
            });
            const body = await r.json();
            return body.room_id;
        }
        """,
        [workspace_id, name],
    )
    max_sequence = 0
    for content in contents:
        max_sequence = page.evaluate(
            """
            async ([roomId, content]) => {
                const r = await fetch(`/api/v1/rooms/${roomId}/messages`, {
                    method: 'POST',
                    headers: {
                        Authorization: 'Bearer demo',
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({content})
                });
                const body = await r.json();
                return body.sequence;
            }
            """,
            [room_id, content],
        )
    return {"room_id": room_id, "max_sequence": max_sequence}


def _run_rapid_room_switch_race(live_server: _LiveServer, trigger_switch) -> None:
    """Shared body for both finding-39 rapid-switch recipes: room B carries
    more messages (a higher max sequence) than room A, trigger_switch(page,
    a_id, b_id) performs whichever race (a held B fetch, or a synchronous
    double switchRoom call) lands the tab on A while B's snapshot is still
    the one in flight, and this then asserts none of B leaked into A: the
    socket that ends up open for A carries A's own max as its subscribe
    cursor, a read-cursor PUT for A never exceeds A's own max, and B's
    distinctive message text is never observed inside #messages, across the
    whole race, not merely absent at the end.
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        workspace_id = page.evaluate("workspaceId")

        room_a = _create_room_with_messages(page, workspace_id, "room-a", ["A message 1"])
        room_b = _create_room_with_messages(
            page, workspace_id, "room-b", ["B message 1", "B message 2", "B message 3"]
        )
        assert room_b["max_sequence"] > room_a["max_sequence"]
        page.evaluate("refreshRooms()")

        ws_urls: list[str] = []
        page.on("websocket", lambda ws: ws_urls.append(ws.url))
        read_cursor_puts: list[dict] = []

        def record_read_cursor_put(request: object) -> None:
            url = request.url  # type: ignore[attr-defined]
            method = request.method  # type: ignore[attr-defined]
            if method == "PUT" and "/read-cursor" in url:
                data = request.post_data_json  # type: ignore[attr-defined]
                read_cursor_puts.append({"url": url, "body": data})

        page.on("request", record_read_cursor_put)

        # A MutationObserver, not a post-hoc scrape: it catches B's text if it
        # ever rendered into #messages at any point during the race, even if
        # a later rebuild would have since removed it again.
        page.evaluate(
            """
            () => {
                window.__observedMessageTexts = [];
                const target = document.getElementById('messages');
                const observer = new MutationObserver((mutations) => {
                    for (const mutation of mutations) {
                        for (const node of mutation.addedNodes) {
                            window.__observedMessageTexts.push(node.textContent || '');
                        }
                    }
                });
                observer.observe(target, {childList: true, subtree: true});
                window.__stopObservingMessages = () => observer.disconnect();
            }
            """
        )

        trigger_switch(page, room_a["room_id"], room_b["room_id"])

        page.wait_for_function(
            """
            (expectedRoomId) => roomId === expectedRoomId
                && Array.from(document.querySelectorAll('#messages .msg .bubble'))
                    .some(el => el.textContent.includes('A message 1'))
            """,
            arg=room_a["room_id"],
            timeout=10000,
        )
        # Deterministic rather than waiting out the auto-read debounce: this
        # is the same markRoomRead() a real reader settling on the room would
        # eventually trigger, just not left to a 1.5s timer plus focus state.
        page.evaluate("markRoomRead()")
        # read_cursor_puts is filled by the Python-side "request" listener
        # above, not by anything in the page, so it is polled from here
        # rather than through wait_for_function.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any(
            room_a["room_id"] in put["url"] for put in read_cursor_puts
        ):
            page.wait_for_timeout(50)

        observed_texts = page.evaluate(
            "() => { window.__stopObservingMessages(); return window.__observedMessageTexts; }"
        )
        browser.close()

    a_ws_urls = [url for url in ws_urls if room_a["room_id"] in url and "/ws" in url]
    assert a_ws_urls, ws_urls
    a_subscribe_query = parse_qs(urlparse(a_ws_urls[-1]).query)
    assert int(a_subscribe_query["last_sequence"][0]) == room_a["max_sequence"]

    a_read_cursor_puts = [p for p in read_cursor_puts if room_a["room_id"] in p["url"]]
    assert a_read_cursor_puts
    for put in a_read_cursor_puts:
        assert put["body"]["last_read_sequence"] <= room_a["max_sequence"]

    assert not any("B message" in text for text in observed_texts), observed_texts


def test_switch_room_ignores_a_held_earlier_rooms_snapshot(live_server: _LiveServer) -> None:
    """Finding 39 (round 3): switchRoom(B) then switchRoom(A) while B's own
    /state fetch is still held back used to let A settle on B's response,
    because loadState() deduped A's call onto B's in-flight fetch regardless
    of which room it actually answered for. Held here with page.route so B's
    fetch is provably still in flight at the moment A is asked for.
    """

    def trigger(page, a_id: str, b_id: str) -> None:
        # The route handler below only fetches B's real response and signals
        # that it is holding it, then returns without resolving the route:
        # switchRoom(a_id) is fired from the main thread afterward, exactly
        # like the actual browser would (a click landing while the previous
        # room's snapshot is still in flight), not from inside the handler
        # itself, which would depend on A's own /state request being routed
        # through this same handler while it is still on the stack for B.
        holder: dict[str, object] = {}

        def hold_b_state(route: object) -> None:
            request_url = route.request.url  # type: ignore[attr-defined]
            if f"/rooms/{b_id}/state" not in request_url or "route" in holder:
                route.continue_()  # type: ignore[attr-defined]
                return
            holder["route"] = route
            holder["response"] = route.fetch()  # type: ignore[attr-defined]

        page.route("**/api/v1/rooms/*/state*", hold_b_state)
        # Fire-and-forget: page.evaluate awaits whatever it is given, and
        # switchRoom's own promise does not resolve until its socket
        # connects, so this must not be `=> switchRoom(id)` (an implicit
        # return of that promise) or this call would block right here for
        # the same reason held B's route is never let through.
        page.evaluate("(id) => { switchRoom(id); }", b_id)
        # Playwright's route dispatch only progresses on its own API calls,
        # not on a bare Python-side wait, so this polls with wait_for_timeout
        # rather than blocking on a threading.Event.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "route" not in holder:
            page.wait_for_timeout(50)
        assert "route" in holder, "B's /state fetch was never intercepted"
        try:
            # B's real response is fetched and held; switch away from it
            # while the route is still unresolved.
            page.evaluate("(id) => { switchRoom(id); }", a_id)
            page.wait_for_function(
                "(expectedRoomId) => roomId === expectedRoomId", arg=a_id, timeout=5000
            )
        finally:
            route = holder.get("route")
            response = holder.get("response")
            if route is not None:
                if response is not None:
                    route.fulfill(response=response)  # type: ignore[attr-defined]
                else:
                    route.continue_()  # type: ignore[attr-defined]

    _run_rapid_room_switch_race(live_server, trigger)


def test_switch_room_double_click_does_not_apply_the_wrong_room(
    live_server: _LiveServer,
) -> None:
    """Finding 39 (round 3): the same defect reproduces with no network delay
    at all, from switchRoom(B) and switchRoom(A) fired back to back in one
    tick (a double click) rather than by holding a fetch open.
    """

    def trigger(page, a_id: str, b_id: str) -> None:
        page.evaluate("([bId, aId]) => { switchRoom(bId); switchRoom(aId); }", [b_id, a_id])

    _run_rapid_room_switch_race(live_server, trigger)


def _channel_menu_rect(page: Page) -> dict:
    page.click("#channel-menu-button")
    rect = page.evaluate(
        """
        () => {
            const el = document.getElementById('channel-menu');
            const rect = el.getBoundingClientRect();
            return {left: rect.left, right: rect.right};
        }
        """
    )
    page.evaluate("closeChannelMenu()")
    return rect


def test_rtl_hides_the_closed_drawer_and_keeps_the_context_head_offset(
    live_server: _LiveServer,
) -> None:
    """Finding 66. Round 2 regression: under rtl, the closed mobile sidebar
    drawer used to sit on screen instead of off-canvas, because its
    translateX direction was still hardcoded for ltr even after its anchor
    became inset-inline-start. Also checks the plainer half of the finding
    (the nine four-value padding shorthands turned into padding-block plus
    padding-inline-start/end): .context-head's computed padding-inline-start
    must resolve to the same pixel offset padding-left gave it under ltr, on
    both sides of the dir flip.

    Round 4: the channel-menu popover positioned itself with menu.style.right,
    computed from window.innerWidth minus the button's physical right edge.
    getBoundingClientRect is always physical, so under rtl that arithmetic
    answers the wrong edge once inset-inline-end remaps right to left: opened
    at 800px or 375px under rtl, most of the 180px-wide popover rendered off
    the left edge of the viewport. Checked at both widths since the finding
    named both explicitly and a fixed-offset regression would not show at
    every size.
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_viewport_size({"width": 800, "height": 900})
        _enter_demo_workspace(page, live_server.base_url)

        ltr_padding_left = page.evaluate(
            "getComputedStyle(document.querySelector('.context-head')).paddingLeft"
        )
        assert ltr_padding_left != "0px"

        page.evaluate("document.documentElement.dir = 'rtl'")
        # .sidebar has a 160ms transform transition; the dir flip changes
        # which rule applies to it, which is itself a transition-triggering
        # change, so the bounding rect below is read after that settles
        # rather than mid-animation.
        page.wait_for_timeout(250)

        rtl_padding_inline_start = page.evaluate(
            "getComputedStyle(document.querySelector('.context-head')).paddingInlineStart"
        )
        assert rtl_padding_inline_start == ltr_padding_left

        viewport_width_800 = page.evaluate("window.innerWidth")
        drawer_rect = page.evaluate(
            """
            () => {
                const rect = document.getElementById('sidebar').getBoundingClientRect();
                return {left: rect.left, right: rect.right};
            }
            """
        )
        menu_rect_800 = _channel_menu_rect(page)

        page.set_viewport_size({"width": 375, "height": 900})
        page.wait_for_timeout(100)
        viewport_width_375 = page.evaluate("window.innerWidth")
        menu_rect_375 = _channel_menu_rect(page)

        browser.close()

    assert drawer_rect["right"] <= 0 or drawer_rect["left"] >= viewport_width_800, drawer_rect
    for viewport_width, menu_rect in (
        (viewport_width_800, menu_rect_800),
        (viewport_width_375, menu_rect_375),
    ):
        assert menu_rect["left"] >= 0, menu_rect
        assert menu_rect["right"] <= viewport_width, (menu_rect, viewport_width)


def test_switch_room_leaves_at_most_one_open_socket(live_server: _LiveServer) -> None:
    """Socket lifecycle (scope note): switching B -> A -> B rapidly used to
    leave a second, leaked WebSocket open for B. A socket switchRoom deliberately
    replaced still fired its own onclose once the close handshake actually
    completed, saw itself as an unexpected drop (nothing had nulled ws yet at
    that point), and scheduled its own reconnect; by the time that reconnect
    timer fired, a legitimate new socket for the same room already existed, so
    the timer's connectWS() opened a second, orphaned one nothing ever closed.

    A page-level Proxy around the native WebSocket constructor counts every
    socket this page ever opens and tracks whether each has since closed, so
    this is a black-box count of real sockets, not a check against the app's
    own bookkeeping variables.
    """
    ws_tracker_script = """
    (() => {
        window.__wsRecords = [];
        const NativeWebSocket = window.WebSocket;
        window.WebSocket = new Proxy(NativeWebSocket, {
            construct(target, args) {
                const instance = new target(...args);
                const record = {open: true};
                window.__wsRecords.push(record);
                instance.addEventListener('close', () => { record.open = false; });
                // The bug this test targets depends on the closed socket's own
                // onclose handler running AFTER a replacement socket already
                // exists, which on real localhost timing is a coin flip (the
                // close handshake and the next fetch race each other). Forcing
                // app code's own onclose to run 50ms after the real close
                // event makes that interleave deterministic instead: the round
                // of switches below always finishes well inside 50ms, so an
                // onclose that was not nulled out before close() (the bug)
                // always fires into stale state, and one that was (the fix)
                // never fires at all.
                let realOnClose = null;
                Object.defineProperty(instance, 'onclose', {
                    configurable: true,
                    get() { return realOnClose; },
                    set(handler) { realOnClose = handler; }
                });
                instance.addEventListener('close', (event) => {
                    setTimeout(() => { if (realOnClose) realOnClose(event); }, 50);
                });
                return instance;
            }
        });
    })();
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        # Installed before the app's own script ever runs, so it also catches
        # the very first connectWS() call from entering the demo workspace.
        page.add_init_script(ws_tracker_script)
        _enter_demo_workspace(page, live_server.base_url)
        workspace_id = page.evaluate("workspaceId")

        room_a = _create_room_with_messages(page, workspace_id, "room-a-lifecycle", ["A"])
        room_b = _create_room_with_messages(page, workspace_id, "room-b-lifecycle", ["B"])
        page.evaluate("refreshRooms()")

        # All three switchRoom calls fired in one tick, not awaited: "rapid"
        # means genuinely overlapping (B's own connectWS has not run by the
        # time A starts, and A's has not run by the time B starts again),
        # not merely fast-but-sequential. Awaiting each call in turn, or
        # waiting for roomId to settle between them, gives connectWS a full
        # chance to reassign ws before the previous socket's close finishes,
        # which happens to already dodge this bug on its own; the real
        # defect only shows up when several switches pile up first.
        page.evaluate(
            "([b, a, b2]) => { switchRoom(b); switchRoom(a); switchRoom(b2); }",
            [room_b["room_id"], room_a["room_id"], room_b["room_id"]],
        )
        page.wait_for_function(
            "(expectedRoomId) => roomId === expectedRoomId",
            arg=room_b["room_id"],
            timeout=10000,
        )
        # Long enough for a stale reconnect timer (the bug's own mechanism,
        # armed at the default 1000ms backoff) to have fired if one was set,
        # and past this test's own artificial 50ms onclose delay.
        page.wait_for_timeout(2000)

        records = page.evaluate("window.__wsRecords")
        browser.close()

    open_count = sum(1 for record in records if record["open"])
    # At least the initial demo-entry socket plus one switch-driven socket:
    # a superseded switchRoom call correctly never reaches connectWS at all
    # (see the round-3 fix above), so this does not assume one socket per
    # switchRoom call, only that switching sockets happened at all.
    assert len(records) >= 2, records
    assert open_count == 1, records
