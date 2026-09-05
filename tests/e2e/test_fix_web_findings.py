"""Round 2 web-client fix track: findings 78-84 (see scratchpad/r2/fix/web.json)
plus one extra low item named directly in the track brief (an `agent.left_room`
event with no client handler).

Each test below fails on the pre-fix client and passes after. Every test
installs a page-level `unhandledrejection` listener before doing anything
else (finding 80's own bar for this suite) and fails the assertion if one
fires during the flow under test.

Runs a real Chromium against the real app, same pattern as test_web_client.py
and test_fix_realtime_client_gap_resync.py.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest

pytest.importorskip("playwright.sync_api")

import uvicorn
from playwright.sync_api import Page, sync_playwright

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
    server = _LiveServer(str(tmp_path / "fix-web-findings.db"))
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _install_unhandled_rejection_guard(page: Page) -> None:
    """Finding 80's own bar: fail any test in this module on an unhandled
    rejection, installed before the flow under test runs."""
    page.evaluate(
        "() => { window.__unhandledRejections = []; "
        "window.addEventListener('unhandledrejection', "
        "e => window.__unhandledRejections.push(String(e.reason))); }"
    )


def _assert_no_unhandled_rejections(page: Page) -> None:
    rejections = page.evaluate("() => window.__unhandledRejections")
    assert rejections == [], f"unhandled rejection(s): {rejections}"


# ---------------------------------------------------------------------------
# 78: a revoked bearer token leaves the app reconnecting forever
# ---------------------------------------------------------------------------


def test_78_a_4401_socket_close_returns_to_setup_with_a_visible_message(
    live_server: _LiveServer,
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        _install_unhandled_rejection_guard(page)

        # A client-initiated close still delivers the requested code to this
        # tab's own onclose handler, which is exactly what the server does on
        # websocket.py:259 when a bearer token is revoked mid-session.
        page.evaluate(
            """
            async () => {
              const { state } = await import('/static/js/state.js');
              state.ws.close(4401, 'authentication revoked');
            }
            """
        )

        page.wait_for_function(
            "() => document.getElementById('setup-screen').style.display !== 'none'",
            timeout=5000,
        )
        assert page.is_visible("#app-main") is False or (
            page.evaluate("() => document.getElementById('app-main').style.display") == "none"
        )
        error_text = page.evaluate("() => document.getElementById('setup-token-error').textContent")
        assert "no longer valid" in error_text
        ws_after = page.evaluate(
            "async () => { const { state } = await import('/static/js/state.js');"
            " return state.ws; }"
        )
        assert ws_after is None
        # A stale reconnect timer surviving this would eventually reopen a
        # socket with the same rejected credential — nothing should still be
        # pending.
        page.wait_for_timeout(300)
        assert page.evaluate("() => document.getElementById('app-main').style.display") == "none"
        _assert_no_unhandled_rejections(page)
        browser.close()


def test_78_a_401_on_a_plain_fetch_in_bearer_mode_also_ends_the_session(
    live_server: _LiveServer,
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        _install_unhandled_rejection_guard(page)

        result = page.evaluate(
            """
            async () => {
              const { api } = await import('/static/js/api.js');
              const { state } = await import('/static/js/state.js');
              state.accessToken = 'revoked-token';
              try {
                await api('GET', `/rooms/${state.roomId}/state?last_sequence=0`);
                return 'no-throw';
              } catch (e) {
                return e.status;
              }
            }
            """
        )
        assert result == 401
        page.wait_for_function(
            "() => document.getElementById('setup-screen').style.display !== 'none'",
            timeout=5000,
        )
        assert "no longer valid" in page.evaluate(
            "() => document.getElementById('setup-token-error').textContent"
        )
        _assert_no_unhandled_rejections(page)
        browser.close()


def test_78_a_second_revocation_after_a_fresh_bearer_sign_in_also_returns_to_setup(
    live_server: _LiveServer,
) -> None:
    """Round 2 finding: state.bearerSessionEnding was set true by the first
    revocation and never cleared on a later sign-in, so a person who got
    signed out, typed a fresh (or the same) token back in, and then hit a
    second revocation saw a dead socket claiming "Connected" with no
    message at all -- handleBearerUnauthorized's own guard silently no-opped
    forever after the first time it fired on this tab."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        _install_unhandled_rejection_guard(page)

        page.evaluate(
            """
            async () => {
              const { state } = await import('/static/js/state.js');
              state.ws.close(4401, 'authentication revoked');
            }
            """
        )
        page.wait_for_function(
            "() => document.getElementById('setup-screen').style.display !== 'none'",
            timeout=5000,
        )

        # Sign back in on the same tab/page (no reload) with a fresh token,
        # exactly like a person retyping their credential after being kicked
        # out. The demo token authenticates the same demo user again.
        page.fill("#setup-token", "demo")
        page.click("#setup-button")
        page.wait_for_selector("#app-main", state="visible", timeout=10000)
        page.wait_for_function(
            "() => document.getElementById('agents-panel').innerHTML.trim().length > 0",
            timeout=10000,
        )
        _wait_for_socket_connected(page)

        # A second revocation on this freshly re-authenticated session must
        # be handled again, not silently swallowed by a guard flag the first
        # revocation left latched true.
        page.evaluate(
            """
            async () => {
              const { state } = await import('/static/js/state.js');
              state.ws.close(4401, 'authentication revoked');
            }
            """
        )
        page.wait_for_function(
            "() => document.getElementById('setup-screen').style.display !== 'none'",
            timeout=5000,
        )
        error_text = page.evaluate("() => document.getElementById('setup-token-error').textContent")
        assert "no longer valid" in error_text
        _assert_no_unhandled_rejections(page)
        browser.close()


