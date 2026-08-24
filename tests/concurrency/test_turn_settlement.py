"""Every way a turn can stop leaves the run in a state a reader can name.

Commit 28e2e8f claimed that none of a continuing turn's three exits leaves a run
running with nobody about to prompt it. Three did.

A cancellation on a branch that is not lifecycle-managed returns early through
``nexus.cancel_execution`` without terminalizing. The loop's next iteration got a
cancelled stop reason, returned a result with no tool request, and nothing settled
anything: the run sat ``STREAMING`` with a NULL settlement and the lease sweep named it
``PARKED`` a quarter of an hour later — "turn stopped without an answer", which is
untrue of a run somebody cancelled, and non-resumable besides.

``_step_schema`` offered "delegate" and "wait" and no branch handled either, so a model
that picked one reached the same silence by a shorter road.

And a turn that stopped at a reviewer was held in a per-process dict, so an approval
decided on any second process found nothing to resume: the run was put back on a fresh
``STREAMING`` lease and then nobody prompted it. The continuation is durable now, so
whichever process decides the approval carries the turn to its answer.
"""

from __future__ import annotations

from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import ExecutionStatus, HarnessState, RunSettlement
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService


class _CancelsItselfMidTurn:
    """Asks for a tool, cancelling the run on the way, so the next prompt is cancelled."""

    def __init__(self) -> None:
        self.svc: MultiplayerService | None = None
        self.execution_id = ""
        self.prompts: list[str] = []

    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del schema
        self.prompts.append(prompt)
        assert self.svc is not None
        if len(self.prompts) == 1:
            await self.svc.cancel_execution(self.execution_id, "owner")
            return {
                "action": "tool",
                "tool": "channel.read_context",
                "input": {},
                "output": {"content": "reading the channel"},
            }
        return {"action": "finish", "output": {"content": "answered"}}


class _ChoosesAnActionTheServerDoesNotContinue:
    def __init__(self, action: str) -> None:
        self.action = action
        self.schemas: list[dict[str, Any]] = []

    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del prompt
        self.schemas.append(schema)
        return {"action": self.action, "output": {"content": "handing back"}}


class _AsksForATaskThenAnswers:
    """One approval-gated tool, then the answer the room is waiting for."""

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
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner"}))
    await svc.initialize()
    yield svc
    await db.close()


