"""Reproduces the README hero screenshots under site/assets/:

  screenshot-hero-dark.png / -light.png                (1200x830, 1x)
  screenshot-hero-dark@2x.png / -light@2x.png           (2400x1660, 2x)
  screenshot-hero-mobile-dark.png / -mobile-light.png   (720x648, 1x)
  screenshot-hero-mobile-dark@2x.png / -mobile-light@2x.png (1440x1296, 2x)
  branch-card-dark.png / -light.png (+@2x)                 the floating layer, cropped
  view-branch-*.png, view-brief-*.png (+@2x)              the other two tabs of the stack

Requires a running XYZZY demo server (the "xyzzy-demo" entry in
.claude/launch.json, or `XYZZY_DEMO=1 python -m multiplayer.server`) so the
entry card's "Explore the demo workspace" link enters the seeded room, and
Playwright with its Chromium browser installed:

    pip install playwright pillow
    playwright install chromium

Run from the repo root:

    python scripts/capture_hero.py [base_url]
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

OUT_DIR = "site/assets"
BASE_URL = "http://localhost:8010"

# (name, css_width, css_height, color_scheme, device_scale_factor)
SHOTS = [
    ("screenshot-hero-dark", 1200, 830, "dark", 1),
    ("screenshot-hero-light", 1200, 830, "light", 1),
    ("screenshot-hero-dark@2x", 1200, 830, "dark", 2),
    ("screenshot-hero-light@2x", 1200, 830, "light", 2),
    ("screenshot-hero-mobile-dark", 720, 648, "dark", 1),
    ("screenshot-hero-mobile-light", 720, 648, "light", 1),
    ("screenshot-hero-mobile-dark@2x", 720, 648, "dark", 2),
    ("screenshot-hero-mobile-light@2x", 720, 648, "light", 2),
]


def main() -> None:
    global BASE_URL
    BASE_URL = base_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8010"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, width, height, scheme, scale in SHOTS:
            context = browser.new_context(
                viewport={"width": width, "height": height},
                color_scheme=scheme,
                device_scale_factor=scale,
            )
            page = context.new_page()
            page.goto(base_url, wait_until="networkidle")
            # The demo opens on its entry card; one click enters the seeded
            # workspace, which is what the hero shows.
            page.locator("#setup-demo-button").click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(2500)  # let the seeded room render fully
            out_path = f"{OUT_DIR}/{name}.png"
            page.screenshot(path=out_path)
            print(out_path)
            context.close()
        capture_views(browser)
        browser.close()

    crop_branch_card()


# The landing page's tabbed stack shows three real views of the same seeded
# room: the channel (the hero capture above), the branch view with the
# specialist outputs side by side, and the published Decision Brief.
VIEWS = [
    ("view-branch", "button.branch-nav"),  # the branch under AI WORK in the sidebar
    ("view-brief", "#nav-artifacts"),  # Artifacts opens the published brief
]


def capture_views(browser) -> None:
    for name, selector in VIEWS:
        for scheme in ("light", "dark"):
            for scale in (1, 2):
                context = browser.new_context(
                    viewport={"width": 1200, "height": 830},
                    color_scheme=scheme,
                    device_scale_factor=scale,
                )
                page = context.new_page()
                page.goto(BASE_URL, wait_until="networkidle")
                page.locator("#setup-demo-button").click()
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(1500)
                page.locator(selector).first.click()
                page.wait_for_timeout(1500)
                suffix = "@2x" if scale == 2 else ""
                out_path = f"{OUT_DIR}/{name}-{scheme}{suffix}.png"
                page.screenshot(path=out_path)
                print(out_path)
                context.close()


# The floating layer on the landing page is a real fragment of the desktop
# capture: the branch card row (title, agent count, run state, verdict
# counts). Cropped from the 2x file so it stays sharp at any display density.
BRANCH_CARD_BOX_2X = (568, 1236, 2012, 1364)


def crop_branch_card() -> None:
    from PIL import Image

    for theme in ("light", "dark"):
        with Image.open(f"{OUT_DIR}/screenshot-hero-{theme}@2x.png") as im:
            card = im.crop(BRANCH_CARD_BOX_2X)
            card.save(f"{OUT_DIR}/branch-card-{theme}@2x.png")
            card.resize((card.width // 2, card.height // 2), Image.LANCZOS).save(
                f"{OUT_DIR}/branch-card-{theme}.png"
            )
            print(f"{OUT_DIR}/branch-card-{theme}.png")


if __name__ == "__main__":
    main()
