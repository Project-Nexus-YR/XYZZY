"""Borrowed from web2: the ``message.created`` room event's payload must carry
the message's own ``created_at`` (ISO format) beside the fields it already
has, so a client can render a live message identical to its snapshot row
instead of stamping it with whatever time the event happened to broadcast.
"""

from __future__ import annotations

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import MessageRole
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner"}))
    await svc.initialize()
    yield svc
    await db.close()


@pytest.mark.asyncio
async def test_message_created_event_payload_carries_the_messages_own_created_at(
    service: MultiplayerService,
) -> None:
    svc = service
    org = await svc.create_organization("Event org", "event-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")

    message = await svc.send_message(room.room_id, MessageRole.HUMAN, "owner", "hello there")

    events = await svc.get_room_events(room.room_id)
    created = next(e for e in events if e.event_type.value == "message.created")
    assert created.payload["message_id"] == message.message_id
    assert created.payload["created_at"] == message.created_at.isoformat()
