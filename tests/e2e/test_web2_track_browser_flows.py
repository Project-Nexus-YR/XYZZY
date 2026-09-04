"""Round 2's growth of the browser suite (item 4): the flows the client has
that the nine round-1 tests never drove — a thread reply, a branch view with
include and exclude, publishing a Decision Brief and opening its evidence,
the approval flow, a non-admin's member-role refusal, search, and the public
share page with no session at all.

Each test is a real Chromium session against the demo server (XYZZY_DEMO=1,
the sign-in-free path from a real workspace seeded with the SIMULATED model
provider, so a full branch run completes with no network and no API key).
Skips cleanly when Chromium is not installed, via the same `_require_chromium`
fixture test_web_client.py uses.
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
    def __init__(self, db_path: str, *, demo: bool = True, auth_tokens=None) -> None:
        self.port = _free_port()
        app = create_app(db_path, demo=demo, auth_tokens=auth_tokens)
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
    server = _LiveServer(str(tmp_path / "web2-flows.db"), demo=True)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def test_thread_open_and_reply(live_server: _LiveServer) -> None:
    """Opening a thread and replying in it: the round-1 suite never drove
    the thread panel at all."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)

        # The reply action only becomes visible on :hover/:focus-within, a CSS
        # reveal Playwright's own hover simulation cannot be relied on to have
        # finished before a click; dispatching the click straight from the DOM
        # exercises the same delegated handler without depending on that
        # transition. The last DOM child of #messages is not reliably the
        # last chat message: a membership system notice ("X was invited")
        # can arrive over the socket and append after the snapshot render,
        # landing after every human message with no .msg-actions of its own
        # (see seed_demo_workspace's invites). Selecting among ".msg.human"
        # and taking the last one is what "the last message" means here.
        page.wait_for_function(
            "() => document.querySelectorAll("
            "'#messages .msg.human [data-action=openThread]').length > 0",
            timeout=10000,
        )
        page.eval_on_selector_all(
            "#messages .msg.human [data-action=openThread]",
            "els => els[els.length - 1].click()",
        )

        page.wait_for_selector("#thread-reply-form:not([hidden])", state="visible", timeout=10000)
        reply_text = "Round 2 thread reply"
        page.fill("#thread-reply-input", reply_text)
        page.click("#thread-reply-form button[type=submit]")

        page.wait_for_function(
            "(text) => Array.from(document.querySelectorAll('#thread-list .thread-body'))"
            ".some(el => el.textContent.includes(text))",
            arg=reply_text,
            timeout=10000,
        )
        browser.close()


def _launch_default_branch(page: Page) -> None:
    """Opens the AI branch tray and launches it with the pre-checked
    (valid, 3-specialist) default template selection against a fixed
    question, then selects that branch and waits for its outputs to
    render. The SIMULATED provider (no XYZZY_AUTH_TOKENS, no API key
    needed) completes a run fast enough for a bounded wait_for_function.

    The demo seed's own room already carries a branch with completed
    outputs, and the just-launched branch does not become the selected one
    on its own (only clicking a branch's nav entry does, exactly as a
    person would): a bare "at least N output cards" wait can be satisfied
    by that pre-existing branch's cards while the new branch's own outputs
    are still arriving. Recording the branch ids before launch and then
    clicking the one that is new keeps every following step scoped to the
    branch this call actually created.
    """
    initial_branch_ids = page.eval_on_selector_all(
        "#branches-list .branch-nav", "els => els.map(el => el.dataset.branchId)"
    )
    page.click("[data-action=toggleAITray]")
    page.wait_for_selector("#ai-tray.open", state="visible", timeout=10000)
    page.fill("#analysis-question", "Round 2 test question: pick a caching layer.")
    page.click("#launch-button")
    # Launching does not itself switch the center view (only selecting an
    # existing branch does), and the outputs panel it fills lives inside
    # #view-branch — invisible, and so unusable by anything but a raw DOM
    # click, until that view becomes the active one.
    page.click("[data-action=openContext][data-action-arg=branch]")
    page.wait_for_selector("#view-branch.active", state="visible", timeout=10000)
    page.wait_for_function(
        "(ids) => Array.from(document.querySelectorAll('#branches-list .branch-nav'))"
        ".some(el => !ids.includes(el.dataset.branchId))",
        arg=initial_branch_ids,
        timeout=30000,
    )
    page.eval_on_selector_all(
        "#branches-list .branch-nav",
        "(els, ids) => els.find(el => !ids.includes(el.dataset.branchId)).click()",
        initial_branch_ids,
    )
    # All three preferred specialists (Architect, Security Reviewer, Researcher)
    # are pre-checked, so the branch launches three agents; a caller that only
    # decides however many cards happen to have rendered so far (rather than
    # all three) leaves a straggler unreviewed and synthesis refuses it.
    page.wait_for_function(
        "() => document.querySelectorAll('#outputs-panel .output-card').length >= 3",
        timeout=30000,
    )


