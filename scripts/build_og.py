"""Reproduces site/assets/og.png (1200x630) and site/assets/icon-180.png from
the tokens in DESIGN.md: paper, ink, forest green and gold.

Requires Playwright with Chromium (pip install playwright; playwright install
chromium). Run from the repo root:

    python scripts/build_og.py
"""

from __future__ import annotations

from playwright.sync_api import sync_playwright

CHECK = (
    "<svg viewBox='0 0 32 32' xmlns='http://www.w3.org/2000/svg' width='{s}' height='{s}'>"
    "<rect width='32' height='32' rx='4' fill='#1B4529'/>"
    "<path d='M9 16.5l5 5 9-10' fill='none' stroke='#E2B54E' stroke-width='3.2'"
    " stroke-linecap='round' stroke-linejoin='round'/></svg>"
)

OG = """<!doctype html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,500;1,500&family=IBM+Plex+Mono:wght@400&family=IBM+Plex+Sans:wght@400;600&display=swap" rel="stylesheet">
<style>
  html,body{{margin:0;width:1200px;height:630px;background:#F6F2EB;color:#262018;
    font-family:"IBM Plex Sans",sans-serif;letter-spacing:-0.013em}}
  .card{{position:relative;width:1200px;height:630px;padding:72px 88px;box-sizing:border-box}}
  .mark{{display:flex;align-items:center;gap:16px;font-family:"Cormorant Garamond",Georgia,serif;font-weight:600;font-size:34px;letter-spacing:.02em}}
  h1{{font-family:"Cormorant Garamond",Georgia,serif;font-weight:500;font-size:88px;line-height:0.98;letter-spacing:-0.012em;margin:64px 0 24px;max-width:20ch}}
  h1 em{{font-style:italic;color:#6D5100}}
  p{{font-size:26px;line-height:1.4;color:#5C5548;margin:0;max-width:44ch}}
  .foot{{position:absolute;left:88px;bottom:52px;font-family:"IBM Plex Mono",monospace;font-size:20px;color:#6D5100}}
</style></head><body><div class="card">
<div class="mark">{check} XYZZY</div>
<h1>Decide with AI, together, and <em>keep</em> the receipts.</h1>
<p>Parallel specialists, a verdict on every output, a brief with every claim traced.</p>
<div class="foot">self-hosted · one Python process · Apache-2.0</div>
</div></body></html>"""

ICON = (
    "<!doctype html><html><body style='margin:0;background:#1B4529'>"
    + CHECK.format(s=180)
    + "</body></html>"
)


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 630})
        page.set_content(OG.format(check=CHECK.format(s=40)), wait_until="networkidle")
        page.wait_for_timeout(800)
        page.screenshot(path="site/assets/og.png")
        print("site/assets/og.png")
        icon = browser.new_page(viewport={"width": 180, "height": 180})
        icon.set_content(ICON)
        icon.screenshot(path="site/assets/icon-180.png")
        print("site/assets/icon-180.png")
        browser.close()


if __name__ == "__main__":
    main()
