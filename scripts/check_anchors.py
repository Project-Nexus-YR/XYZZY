"""Resolves every `blob/main/<path>#L<n>` link in site/index.html and
docs/readme-trace.md against the working tree.

Exits nonzero when a link's target file is missing or the cited line is
blank. Run from the repo root:

    python scripts/check_anchors.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ANCHOR_RE = re.compile(r"blob/main/([^\s\"'<>#]+)#L(\d+)")
CHECKED_FILES = ("site/index.html", "docs/readme-trace.md")


def find_anchors(text: str) -> list[tuple[str, int]]:
    return [(path, int(line)) for path, line in ANCHOR_RE.findall(text)]


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    failures: list[str] = []
    checked = 0

    for rel in CHECKED_FILES:
        source = root / rel
        if not source.exists():
            failures.append(f"{rel}: source file is missing")
            continue
        for target_path, line_no in find_anchors(source.read_text(encoding="utf-8")):
            checked += 1
            target = root / target_path
            if not target.exists():
                failures.append(f"{rel}: {target_path}#L{line_no} -> file does not exist")
                continue
            lines = target.read_text(encoding="utf-8").splitlines()
            if line_no < 1 or line_no > len(lines):
                failures.append(f"{rel}: {target_path}#L{line_no} -> line out of range")
                continue
            if not lines[line_no - 1].strip():
                failures.append(f"{rel}: {target_path}#L{line_no} -> line is blank")

    if failures:
        for failure in failures:
            print(failure)
        print(f"{len(failures)} of {checked} anchors failed")
        return 1

    print(f"{checked} anchors resolved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
