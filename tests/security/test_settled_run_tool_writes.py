"""A settled run writes nothing, including through an approval it left behind.

``complete_execution`` refused a settled run's output, so removing an agent looked
airtight: the run settled AGENT_REMOVED, the events were right, and no agent output
was written. The tool writers were not guarded at all. ``_require_run_authority_in_
transaction`` re-derived capabilities and never read ``agent_runs.harness_state``, and
``approve_action`` did not read it either — so releasing an approval that was still
PENDING when the agent was removed executed ``artifact.write`` and published an
artifact whose ``created_by`` is the removed agent. Output arrived after the
settlement, through the one door that outlived it.

Both halves are covered here, because either alone still refuses the other's case:
the approval door decides before dispatching, and the writer decides again inside the
transaction that writes, where a settlement landing in between is still visible.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import (
    ApprovalStatus,
    HarnessState,
    MessageRole,
    RunSettlement,
)
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import DomainError, MultiplayerService


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
    svc.nexus = NexusAgentBridge(model_provider=_ArtifactProvider())
    yield svc
    await db.close()


async def _pending_approval_then_removal(svc: MultiplayerService) -> tuple[str, str, str]:
    """A run that asked for an approval-gated tool, and then lost its agent."""
    org = await svc.create_organization("Settled org", "settled-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(
        room.room_id,
        next(t.template_id for t in templates if t.name == "Synthesizer"),
        name="Synthesizer",
        requested_by="owner",
    )
    await svc.send_message(
        room.room_id,
        MessageRole.HUMAN,
        "owner",
        "@Synthesizer draft the plan",
        invoke_mentioned_agents=True,
    )
    approvals = await svc.list_pending_approvals(room.room_id)
    assert len(approvals) == 1
    await svc.remove_agent_from_room(agent.agent_id, room.room_id, "owner", require_member=True)
    run = await svc.repos.agent_runs.get_by_execution(approvals[0].execution_id)
    assert run is not None and run.settlement is RunSettlement.AGENT_REMOVED
    return room.room_id, agent.agent_id, approvals[0].approval_id


@pytest.mark.asyncio
async def test_an_approval_left_by_a_settled_run_is_closed_with_it(
    service: MultiplayerService,
) -> None:
    """Removing the agent ends the approval too, so there is nothing left to release."""
    svc = service
    room_id, agent_id, approval_id = await _pending_approval_then_removal(svc)

    approval = await svc.repos.approvals.get(approval_id)
    assert approval is not None and approval.status.value == "EXPIRED"
    with pytest.raises(DomainError, match="is not pending"):
        await svc.approve_action(approval_id, "owner", require_member=True)

    assert await svc.list_room_artifacts(room_id) == []
    rejected = [
        e for e in await svc.get_room_events(room_id) if e.event_type.value == "tool.call_rejected"
    ]
    assert len(rejected) == 1
    assert rejected[0].payload["reason"] == "agent removed from room"
    types = [e.event_type.value for e in await svc.get_room_events(room_id)]
    assert "tool.call_completed" not in types
    assert "artifact.created" not in types


@pytest.mark.asyncio
async def test_the_approval_door_still_refuses_a_settled_run_if_one_ever_reaches_it(
    service: MultiplayerService,
) -> None:
    """The second line, behind the closure above.

    Settlement now closes the approvals of the run it ends, so a PENDING approval
    against a settled run should not exist. ``approve_action`` reads the run's state
    anyway, and that read is what held before the closure did; the row is put back to
    PENDING here so the guard is still exercised rather than quietly retired.
    """
    svc = service
    room_id, agent_id, approval_id = await _pending_approval_then_removal(svc)
    expired = await svc.repos.approvals.get(approval_id)
    assert expired is not None
    await svc.repos.approvals.update(
        replace(expired, status=ApprovalStatus.PENDING, reviewed_at=None)
    )
    pending = await svc.repos.tool_requests.get_by_approval(approval_id)
    assert pending is not None
    await svc.db.execute(
        "UPDATE tool_requests SET status = 'PENDING_APPROVAL' WHERE request_id = ?",
        (pending.request_id,),
    )

    approved = await svc.approve_action(approval_id, "owner", require_member=True)

    assert approved.status.value == "APPROVED"
    assert await svc.list_room_artifacts(room_id) == []
    reasons = [
        e.payload["reason"]
        for e in await svc.get_room_events(room_id)
        if e.event_type.value == "tool.call_rejected"
    ]
    assert any("is settled (AGENT_REMOVED)" in reason for reason in reasons)
    types = [e.event_type.value for e in await svc.get_room_events(room_id)]
    assert "tool.call_completed" not in types
    assert "artifact.created" not in types


@pytest.mark.asyncio
async def test_the_writer_refuses_a_settled_run_inside_the_transaction_that_writes(
    service: MultiplayerService,
) -> None:
    """The approval door decides before dispatch; this is the decision beside the write.

    Dispatching the stored request directly is what a caller that skipped the approval
    door does, and what a settlement landing after that door's check would leave. The
    writer re-derives authority in its own transaction, so it sees the settlement.
    """
    svc = service
    room_id, agent_id, approval_id = await _pending_approval_then_removal(svc)
    approval = await svc.repos.approvals.get(approval_id)
    assert approval is not None
    pending = await svc.repos.tool_requests.get_by_approval(approval_id)
    assert pending is not None

    resolved = await svc._execute_tool_request(pending)

    assert resolved.status == "REJECTED"
    assert await svc.list_room_artifacts(room_id) == []
    run = await svc.repos.agent_runs.get_by_execution(pending.execution_id)
    assert run is not None and run.harness_state is HarnessState.SETTLED
    # The settlement it already had is the one that stands.
    assert run.settlement is RunSettlement.AGENT_REMOVED
    types = [e.event_type.value for e in await svc.get_room_events(room_id)]
    assert "artifact.created" not in types
    assert "agent.run.authority_revoked" in types