def test_branch_view_include_and_exclude(live_server: _LiveServer) -> None:
    """A branch view with include and exclude: launches a real branch run,
    then marks one output included and the other excluded, and asserts the
    selection state (the active class, and the excluded card's visual
    demotion) reflects each choice."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        _launch_default_branch(page)

        # Re-renders of #outputs-panel (loadState() runs more than once as the
        # branch's runs settle) can detach a Playwright-resolved locator
        # between resolution and click; a fresh DOM query at click time does
        # not have that race.
        page.eval_on_selector(
            "#outputs-panel .output-card:nth-of-type(1) button.include", "el => el.click()"
        )
        page.eval_on_selector(
            "#outputs-panel .output-card:nth-of-type(2) button.exclude", "el => el.click()"
        )

        page.wait_for_function(
            "() => document.querySelectorAll('#outputs-panel .output-card')[0]"
            ".querySelector('button.include').classList.contains('active')",
            timeout=10000,
        )
        page.wait_for_function(
            "() => document.querySelectorAll('#outputs-panel .output-card')[1]"
            ".classList.contains('excluded')",
            timeout=10000,
        )
        browser.close()


def test_publish_decision_brief_and_open_evidence(live_server: _LiveServer) -> None:
    """Publishing a Decision Brief from a branch's included outputs, then
    opening the Artifacts view and confirming its evidence tree renders."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        _launch_default_branch(page)

        # Every output must be decided before synthesis will accept the branch.
        page.eval_on_selector_all(
            "#outputs-panel .output-card button.include", "els => els.forEach(el => el.click())"
        )
        page.wait_for_function(
            "() => Array.from(document.querySelectorAll("
            "'#outputs-panel .output-card button.include'))"
            ".every(el => el.classList.contains('active'))",
            timeout=10000,
        )
        title = "Round 2 evidence test brief"
        page.fill("#synthesis-title", title)
        page.wait_for_function(
            "() => !document.getElementById('synthesize-button').disabled", timeout=10000
        )
        page.click("#synthesize-button")

        # A room carries one Decision Brief artifact, versioned on every
        # republish (see MultiplayerService.synthesize_branch): the demo seed
        # already published one, so this call adds a version rather than a
        # second artifact, and the artifact's own `name` stays "Decision
        # Brief" regardless of the title typed above. publishSynthesis()
        # switches to the Artifacts view itself and that view always shows
        # the most recent version, so the typed title (rendered as the
        # document's own heading) is the thing to wait for, not the
        # artifact-selector, which stays empty until a room has more than
        # one distinct artifact.
        page.wait_for_function(
            "(t) => document.getElementById('artifact-surface') "
            "&& document.getElementById('artifact-surface').textContent.includes(t)",
            arg=title,
            timeout=15000,
        )
        page.wait_for_function(
            "() => document.getElementById('ontology-tree').children.length > 0 "
            "&& !document.querySelector('#ontology-tree .ontology-empty')",
            timeout=10000,
        )
        browser.close()


