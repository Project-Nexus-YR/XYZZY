"""Removing an agent from a room is a gate, not a stamp.

``DELETE /rooms/{id}/agents/{id}`` returned 200, settled the runs in flight and emitted
``agent.left_room`` — and then had no effect. ``remove_room_membership_in_transaction``
wrote ``agent_room_memberships.removed_at`` and nothing read it: the roster query
selected from ``agent_instances`` with no join, and the paths that open a run gated on
``agent.room_id``, capability, addressing, identity and harness but never on membership.
The agent stayed on the roster as IDLE, kept its handle, and every later mention opened
a fresh run whose output reached the published brief.

Removal now follows the shape identity revocation already uses on the same path: a
service check inside ``_prepare_agent_run`` that names the refusal, and a fail-closed
BEFORE INSERT trigger on ``agent_runs`` underneath it.
"""

from __future__ import annotations

from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import (
    AgentRun,
    Execution,
    HarnessState,
    MessageRole,
    ParticipantType,
    new_id,
    utcnow,
)
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.authorization import AuthorizationError
from multiplayer.services.service import MultiplayerService


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


async def _room_with_researcher(svc: MultiplayerService) -> tuple[str, str]:
    org = await svc.create_organization("Removal org", "removal-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(
        room.room_id,
        next(t.template_id for t in templates if t.name == "Researcher"),
        name="Researcher",
        requested_by="owner",
    )
    return room.room_id, agent.agent_id


@pytest.mark.asyncio
async def test_a_removed_agent_cannot_open_a_run_through_any_door(
    service: MultiplayerService,
) -> None:
    """Starting a run and resuming a settled one both reach _prepare_agent_run."""
    svc = service
    room_id, agent_id = await _room_with_researcher(svc)
    session = await svc.start_agent_session(room_id, agent_id)
    execution = await svc.start_execution(session.session_id, "owner")
    settled = await svc.repos.agent_runs.get_by_execution(execution.execution_id)
    assert settled is not None
    # A second session, opened while the agent was still a member and never used.
    spare = await svc.start_agent_session(room_id, agent_id)
    before = len(await svc.repos.executions.list_by_room(room_id))

    await svc.remove_agent_from_room(agent_id, room_id, "owner", require_member=True)

    with pytest.raises(AuthorizationError, match="not in room"):
        await svc.start_execution(spare.session_id, "owner")
    with pytest.raises(AuthorizationError, match="not in room"):
        await svc.resume_agent_run(settled.run_id, "owner", require_member=True)
    assert len(await svc.repos.executions.list_by_room(room_id)) == before

    # The refusal is on the log, under the launch-refused event, like every other one.
    refusals = [
        e
        for e in await svc.get_room_events(room_id)
        if e.event_type.value == "agent.launch.refused"
    ]
    assert [e.payload["reason"] for e in refusals] == ["not_a_member", "not_a_member"]


@pytest.mark.asyncio
async def test_a_removed_agent_leaves_the_roster_and_gives_back_its_handle(
    service: MultiplayerService,
) -> None:
    svc = service
    room_id, agent_id = await _room_with_researcher(svc)
    assert [a.agent_id for a in await svc.list_room_agents(room_id)] == [agent_id]
    handle = await svc.repos.handles.get_for_participant(room_id, ParticipantType.AGENT, agent_id)
    assert handle is not None and handle.handle == "researcher"

    await svc.remove_agent_from_room(agent_id, room_id, "owner", require_member=True)

    assert await svc.list_room_agents(room_id) == []
    assert (
        await svc.repos.handles.get_for_participant(room_id, ParticipantType.AGENT, agent_id)
        is None
    )
    state = await svc.get_room_state(room_id, user_id="owner")
    assert state["agents"] == []
    # The instance itself survives: its runs, outputs and events still name it.
    assert await svc.repos.agents.get_instance(agent_id) is not None


@pytest.mark.asyncio
async def test_mentioning_a_removed_agent_opens_no_run_and_says_nobody_answers(
    service: MultiplayerService,
) -> None:
    """A critic watched a removed agent run twice more from exactly this path."""
    svc = service
    room_id, agent_id = await _room_with_researcher(svc)
    await svc.remove_agent_from_room(agent_id, room_id, "owner", require_member=True)

    assert await svc.unrecognized_mention_handles(room_id, "@Researcher again") == ["Researcher"]
    message = await svc.send_message(
        room_id, MessageRole.HUMAN, "owner", "@Researcher again", invoke_mentioned_agents=True
    )

    assert await svc.repos.mentions.list_for_message(message.message_id) == []
    assert await svc.repos.executions.list_by_room(room_id) == []
    assert await svc.repos.agent_outputs.list_by_room(room_id) == []


@pytest.mark.asyncio
async def test_the_database_refuses_a_run_for_a_removed_agent_under_the_service(
    service: MultiplayerService,
) -> None:
    """A future code path that forgets the service check still cannot launch."""
    svc = service
    room_id, agent_id = await _room_with_researcher(svc)
    session = await svc.start_agent_session(room_id, agent_id)
    identity = await svc.repos.agent_identities.get_for_agent(agent_id)
    assert identity is not None

    await svc.remove_agent_from_room(agent_id, room_id, "owner", require_member=True)

    # An execution with no envelope yet, so the insert below is the run's first write
    # and the trigger is the only thing standing between it and a launch.
    execution = await svc.repos.executions.create(
        Execution(
            execution_id=new_id("exec"),
            session_id=session.session_id,
            agent_id=agent_id,
            authorized_by="owner",
        )
    )
    run = AgentRun(
        run_id=new_id("arun"),
        execution_id=execution.execution_id,
        agent_id=agent_id,
        identity_id=identity.identity_id,
        room_id=room_id,
        authorized_by="owner",
        acting_user_id="owner",
        harness_id="nexus",
        credential_hash="a" * 64,
        lease_expires_at=utcnow(),
        harness_state=HarnessState.STARTING,
    )
    with pytest.raises(Exception, match="removed from a room may not launch"):
        async with svc.db.transaction():
            await svc.repos.agent_runs.create_in_transaction(run)
