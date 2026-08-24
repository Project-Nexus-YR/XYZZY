"""Regression: who may point an agent is a durable record, not harness configuration.

A relay that trusts each harness to police its own audience lets a compromised harness
widen it. Addressing lives in the workspace instead, so the harness has no say: it is
read before a run is created, before a mention invokes an agent, and again on interrupt,
cancel and resume.

Addressing gates who may point the agent, not what it does. A wider mode never adds a
capability, and NOBODY parks the agent with its history still readable.
"""

from __future__ import annotations

from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import AddressingMode, MessageRole
from multiplayer.harness import HarnessInfo
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.authorization import AuthorizationError
from multiplayer.security.capabilities import may_address
from multiplayer.services.service import MultiplayerService

OWNER = "owner"
ALLY = "ally"
PLAIN = "plain"
OUTSIDER = "outsider"


class _FinishingProvider:
    async def acomplete(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, response_schema
        return {
            "action": "finish",
            "output": {"content": "assessed"},
            "provider_name": "test-model",
            "provider_model": "addressing-test",
            "provider_response_id": "response_finish",
            "provider_evidence": "finished",
        }


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(
        db, RealtimeHub(), known_users=frozenset({OWNER, ALLY, PLAIN, OUTSIDER})
    )
    await svc.initialize()
    svc.nexus = NexusAgentBridge(model_provider=_FinishingProvider())
    yield svc
    await db.close()


async def _room_with_agent(svc: MultiplayerService) -> tuple[str, str]:
    org = await svc.create_organization("Addressing org", "addr-org", OWNER)
    workspace = await svc.create_workspace(org.org_id, "Main", "main", OWNER)
    room = await svc.create_room(workspace.workspace_id, "Decision", OWNER)
    for user_id in (ALLY, PLAIN):
        await svc.invite_room_member(room.room_id, user_id, "editor", OWNER)
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(
        room.room_id,
        next(t.template_id for t in templates if t.name == "Researcher"),
        name="Researcher",
        requested_by=OWNER,
    )
    return room.room_id, agent.agent_id


async def _mention_runs(svc: MultiplayerService, room_id: str, user_id: str) -> bool:
    """Whether a mention from this principal opened a turn."""
    before = len(await svc.repos.executions.list_by_room(room_id))
    try:
        await svc.send_message(
            room_id,
            MessageRole.HUMAN,
            user_id,
            "@Researcher please assess this",
            invoke_mentioned_agents=True,
        )
    except AuthorizationError:
        return False
    return len(await svc.repos.executions.list_by_room(room_id)) == before + 1


async def _direct_run_starts(svc: MultiplayerService, room_id: str, user_id: str) -> bool:
    agent_id = (await svc.list_room_agents(room_id))[0].agent_id
    session = await svc.start_agent_session(room_id, agent_id)
    try:
        await svc.start_execution(session.session_id, user_id)
    except AuthorizationError:
        return False
    return True


# ── The pure function is the matrix ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("ANYONE", {OWNER: True, ALLY: True, PLAIN: True, OUTSIDER: True}),
        ("OWNER_ONLY", {OWNER: True, ALLY: False, PLAIN: False, OUTSIDER: False}),
        ("ALLOWLIST", {OWNER: True, ALLY: True, PLAIN: False, OUTSIDER: False}),
        ("NOBODY", {OWNER: False, ALLY: False, PLAIN: False, OUTSIDER: False}),
    ],
)
def test_the_addressing_matrix(mode: str, expected: dict[str, bool]) -> None:
    allowlist = frozenset({ALLY})
    for user_id, allowed in expected.items():
        assert may_address(mode, OWNER, allowlist, user_id) is allowed, (mode, user_id)
    # Deny by default: an unknown mode and an unnamed principal grant nothing.
    assert may_address(mode, OWNER, allowlist, "") is False
    assert may_address("SOMETHING_ELSE", OWNER, allowlist, OWNER) is False


# ── Both doors into the agent read the same record ───────────────────────────


@pytest.mark.asyncio
async def test_owner_only_refuses_every_other_member_at_both_doors(
    service: MultiplayerService,
) -> None:
    svc = service
    room_id, agent_id = await _room_with_agent(svc)
    await svc.set_agent_addressing(agent_id, AddressingMode.OWNER_ONLY, OWNER)

    assert await _mention_runs(svc, room_id, OWNER) is True
    assert await _mention_runs(svc, room_id, PLAIN) is False
    assert await _direct_run_starts(svc, room_id, OWNER) is True
    assert await _direct_run_starts(svc, room_id, PLAIN) is False
    reasons = [
        event.payload["reason"]
        for event in await svc.get_room_events(room_id)
        if event.event_type.value == "agent.addressing.refused"
    ]
    assert reasons == ["not_addressable", "not_addressable"]