def test_approval_flow_appears_and_can_be_approved(live_server: _LiveServer) -> None:
    """A tool request's approval appears in the reviewer's Approvals panel,
    and approving it clears the pending state. The approval itself is
    created through the real, authorized `/rooms/{id}/approvals` endpoint
    against a real agent + session + execution (no LLM call needed to reach
    PENDING, exactly as the client's own launch flow creates each of those),
    so what this test drives is the reviewer-facing half: the card
    rendering, the Approve click, and the resulting state change."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)
        room_id = page.evaluate("roomId")

        approval_id = page.evaluate(
            """
            async (roomId) => {
                const headers = {Authorization: 'Bearer demo', 'Content-Type': 'application/json'};
                const templates = await (await fetch('/api/v1/agent-templates', {headers})).json();
                const agentBody = {template_id: templates[0].template_id, name: 'Reviewer Test'};
                const agent = await (await fetch(
                    `/api/v1/rooms/${roomId}/agents`,
                    {method: 'POST', headers, body: JSON.stringify(agentBody)}
                )).json();
                const session = await (await fetch(
                    `/api/v1/rooms/${roomId}/agents/${agent.agent_id}/sessions`,
                    {method: 'POST', headers}
                )).json();
                const execution = await (await fetch(
                    `/api/v1/sessions/${session.session_id}/execute`,
                    {method: 'POST', headers}
                )).json();
                const params = new URLSearchParams({
                    execution_id: execution.execution_id, agent_id: agent.agent_id,
                    action: 'Round 2 approval test action'
                });
                const approval = await (await fetch(`/api/v1/rooms/${roomId}/approvals?${params}`, {
                    method: 'POST', headers
                })).json();
                return approval.approval_id;
            }
            """,
            room_id,
        )
        assert approval_id

        page.click("[aria-label='Workspace details']")
        page.click(".records summary")
        page.wait_for_function(
            "() => document.querySelectorAll('#approvals-panel .approval-card').length > 0",
            timeout=10000,
        )
        page.click("#approvals-panel .btn-approve")

        # The list endpoint returns only PENDING approvals (see
        # list_approvals / list_pending_approvals), so an approved row simply
        # stops appearing — that disappearance, not a status flip in place,
        # is what "approved" looks like from this endpoint.
        still_pending = True
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            still_pending = page.evaluate(
                """
                async (id) => {
                    const r = await fetch(`/api/v1/rooms/${window.roomId}/approvals`, {
                        headers: {Authorization: 'Bearer demo'}
                    });
                    const rows = await r.json();
                    return rows.some(a => a.approval_id === id);
                }
                """,
                approval_id,
            )
            if not still_pending:
                break
            page.wait_for_timeout(200)
        browser.close()

    assert not still_pending


def test_member_role_change_is_refused_for_a_non_admin(tmp_path) -> None:
    """Only an admin sees a role control at all, and the server backs that
    up: an editor's own direct PATCH against the same endpoint the control
    would call is refused. Needs its own two-token server (not the
    single-identity demo path) so a second, non-admin browser session
    exists to drive."""
    server = _LiveServer(
        str(tmp_path / "web2-role-refusal.db"),
        demo=False,
        auth_tokens={"admin-token": "user_admin", "editor-token": "user_editor"},
    )
    server.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()

            admin_page = browser.new_page()
            admin_page.goto(server.base_url)
            admin_page.wait_for_selector("#setup-token", state="visible", timeout=10000)
            # The first-run name/channel fields sit inside a closed <details>,
            # only opened automatically after a first submit attempt fails
            # validation; opening it directly is equivalent and one step
            # shorter for a token this fresh a workspace has never seen.
            admin_page.eval_on_selector("#setup-first-run", "el => { el.open = true; }")
            admin_page.fill("#setup-token", "admin-token")
            admin_page.fill("#setup-name", "Admin")
            admin_page.fill("#setup-room", "role-refusal-room")
            admin_page.click("#setup-button")
            admin_page.wait_for_selector("#app-main", state="visible", timeout=10000)
            _wait_for_socket_connected(admin_page)
            room_id = admin_page.evaluate("roomId")
            admin_page.evaluate(
                """
                async (roomId) => {
                    await fetch(`/api/v1/rooms/${roomId}/members/invitations`, {
                        method: 'POST',
                        headers: {
                            Authorization: 'Bearer admin-token',
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({user_id: 'user_editor', role: 'editor'})
                    });
                }
                """,
                room_id,
            )
            admin_page.close()

            editor_page = browser.new_page()
            editor_page.goto(server.base_url)
            editor_page.wait_for_selector("#setup-token", state="visible", timeout=10000)
            editor_page.fill("#setup-token", "editor-token")
            editor_page.click("#setup-button")
            editor_page.wait_for_selector("#app-main", state="visible", timeout=10000)
            _wait_for_socket_connected(editor_page)
            editor_page.wait_for_function(
                "() => document.getElementById('rooms-list').children.length > 0", timeout=10000
            )
            editor_page.click("#rooms-list .nav-item")
            editor_page.click("[aria-label='Open members']")
            editor_page.wait_for_selector("#members-cards .card", state="visible", timeout=10000)

            # The client never renders a role control for a non-admin viewer.
            select_count = editor_page.eval_on_selector_all(
                "#members-cards select", "els => els.length"
            )
            assert select_count == 0

            # The server backs that up: a direct PATCH is refused too.
            status = editor_page.evaluate(
                """
                async (roomId) => {
                    const r = await fetch(`/api/v1/rooms/${roomId}/members/user_admin`, {
                        method: 'PATCH',
                        headers: {
                            Authorization: 'Bearer editor-token',
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({role: 'viewer'})
                    });
                    return r.status;
                }
                """,
                room_id,
            )
            assert status == 403
            browser.close()
    finally:
        server.stop()


def test_notification_mention_opens_from_the_panel(tmp_path) -> None:
    """Round 4 regression: openNotifications (messages.js) referenced a bare
    `lastNotifications` that does not exist in that module's scope -- the
    field lives on `state`, and every other read of it in the same file
    already goes through `state.lastNotifications`. That ReferenceError only
    fires once the list is non-empty (the empty-state branch returns first),
    so a room with zero notifications never surfaced it; this creates a real
    mention notification first; opening the panel used to render zero rows
    and throw instead of showing the one that exists. Needs its own
    two-token server (not the single-identity demo path) so a second member
    can actually address the first one by a real handle."""
    server = _LiveServer(
        str(tmp_path / "web2-notification-mention.db"),
        demo=False,
        auth_tokens={"admin-token": "user_admin", "member-token": "user_member"},
    )
    server.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()

            admin_page = browser.new_page()
            admin_page.goto(server.base_url)
            admin_page.wait_for_selector("#setup-token", state="visible", timeout=10000)
            admin_page.eval_on_selector("#setup-first-run", "el => { el.open = true; }")
            admin_page.fill("#setup-token", "admin-token")
            admin_page.fill("#setup-name", "Admin")
            admin_page.fill("#setup-room", "notification-mention-room")
            admin_page.click("#setup-button")
            admin_page.wait_for_selector("#app-main", state="visible", timeout=10000)
            _wait_for_socket_connected(admin_page)
            room_id = admin_page.evaluate("roomId")
            admin_page.evaluate(
                """
                async (roomId) => {
                    await fetch(`/api/v1/rooms/${roomId}/members/invitations`, {
                        method: 'POST',
                        headers: {
                            Authorization: 'Bearer admin-token',
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify({user_id: 'user_member', role: 'editor'})
                    });
                }
                """,
                room_id,
            )

            errors: list[str] = []
            admin_page.on(
                "console", lambda msg: errors.append(msg.text) if msg.type == "error" else None
            )
            admin_page.on("pageerror", lambda exc: errors.append(str(exc)))

            # A member's own handle is issued from their user id, not their
            # display name (see MultiplayerService._issue_handle), so
            # @user_member is what actually addresses them.
            admin_page.evaluate(
                """
                async (roomId) => {
                    const headers = {
                        Authorization: 'Bearer admin-token',
                        'Content-Type': 'application/json'
                    };
                    await fetch(`/api/v1/rooms/${roomId}/messages`, {
                        method: 'POST', headers,
                        body: JSON.stringify({content: 'Reading this, @user_member?'})
                    });
                }
                """,
                room_id,
            )

            member_page = browser.new_page()
            member_page.goto(server.base_url)
            member_page.wait_for_selector("#setup-token", state="visible", timeout=10000)
            member_page.fill("#setup-token", "member-token")
            member_page.click("#setup-button")
            member_page.wait_for_selector("#app-main", state="visible", timeout=10000)
            _wait_for_socket_connected(member_page)
            member_room_id = member_page.evaluate("roomId")
            member_errors: list[str] = []
            member_page.on(
                "console",
                lambda msg: member_errors.append(msg.text) if msg.type == "error" else None,
            )
            member_page.on("pageerror", lambda exc: member_errors.append(str(exc)))

            member_page.wait_for_function(
                "() => document.getElementById('notif-dot')"
                " && !document.getElementById('notif-dot').classList.contains('hidden')",
                timeout=10000,
            )
            member_page.click("[aria-label='Notifications']")
            member_page.wait_for_selector(
                "#notifications-list .notif-row", state="visible", timeout=10000
            )
            row_text = member_page.eval_on_selector(
                "#notifications-list .notif-row .body", "el => el.textContent"
            )
            assert "user_member" in row_text
            assert member_errors == [], (
                f"console/page errors opening notifications: {member_errors}"
            )
            assert member_room_id
            browser.close()
    finally:
        server.stop()


def test_search_finds_seeded_conversation_text(live_server: _LiveServer) -> None:
    """Search: a query for text the demo seed's own conversation carries
    returns a hit, and clicking it opens that message (a MESSAGE-kind hit
    opens its thread directly rather than the generic scroll-and-highlight
    every other object kind gets — see openSearchHit)."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        _enter_demo_workspace(page, live_server.base_url)

        page.click("[aria-label='Search channel']")
        page.fill("#search-input", "Adyen")
        page.click("#search-input")
        page.press("#search-input", "Enter")

        page.wait_for_function(
            "() => document.querySelectorAll('#search-results .search-hit').length > 0",
            timeout=10000,
        )
        page.click("#search-results .search-hit")
        page.wait_for_function(
            "() => document.querySelector('[data-context-view=thread]')"
            ".classList.contains('active') "
            "&& document.getElementById('thread-list').children.length > 0",
            timeout=10000,
        )
        browser.close()


def test_share_page_renders_a_published_brief_without_a_session() -> None:
    """The share page renders a published Decision Brief with no cookie, no
    bearer token, and no prior page load in this browser context at all —
    the exact shape an outside reader's first request takes."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        setup_page = browser.new_page()
        server = _LiveServer(":memory:", demo=True)
        server.start()
        try:
            _enter_demo_workspace(setup_page, server.base_url)
            artifact_id = setup_page.evaluate(
                """
                async () => {
                    const r = await fetch(`/api/v1/rooms/${window.roomId}/artifacts`, {
                        headers: {Authorization: 'Bearer demo'}
                    });
                    const arts = await r.json();
                    return arts[0].artifact_id;
                }
                """
            )
            share_path = setup_page.evaluate(
                """
                async (artifactId) => {
                    const r = await fetch(`/api/v1/artifacts/${artifactId}/shares`, {
                        method: 'POST',
                        headers: {Authorization: 'Bearer demo', 'Content-Type': 'application/json'}
                    });
                    const body = await r.json();
                    return body.url_path;
                }
                """,
                artifact_id,
            )
            setup_page.close()

            # A brand-new, cookie-less context: nothing from the setup page's
            # session (or its bearer token) carries over into this request.
            fresh_context = browser.new_context()
            share_page = fresh_context.new_page()
            share_page.goto(server.base_url + share_path)
            share_page.wait_for_selector(".doc", state="visible", timeout=10000)
            assert "share-doc" in (share_page.get_attribute("body", "class") or "")
            fresh_context.close()
        finally:
            server.stop()
            browser.close()
