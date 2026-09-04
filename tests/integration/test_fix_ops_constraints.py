"""constraints.txt must pin the whole closure pyproject can install.

Before this test, playwright, greenlet and pyee (the e2e extra) and pillow
(the capture extra) were absent from constraints.txt, so the slowest, most
security-sensitive part of the CI gate resolved to PyPI's release of the day
rather than to a reviewed pin. This asserts every name pyproject declares,
directly or through an optional-dependencies group, has a pin, plus the four
extra-only packages the direct-name check cannot see because they are
transitive.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _pinned_names() -> set[str]:
    text = (ROOT / "constraints.txt").read_text(encoding="utf-8")
    names = re.findall(r"(?m)^([A-Za-z0-9][A-Za-z0-9._-]*)==", text)
    return {_normalize(name) for name in names}


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement_name(spec: str) -> str:
    return re.split(r"[\[<>=!~; ]", spec, maxsplit=1)[0]


def test_every_declared_dependency_is_pinned() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    declared = list(project["dependencies"])
    for group in project["optional-dependencies"].values():
        declared.extend(group)

    pinned = _pinned_names()
    # ast-serialize has no cp311 wheel and is deliberately left unpinned; see
    # the comment at the top of constraints.txt.
    exempt = {"ast-serialize"}
    missing = {
        _normalize(_requirement_name(spec))
        for spec in declared
        if _normalize(_requirement_name(spec)) not in pinned
    } - exempt
    assert not missing, f"pyproject names with no pin in constraints.txt: {missing}"


def test_e2e_and_capture_transitive_closure_is_pinned() -> None:
    # playwright pulls in greenlet and pyee; neither is named in pyproject,
    # so the direct-name check above cannot see them.
    pinned = _pinned_names()
    for name in ("playwright", "greenlet", "pyee", "pillow"):
        assert name in pinned, f"{name} (e2e/capture closure) has no pin in constraints.txt"
