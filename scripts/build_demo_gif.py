"""Assembles site/assets/demo.gif from seven still scenes of one walkthrough:
opening the demo, the General channel, an open thread, the branch view, the
top of a published Decision Brief, its evidence chain, and an Ask Meta
answer (see the demo.gif alt text in README.md for the exact scene list).

Capture the scenes first, one PNG per scene in capture order, for example
with scripts/capture_hero.py's Playwright pattern (screenshot to
scenes/01-entry.png, scenes/02-channel.png, ... against a running
"xyzzy-demo" server), then assemble:

    python scripts/build_demo_gif.py scenes/*.png
"""

from __future__ import annotations

import glob
import sys

from PIL import Image

OUT_PATH = "site/assets/demo.gif"
SIZE = (960, 600)
HOLD_MS = 3000  # 7 scenes x 3s = 21s, inside the 12-40s rubric range


def main() -> None:
    paths = sorted(sys.argv[1:] or glob.glob("scenes/*.png"))
    if not paths:
        raise SystemExit("no scene PNGs given (pass paths, or populate scenes/)")

    frames = [Image.open(p).convert("RGB").resize(SIZE) for p in paths]
    frames[0].save(
        OUT_PATH,
        save_all=True,
        append_images=frames[1:],
        duration=HOLD_MS,
        loop=0,
        optimize=True,
    )
    print(f"{OUT_PATH}: {len(frames)} frames, {SIZE[0]}x{SIZE[1]}")


if __name__ == "__main__":
    main()
