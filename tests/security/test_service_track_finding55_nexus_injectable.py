"""Finding 55: NexusAgentBridge is injectable, like presence_redis beside it.

``MultiplayerService.__init__`` used to hard wire a real ``NexusAgentBridge``,
so the 30-odd test files that needed a different one had to reach past the
constructor and overwrite the public attribute after construction. This
proves the bridge can be handed in like any other collaborator, with a
default that keeps every caller who does not pass one unaffected.
"""

from __future__ import annotations

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService


@pytest.fixture
async def db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    database = Database(":memory:")
    await database.connect()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_a_bridge_passed_to_the_constructor_is_used_as_is(db: Database) -> None:
    injected = NexusAgentBridge(db_path=":memory:")
    svc = MultiplayerService(db, RealtimeHub(), nexus=injected)
    assert svc.nexus is injected


@pytest.mark.asyncio
async def test_omitting_the_bridge_still_constructs_the_default(db: Database) -> None:
    svc = MultiplayerService(db, RealtimeHub())
    assert isinstance(svc.nexus, NexusAgentBridge)
