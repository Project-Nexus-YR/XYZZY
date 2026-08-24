"""A removal cannot be reversed, and a return is a new membership beside it.

Migration 024 left out the duplicate-insert guard that 018 and 021 both carry, on the
reasoning that the rejoin path was an ``INSERT OR IGNORE`` a guard would turn into an
abort. That was wrong twice over, and both halves are pinned here.

It was exploitable. ``recursive_triggers`` is off, so the delete an ``INSERT OR
REPLACE`` performs never reached ``agent_memberships_reject_delete``: replacing the
``(agent_id, room_id)`` row put ``removed_at`` back to NULL, returned the agent to the
roster with its handle, and let it open a session after ``agent.left_room`` — the
false record 024 was written to prevent.

And the flow the omission was protecting did not exist. ``add_room_membership`` is
``INSERT OR IGNORE``, so it silently no-opped against a removed row, and no verb added
an agent back to a room at all. A removed agent could not rejoin by any path.

Migration 026 answers both: the table holds a history rather than a state, a rejoin is
a new row naming the departure it follows, and the departure row is never touched.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import DomainError
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.authorization import AuthorizationError
from multiplayer.server import create_app
from multiplayer.services.service import AgentLaunchRefused, MultiplayerService

TOKENS = {"owner-token": "owner", "sam-token": "sam"}
OWNER = {"Authorization": "Bearer owner-token"}
SAM = {"Authorization": "Bearer sam-token"}


class _FinishingProvider:
    async def acomplete(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, response_schema
        return {"action": "finish", "output": {"content": "assessed"}}


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner", "sam"}))
    await svc.initialize()
    svc.nexus = NexusAgentBridge(model_provider=_FinishingProvider())
    yield svc
    await db.close()


async def _room_with_removed_agent(svc: MultiplayerService) -> tuple[str, str]:
    """A room and an agent that has been removed from it."""
    org = await svc.create_organization("Rejoin org", "rejoin-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    await svc.invite_room_member(room.room_id, "sam", "editor", "owner")
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(
        room.room_id,
        next(t.template_id for t in templates if t.name == "Researcher"),
        name="Researcher",
        requested_by="owner",
    )
    await svc.remove_agent_from_room(agent.agent_id, room.room_id, "owner")
    return room.room_id, agent.agent_id


async def _memberships(svc: MultiplayerService, agent_id: str) -> list[dict[str, Any]]:
    return await svc.db.fetch_all(
        "SELECT * FROM agent_room_memberships WHERE agent_id = ? ORDER BY joined_at, rowid",
        (agent_id,),
    )


# ── The reversal the guard closes ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_insert_or_replace_cannot_put_a_removed_agent_back(
    service: MultiplayerService,
) -> None:
    """The exploit 024 left open: REPLACE deletes without firing the delete guard."""
    svc = service
    room_id, agent_id = await _room_with_removed_agent(svc)

    with pytest.raises(Exception, match="rejoins through a new membership"):
        await svc.db.execute(
            "INSERT OR REPLACE INTO agent_room_memberships("
            "membership_id, agent_id, room_id, joined_at) VALUES (?, ?, ?, ?)",
            ("member_forged", agent_id, room_id, "2030-01-01T00:00:00+00:00"),
        )

    # The departure is untouched and the agent is still off the roster.
    rows = await _memberships(svc, agent_id)
    assert len(rows) == 1
    assert rows[0]["removed_at"] is not None
    assert await svc.list_room_agents(room_id) == []
    with pytest.raises(AgentLaunchRefused, match="not_a_member|is not in room"):
        await svc.start_agent_session(room_id, agent_id)


@pytest.mark.asyncio
async def test_a_forged_rejoin_must_name_a_real_departure(
    service: MultiplayerService,
) -> None:
    """Naming a departure that is not this agent's is not a rejoin."""
    svc = service
    room_id, agent_id = await _room_with_removed_agent(svc)

    with pytest.raises(Exception, match="names the departure it follows"):
        await svc.db.execute(
            "INSERT INTO agent_room_memberships("
            "membership_id, agent_id, room_id, joined_at, rejoined_from_membership_id) "
            "VALUES (?, ?, ?, ?, ?)",
            ("member_forged", agent_id, room_id, "2030-01-01T00:00:00+00:00", "member_nonexistent"),
        )
    assert len(await _memberships(svc, agent_id)) == 1


@pytest.mark.asyncio
async def test_a_second_live_membership_cannot_be_inserted(
    service: MultiplayerService,
) -> None:
    """An agent already in the room is not rejoinable, by SQL or by verb."""
    svc = service
    org = await svc.create_organization("Live org", "live-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(
        room.room_id,
        next(t.template_id for t in templates if t.name == "Researcher"),
        name="Researcher",
        requested_by="owner",
    )

    with pytest.raises(Exception, match="rejoins through a new membership"):
        await svc.db.execute(
            "INSERT INTO agent_room_memberships("
            "membership_id, agent_id, room_id, joined_at) VALUES (?, ?, ?, ?)",
            ("member_second", agent.agent_id, room.room_id, "2030-01-01T00:00:00+00:00"),
        )
    with pytest.raises(DomainError, match="already in room"):
        await svc.rejoin_agent_to_room(agent.agent_id, room.room_id, "owner")


