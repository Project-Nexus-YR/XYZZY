"""Shared loader for tests that assert on the web client's own source text.

The client used to be one file, so a test could read ``web/index.html`` and
grep it for a snippet of markup, CSS or JavaScript. Round 2 split it into
``index.html`` (markup only), ``app.css`` and the ES modules under
``web/js/``, and moved the whole directory to ``src/multiplayer/web`` so it
travels with the package instead of needing an editable install to be found
(see ``multiplayer.server.resolve_static_dir``). A test that still wants "the
client's source, concatenated" calls :func:`client_source_text` instead of
reading ``index.html`` alone. The concatenation order does not matter to
callers: every existing assertion is a substring check, not a position check.
"""

from __future__ import annotations

from multiplayer.server import resolve_static_dir

_WEB_DIR = resolve_static_dir()


def client_source_text() -> str:
    """The full text of the app shell: markup, styles and every JS module.

    Equivalent, for substring assertions, to the old single-file
    ``web/index.html``: every string that used to appear somewhere in that
    file still appears somewhere in this concatenation.
    """
    parts = [(_WEB_DIR / "index.html").read_text(encoding="utf-8")]
    parts.append((_WEB_DIR / "app.css").read_text(encoding="utf-8"))
    for module_path in sorted((_WEB_DIR / "js").glob("*.js")):
        parts.append(module_path.read_text(encoding="utf-8"))
    return "\n".join(parts)
