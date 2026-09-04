"""Finding 3: the turn entrance is a compare and swap, not an unconditional advance.

``execute_agent_step`` used to advance a run to STREAMING with no check that the
run was idle. A second call against a run already holding at a reviewer
(AWAITING_APPROVAL) prompted the model again on top of the parked turn, raced
two turns onto one execution, and could settle the run out from under a still
pending approval. No concurrency is needed to show it: one extra call against a
parked run is enough. This asserts the second call is refused instead.
"""

from __future__ import annotations

from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import DomainError, HarnessState
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService

OWNER = "owner"


class _AsksForATaskThenAnswers:
    """One approval-gated tool call, then the answer, if ever prompted again."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del schema
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            return {
                "action": "tool",
                "tool": "task.create",
                "input": {"title": "Roll the migration back"},
                "output": {"content": "requesting a task"},
            }
        return {"action": "finish", "output": {"content": "the task is filed"}}


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({OWNER}))
    await svc.initialize()
    yield svc
    await db.close()


async def _suspended_execution(svc: MultiplayerService, provider: Any) -> str:
    org = await svc.create_organization("Finding3 org", "finding3-org", OWNER)
    workspace = await svc.create_workspace(org.org_id, "Main", "main", OWNER)
    room = await svc.create_room(workspace.workspace_id, "Decision", OWNER)
    svc.nexus = NexusAgentBridge(model_provider=provider)
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(
        room.room_id,
        next(t.template_id for t in templates if t.name == "Synthesizer"),
        name="Synthesizer",
        requested_by=OWNER,
        harness_id="model-provider",
    )
    session = await svc.start_agent_session(room.room_id, agent.agent_id)
    execution = await svc.start_execution(session.session_id, OWNER)
    await svc.execute_agent_step(execution.execution_id, "File the rollback.", OWNER)
    run = (
        await svc.db.fetch_all(
            "SELECT harness_state FROM agent_runs WHERE execution_id = ?",
            (execution.execution_id,),
        )
    )[0]
    assert run["harness_state"] == HarnessState.AWAITING_APPROVAL.value
    return execution.execution_id


@pytest.mark.asyncio
async def test_a_second_step_against_a_parked_run_is_refused(service: MultiplayerService):
    provider = _AsksForATaskThenAnswers()
    execution_id = await _suspended_execution(service, provider)

    with pytest.raises(DomainError):
        await service.execute_agent_step(execution_id, "are you there?", OWNER)

    # No extra prompt reached the model, and the run is still parked exactly
    # where the reviewer left it, not settled out from under the approval.
    assert len(provider.prompts) == 1
    run = (
        await service.db.fetch_all(
            "SELECT harness_state FROM agent_runs WHERE execution_id = ?", (execution_id,)
        )
    )[0]
    assert run["harness_state"] == HarnessState.AWAITING_APPROVAL.value
    approvals = await service.db.fetch_all(
        "SELECT status FROM approvals WHERE execution_id = ?", (execution_id,)
    )
    assert [row["status"] for row in approvals] == ["PENDING"]
