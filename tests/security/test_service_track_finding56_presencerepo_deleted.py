"""Finding 56: PresenceRepo, a zero caller no-op, is deleted along with its wiring.

It presented the same set / get_room_presence surface as a working repository
while silently discarding writes and returning nothing on reads. The real
presence implementation is multiplayer.services.presence.PresenceService,
already reachable as svc.presence; this proves the decoy in Repos is gone
rather than sitting beside the name the next caller would reach for.
"""

from __future__ import annotations

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.db.repositories import Repos
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.presence import PresenceService
from multiplayer.services.service import MultiplayerService


@pytest.mark.asyncio
async def test_repos_has_no_presence_attribute() -> None:
    db = Database(":memory:")
    await db.connect()
    repos = Repos(db)
    assert not hasattr(repos, "presence")
    await db.close()


def test_repos_module_has_no_presencerepo_class() -> None:
    import multiplayer.db.repositories as repositories_module

    assert not hasattr(repositories_module, "PresenceRepo")


@pytest.mark.asyncio
async def test_svc_presence_is_the_real_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub())
    assert isinstance(svc.presence, PresenceService)
    assert not hasattr(svc.repos, "presence")
    await db.close()
