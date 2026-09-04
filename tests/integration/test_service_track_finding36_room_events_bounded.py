"""Finding 36 (borrowed from the api track): get_room_events and get_room_state
bound the events they build in memory, rather than assembling every event
past a sequence unconditionally before anything downstream gets to truncate
it. The api track bounded the route; this bounds the service call underneath
it, at both a passed-in ``limit`` and a hard ceiling no caller can raise past.
"""

from __future__ import annotations

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import MessageRole
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import _ROOM_EVENTS_MAX_LIMIT, MultiplayerService

OWNER = "owner"


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({OWNER}))
    await svc.initialize()
    yield svc
    await db.close()


async def _room_with_events(svc: MultiplayerService, count: int) -> str:
    org = await svc.create_organization("Finding36 org", "finding36-org", OWNER)
    workspace = await svc.create_workspace(org.org_id, "Main", "main", OWNER)
    room = await svc.create_room(workspace.workspace_id, "Decision", OWNER)
    for index in range(count):
        await svc.send_message(room.room_id, MessageRole.HUMAN, OWNER, f"msg{index}")
    return room.room_id


@pytest.mark.asyncio
async def test_get_room_events_stops_at_the_passed_limit(service: MultiplayerService) -> None:
    room_id = await _room_with_events(service, 10)

    events = await service.get_room_events(room_id, limit=5)

    assert len(events) == 5
    # The first 5 by sequence, not an arbitrary 5: a caller past the cap is
    # the one that pages on, from where this page actually stopped.
    assert [e.sequence for e in events] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_get_room_state_passes_its_event_limit_through(service: MultiplayerService) -> None:
    room_id = await _room_with_events(service, 10)

    state = await service.get_room_state(room_id, event_limit=4)

    assert len(state["events_since"]) == 4


@pytest.mark.asyncio
async def test_a_limit_above_the_ceiling_is_clamped_not_honoured(
    service: MultiplayerService,
) -> None:
    room_id = await _room_with_events(service, 10)

    events = await service.get_room_events(room_id, limit=10_000_000)

    assert len(events) == 11  # room.created plus every message, no more
    assert len(events) <= _ROOM_EVENTS_MAX_LIMIT