# ---------------------------------------------------------------------------
# 79: the button-only confirm dialogs open with focus stuck on <body>
# ---------------------------------------------------------------------------


def test_79_remove_agent_confirm_focuses_cancel_and_traps_tab(
    live_server: _LiveServer,
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        _install_unhandled_rejection_guard(page)

        page.evaluate(
            """
            async () => {
              const { confirmRemoveAgent } = await import('/static/js/members.js');
              confirmRemoveAgent('agent-x', 'Agent X');
            }
            """
        )
        page.wait_for_function(
            "() => !document.getElementById('modal-backdrop').classList.contains('hidden')",
            timeout=5000,
        )
        focused_text = page.evaluate("() => document.activeElement.textContent")
        assert focused_text == "Cancel"

        # Shift+Tab from the first (and here only reachable) control must stay
        # inside the dialog rather than escaping to whatever is behind it.
        page.keyboard.press("Shift+Tab")
        after = page.evaluate("() => document.activeElement.textContent")
        assert after in ("Cancel", "Remove")
        assert page.evaluate(
            "() => document.getElementById('modal-card').contains(document.activeElement)"
        )
        _assert_no_unhandled_rejections(page)
        browser.close()


# ---------------------------------------------------------------------------
# 80: approve/reject and socket-triggered loadState() failures were silent
# ---------------------------------------------------------------------------


def test_80_reject_action_on_a_missing_approval_surfaces_a_toast(
    live_server: _LiveServer,
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        _install_unhandled_rejection_guard(page)

        page.evaluate(
            """
            async () => {
              const { rejectAction } = await import('/static/js/members.js');
              await rejectAction('does-not-exist');
            }
            """
        )
        page.wait_for_function(
            "() => document.querySelectorAll('#toast-region .toast.error').length > 0",
            timeout=5000,
        )
        _assert_no_unhandled_rejections(page)
        browser.close()


def test_80_a_failing_socket_triggered_loadstate_does_not_throw_unhandled(
    live_server: _LiveServer,
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        _install_unhandled_rejection_guard(page)

        # Route the state fetch to a 500 for one call, then trigger a
        # socket-driven loadState() the same way a live reaction event would
        # (handleRealtimeEvent's 'message.reaction_added' case).
        page.evaluate(
            """
            () => {
              const orig = window.fetch.bind(window);
              let armed = true;
              window.fetch = (...args) => {
                if (armed && String(args[0]).includes('/state?')) {
                  armed = false;
                  return Promise.resolve(new Response(JSON.stringify({detail: 'probe 500'}),
                    {status: 500, headers: {'Content-Type': 'application/json'}}));
                }
                return orig(...args);
              };
            }
            """
        )
        page.evaluate(
            """
            async () => {
              const socketModule = await import('/static/js/socket.js');
              const { state } = await import('/static/js/state.js');
              socketModule.handleRealtimeEvent({
                type: 'room_event', event_type: 'message.reaction_added', room_id: state.roomId,
                sequence: state.lastSequence + 1, actor_id: 'owner', actor_type: 'user',
                timestamp: new Date().toISOString(), payload: {},
              });
            }
            """
        )
        page.wait_for_timeout(500)
        _assert_no_unhandled_rejections(page)
        browser.close()


# ---------------------------------------------------------------------------
# 81: a 422 validation body used to render as "[object Object]"
# ---------------------------------------------------------------------------


def test_81_errormessage_renders_a_422_list_body_field_by_field(
    live_server: _LiveServer,
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        _install_unhandled_rejection_guard(page)

        result = page.evaluate(
            """
            async () => {
              const { errorMessage } = await import('/static/js/util.js');
              const err = new Error(JSON.stringify({detail: [
                {type: 'string_type', loc: ['body', 'content'],
                 msg: 'Input should be a valid string'},
              ]}));
              return errorMessage(err);
            }
            """
        )
        assert result == "Input should be a valid string"
        assert "[object Object]" not in result
        _assert_no_unhandled_rejections(page)
        browser.close()


# ---------------------------------------------------------------------------
# 82: no landmarks, no skip link
# ---------------------------------------------------------------------------


def test_82_landmarks_and_skip_link_are_present(live_server: _LiveServer) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        _install_unhandled_rejection_guard(page)

        counts = page.evaluate(
            """
            () => ({
              header: document.querySelectorAll('header, [role=banner]').length,
              nav: document.querySelectorAll('nav, [role=navigation]').length,
              main: document.querySelectorAll('main, [role=main]').length,
              aside: document.querySelectorAll('aside, [role=complementary]').length,
              skipLink: document.querySelectorAll('a.skip-link[href="#msg-input"]').length,
            })
            """
        )
        assert counts["header"] >= 1
        assert counts["nav"] >= 1
        assert counts["main"] >= 1
        assert counts["aside"] >= 1
        assert counts["skipLink"] == 1

        # The skip link is the very first focusable thing a keyboard user
        # reaches, and it actually moves focus to the composer. Reloaded
        # first: Chromium anchors sequential Tab navigation to the last
        # element a real mouse click landed on (here, the now-hidden demo
        # button _enter_demo_workspace clicked), even once that element is
        # gone and document.activeElement reads back as <body> — a browser
        # bookkeeping quirk unrelated to this page's own tab order. A fresh
        # load removes that stale anchor, matching what an actual first-time
        # keyboard visitor's Tab sequence looks like.
        page.reload()
        page.wait_for_selector("#app-main", state="visible", timeout=10000)
        page.wait_for_function(
            "() => document.getElementById('agents-panel').innerHTML.trim().length > 0",
            timeout=10000,
        )
        _install_unhandled_rejection_guard(page)
        page.keyboard.press("Tab")
        assert page.evaluate("() => document.activeElement.className") == "skip-link"
        page.keyboard.press("Enter")
        page.wait_for_function(
            "() => document.activeElement && document.activeElement.id === 'msg-input'",
            timeout=5000,
        )
        _assert_no_unhandled_rejections(page)
        browser.close()


# ---------------------------------------------------------------------------
# 83: menu, ws-status and drawer ARIA that behaviour did not honour
# ---------------------------------------------------------------------------


def test_83_channel_menu_focuses_first_item_and_arrow_keys_move_between_items(
    live_server: _LiveServer,
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        _install_unhandled_rejection_guard(page)

        page.click("#channel-menu-button")
        page.wait_for_function(
            "() => !document.getElementById('channel-menu').classList.contains('hidden')"
        )
        first_focused = page.evaluate("() => document.activeElement.textContent")
        assert first_focused == "Invite people"

        page.keyboard.press("ArrowDown")
        assert page.evaluate("() => document.activeElement.textContent") == "Channel details"
        page.keyboard.press("ArrowUp")
        assert page.evaluate("() => document.activeElement.textContent") == "Invite people"
        page.keyboard.press("Escape")
        page.wait_for_function(
            "() => document.getElementById('channel-menu').classList.contains('hidden')"
        )
        assert page.evaluate("() => document.activeElement.id") == "channel-menu-button"
        _assert_no_unhandled_rejections(page)
        browser.close()


def test_83_ws_status_is_a_status_live_region(live_server: _LiveServer) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        _install_unhandled_rejection_guard(page)

        role = page.evaluate("() => document.getElementById('ws-status').getAttribute('role')")
        assert role == "status"
        _assert_no_unhandled_rejections(page)
        browser.close()


def test_83_mobile_drawer_traps_tab_and_returns_focus_on_close(
    live_server: _LiveServer,
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 375, "height": 812})
        _enter_demo_workspace(page, live_server.base_url)
        _install_unhandled_rejection_guard(page)

        page.click("#sidebar-toggle")
        page.wait_for_function(
            "() => document.getElementById('sidebar').classList.contains('open')"
        )
        assert page.evaluate(
            "() => document.getElementById('sidebar').contains(document.activeElement)"
        )
        page.keyboard.press("Escape")
        page.wait_for_function(
            "() => !document.getElementById('sidebar').classList.contains('open')"
        )
        assert page.evaluate("() => document.activeElement.id") == "sidebar-toggle"
        _assert_no_unhandled_rejections(page)
        browser.close()


def test_83_closing_the_drawer_through_its_backdrop_also_returns_focus(
    live_server: _LiveServer,
) -> None:
    """Round 2 finding: the backdrop click was wired to toggleSidebar(false)
    (app.js's closeSidebar), a separate code path from closeSidebarDrawer
    that never restored focus -- so Escape returned focus to #sidebar-toggle
    but clicking the backdrop left it on whatever element inside the
    now-hidden drawer last held it."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 375, "height": 812})
        _enter_demo_workspace(page, live_server.base_url)
        _install_unhandled_rejection_guard(page)

        page.click("#sidebar-toggle")
        page.wait_for_function(
            "() => document.getElementById('sidebar').classList.contains('open')"
        )
        assert page.evaluate(
            "() => document.getElementById('sidebar').contains(document.activeElement)"
        )
        page.click("#sidebar-backdrop")
        page.wait_for_function(
            "() => !document.getElementById('sidebar').classList.contains('open')"
        )
        assert page.evaluate("() => document.activeElement.id") == "sidebar-toggle"
        _assert_no_unhandled_rejections(page)
        browser.close()


# ---------------------------------------------------------------------------
# 84: unescaped enum/id sites (hygiene, no live injection)
# ---------------------------------------------------------------------------


def test_84_agent_status_and_task_fields_are_escaped_in_the_panel(
    live_server: _LiveServer,
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        _install_unhandled_rejection_guard(page)

        result = page.evaluate(
            """
            async () => {
              const { renderAgents } = await import('/static/js/members.js');
              const payload = '"><img src=x onerror=alert(1)>';
              renderAgents([{agent_id: 'a1', name: 'A', role: 'analyst',
                             status: payload, handle: ''}]);
              const panel = document.getElementById('agents-panel');
              return {
                html: panel.innerHTML,
                imgCount: panel.querySelectorAll('img').length,
              };
            }
            """
        )
        assert result["imgCount"] == 0
        # Escaped, the payload's own '<' and '>' become '&lt;'/'&gt;' — the
        # literal substring "onerror=alert" still appears as inert text, but
        # an actual '<img' tag opener must not.
        assert "<img" not in result["html"]
        _assert_no_unhandled_rejections(page)
        browser.close()


def test_84_search_hit_selector_builders_escape_the_object_id(
    live_server: _LiveServer,
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        _install_unhandled_rejection_guard(page)

        result = page.evaluate(
            """
            async () => {
              const { SEARCH_HIT_TARGETS } = await import('/static/js/thread.js');
              const selector = SEARCH_HIT_TARGETS.TASK({objectId: '"]  ,*{}'}).selector;
              let threw = false;
              try { document.querySelector(selector); } catch (e) { threw = true; }
              return {selector, threw};
            }
            """
        )
        # A crafted id must not be able to close the attribute-selector string
        # early: CSS.escape() backslash-escapes the quote so the whole thing
        # stays one valid attribute selector (matching nothing, since no
        # element carries that literal id) rather than throwing a
        # DOMException for an invalid selector or, unescaped, closing the
        # attribute value and injecting a second simple selector.
        assert result["threw"] is False
        assert '"]  ,*{}"' not in result["selector"]
        _assert_no_unhandled_rejections(page)
        browser.close()


# ---------------------------------------------------------------------------
# Extra low item: no handler for agent.left_room
# ---------------------------------------------------------------------------


def test_extra_agent_left_room_refreshes_the_agents_panel(
    live_server: _LiveServer,
) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        _install_unhandled_rejection_guard(page)

        before_calls = page.evaluate(
            """
            () => {
              window.__stateFetches = 0;
              const orig = window.fetch.bind(window);
              window.fetch = (...args) => {
                if (String(args[0]).includes('/state?')) window.__stateFetches++;
                return orig(...args);
              };
              return window.__stateFetches;
            }
            """
        )
        assert before_calls == 0

        page.evaluate(
            """
            async () => {
              const socketModule = await import('/static/js/socket.js');
              const { state } = await import('/static/js/state.js');
              socketModule.handleRealtimeEvent({
                type: 'room_event', event_type: 'agent.left_room', room_id: state.roomId,
                sequence: state.lastSequence + 1, actor_id: 'owner', actor_type: 'user',
                timestamp: new Date().toISOString(),
                payload: {agent_id: 'agent-removed-out-of-band', removed_by: 'owner',
                          settled_run_ids: []},
              });
            }
            """
        )
        page.wait_for_function("() => window.__stateFetches >= 1", timeout=5000)
        _assert_no_unhandled_rejections(page)
        browser.close()