async def _room_with_agent(
    svc: MultiplayerService,
    provider: Any,
    template: str = "Researcher",
    harness_id: str = "nexus",
) -> tuple[str, str]:
    org = await svc.create_organization("Settle org", "settle-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    svc.nexus = NexusAgentBridge(model_provider=provider)
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(
        room.room_id,
        next(t.template_id for t in templates if t.name == template),
        name=template,
        requested_by="owner",
        harness_id=harness_id,
    )
    return room.room_id, agent.agent_id


async def _start(svc: MultiplayerService, room_id: str, agent_id: str) -> str:
    session = await svc.start_agent_session(room_id, agent_id)
    execution = await svc.start_execution(session.session_id, "owner")
    return execution.execution_id


async def _the_run(svc: MultiplayerService) -> dict[str, Any]:
    rows = await svc.db.fetch_all("SELECT * FROM agent_runs ORDER BY created_at, run_id")
    assert len(rows) == 1, rows
    return rows[0]


# ── A cancelled turn settles as cancelled ────────────────────────────────────


@pytest.mark.asyncio
async def test_a_cancelled_continuation_settles_promptly_as_cancelled(
    service: MultiplayerService,
) -> None:
    """Not PARKED fifteen minutes later: cancelled, now, because that is what happened."""
    svc = service
    provider = _CancelsItselfMidTurn()
    room_id, agent_id = await _room_with_agent(svc, provider)
    execution_id = await _start(svc, room_id, agent_id)
    provider.svc, provider.execution_id = svc, execution_id

    await svc.execute_agent_step(execution_id, "Assess the deploy.", "owner")

    # Promptly: settled by the time the dispatcher returned, with nothing left for the
    # sweep to find and nothing for it to mislabel.
    run = await _the_run(svc)
    assert run["harness_state"] == HarnessState.SETTLED.value
    assert run["settlement"] == RunSettlement.CANCELLED.value
    assert await svc.sweep_expired_run_leases() == 0

    execution = await svc.repos.executions.get(execution_id)
    assert execution is not None
    assert execution.status is ExecutionStatus.CANCELLED
    # And the reason is true: nobody ran out of attempts here.
    assert "cancelled" in (execution.error or "").lower()
    assert "without an answer" not in (execution.error or "")
    assert run["settlement"] != RunSettlement.PARKED.value

    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert "execution.cancelled" in types
    assert "agent.run.orphaned" not in types


@pytest.mark.asyncio
async def test_a_cancelled_run_is_resumable_where_a_parked_one_is_not(
    service: MultiplayerService,
) -> None:
    """PARKED is refused a resume. A cancellation is not the same fact about the run."""
    svc = service
    provider = _CancelsItselfMidTurn()
    room_id, agent_id = await _room_with_agent(svc, provider)
    execution_id = await _start(svc, room_id, agent_id)
    provider.svc, provider.execution_id = svc, execution_id
    await svc.execute_agent_step(execution_id, "Assess the deploy.", "owner")

    resumed = await svc.resume_agent_run((await _the_run(svc))["run_id"], "owner")
    assert resumed.execution_id != execution_id


# ── An action the server does not continue ends the turn too ─────────────────


@pytest.mark.parametrize("action", ["wait", "delegate"])
@pytest.mark.asyncio
async def test_an_action_the_server_does_not_continue_settles_the_run(
    service: MultiplayerService, action: str
) -> None:
    """Neither is offered any more, and one arriving anyway still ends the run."""
    svc = service
    provider = _ChoosesAnActionTheServerDoesNotContinue(action)
    room_id, agent_id = await _room_with_agent(svc, provider)
    execution_id = await _start(svc, room_id, agent_id)

    result = await svc.execute_agent_step(execution_id, "Assess the deploy.", "owner")

    # The schema no longer invites it.
    assert provider.schemas[0]["properties"]["action"]["enum"] == ["tool", "finish"]
    # And a harness answering outside the schema is still brought to an end.
    assert result["settlement"] == RunSettlement.FAILED.value
    assert action in result["error"]
    run = await _the_run(svc)
    assert run["harness_state"] == HarnessState.SETTLED.value
    assert run["settlement"] == RunSettlement.FAILED.value
    assert await svc.sweep_expired_run_leases() == 0
    execution = await svc.repos.executions.get(execution_id)
    assert execution is not None and execution.status is ExecutionStatus.FAILED
    del room_id


# ── A suspended turn outlives the process that suspended it ──────────────────


@pytest.mark.asyncio
async def test_a_second_process_can_resume_a_turn_it_did_not_suspend(
    service: MultiplayerService,
) -> None:
    """The approval can be decided anywhere; the turn has to be carried on from there."""
    svc = service
    provider = _AsksForATaskThenAnswers()
    room_id, agent_id = await _room_with_agent(
        svc, provider, template="Synthesizer", harness_id="model-provider"
    )
    execution_id = await _start(svc, room_id, agent_id)

    await svc.execute_agent_step(execution_id, "File the rollback.", "owner")

    # The turn is holding at the reviewer, durably rather than in this process.
    assert len(provider.prompts) == 1
    parked = await svc.db.fetch_all("SELECT * FROM suspended_turns")
    assert [row["execution_id"] for row in parked] == [execution_id]
    run = await _the_run(svc)
    assert run["harness_state"] == HarnessState.AWAITING_APPROVAL.value

    # A second service over the same database — a second process, as far as the
    # continuation is concerned — decides the approval.
    other = MultiplayerService(svc.db, RealtimeHub(), known_users=frozenset({"owner"}))
    other.nexus = NexusAgentBridge(model_provider=provider)
    approval_id = (await svc.db.fetch_all("SELECT approval_id FROM approvals"))[0]["approval_id"]

    await other.approve_action(approval_id, "owner")

    # It ran the tool and carried the turn to the answer the room was waiting for.
    assert len(provider.prompts) == 2
    assert await svc.repos.tasks.list_by_room(room_id)
    outputs = await svc.repos.agent_outputs.list_by_room(room_id)
    assert [output.content for output in outputs] == ["the task is filed"]
    run = await _the_run(svc)
    assert run["harness_state"] == HarnessState.SETTLED.value
    assert run["settlement"] == RunSettlement.END_TURN.value
    # Nothing is left waiting to be resumed twice.
    assert await svc.db.fetch_all("SELECT * FROM suspended_turns") == []


@pytest.mark.asyncio
async def test_a_settled_run_leaves_nothing_parked_behind_it(
    service: MultiplayerService,
) -> None:
    """Nothing prompts a settled run again, so no continuation may outlive one."""
    svc = service
    provider = _AsksForATaskThenAnswers()
    room_id, agent_id = await _room_with_agent(
        svc, provider, template="Synthesizer", harness_id="model-provider"
    )
    execution_id = await _start(svc, room_id, agent_id)
    await svc.execute_agent_step(execution_id, "File the rollback.", "owner")
    assert await svc.db.fetch_all("SELECT * FROM suspended_turns")

    await svc.remove_agent_from_room(agent_id, room_id, "owner")

    assert await svc.db.fetch_all("SELECT * FROM suspended_turns") == []
    run = await _the_run(svc)
    assert run["settlement"] == RunSettlement.AGENT_REMOVED.value
