"""Item 2: the policy only becomes strict once nothing inline is left, and
this is the proof. server.py's CSP now sends no `unsafe-inline` on any
directive; this test walks every view of the app shell and the public share
page and asserts the browser's own console never once reports a violation —
"Content Security Policy" or "Refused to" is Chromium's fixed vocabulary for
exactly that, on any directive. It must fail on the round-1 client, whose
policy allowed inline script and style outright and whose markup was full of
onclick/onchange/style attributes that no strict policy would grant.
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
    server = _LiveServer(str(tmp_path / "web2-csp.db"))
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _is_violation(text: str) -> bool:
    return "Content Security Policy" in text or "Refused to" in text


def test_no_csp_violation_walking_every_view(live_server: _LiveServer) -> None:
    violations: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page: Page = browser.new_page()
        page.on(
            "console", lambda msg: violations.append(msg.text) if _is_violation(msg.text) else None
        )

        _enter_demo_workspace(page, live_server.base_url)

        # channel (conversation, the landing view)
        page.wait_for_selector("#messages .msg", state="visible", timeout=10000)

        # thread
        # The reply action only reveals on :hover/:focus-within, a CSS transition
        # Playwright's own hover simulation cannot be relied on to have finished
        # before a click (see test_web_client.py's test_thread_open_and_reply);
        # dispatching the click straight from the DOM sidesteps that race. The
        # last DOM child of #messages is not reliably the last chat message: a
        # membership system notice ("X was invited") can arrive over the socket
        # after the snapshot render and land after every human message, with no
        # openThread action of its own (see seed_demo_workspace's invites).
        # Selecting among ".msg.human" and taking the last one is what "the
        # last message" means here.
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

        # branch
        page.click("[data-action=openContext][data-action-arg=branch]")
        page.wait_for_selector("#view-branch.active", state="visible", timeout=10000)

        # brief (the artifact reader the seeded Decision Brief opens into)
        page.click("[data-action=openContext][data-action-arg=artifacts]")
        page.wait_for_selector("#view-artifact.active", state="visible", timeout=10000)

        # agents and tasks (records subsections under members)
        page.click("[aria-label='Workspace details']")
        page.click(".records summary")
        page.wait_for_selector("#agents-panel .card", state="visible", timeout=10000)
        page.wait_for_selector("#tasks-panel", state="visible", timeout=10000)

        # members
        page.click("[data-action=openContext][data-action-arg=members]")
        page.wait_for_selector("#members-cards .card", state="visible", timeout=10000)

        # search
        page.click("[aria-label='Search channel']")
        page.fill("#search-input", "Stripe")
        page.press("#search-input", "Enter")
        page.wait_for_function(
            "() => document.querySelectorAll('#search-results .search-hit').length > 0",
            timeout=10000,
        )

        # settings-equivalent controls: theme toggle and identity, always in
        # the sidebar footer rather than a separate view.
        page.click("#theme-toggle")
        page.wait_for_function(
            "() => document.documentElement.dataset.theme === 'dark'", timeout=5000
        )
        page.click("#theme-toggle")

        # the share page, from the same brief opened above
        artifact_id = page.evaluate(
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
        share_path = page.evaluate(
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
        share_page = browser.new_page()
        share_page.on(
            "console", lambda msg: violations.append(msg.text) if _is_violation(msg.text) else None
        )
        share_page.goto(live_server.base_url + share_path)
        share_page.wait_for_selector(".doc", state="visible", timeout=10000)

        browser.close()

    assert violations == []
