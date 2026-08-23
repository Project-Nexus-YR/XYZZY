"""Concurrency acceptance: one fact, one owner, and a run that always reaches a name.

``agent_runs`` is the identity-and-authority envelope around the existing ``executions``
row, not a second state machine over the same fact: ``executions.status`` stays the
domain state and ``agent_runs.harness_state`` the transport, each mapping to one domain
status. The tests here cross the harness states with the settlement outcomes and check
that the two records never disagree.

Two specific holes are closed. ``reject_action`` used to resolve the tool request and
stop, leaving the run AWAITING_APPROVAL — not settled, not leased, unsweepable forever;
it now ends in one of two named places inside the transaction that writes it. And a turn
still in flight when its run was settled used to land anyway, because
``complete_execution`` consulted neither ``agent_runs`` nor any credential.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import (
    DomainError,
    ExecutionStatus,
    HarnessState,
    MessageRole,
    RunSettlement,
)
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService

TERMINAL = {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}


class _GatedProvider:
    """Holds the turn inside the provider call until the test lets it finish."""

    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.response = response or {"action": "finish", "output": {"content": "assessed"}}

    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, schema
        self.entered.set()
        await self.release.wait()
        return self.response


class _ArtifactProvider:
    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, schema
        return {
            "action": "tool",
            "tool": "artifact.write",
            "input": {"name": "Rollout plan"},
            "output": {"content": "requesting a tool"},
        }


class _BrokenProvider:
    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, schema
        raise bridge_module.ModelProviderError("the provider is down")


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner"}))
    await svc.initialize()
    yield svc
    await db.close()


async def _room_with_agent(
    svc: MultiplayerService, provider: Any, template_name: str = "Researcher"
) -> tuple[str, str]:
    org = await svc.create_organization("Lifecycle org", "life-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    svc.nexus = NexusAgentBridge(model_provider=provider)
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(
        room.room_id,
        next(t.template_id for t in templates if t.name == template_name),
        name=template_name,
        requested_by="owner",
    )
    return room.room_id, agent.agent_id


async def _the_run(svc: MultiplayerService) -> dict[str, Any]:
    rows = await svc.db.fetch_all("SELECT * FROM agent_runs ORDER BY created_at, run_id")
    assert len(rows) == 1, rows
    return rows[0]


async def _assert_records_agree(svc: MultiplayerService) -> None:
    """A settled envelope and an open execution would be one fact with two owners."""
    for row in await svc.db.fetch_all("SELECT execution_id, harness_state FROM agent_runs"):
        execution = await svc.repos.executions.get(str(row["execution_id"]))
        assert execution is not None
        if row["harness_state"] == HarnessState.SETTLED.value:
            assert execution.status in TERMINAL, execution
        else:
            assert execution.status not in TERMINAL, execution


# ── The states, crossed with the outcomes ────────────────────────────────────


@pytest.mark.asyncio
async def test_a_finished_turn_settles_end_turn(service: MultiplayerService) -> None:
    svc = service
    room_id, _ = await _room_with_agent(svc, _GatedProvider())
    provider = svc.nexus.model_provider
    assert isinstance(provider, _GatedProvider)
    provider.release.set()
    await svc.send_message(
        room_id, MessageRole.HUMAN, "owner", "@Researcher assess", invoke_mentioned_agents=True
    )

    run = await _the_run(svc)
    assert run["harness_state"] == HarnessState.SETTLED.value
    assert run["settlement"] == RunSettlement.END_TURN.value
    await _assert_records_agree(svc)


@pytest.mark.asyncio
async def test_a_provider_failure_settles_failed(service: MultiplayerService) -> None:
    svc = service
    room_id, agent_id = await _room_with_agent(svc, _BrokenProvider())
    session = await svc.start_agent_session(room_id, agent_id)
    execution = await svc.start_execution(session.session_id, "owner")

    await svc.execute_agent_step(execution.execution_id, "Assess it.")

    run = await _the_run(svc)
    assert run["settlement"] == RunSettlement.FAILED.value
    await _assert_records_agree(svc)


@pytest.mark.asyncio
async def test_a_cancel_settles_cancelled(service: MultiplayerService) -> None:
    svc = service
    room_id, _ = await _room_with_agent(svc, _GatedProvider())
    provider = svc.nexus.model_provider
    assert isinstance(provider, _GatedProvider)
    provider.release.set()
    # A managed branch owns its run's lifecycle, so cancel is authoritative there.
    from multiplayer.domain.models import BranchMode

    agent_id = (await svc.list_room_agents(room_id))[0].agent_id
    branch, runs = await svc.start_branch(
        room_id, BranchMode.TURN_LOCKED_SINGLE, "Assess it.", "owner", [agent_id]
    )

    assert await svc.cancel_execution(runs[0].execution_id, "owner") is True

    run = await _the_run(svc)
    assert run["settlement"] == RunSettlement.CANCELLED.value
    assert branch.branch_id
    await _assert_records_agree(svc)


# ── A refused approval never leaves the run where it found it ────────────────


@pytest.mark.asyncio
async def test_a_refused_approval_settles_the_run(service: MultiplayerService) -> None:
    svc = service
    room_id, _ = await _room_with_agent(svc, _ArtifactProvider(), "Synthesizer")
    await svc.send_message(
        room_id, MessageRole.HUMAN, "owner", "@Synthesizer draft it", invoke_mentioned_agents=True
    )
    approval = (await svc.list_pending_approvals(room_id))[0]
    assert (await _the_run(svc))["harness_state"] == HarnessState.AWAITING_APPROVAL.value

    await svc.reject_action(approval.approval_id, "owner")

    run = await _the_run(svc)
    assert run["harness_state"] == HarnessState.SETTLED.value
    assert run["settlement"] == RunSettlement.APPROVAL_REFUSED.value
    assert await svc.repos.artifacts.list_by_room(room_id) == []
    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert "approval.rejected" in types
    assert "tool.call_rejected" in types
    assert "agent.run.settled" in types
    await _assert_records_agree(svc)


@pytest.mark.asyncio
async def test_a_refused_approval_can_instead_continue_the_turn_on_a_fresh_lease(
    service: MultiplayerService,
) -> None:
    svc = service
    room_id, _ = await _room_with_agent(svc, _ArtifactProvider(), "Synthesizer")
    await svc.send_message(
        room_id, MessageRole.HUMAN, "owner", "@Synthesizer draft it", invoke_mentioned_agents=True
    )
    approval = (await svc.list_pending_approvals(room_id))[0]
    waiting = await _the_run(svc)

    await svc.reject_action(approval.approval_id, "owner", continue_turn=True)

    run = await _the_run(svc)
    assert run["harness_state"] == HarnessState.STREAMING.value
    assert run["settlement"] is None
    # A fresh lease, not the reviewer's long one: nobody is thinking any more.
    assert run["lease_expires_at"] < waiting["lease_expires_at"]
    assert await svc.repos.artifacts.list_by_room(room_id) == []
    await _assert_records_agree(svc)

    # And it is still sweepable, which is the point of not leaving it where it was.
    await svc.db.execute(
        "UPDATE agent_runs SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE run_id = ?",
        (run["run_id"],),
    )
    assert await svc.sweep_expired_run_leases() == 1
    assert (await _the_run(svc))["settlement"] == RunSettlement.ORPHANED.value


# ── A settled run cannot write ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_turn_settled_mid_flight_writes_nothing_when_it_lands(
    service: MultiplayerService,
) -> None:
    """The dispatcher is inside the provider call when the agent is removed."""
    svc = service
    provider = _GatedProvider()
    room_id, agent_id = await _room_with_agent(svc, provider)
    session = await svc.start_agent_session(room_id, agent_id)
    execution = await svc.start_execution(session.session_id, "owner")

    step = asyncio.create_task(svc.execute_agent_step(execution.execution_id, "Assess it."))
    await asyncio.wait_for(provider.entered.wait(), timeout=5)
    await svc.remove_agent_from_room(agent_id, room_id, "owner", require_member=True)
    provider.release.set()

    with pytest.raises(DomainError, match="settled"):
        await step

    assert await svc.repos.agent_outputs.list_by_room(room_id) == []
    run = await _the_run(svc)
    assert run["settlement"] == RunSettlement.AGENT_REMOVED.value
    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert "agent.output.created" not in types
    assert "agent.run.completed" not in types
    # The settlement the removal decided is the one that stands.
    assert types.count("agent.run.settled") == 1


@pytest.mark.asyncio
async def test_two_settlements_race_and_only_the_first_one_stands(
    service: MultiplayerService,
) -> None:
    svc = service
    room_id, agent_id = await _room_with_agent(svc, _GatedProvider())
    session = await svc.start_agent_session(room_id, agent_id)
    execution = await svc.start_execution(session.session_id, "owner")
    run = await svc.repos.agent_runs.get_by_execution(execution.execution_id)
    assert run is not None
    await svc.db.execute(
        "UPDATE agent_runs SET lease_expires_at = '2000-01-01T00:00:00+00:00' WHERE run_id = ?",
        (run.run_id,),
    )

    settled, removed = await asyncio.gather(
        svc.sweep_expired_run_leases(),
        svc.remove_agent_from_room(agent_id, room_id, "owner", require_member=True),
        return_exceptions=True,
    )
    assert not isinstance(settled, BaseException), settled
    assert not isinstance(removed, BaseException), removed

    row = await _the_run(svc)
    assert row["harness_state"] == HarnessState.SETTLED.value
    assert row["settlement"] in {
        RunSettlement.ORPHANED.value,
        RunSettlement.AGENT_REMOVED.value,
    }
    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert types.count("agent.run.settled") == 1
    await _assert_records_agree(svc)