# ── The rejoin that now works ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_rejoin_writes_a_new_membership_and_keeps_the_departure(
    service: MultiplayerService,
) -> None:
    """The whole defect's other half: rejoining works, and leaving is still recorded."""
    svc = service
    room_id, agent_id = await _room_with_removed_agent(svc)
    departure = (await _memberships(svc, agent_id))[0]

    membership = await svc.rejoin_agent_to_room(agent_id, room_id, "owner")

    rows = await _memberships(svc, agent_id)
    assert len(rows) == 2, rows
    # The departure row is exactly as it was: same id, same removal timestamp.
    assert rows[0]["membership_id"] == departure["membership_id"]
    assert rows[0]["removed_at"] == departure["removed_at"]
    # And the new one is live, and says which departure it follows.
    assert rows[1]["membership_id"] == membership.membership_id
    assert rows[1]["removed_at"] is None
    assert rows[1]["rejoined_from_membership_id"] == departure["membership_id"]

    # The agent is a member again by every door that reads membership.
    assert [a.agent_id for a in await svc.list_room_agents(room_id)] == [agent_id]
    assert await svc.repos.agents.has_room_membership(agent_id, room_id)
    session = await svc.start_agent_session(room_id, agent_id)
    assert session.agent_id == agent_id

    # And the room's record shows a departure and a return, in that order.
    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert types.index("agent.left_room") < types.index("agent.rejoined_room")
    rejoined = next(
        event
        for event in await svc.get_room_events(room_id)
        if event.event_type.value == "agent.rejoined_room"
    )
    assert rejoined.payload["agent_id"] == agent_id
    assert rejoined.payload["rejoined_from_membership_id"] == departure["membership_id"]
    assert rejoined.payload["rejoined_by"] == "owner"
    # The handle went back to the room with the membership; it comes back too.
    assert rejoined.payload["handle"]


@pytest.mark.asyncio
async def test_a_rejoined_agent_can_leave_again_and_return_again(
    service: MultiplayerService,
) -> None:
    """Each spell is its own row, so the history is a list rather than a flag."""
    svc = service
    room_id, agent_id = await _room_with_removed_agent(svc)
    await svc.rejoin_agent_to_room(agent_id, room_id, "owner")
    await svc.remove_agent_from_room(agent_id, room_id, "owner")
    await svc.rejoin_agent_to_room(agent_id, room_id, "owner")

    rows = await _memberships(svc, agent_id)
    assert len(rows) == 3
    assert [row["removed_at"] is None for row in rows] == [False, False, True]
    assert rows[2]["rejoined_from_membership_id"] == rows[1]["membership_id"]


@pytest.mark.asyncio
async def test_rejoining_needs_administer(service: MultiplayerService) -> None:
    """The grant removal takes, because a rejoin is the membership change it reverses."""
    svc = service
    room_id, agent_id = await _room_with_removed_agent(svc)

    with pytest.raises(AuthorizationError):
        await svc.rejoin_agent_to_room(agent_id, room_id, "sam", require_member=True)
    assert len(await _memberships(svc, agent_id)) == 1
    assert await svc.list_room_agents(room_id) == []


# ── The same verb, over HTTP ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_rejoin_endpoint_needs_administer_and_records_a_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    app = create_app(":memory:", auth_tokens=TOKENS)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            bootstrap = (
                await client.post(
                    "/api/v1/me/bootstrap",
                    headers=OWNER,
                    json={"display_name": "Owner", "room_name": "Decision"},
                )
            ).json()
            room_id = bootstrap["room"]["room_id"]
            assert (
                await client.post(
                    f"/api/v1/rooms/{room_id}/members/invitations",
                    headers=OWNER,
                    json={"user_id": "sam", "role": "editor"},
                )
            ).status_code == 200
            templates = (await client.get("/api/v1/agent-templates", headers=OWNER)).json()
            researcher = next(t for t in templates if t["name"] == "Researcher")
            agent_id = (
                await client.post(
                    f"/api/v1/rooms/{room_id}/agents",
                    headers=OWNER,
                    json={"template_id": researcher["template_id"]},
                )
            ).json()["agent_id"]
            assert (
                await client.delete(f"/api/v1/rooms/{room_id}/agents/{agent_id}", headers=OWNER)
            ).status_code == 200
            assert (await client.get(f"/api/v1/rooms/{room_id}/agents", headers=OWNER)).json() == []

            # An editor may not put an agent back any more than they may take it out.
            refused = await client.post(
                f"/api/v1/rooms/{room_id}/agents/{agent_id}/memberships", headers=SAM
            )
            assert refused.status_code == 403, refused.text

            rejoined = await client.post(
                f"/api/v1/rooms/{room_id}/agents/{agent_id}/memberships", headers=OWNER
            )
            assert rejoined.status_code == 200, rejoined.text
            assert rejoined.json()["rejoined_from_membership_id"]

            roster = (await client.get(f"/api/v1/rooms/{room_id}/agents", headers=OWNER)).json()
            assert [a["agent_id"] for a in roster] == [agent_id]
            types = [
                event["event_type"]
                for event in (
                    await client.get(f"/api/v1/rooms/{room_id}/events", headers=OWNER)
                ).json()
            ]
            assert types.index("agent.left_room") < types.index("agent.rejoined_room")
