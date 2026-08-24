"""Regression: the initiator's grant bounds a run from above and never substitutes for it.

``execute_agent_step`` authorized its caller at room MUTATE and then derived the terms
from the initiator alone, so a member an admin had narrowed to ``["research"]`` could
step a run another member initiated and be offered — and execute — ``artifact.write``
under that member's terms.

The caller now gets the intersection of the two grants. It narrows the ``user`` term
rather than adding a sixth, so the five-way intersection is untouched, and every verb
that advances or steers a run passes its caller: step, redirect, intervene, pause,
resume, cancel, and approval.
"""

from __future__ import annotations

from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import MessageRole
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService

OWNER = "owner"
NARROW = "narrow"
# Everything this member may lend. It grants no tool at all: channel.read_context
# needs retrieval, and both gated tools need writing.
NARROWED_TO = ["research"]


class _RecordingProvider:
    """Finishes every step, and keeps the response schema it was offered."""

    def __init__(self) -> None:
        self.offered_schemas: list[dict[str, Any]] = []

    async def acomplete(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        del prompt
        self.offered_schemas.append(response_schema)
        return {
            "action": "finish",
            "output": {"content": "assessed"},
            "provider_name": "test-model",
            "provider_model": "intersection-test",
            "provider_response_id": "response_finish",
            "provider_evidence": "finished",
        }


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({OWNER, NARROW}))
    await svc.initialize()
    svc.nexus = NexusAgentBridge(model_provider=_RecordingProvider())
    yield svc
    await db.close()


async def _room(svc: MultiplayerService) -> str:
    org = await svc.create_organization("Ceiling org", "ceil-org", OWNER)
    workspace = await svc.create_workspace(org.org_id, "Main", "main", OWNER)
    room = await svc.create_room(workspace.workspace_id, "Decision", OWNER)
    await svc.invite_room_member(room.room_id, NARROW, "editor", OWNER)
    await svc.set_member_capabilities(room.room_id, NARROW, NARROWED_TO, OWNER)
    return room.room_id


async def _agent(svc: MultiplayerService, room_id: str, template_name: str) -> str:
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(
        room_id,
        next(t.template_id for t in templates if t.name == template_name),
        name=template_name,
        requested_by=OWNER,
    )
    return agent.agent_id


async def _run(svc: MultiplayerService, room_id: str, agent_id: str) -> str:
    session = await svc.start_agent_session(room_id, agent_id)
    execution = await svc.start_execution(session.session_id, OWNER)
    return execution.execution_id


async def _assert_bounded_by_the_caller(
    svc: MultiplayerService, room_id: str, agent_id: str
) -> None:
    """Whatever the initiator holds, the run under this caller holds no more."""
    agent = await svc.get_agent(agent_id)
    initiator_only = await svc._lendable_terms(agent, room_id, OWNER)
    delegated = await svc._lendable_terms(agent, room_id, OWNER, NARROW)
    caller_own = await svc._user_term(room_id, NARROW)
    assert delegated.lendable() <= caller_own
    assert delegated.lendable() < initiator_only.lendable()
    # The narrowing lands on the user term; the other four are untouched.
    assert delegated.terms.agent == initiator_only.terms.agent
    assert delegated.terms.skill == initiator_only.terms.skill
    assert delegated.terms.channel == initiator_only.terms.channel
    assert delegated.terms.workspace == initiator_only.terms.workspace


def _offered_tools(svc: MultiplayerService) -> list[str]:
    provider = svc.nexus.model_provider
    assert isinstance(provider, _RecordingProvider)
    schema = provider.offered_schemas[-1]["properties"]
    tool = schema.get("tool")
    return list(tool["enum"]) if tool else []


# ── step ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_step_offers_the_caller_only_what_the_caller_holds(
    service: MultiplayerService,
) -> None:
    svc = service
    room_id = await _room(svc)
    agent_id = await _agent(svc, room_id, "Researcher")

    initiator_run = await _run(svc, room_id, agent_id)
    await svc.execute_agent_step(initiator_run, "Assess it.", OWNER)
    assert _offered_tools(svc) == ["channel.read_context"]

    caller_run = await _run(svc, room_id, agent_id)
    await svc.execute_agent_step(caller_run, "Assess it.", NARROW)

    assert _offered_tools(svc) == []
    await _assert_bounded_by_the_caller(svc, room_id, agent_id)


