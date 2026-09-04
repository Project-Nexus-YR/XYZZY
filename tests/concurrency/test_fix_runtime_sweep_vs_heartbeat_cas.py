"""Finding 14: the lease sweep must not settle a run on a lease it read
outside its own transaction if something renewed that lease in the meantime.

``sweep_expired_run_leases`` reads every expired run with a plain,
untransacted query, then settles each one. A write that renews the lease
(a streaming heartbeat, or a reviewer's decision extending it) landing between
that read and the settling write used to be silently overwritten: the settle
wrote unconditionally, so a run that was actually still live could be stamped
settled right out from under it. The fix makes the settle a compare and swap
against the exact lease value the sweep read: zero rows moved means somebody
else already moved the run on, and the sweep leaves it alone.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import ExecutionStatus, HarnessState, utcnow
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService


class _RequestsApproval:
    """Asks for an approval-gated tool and never gets to answer within this test."""

    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, schema
        return {
            "action": "tool",
            "tool": "task.create",
            "input": {"title": "Cut the auth migration"},
            "output": {"content": "requesting a task"},
        }


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner"}))
    await svc.initialize()
    svc.nexus = NexusAgentBridge(model_provider=_RequestsApproval())
    yield svc
    await db.close()


async def _parked_awaiting_approval(svc: MultiplayerService) -> tuple[str, str]:
    """A real run parked at a reviewer, its lease already in the past."""
    org = await svc.create_organization("Sweep org", "sweep-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(
        room.room_id,
        next(t.template_id for t in templates if t.name == "Synthesizer"),
        requested_by="owner",
    )
    session = await svc.start_agent_session(room.room_id, agent.agent_id)
    execution = await svc.start_execution(session.session_id, "owner")
    await svc.execute_agent_step(execution.execution_id, "Open a task for it.", "owner")

    run = await svc.repos.agent_runs.get_by_execution(execution.execution_id)
    assert run is not None
    assert run.harness_state is HarnessState.AWAITING_APPROVAL
    await svc.db.execute(
        "UPDATE agent_runs SET lease_expires_at = ? WHERE run_id = ?",
        ((utcnow() - timedelta(minutes=1)).isoformat(), run.run_id),
    )
    await svc.db.commit()
    return execution.execution_id, run.run_id


@pytest.mark.asyncio
async def test_a_lease_renewed_between_the_sweeps_read_and_write_is_not_clobbered(
    service: MultiplayerService,
) -> None:
    svc = service
    execution_id, run_id = await _parked_awaiting_approval(svc)
    real_list_expired = svc.repos.agent_runs.list_expired

    async def list_expired_then_renew(now: object) -> list[Any]:
        expired = await real_list_expired(now)
        # Lands right after the sweep's read: something (a reviewer's decision
        # extending the wait, in the general case) renews the lease well into
        # the future before the sweep gets to write its settlement.
        await svc.repos.agent_runs.advance(
            run_id, HarnessState.AWAITING_APPROVAL, utcnow() + timedelta(minutes=15), "owner"
        )
        return expired

    svc.repos.agent_runs.list_expired = list_expired_then_renew  # type: ignore[method-assign]

    settled = await svc.sweep_expired_run_leases()

    assert settled == 0
    run = await svc.repos.agent_runs.get_by_execution(execution_id)
    assert run is not None
    assert run.harness_state is HarnessState.AWAITING_APPROVAL
    assert run.settlement is None
    execution = await svc.repos.executions.get(execution_id)
    assert execution is not None
    assert execution.status is ExecutionStatus.RUNNING


@pytest.mark.asyncio
async def test_an_unrenewed_lease_is_still_swept_normally(service: MultiplayerService) -> None:
    """Without a competing renewal, the sweep settles the run as before."""
    svc = service
    execution_id, _ = await _parked_awaiting_approval(svc)

    settled = await svc.sweep_expired_run_leases()

    assert settled == 1
    run = await svc.repos.agent_runs.get_by_execution(execution_id)
    assert run is not None
    assert run.harness_state is HarnessState.SETTLED
