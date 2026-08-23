"""Failure injection: a harness that dies leaves a run somebody can still describe.

Every non-settled run holds a heartbeat lease — a long one while a reviewer is
thinking, since that may take hours — and a sweep at startup and on an interval settles
each expired lease. No state is exempt: an exemption is not a longer deadline but no
deadline, and it manufactures the fourth case the guarantee denies. A run is settled,
holds a live lease, or is swept.

The lease has an attempt counter beside it. A run picked up its full allowance that
died every time is PARKED rather than swept again, so a stuck run reaches a terminal
state a reader can name instead of being re-orphaned forever.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import (
    ExecutionStatus,
    HarnessState,
    MessageRole,
    RunSettlement,
)
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService

LONG_AGO = "2000-01-01T00:00:00+00:00"


class _GatedProvider:
    """Holds the run inside the provider call, as a harness that stopped answering does."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, schema
        self.entered.set()
        await self.release.wait()
        return {"action": "finish", "output": {"content": "assessed"}}


class _ArtifactProvider:
    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, schema
        return {
            "action": "tool",
            "tool": "artifact.write",
            "input": {"name": "Rollout plan"},
            "output": {"content": "requesting a tool"},
        }


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
    svc: MultiplayerService, template_name: str = "Researcher"
) -> tuple[str, str]:
    org = await svc.create_organization("Crash org", "crash-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(
        room.room_id,
        next(t.template_id for t in templates if t.name == template_name),
        name=template_name,
        requested_by="owner",
    )
    return room.room_id, agent.agent_id


async def _expire_every_lease(svc: MultiplayerService) -> None:
    await svc.db.execute(
        "UPDATE agent_runs SET lease_expires_at = ? WHERE harness_state <> ?",
        (LONG_AGO, HarnessState.SETTLED.value),
    )


async def _runs(svc: MultiplayerService) -> list[dict[str, Any]]:
    return await svc.db.fetch_all("SELECT * FROM agent_runs ORDER BY created_at, run_id")


# ── A harness that stops answering mid-stream ────────────────────────────────


@pytest.mark.asyncio
async def test_a_harness_killed_mid_stream_is_swept_to_a_named_settlement(
    service: MultiplayerService,
) -> None:
    svc = service
    provider = _GatedProvider()
    svc.nexus = NexusAgentBridge(model_provider=provider)
    room_id, agent_id = await _room_with_agent(svc)
    session = await svc.start_agent_session(room_id, agent_id)
    execution = await svc.start_execution(session.session_id, "owner")

    step = asyncio.create_task(svc.execute_agent_step(execution.execution_id, "Assess it."))
    await asyncio.wait_for(provider.entered.wait(), timeout=5)
    step.cancel()
    with pytest.raises(asyncio.CancelledError):
        await step

    # The turn is in flight as far as any reader can tell, and it holds a lease.
    live = (await _runs(svc))[0]
    assert live["harness_state"] == HarnessState.STREAMING.value
    assert live["settlement"] is None
    assert await svc.sweep_expired_run_leases() == 0

    await _expire_every_lease(svc)
    assert await svc.sweep_expired_run_leases() == 1

    swept = (await _runs(svc))[0]
    assert swept["harness_state"] == HarnessState.SETTLED.value
    assert swept["settlement"] == RunSettlement.ORPHANED.value
    assert swept["settled_at"] is not None
    settled = await svc.repos.executions.get(execution.execution_id)
    assert settled is not None and settled.status is ExecutionStatus.FAILED
    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert "agent.run.settled" in types
    assert "agent.run.orphaned" in types
    assert await svc.repos.agent_outputs.list_by_room(room_id) == []


@pytest.mark.asyncio
async def test_the_sweep_settles_every_expired_run_once_and_is_safe_to_repeat(
    service: MultiplayerService,
) -> None:
    svc = service
    svc.nexus = NexusAgentBridge(model_provider=_GatedProvider())
    room_id, agent_id = await _room_with_agent(svc)
    for _ in range(3):
        session = await svc.start_agent_session(room_id, agent_id)
        await svc.start_execution(session.session_id, "owner")
    await _expire_every_lease(svc)

    assert await svc.sweep_expired_run_leases() == 3
    assert await svc.sweep_expired_run_leases() == 0

    settlements = [row["settlement"] for row in await _runs(svc)]
    assert settlements == [RunSettlement.ORPHANED.value] * 3


# ── The long lease is a lease, not an exemption ──────────────────────────────