# ── redirect, intervene, interrupt ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_redirect_and_interrupt_are_bounded_by_the_caller(
    service: MultiplayerService,
) -> None:
    svc = service
    room_id = await _room(svc)
    agent_id = await _agent(svc, room_id, "Researcher")
    execution_id = await _run(svc, room_id, agent_id)
    await svc.execute_agent_step(execution_id, "Assess it.", OWNER)

    await svc.redirect_agent(agent_id, NARROW, "consider the rollback", require_member=True)
    await svc.interrupt_agent(agent_id, NARROW, "hold on", require_member=True)

    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert "human.redirected_agent" in types
    assert "human.interrupted_agent" in types
    await _assert_bounded_by_the_caller(svc, room_id, agent_id)


@pytest.mark.asyncio
async def test_intervene_is_bounded_by_the_caller(service: MultiplayerService) -> None:
    svc = service
    room_id = await _room(svc)
    agent_id = await _agent(svc, room_id, "Researcher")
    execution_id = await _run(svc, room_id, agent_id)

    await svc.intervene_execution(execution_id, NARROW, "check the migration", require_member=True)

    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert "human.redirected_agent" in types
    await _assert_bounded_by_the_caller(svc, room_id, agent_id)


# ── pause, resume, cancel ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pause_and_resume_are_bounded_by_the_caller(service: MultiplayerService) -> None:
    svc = service
    room_id = await _room(svc)
    agent_id = await _agent(svc, room_id, "Researcher")
    execution_id = await _run(svc, room_id, agent_id)
    await svc.execute_agent_step(execution_id, "Assess it.", OWNER)

    assert await svc.pause_execution(execution_id, NARROW) is True
    assert await svc.resume_execution(execution_id, NARROW) is True
    await _assert_bounded_by_the_caller(svc, room_id, agent_id)


@pytest.mark.asyncio
async def test_cancel_is_bounded_by_the_caller(service: MultiplayerService) -> None:
    svc = service
    room_id = await _room(svc)
    agent_id = await _agent(svc, room_id, "Researcher")
    execution_id = await _run(svc, room_id, agent_id)
    await svc.execute_agent_step(execution_id, "Assess it.", OWNER)
    # A cancel settles the run durably now rather than setting a flag in the
    # dispatching process's memory, so it needs a run that is still open: the one
    # above answered and is terminal, and cancelling it is refused on any branch.
    open_execution_id = await _run(svc, room_id, agent_id)

    assert await svc.cancel_execution(open_execution_id, NARROW, require_member=True) is True
    await _assert_bounded_by_the_caller(svc, room_id, agent_id)


# ── approval ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_approval_cannot_lend_what_the_reviewer_does_not_hold(
    service: MultiplayerService,
) -> None:
    """The reviewer is a caller too, so the same ceiling applies to a grant."""
    svc = service
    room_id = await _room(svc)

    class _ArtifactProvider:
        async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
            del prompt, schema
            return {
                "action": "tool",
                "tool": "artifact.write",
                "input": {"name": "Rollout plan"},
                "output": {"content": "requesting a tool"},
            }

    svc.nexus = NexusAgentBridge(model_provider=_ArtifactProvider())
    await _agent(svc, room_id, "Synthesizer")
    await svc.send_message(
        room_id,
        MessageRole.HUMAN,
        OWNER,
        "@Synthesizer draft the plan",
        invoke_mentioned_agents=True,
    )
    approval_id = (await svc.list_pending_approvals(room_id))[0].approval_id

    await svc.approve_action(approval_id, NARROW)

    assert await svc.repos.artifacts.list_by_room(room_id) == []
    stamped = (await svc.db.fetch_all("SELECT effective_json, status FROM tool_requests"))[0]
    assert stamped["status"] == "REJECTED"
    caller_own = await svc._user_term(room_id, NARROW)
    assert set(__import__("json").loads(stamped["effective_json"])) <= caller_own
    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert "tool.call_rejected" in types
    assert "tool.call_completed" not in types
