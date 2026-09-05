"""Web audit #85: the client's ES modules used to carry ten two-module import
cycles, all fanning into socket.js: it imported every renderer (branch,
members, messages, ontology, rooms, shell, thread) to apply a snapshot and
live events, and each of those imported socket.js back for loadState,
connectWS or similar. Nothing failed: ES modules hoist, so the cycle only
ever worked by accident, and any reorder or a top-level use of an import
would have broken it silently.

The fix inverts the fan-in through a small registry (bus.js) instead of
moving code around: socket.js no longer imports any renderer, and the panels
no longer import socket.js: each calls the other through `emit`, wired up
by app.js at startup. This test parses the actual `from './x.js'` imports out
of the served modules and asserts the graph both fixes describe: acyclic,
and socket.js free of every panel/shell import.
"""

from __future__ import annotations

import re

from multiplayer.server import resolve_static_dir

_IMPORT_RE = re.compile(r"from\s+'\./([\w.-]+\.js)'")
_EMIT_RE = re.compile(r"\bemit\(\s*'(\w+)'")
_ON_RE = re.compile(r"\bon\(\s*'(\w+)'")

# The modules socket.js used to import directly to apply a snapshot or a live
# event: the fan-in the ruling names by name.
_FORMER_RENDERERS = {
    "branch.js",
    "members.js",
    "messages.js",
    "ontology.js",
    "rooms.js",
    "shell.js",
    "thread.js",
}


def _module_texts() -> dict[str, str]:
    js_dir = resolve_static_dir() / "js"
    texts = {p.name: p.read_text(encoding="utf-8") for p in sorted(js_dir.glob("*.js"))}
    assert texts, f"found no .js modules under {js_dir}"
    return texts


def _import_graph() -> dict[str, set[str]]:
    return {name: set(_IMPORT_RE.findall(text)) for name, text in _module_texts().items()}


def _topological_sort(graph: dict[str, set[str]]) -> list[str]:
    """Kahn's algorithm. Raises AssertionError, naming a stuck node, on a cycle."""
    remaining = {name: set(deps) for name, deps in graph.items()}
    ordered: list[str] = []
    while remaining:
        ready = [name for name, deps in remaining.items() if not deps]
        assert ready, (
            "import graph has a cycle: no module left with all its "
            f"dependencies already ordered; still stuck: {sorted(remaining)}"
        )
        for name in sorted(ready):
            ordered.append(name)
            del remaining[name]
        for deps in remaining.values():
            deps.difference_update(ready)
    return ordered


def test_import_graph_is_acyclic():
    graph = _import_graph()
    ordered = _topological_sort(graph)
    assert set(ordered) == set(graph)


def test_socket_imports_no_panel_module():
    graph = _import_graph()
    socket_imports = graph["socket.js"]
    fan_in = socket_imports & _FORMER_RENDERERS
    assert not fan_in, f"socket.js still imports former renderer(s): {sorted(fan_in)}"


def test_every_emitted_name_is_registered_and_vice_versa():
    """`emit('x', ...)` and `on('x', fn)` are matched only by the string 'x', not
    by the type checker or any import: a typo on either side would otherwise
    throw at runtime the first time a user's click reached it, instead of
    failing here."""
    emitted: set[str] = set()
    registered: set[str] = set()
    for text in _module_texts().values():
        emitted.update(_EMIT_RE.findall(text))
        registered.update(_ON_RE.findall(text))
    assert emitted, "found no emit('...') calls to check"
    assert registered, "found no on('...') registrations to check"
    assert emitted <= registered, f"emitted but never registered: {sorted(emitted - registered)}"
    assert registered <= emitted, f"registered but never emitted: {sorted(registered - emitted)}"
