"""Finding 54: NEXUS integration is opt-in through XYZZY_NEXUS_PATH.

Unset, the bridge must never walk out of the repository to a sibling checkout;
set, it inserts exactly that path, which is the only supported way to reach the
NEXUS branch at all.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module

_LEGACY_SIBLING_PATH = str(Path(bridge_module.__file__).resolve().parents[4] / "NEXUS" / "src")


@pytest.fixture(autouse=True)
def _restore_module_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reload the module back to its default (env unset) shape after each test."""
    yield
    monkeypatch.delenv("XYZZY_NEXUS_PATH", raising=False)
    importlib.reload(bridge_module)


def test_unset_env_var_never_inserts_the_sibling_checkout_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XYZZY_NEXUS_PATH", raising=False)
    sys.path[:] = [p for p in sys.path if p != _LEGACY_SIBLING_PATH]

    importlib.reload(bridge_module)

    assert bridge_module._HAS_NEXUS is False
    assert _LEGACY_SIBLING_PATH not in sys.path


def test_env_var_names_the_one_supported_opt_in_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    nexus_path = str(tmp_path)
    monkeypatch.setenv("XYZZY_NEXUS_PATH", nexus_path)

    try:
        importlib.reload(bridge_module)
        assert nexus_path in sys.path
        # tmp_path holds no nexus_runtime package, so the opt-in path is taken
        # but the import still fails, same as any environment without NEXUS installed.
        assert bridge_module._HAS_NEXUS is False
    finally:
        sys.path[:] = [p for p in sys.path if p != nexus_path]
