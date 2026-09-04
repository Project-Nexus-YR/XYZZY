"""Captures the seven scenes that scripts/build_demo_gif.py assembles into
site/assets/demo.gif, in the order the README's alt text lists them:
entry card, General channel, an open thread, the branch view, the top of the
Decision Brief, its evidence chain, and an Ask Meta answer.

Needs a running XYZZY demo server on port 8010
(`XYZZY_DEMO=1 XYZZY_PORT=8010 python -m multiplayer.server`) and Playwright
with Chromium. Run from the repo root:

    python scripts/capture_scenes.py [base_url]
    python scripts/build_demo_gif.py build/scenes/*.png
"""

from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

OUT_DIR = "build/scenes"
SIZE = {"width": 960, "height": 600}


def main() -> None:
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8010"
    os.makedirs(OUT_DIR, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_context(viewport=SIZE, color_scheme="light").new_page()
        page.goto(base_url, wait_until="networkidle")
        page.wait_for_timeout(800)
        shot(page, "01-entry")

        page.locator("#setup-demo-button").click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1500)
        shot(page, "02-channel")

        dom_click(page, "button.thread-open")
        page.wait_for_timeout(1000)
        shot(page, "03-thread")
        close_thread(page)

        page.locator("button.branch-nav").first.click()
        page.wait_for_timeout(1400)
        shot(page, "04-branch")

        page.locator("#nav-artifacts").click()
        page.wait_for_timeout(1400)
        shot(page, "05-brief")

        dom_click(page, "button:has-text('Inspect claim provenance')")
        page.wait_for_timeout(1200)
        page.evaluate("document.querySelector('#ontology-tree')?.scrollIntoView({block: 'start'})")
        page.wait_for_timeout(600)
        shot(page, "06-evidence")

        page.locator("#nav-meta").click()
        page.wait_for_timeout(800)
        page.locator("#view-meta button", has_text="Where things stand").first.click()
        page.wait_for_timeout(2500)
        shot(page, "07-meta")
        browser.close()


def dom_click(page, selector: str) -> None:
    """Click through the DOM: these controls sit below the fold at 960x600,
    and the app scrolls its panes, not the page."""
    page.locator(selector).first.evaluate("el => el.click()")


def close_thread(page) -> None:
    """The thread pane stays open until closed; later scenes want the full view."""
    dom_click(page, "button[aria-label='Close context panel']")
    page.wait_for_timeout(500)


def shot(page, name: str) -> None:
    path = f"{OUT_DIR}/{name}.png"
    page.screenshot(path=path)
    print(path)


if __name__ == "__main__":
    main()
