"""Finding 45: expiring the approvals of a settled run must not overwrite a
reviewer's decision that landed in the gap between the pre-transaction read
and the write.

``_expire_undecided_approvals`` reads pending approvals with a plain query
before opening its transaction. A reviewer who approves (or rejects) the same
approval in that gap commits first; the expiry's write used to be
unconditional, wiping her ``reviewer_id`` and ``review_comment`` and leaving
the row EXPIRED even though a human decided it. The fix guards the write on
the row's own current status, so a decision that landed first is left alone.
"""

from __future__ import annotations

from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import ApprovalStatus, RunSettlement
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService


class _RequestsApproval:
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


@pytest.mark.asyncio
async def test_a_reviewer_deciding_between_the_read_and_the_expiry_write_wins(
    service: MultiplayerService,
) -> None:
    svc = service
    org = await svc.create_organization("Expire org", "expire-org", "owner")
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
    approval = (await svc.list_pending_approvals(room.room_id))[0]

    real_list_pending = svc.repos.approvals.list_pending_by_execution

    async def read_then_approve(execution_id: str) -> list[Any]:
        pending = await real_list_pending(execution_id)
        # Lands in the gap between this read and the expiry's own transaction:
        # a reviewer decides the same approval first.
        await svc.approve_action(approval.approval_id, "owner", "go ahead")
        return pending

    svc.repos.approvals.list_pending_by_execution = read_then_approve  # type: ignore[method-assign]

    await svc.cancel_execution(execution.execution_id, "owner")

    reloaded = await svc.repos.approvals.get(approval.approval_id)
    assert reloaded is not None
    assert reloaded.status is ApprovalStatus.APPROVED
    assert reloaded.reviewer_id == "owner"
    assert reloaded.review_comment == "go ahead"

    run = await svc.repos.agent_runs.get_by_execution(execution.execution_id)
    assert run is not None
    assert run.settlement is RunSettlement.CANCELLED
    types = [e.event_type.value for e in await svc.get_room_events(room.room_id)]
    assert "approval.granted" in types
    assert "approval.expired" not in types
