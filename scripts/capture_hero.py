"""Reproduces the README hero screenshots under site/assets/:

  screenshot-hero-dark.png / -light.png                (1200x830, 1x)
  screenshot-hero-dark@2x.png / -light@2x.png           (2400x1660, 2x)
  screenshot-hero-mobile-dark.png / -mobile-light.png   (720x648, 1x)
  screenshot-hero-mobile-dark@2x.png / -mobile-light@2x.png (1440x1296, 2x)

Requires a running XYZZY demo server (the "xyzzy-demo" entry in
.claude/launch.json, or `XYZZY_DEMO=1 python -m multiplayer.server`) so the
page opens straight into a seeded workspace with no sign-in step, and
Playwright with its Chromium browser installed:

    pip install playwright
    playwright install chromium

Run from the repo root:

    python scripts/capture_hero.py [base_url]
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

OUT_DIR = "site/assets"

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
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8010"

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
            page.wait_for_timeout(1500)  # let the demo seed render fully
            out_path = f"{OUT_DIR}/{name}.png"
            page.screenshot(path=out_path)
            print(out_path)
            context.close()
        browser.close()


if __name__ == "__main__":
    main()