@pytest.mark.asyncio
async def test_a_run_awaiting_approval_gets_a_long_lease_and_is_still_swept(
    service: MultiplayerService,
) -> None:
    svc = service
    svc.nexus = NexusAgentBridge(model_provider=_ArtifactProvider())
    room_id, _ = await _room_with_agent(svc, "Synthesizer")
    await svc.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "@Synthesizer draft the plan",
        invoke_mentioned_agents=True,
    )
    assert len(await svc.list_pending_approvals(room_id)) == 1

    waiting = (await _runs(svc))[0]
    assert waiting["harness_state"] == HarnessState.AWAITING_APPROVAL.value
    # A reviewer may take hours, so the deadline is long. It is still a deadline.
    assert await svc.sweep_expired_run_leases() == 0

    await _expire_every_lease(svc)
    assert await svc.sweep_expired_run_leases() == 1

    swept = (await _runs(svc))[0]
    assert swept["settlement"] == RunSettlement.ORPHANED.value
    assert await svc.repos.artifacts.list_by_room(room_id) == []


@pytest.mark.asyncio
async def test_no_harness_state_is_exempt_from_the_sweep(service: MultiplayerService) -> None:
    """A run is settled, holds a live lease, or is swept. There is no fourth case."""
    svc = service
    svc.nexus = NexusAgentBridge(model_provider=_GatedProvider())
    room_id, agent_id = await _room_with_agent(svc)
    for state in (
        HarnessState.STARTING,
        HarnessState.STREAMING,
        HarnessState.AWAITING_APPROVAL,
        HarnessState.CANCEL_REQUESTED,
    ):
        session = await svc.start_agent_session(room_id, agent_id)
        execution = await svc.start_execution(session.session_id, "owner")
        await svc.db.execute(
            "UPDATE agent_runs SET harness_state = ?, lease_expires_at = ? WHERE execution_id = ?",
            (state.value, LONG_AGO, execution.execution_id),
        )

    assert await svc.sweep_expired_run_leases() == 4

    rows = await _runs(svc)
    assert {row["harness_state"] for row in rows} == {HarnessState.SETTLED.value}
    assert all(row["settlement"] is not None for row in rows)


# ── The attempt limit: a stuck run parks rather than being swept forever ─────


@pytest.mark.asyncio
async def test_a_run_that_used_every_attempt_is_parked_rather_than_orphaned(
    service: MultiplayerService,
) -> None:
    svc = service
    svc.nexus = NexusAgentBridge(model_provider=_GatedProvider())
    room_id, agent_id = await _room_with_agent(svc)
    session = await svc.start_agent_session(room_id, agent_id)
    execution = await svc.start_execution(session.session_id, "owner")
    row = (await _runs(svc))[0]
    assert row["attempts"] == 1
    assert row["max_attempts"] == 3
    await svc.db.execute(
        "UPDATE agent_runs SET attempts = max_attempts, lease_expires_at = ? "
        "WHERE execution_id = ?",
        (LONG_AGO, execution.execution_id),
    )

    assert await svc.sweep_expired_run_leases() == 1

    parked = (await _runs(svc))[0]
    assert parked["settlement"] == RunSettlement.PARKED.value
    settled = [
        event.payload
        for event in await svc.get_room_events(room_id)
        if event.event_type.value == "agent.run.settled"
    ]
    assert [payload["settlement"] for payload in settled] == [RunSettlement.PARKED.value]
    # Parked is terminal in the strongest sense: nothing picks this run up again.
    from multiplayer.domain.models import DomainError

    with pytest.raises(DomainError, match="parked"):
        await svc.resume_agent_run(parked["run_id"], "owner")


@pytest.mark.asyncio
async def test_resuming_a_swept_run_carries_the_attempt_count_forward(
    service: MultiplayerService,
) -> None:
    """Resume opens a new run, so the allowance has to travel with the chain."""
    svc = service
    svc.nexus = NexusAgentBridge(model_provider=_GatedProvider())
    room_id, agent_id = await _room_with_agent(svc)
    session = await svc.start_agent_session(room_id, agent_id)
    await svc.start_execution(session.session_id, "owner")

    settlements: list[str] = []
    run_id = (await _runs(svc))[0]["run_id"]
    for _ in range(3):
        await _expire_every_lease(svc)
        await svc.sweep_expired_run_leases()
        row = next(r for r in await _runs(svc) if r["run_id"] == run_id)
        settlements.append(str(row["settlement"]))
        if row["settlement"] == RunSettlement.PARKED.value:
            break
        resumed = await svc.resume_agent_run(run_id, "owner")
        run = await svc.repos.agent_runs.get_by_execution(resumed.execution_id)
        assert run is not None and run.resumed_from_run_id == run_id
        run_id = run.run_id

    assert settlements == [
        RunSettlement.ORPHANED.value,
        RunSettlement.ORPHANED.value,
        RunSettlement.PARKED.value,
    ]
