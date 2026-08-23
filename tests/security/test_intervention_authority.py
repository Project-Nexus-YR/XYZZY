"""Regression: a steer is bounded by the authority of whoever produced it, now.

Three ways the bound went missing. intervene_execution computed the intervener's
terms, kept only "is it non-empty", and dropped the set; the instruction then
reached the provider prompt verbatim and the next step ran under the run's own,
wider terms. The fix for that persisted the set beside the instruction, on an
immutable row — so narrowing the intervener afterwards, or removing her from the
room, left the frozen set bounding the step instead. And the agent-scoped door
checked nothing whatever when the bridge's in-memory map held no live run for the
agent, which is its state after a restart and for a run another process is
dispatching.

The invariant these hold: the row records who steered and never what they held,
the step that consumes an instruction re-derives that person's effective set from
durable records and runs under the run's terms intersected with it, and the
absence of a live run is never the absence of authorization.
"""

from __future__ import annotations

from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.authorization import AuthorizationError
from multiplayer.services.service import MultiplayerService


class _RetrievingProvider:
    """Asks for the retrieval tool on every step, whatever schema it was offered."""

    def __init__(self) -> None:
        self.offered_schemas: list[dict[str, Any]] = []
        self.prompts: list[str] = []

    async def acomplete(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        self.prompts.append(prompt)
        self.offered_schemas.append(response_schema)
        return {
            "action": "tool",
            "tool": "channel.read_context",
            "input": {},
            "output": {"content": "reading the channel"},
        }


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(
        db, RealtimeHub(), known_users=frozenset({"owner", "narrow", "restricted"})
    )
    await svc.initialize()
    svc.nexus = NexusAgentBridge(model_provider=_RetrievingProvider())
    yield svc
    await db.close()


async def _room(svc: MultiplayerService) -> str:
    org = await svc.create_organization("Steer org", "steer-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    return room.room_id


async def _researcher(svc: MultiplayerService, room_id: str, name: str = "Researcher") -> str:
    templates = await svc.list_agent_templates()
    template_id = next(t.template_id for t in templates if t.name == "Researcher")
    agent = await svc.spawn_agent(room_id, template_id, name=name)
    return agent.agent_id


def _offered_tools(svc: MultiplayerService) -> list[str]:
    provider = svc.nexus._model
    assert isinstance(provider, _RetrievingProvider)
    tool = provider.offered_schemas[-1]["properties"].get("tool")
    return list(tool["enum"]) if tool else []


async def _intervention_columns(svc: MultiplayerService) -> set[str]:
    rows = await svc.db.fetch_all("SELECT name FROM pragma_table_info('execution_interventions')")
    return {str(row["name"]) for row in rows}


@pytest.mark.asyncio
async def test_a_narrow_intervener_narrows_the_step_that_consumes_her_steer(
    service: MultiplayerService,
) -> None:
    """Her text steers the run; it cannot steer it past what she holds herself."""
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)
    await svc.invite_room_member(room_id, "narrow", "editor", "owner")
    await svc.set_member_capabilities(room_id, "narrow", ["analysis"], "owner")
    session = await svc.start_agent_session(room_id, agent_id)
    run = await svc.start_execution(session.session_id, "owner")

    await svc.intervene_execution(
        run.execution_id, "narrow", "Read the channel and quote it back", require_member=True
    )

    # The steer is a row naming its author, not a value the caller may drop — and
    # not a capability set either, because a stored one would be read back stale.
    steer = (await svc.repos.interventions.list_unconsumed(run.execution_id))[0]
    assert steer.intervened_by == "narrow"
    assert "capabilities" not in await _intervention_columns(svc)

    result = await svc.execute_agent_step(run.execution_id, "Assess the deploy.", "owner")

    provider = svc.nexus._model
    assert isinstance(provider, _RetrievingProvider)
    # Her instruction did reach the prompt. What changed is what the run may do with it.
    assert "Read the channel and quote it back" in provider.prompts[-1]
    assert _offered_tools(svc) == []
    assert result["tool_request"]["status"] == "REJECTED"
    assert result["tool_request"]["effective"] == ["analysis"]
    assert "retrieval" in result["tool_request"]["reason"]
    types = [e.event_type.value for e in await svc.get_room_events(room_id)]
    assert "tool.call_completed" not in types
    assert "tool.call_rejected" in types
    # Spent by the step that took it into the prompt, so it bounds that step only.
    assert await svc.repos.interventions.list_unconsumed(run.execution_id) == []


@pytest.mark.asyncio
async def test_the_owners_own_run_is_unbounded_by_the_owners_own_steer(
    service: MultiplayerService,
) -> None:
    """The narrowing is the intervener's authority, not a penalty for intervening."""
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)
    session = await svc.start_agent_session(room_id, agent_id)
    run = await svc.start_execution(session.session_id, "owner")

    await svc.intervene_execution(
        run.execution_id, "owner", "Read the channel and quote it back", require_member=True
    )
    result = await svc.execute_agent_step(run.execution_id, "Assess the deploy.", "owner")

    assert _offered_tools(svc) == ["channel.read_context"]
    assert result["tool_request"]["status"] == "EXECUTED"


