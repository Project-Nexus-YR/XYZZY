"""Removal is the gate every launch door reads, so the row it reads must hold.

Migration 020 made ``agent_room_memberships.removed_at`` decide whether an agent may
open a run. Migration 021 gave ``agent_identities`` the same permanence, for the same
reason, and left this table without any: a plain ``UPDATE ... SET removed_at = NULL``
un-removed a removed agent and put it back on the roster with its handle. No service
path reaches that, so these guards are defence in depth on the table the whole gate
rests on.

``start_agent_session`` is the exception that was reachable. It gated on
``agent_instances.room_id``, which records where an agent was created rather than
whether it is still there, so a removed agent still got a durable session row and a
``session.started`` room event announcing that it had begun work.
"""

from __future__ import annotations

from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import AgentLaunchRefused, MultiplayerService


class _FinishingProvider:
    async def acomplete(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, response_schema
        return {"action": "finish", "output": {"content": "assessed"}}


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner"}))
    await svc.initialize()
    svc.nexus = NexusAgentBridge(model_provider=_FinishingProvider())
    yield svc
    await db.close()


async def _room_with_agents(svc: MultiplayerService) -> tuple[str, str, str]:
    """A room, an agent that has been removed, and one that is still a member."""
    org = await svc.create_organization("Permanence org", "permanence-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    templates = await svc.list_agent_templates()
    template_id = next(t.template_id for t in templates if t.name == "Researcher")
    removed = await svc.spawn_agent(
        room.room_id, template_id, name="Researcher", requested_by="owner"
    )
    live = await svc.spawn_agent(room.room_id, template_id, name="Second", requested_by="owner")
    await svc.remove_agent_from_room(removed.agent_id, room.room_id, "owner")
    return room.room_id, removed.agent_id, live.agent_id


@pytest.mark.asyncio
async def test_a_removal_cannot_be_reversed_in_place(service: MultiplayerService) -> None:
    svc = service
    room_id, removed_id, _ = await _room_with_agents(svc)

    with pytest.raises(Exception, match="may not be reversed"):
        await svc.db.execute(
            "UPDATE agent_room_memberships SET removed_at = NULL "
            "WHERE agent_id = ? AND room_id = ?",
            (removed_id, room_id),
        )

    row = await svc.db.fetch_one(
        "SELECT removed_at FROM agent_room_memberships WHERE agent_id = ? AND room_id = ?",
        (removed_id, room_id),
    )
    assert row is not None and row["removed_at"] is not None


@pytest.mark.asyncio
async def test_a_removal_cannot_be_restamped(service: MultiplayerService) -> None:
    """A backdated removal is a different claim about when the agent left."""
    svc = service
    room_id, removed_id, _ = await _room_with_agents(svc)

    with pytest.raises(Exception, match="may not be reversed"):
        await svc.db.execute(
            "UPDATE agent_room_memberships SET removed_at = '2000-01-01T00:00:00Z' "
            "WHERE agent_id = ? AND room_id = ?",
            (removed_id, room_id),
        )


@pytest.mark.asyncio
async def test_a_removal_cannot_be_deleted(service: MultiplayerService) -> None:
    svc = service
    room_id, removed_id, _ = await _room_with_agents(svc)

    with pytest.raises(Exception, match="may not be deleted"):
        await svc.db.execute(
            "DELETE FROM agent_room_memberships WHERE agent_id = ? AND room_id = ?",
            (removed_id, room_id),
        )


@pytest.mark.asyncio
async def test_a_membership_cannot_be_repointed(service: MultiplayerService) -> None:
    """Re-pointing a live membership at another agent is a grant, not an edit."""
    svc = service
    room_id, removed_id, live_id = await _room_with_agents(svc)

    with pytest.raises(Exception, match="may not be re-pointed"):
        await svc.db.execute(
            "UPDATE agent_room_memberships SET agent_id = ? WHERE agent_id = ? AND room_id = ?",
            (removed_id, live_id, room_id),
        )


@pytest.mark.asyncio
async def test_a_removed_agent_cannot_open_a_session(service: MultiplayerService) -> None:
    """The last removal door: every other one reads membership, this one did not."""
    svc = service
    room_id, removed_id, _ = await _room_with_agents(svc)

    with pytest.raises(AgentLaunchRefused, match="not_a_member|is not in room"):
        await svc.start_agent_session(room_id, removed_id)

    sessions = await svc.db.fetch_all(
        "SELECT session_id FROM sessions WHERE agent_id = ?", (removed_id,)
    )
    assert sessions == []


@pytest.mark.asyncio
async def test_a_member_agent_can_still_open_a_session(service: MultiplayerService) -> None:
    """The gate must refuse the removed agent without refusing the room's own."""
    svc = service
    room_id, _, live_id = await _room_with_agents(svc)

    session = await svc.start_agent_session(room_id, live_id)
    assert session.agent_id == live_id