@pytest.mark.asyncio
async def test_an_allowlist_admits_exactly_the_named_members(
    service: MultiplayerService,
) -> None:
    svc = service
    room_id, agent_id = await _room_with_agent(svc)
    await svc.set_agent_addressing(
        agent_id, AddressingMode.ALLOWLIST, OWNER, allowlist=frozenset({ALLY})
    )

    assert await _mention_runs(svc, room_id, ALLY) is True
    assert await _mention_runs(svc, room_id, PLAIN) is False
    assert await _mention_runs(svc, room_id, OWNER) is True

    # Taking the ally off the list closes the door again, with no other change.
    await svc.set_agent_addressing(agent_id, AddressingMode.ALLOWLIST, OWNER)
    assert await _mention_runs(svc, room_id, ALLY) is False


@pytest.mark.asyncio
async def test_anyone_opens_the_agent_to_the_room_and_nobody_parks_it(
    service: MultiplayerService,
) -> None:
    svc = service
    room_id, agent_id = await _room_with_agent(svc)
    await svc.set_agent_addressing(agent_id, AddressingMode.ANYONE, OWNER)
    assert await _mention_runs(svc, room_id, PLAIN) is True

    await svc.set_agent_addressing(agent_id, AddressingMode.NOBODY, OWNER)

    assert await _mention_runs(svc, room_id, OWNER) is False
    assert await _mention_runs(svc, room_id, PLAIN) is False
    assert await _direct_run_starts(svc, room_id, OWNER) is False
    # Parked, not erased: what the agent already said is still readable.
    assert len(await svc.repos.agent_outputs.list_by_room(room_id)) == 1


@pytest.mark.asyncio
async def test_a_non_member_is_refused_whatever_the_mode_says(
    service: MultiplayerService,
) -> None:
    """Addressing widens nothing: room membership is still the first door."""
    svc = service
    room_id, agent_id = await _room_with_agent(svc)
    await svc.set_agent_addressing(agent_id, AddressingMode.ANYONE, OWNER)

    assert await _mention_runs(svc, room_id, OUTSIDER) is False
    assert await svc.repos.executions.list_by_room(room_id) == []


@pytest.mark.asyncio
async def test_setting_addressing_requires_room_administer(
    service: MultiplayerService,
) -> None:
    svc = service
    room_id, agent_id = await _room_with_agent(svc)

    with pytest.raises(AuthorizationError):
        await svc.set_agent_addressing(agent_id, AddressingMode.ANYONE, PLAIN, require_member=True)
    assert (await svc.get_agent_addressing(agent_id)).mode is AddressingMode.ANYONE

    await svc.set_agent_addressing(agent_id, AddressingMode.NOBODY, OWNER, require_member=True)
    assert (await svc.get_agent_addressing(agent_id)).mode is AddressingMode.NOBODY
    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert "agent.addressing.updated" in types


@pytest.mark.asyncio
async def test_a_harness_advertising_a_wider_audience_changes_nothing(
    service: MultiplayerService,
) -> None:
    """advertised_capabilities is display metadata; it is never a term or a grant."""
    svc = service
    room_id, agent_id = await _room_with_agent(svc)
    await svc.set_agent_addressing(agent_id, AddressingMode.OWNER_ONLY, OWNER)
    before = (await svc.agent_capability_terms(agent_id, OWNER)).lendable()

    boastful = HarnessInfo("nexus", 1, frozenset({"coding", "security", "everything"}))
    assert boastful.advertised_capabilities  # the harness may claim what it likes

    assert await _mention_runs(svc, room_id, PLAIN) is False
    assert (await svc.agent_capability_terms(agent_id, OWNER)).lendable() == before


# ── Steering a run reads the record again ────────────────────────────────────


@pytest.mark.asyncio
async def test_interrupt_and_cancel_re_read_addressing(service: MultiplayerService) -> None:
    svc = service
    room_id, agent_id = await _room_with_agent(svc)
    session = await svc.start_agent_session(room_id, agent_id)
    execution = await svc.start_execution(session.session_id, OWNER)

    await svc.set_agent_addressing(agent_id, AddressingMode.NOBODY, OWNER)

    with pytest.raises(AuthorizationError):
        await svc.cancel_execution(execution.execution_id, OWNER, require_member=True)
    with pytest.raises(AuthorizationError):
        await svc.intervene_execution(
            execution.execution_id, OWNER, "change course", require_member=True
        )
    with pytest.raises(AuthorizationError):
        await svc.pause_execution(execution.execution_id, OWNER)
    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert "human.redirected_agent" not in types
    assert "execution.cancelled" not in types
