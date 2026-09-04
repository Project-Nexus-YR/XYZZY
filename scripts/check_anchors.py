"""Resolves every `blob/main/<path>#L<n>` link in site/index.html and
docs/readme-trace.md, and every `` `path:line` `` citation in site/trace.md,
against the working tree. Where a trace citation is immediately followed by
a quoted symbol (`` `path:line`: `symbol` ``), also verifies the cited
line actually contains that symbol, so a citation cannot merely point at a
line that exists while quoting a different one.

Exits nonzero when a citation's target file is missing, the cited line is
blank, or a quoted symbol is not found on the line it is cited against. Run
from the repo root:

    python scripts/check_anchors.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ANCHOR_RE = re.compile(r"blob/main/([^\s\"'<>#]+)#L(\d+)")
CHECKED_FILES = ("site/index.html", "docs/readme-trace.md")

# A trace citation names a real, checked-in file by its extension so it
# cannot mistake a version number or a URL fragment for a path: a backtick
# quoted `path/to/file.ext:123` or `path/to/file.ext:123-456`, where only the
# range's start line is verified (the end line documents where a span ends,
# not a second citation).
TRACE_CITATION_RE = re.compile(r"`([\w./-]+\.(?:py|sql|toml|md|html|txt|yml)):(\d+)(?:-\d+)?`")

# The same citation, immediately followed by a colon and a backtick-quoted
# symbol (the shape the trace uses when it claims a specific line reads a
# specific thing, e.g. `` `pyproject.toml:71-74`: `[tool.mypy]` ``). Only the
# range's first line is checked against the quote: that is the line the
# citation is naming, the rest of the range is context.
TRACE_QUOTED_CITATION_RE = re.compile(
    r"`([\w./-]+\.(?:py|sql|toml|md|html|txt|yml)):(\d+)(?:-\d+)?`:\s*`([^`]+)`"
)
TRACE_FILE = "site/trace.md"


def find_anchors(text: str) -> list[tuple[str, int]]:
    return [(path, int(line)) for path, line in ANCHOR_RE.findall(text)]


def find_trace_citations(text: str) -> list[tuple[str, int]]:
    return [(path, int(line)) for path, line in TRACE_CITATION_RE.findall(text)]


def find_quoted_trace_citations(text: str) -> list[tuple[str, int, str]]:
    return [
        (path, int(line), symbol) for path, line, symbol in TRACE_QUOTED_CITATION_RE.findall(text)
    ]


def _read_lines(
    root: Path, rel: str, target_path: str, line_no: int, failures: list[str]
) -> str | None:
    target = root / target_path
    if not target.exists():
        failures.append(f"{rel}: {target_path}:{line_no} -> file does not exist")
        return None
    lines = target.read_text(encoding="utf-8").splitlines()
    if line_no < 1 or line_no > len(lines):
        failures.append(f"{rel}: {target_path}:{line_no} -> line out of range")
        return None
    return lines[line_no - 1]


def _check(root: Path, rel: str, target_path: str, line_no: int, failures: list[str]) -> None:
    line = _read_lines(root, rel, target_path, line_no, failures)
    if line is not None and not line.strip():
        failures.append(f"{rel}: {target_path}:{line_no} -> line is blank")


def _check_quoted(
    root: Path, rel: str, target_path: str, line_no: int, symbol: str, failures: list[str]
) -> None:
    line = _read_lines(root, rel, target_path, line_no, failures)
    if line is not None and symbol not in line:
        failures.append(
            f"{rel}: {target_path}:{line_no} -> does not contain the quoted {symbol!r} "
            f"(actual line: {line.strip()!r})"
        )


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
            _check(root, rel, target_path, line_no, failures)

    trace_source = root / TRACE_FILE
    if not trace_source.exists():
        failures.append(f"{TRACE_FILE}: source file is missing")
    else:
        trace_text = trace_source.read_text(encoding="utf-8")
        for target_path, line_no in find_trace_citations(trace_text):
            checked += 1
            _check(root, TRACE_FILE, target_path, line_no, failures)
        for target_path, line_no, symbol in find_quoted_trace_citations(trace_text):
            checked += 1
            _check_quoted(root, TRACE_FILE, target_path, line_no, symbol, failures)

    if failures:
        for failure in failures:
            print(failure)
        print(f"{len(failures)} of {checked} anchors failed")
        return 1

    print(f"{checked} anchors resolved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
