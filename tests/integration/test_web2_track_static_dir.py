"""Round 1's finding 62: a non-editable install used to 404 the UI.

server.py resolved ``web/`` relative to the repository layout, which only
existed for an editable checkout; a normal ``pip install .`` copies the
package into site-packages with no repository around it, so that path
resolved to nothing and the client never served. The fix is for the client
to live inside the package as package data (``src/multiplayer/web``) and be
found through ``importlib.resources``, which works the same way regardless
of how the package got onto ``sys.path``. This test installs nothing: it
only asserts the resolved directory is real and holds the three files the
client needs, exercising the exact function ``server.py`` calls at startup.
"""

from __future__ import annotations

from pathlib import Path

import multiplayer
from multiplayer.server import resolve_static_dir


def test_resolved_static_dir_holds_the_client_files() -> None:
    static_dir = resolve_static_dir()

    assert static_dir.is_dir(), static_dir
    assert (static_dir / "index.html").is_file()
    assert (static_dir / "app.css").is_file()
    assert (static_dir / "js" / "app.js").is_file()
    assert (static_dir / "share.css").is_file()


def test_resolved_static_dir_matches_the_package_layout_on_disk() -> None:
    """The same lookup importlib.resources performs, done by hand from the
    package's own ``__file__``, so a change to server.py's resolver cannot
    silently start pointing somewhere importlib.resources would not agree
    with (a stale hardcoded path, a typo in the package-data glob)."""
    assert multiplayer.__file__ is not None
    expected = Path(multiplayer.__file__).parent / "web"
    assert resolve_static_dir() == expected