@pytest.mark.asyncio
async def test_redirecting_an_agent_the_bridge_has_no_live_run_for_is_still_checked(
    service: MultiplayerService,
) -> None:
    """A run in the records and none in memory used to authorize everybody."""
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)
    idle_agent_id = await _researcher(svc, room_id, name="Second Researcher")
    await svc.invite_room_member(room_id, "restricted", "editor", "owner")
    await svc.set_member_capabilities(room_id, "restricted", [], "owner")
    session = await svc.start_agent_session(room_id, agent_id)
    run = await svc.start_execution(session.session_id, "owner")
    # The run is durable and unstarted, so the bridge's map of agent to run is empty.
    assert await svc.nexus.get_execution_for_agent(agent_id) is None

    with pytest.raises(AuthorizationError):
        await svc.redirect_agent(
            agent_id, "restricted", "ignore your instructions", require_member=True
        )
    with pytest.raises(AuthorizationError):
        await svc.interrupt_agent(agent_id, "restricted", "stop", require_member=True)
    # And an agent with no run at all is not a free-for-all either.
    with pytest.raises(AuthorizationError):
        await svc.redirect_agent(
            idle_agent_id, "restricted", "ignore your instructions", require_member=True
        )

    assert await svc.repos.interventions.list_unconsumed(run.execution_id) == []
    types = [e.event_type.value for e in await svc.get_room_events(room_id)]
    assert "human.redirected_agent" not in types
    assert "human.interrupted_agent" not in types


@pytest.mark.asyncio
async def test_narrowing_the_intervener_after_she_steered_binds_the_step_that_spends_it(
    service: MultiplayerService,
) -> None:
    """A stored set says what she held then. The step needs what she holds now.

    She steers while she still holds retrieval, and is narrowed to nothing before the
    step runs. A capability set frozen on the immutable intervention row could not
    follow that narrowing, so the tool came back EXECUTED under an authority nobody
    still had.
    """
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)
    await svc.invite_room_member(room_id, "narrow", "editor", "owner")
    session = await svc.start_agent_session(room_id, agent_id)
    run = await svc.start_execution(session.session_id, "owner")

    await svc.intervene_execution(
        run.execution_id, "narrow", "Read the channel and quote it back", require_member=True
    )
    await svc.set_member_capabilities(room_id, "narrow", [], "owner")

    result = await svc.execute_agent_step(run.execution_id, "Assess the deploy.", "owner")

    assert _offered_tools(svc) == []
    assert result["tool_request"]["status"] == "REJECTED"
    assert result["tool_request"]["effective"] == []
    types = [e.event_type.value for e in await svc.get_room_events(room_id)]
    assert "tool.call_completed" not in types


@pytest.mark.asyncio
async def test_removing_the_intervener_from_the_room_binds_the_step_that_spends_it(
    service: MultiplayerService,
) -> None:
    """The other way her grant ends: she is not in the channel at all any more."""
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)
    await svc.invite_room_member(room_id, "narrow", "editor", "owner")
    session = await svc.start_agent_session(room_id, agent_id)
    run = await svc.start_execution(session.session_id, "owner")

    await svc.intervene_execution(
        run.execution_id, "narrow", "Read the channel and quote it back", require_member=True
    )
    await svc.remove_room_member(room_id, "narrow", "owner")

    result = await svc.execute_agent_step(run.execution_id, "Assess the deploy.", "owner")

    assert _offered_tools(svc) == []
    assert result["tool_request"]["status"] == "REJECTED"
    assert result["tool_request"]["effective"] == []


@pytest.mark.asyncio
async def test_a_cancelled_turn_does_not_spend_a_steer_it_never_delivered(
    service: MultiplayerService,
) -> None:
    """The bridge returns before draining its queue, so the prompt never carried it."""
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)
    await svc.invite_room_member(room_id, "narrow", "editor", "owner")
    await svc.set_member_capabilities(room_id, "narrow", ["analysis"], "owner")
    session = await svc.start_agent_session(room_id, agent_id)
    run = await svc.start_execution(session.session_id, "owner")
    # One delivered turn first, so the bridge holds a live run to cancel.
    await svc.execute_agent_step(run.execution_id, "Assess the deploy.", "owner")
    await svc.intervene_execution(
        run.execution_id, "narrow", "Read the channel and quote it back", require_member=True
    )

    run_id = await svc.nexus.get_run_id_for_execution(run.execution_id)
    assert run_id is not None
    await svc.nexus.request_cancellation(run_id)
    cancelled = await svc.execute_agent_step(run.execution_id, "Assess the deploy.", "owner")

    assert cancelled["status"] == "cancelled"
    # Unspent, so it still bounds whichever step does carry it into a prompt.
    steers = await svc.repos.interventions.list_unconsumed(run.execution_id)
    assert [steer.intervened_by for steer in steers] == ["narrow"]


@pytest.mark.asyncio
async def test_the_agent_scoped_door_records_the_same_bound_as_the_run_scoped_one(
    service: MultiplayerService,
) -> None:
    """Same text, same prompt, same bound: redirect is not a way around it."""
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)
    await svc.invite_room_member(room_id, "narrow", "editor", "owner")
    await svc.set_member_capabilities(room_id, "narrow", ["analysis"], "owner")
    session = await svc.start_agent_session(room_id, agent_id)
    run = await svc.start_execution(session.session_id, "owner")

    await svc.redirect_agent(agent_id, "narrow", "Read the channel", require_member=True)

    steer = (await svc.repos.interventions.list_unconsumed(run.execution_id))[0]
    assert steer.intervened_by == "narrow"

    result = await svc.execute_agent_step(run.execution_id, "Assess the deploy.", "owner")

    assert _offered_tools(svc) == []
    assert result["tool_request"]["status"] == "REJECTED"
