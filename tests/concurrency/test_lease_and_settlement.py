"""A lease is never issued for a continuation that is not there, and a run stopped
because an authority went away is not recorded as an agent that failed.

Two untruths the record could tell. Neither granted anything; both made the system
say something that had not happened.

The first: ``_resume_suspended_turn`` returned in silence when there was nothing to
claim, and by then the decision above had already issued a fresh STREAMING lease. The
approval used to commit in the gateway's transaction while the continuation was a
later, separate write, so a crash or a race between them left an approval whose grant
put the run back on a lease nobody held — STREAMING, NULL settlement, no continuation
— the one state ``_continue_agent_turn`` promises cannot happen. The two writes are
one transaction now; a row lost after that commit settles the run instead of vanishing.

The second: ``_settle_undispatched_run`` passed no settlement, so a run stopped
because its authorizing human was removed settled FAILED, and so did one whose
dispatcher died before it started. ``AUTHORITY_REVOKED`` and ``ORPHANED`` exist and
say what actually happened.
"""

from __future__ import annotations

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
from multiplayer.security.authorization import AuthorizationError
from multiplayer.services.service import MultiplayerService


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


class _AnswersAtOnce:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del schema
        self.prompts.append(prompt)
        return {"action": "finish", "output": {"content": "answered"}}


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(
        db, RealtimeHub(), known_users=frozenset({"owner", "delegate", "teammate"})
    )
    await svc.initialize()
    yield svc
    await db.close()


async def _room_with_agent(
    svc: MultiplayerService,
    provider: Any,
    slug: str,
    template: str = "Synthesizer",
    harness_id: str = "model-provider",
) -> tuple[str, str]:
    org = await svc.create_organization(f"Org {slug}", f"org-{slug}", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", f"main-{slug}", "owner")
    room = await svc.create_room(workspace.workspace_id, f"Decision {slug}", "owner")
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


async def _start(
    svc: MultiplayerService, room_id: str, agent_id: str, authorized_by: str = "owner"
) -> str:
    session = await svc.start_agent_session(room_id, agent_id)
    execution = await svc.start_execution(session.session_id, authorized_by)
    return execution.execution_id


async def _suspend_at_a_reviewer(
    svc: MultiplayerService, slug: str, acting_as: str = "owner"
) -> tuple[str, str, str, _AsksForATaskThenAnswers, str]:
    """Drive a turn to the gate: one approval-gated tool call, waiting for a human."""
    provider = _AsksForATaskThenAnswers()
    room_id, agent_id = await _room_with_agent(svc, provider, slug)
    execution_id = await _start(svc, room_id, agent_id)
    await svc.execute_agent_step(execution_id, "File the rollback.", acting_as)
    run = await _the_run(svc, execution_id)
    assert run["harness_state"] == HarnessState.AWAITING_APPROVAL.value
    approval_id = (
        await svc.db.fetch_all(
            "SELECT approval_id FROM approvals WHERE execution_id = ?", (execution_id,)
        )
    )[0]["approval_id"]
    return room_id, agent_id, execution_id, provider, str(approval_id)


async def _the_run(svc: MultiplayerService, execution_id: str) -> dict[str, Any]:
    rows = await svc.db.fetch_all(
        "SELECT * FROM agent_runs WHERE execution_id = ?", (execution_id,)
    )
    assert len(rows) == 1, rows
    return rows[0]


async def _stranded_runs(svc: MultiplayerService) -> list[dict[str, Any]]:
    """Every run nothing can prompt again that has not been told what became of it.

    A non-terminal run is answerable for exactly as long as something will reach it:
    it has not been dispatched yet, or it is holding at a reviewer with the rest of
    its turn parked behind the approval. Once the call driving a turn has returned,
    anything else non-terminal is a lease held by nobody with no continuation behind
    it — which is the state this whole file exists to rule out.
    """
    return await svc.db.fetch_all(
        "SELECT r.run_id, r.harness_state, r.settlement, e.status AS execution_status "
        "FROM agent_runs r JOIN executions e ON e.execution_id = r.execution_id "
        "WHERE r.harness_state <> ? AND e.status <> ? "
        "AND NOT (r.harness_state = ? AND EXISTS ("
        "SELECT 1 FROM suspended_turns s WHERE s.execution_id = r.execution_id))",
        (
            HarnessState.SETTLED.value,
            ExecutionStatus.PENDING.value,
            HarnessState.AWAITING_APPROVAL.value,
        ),
    )


# ── The approval and the rest of its turn commit together ────────────────────


@pytest.mark.asyncio
async def test_the_gate_writes_the_approval_and_the_continuation_in_one_transaction(
    service: MultiplayerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither exists without the other, so no decision can find only half of it.

    The continuation write is made to fail. If it is part of the gate's transaction
    the approval goes back with it and there is nothing to decide; if it is a later,
    separate write the approval stands, and deciding it is what strands the run.
    """
    svc = service
    provider = _AsksForATaskThenAnswers()
    room_id, agent_id = await _room_with_agent(svc, provider, "atomic")
    execution_id = await _start(svc, room_id, agent_id)

    async def the_write_that_does_not_land(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the continuation write did not land")

    monkeypatch.setattr(svc.repos.suspended_turns, "save", the_write_that_does_not_land)

    with pytest.raises(RuntimeError):
        await svc.execute_agent_step(execution_id, "File the rollback.", "owner")

    assert (
        await svc.db.fetch_all(
            "SELECT approval_id FROM approvals WHERE execution_id = ?", (execution_id,)
        )
        == []
    )
    assert (
        await svc.db.fetch_all(
            "SELECT request_id FROM tool_requests WHERE execution_id = ?", (execution_id,)
        )
        == []
    )
    assert (
        await svc.db.fetch_all(
            "SELECT execution_id FROM suspended_turns WHERE execution_id = ?", (execution_id,)
        )
        == []
    )
    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert "approval.requested" not in types
    assert (await _the_run(svc, execution_id))[
        "harness_state"
    ] != HarnessState.AWAITING_APPROVAL.value


# ── A decision that finds no continuation settles rather than leasing ────────


@pytest.mark.asyncio
async def test_approving_with_no_continuation_left_settles_instead_of_leasing(
    service: MultiplayerService,
) -> None:
    """The grant used to put the run on a fresh STREAMING lease and then walk away."""
    svc = service
    room_id, _, execution_id, provider, approval_id = await _suspend_at_a_reviewer(svc, "approve")

    # The gap a crash between the two writes used to leave behind: an approval a
    # reviewer can decide, with nothing behind it to carry the turn on.
    await svc.repos.suspended_turns.discard(execution_id)

    await svc.approve_action(approval_id, "owner")

    # The reviewer's decision stands and is not reported as lost.
    approvals = await svc.db.fetch_all(
        "SELECT status FROM approvals WHERE approval_id = ?", (approval_id,)
    )
    assert [row["status"] for row in approvals] == ["APPROVED"]
    # The model was never prompted a second time, and the run says so rather than
    # holding a lease nobody is spending.
    assert len(provider.prompts) == 1
    run = await _the_run(svc, execution_id)
    assert run["harness_state"] == HarnessState.SETTLED.value
    assert run["settlement"] == RunSettlement.ORPHANED.value
    execution = await svc.repos.executions.get(execution_id)
    assert execution is not None and execution.status is ExecutionStatus.FAILED
    assert await _stranded_runs(svc) == []
    assert await svc.sweep_expired_run_leases() == 0
    assert await svc.repos.agent_outputs.list_by_room(room_id) == []


@pytest.mark.asyncio
async def test_rejecting_with_continuation_and_none_left_settles_instead_of_leasing(
    service: MultiplayerService,
) -> None:
    """The other door onto the same fresh lease: refuse the tool, continue the turn."""
    svc = service
    room_id, _, execution_id, provider, approval_id = await _suspend_at_a_reviewer(svc, "reject")

    await svc.repos.suspended_turns.discard(execution_id)

    await svc.reject_action(approval_id, "owner", continue_turn=True)

    approvals = await svc.db.fetch_all(
        "SELECT status FROM approvals WHERE approval_id = ?", (approval_id,)
    )
    assert [row["status"] for row in approvals] == ["REJECTED"]
    assert len(provider.prompts) == 1
    run = await _the_run(svc, execution_id)
    assert run["harness_state"] == HarnessState.SETTLED.value
    assert run["settlement"] == RunSettlement.ORPHANED.value
    assert await _stranded_runs(svc) == []
    assert await svc.sweep_expired_run_leases() == 0
    assert await svc.repos.agent_outputs.list_by_room(room_id) == []


# ── A run stopped by removal did not fail ────────────────────────────────────


@pytest.mark.asyncio
async def test_a_run_stopped_by_removal_settles_under_a_name_that_says_so(
    service: MultiplayerService,
) -> None:
    """The agent did not fail. The human whose authority it ran under was removed."""
    svc = service
    provider = _AnswersAtOnce()
    room_id, agent_id = await _room_with_agent(svc, provider, "removed")
    await svc.invite_room_member(room_id, "teammate", "editor", "owner")
    execution_id = await _start(svc, room_id, agent_id, authorized_by="teammate")

    await svc.remove_room_member(room_id, "teammate", "owner")

    with pytest.raises(AuthorizationError):
        await svc.execute_agent_step(execution_id, "Assess the rollback.", "teammate")

    assert provider.prompts == []
    run = await _the_run(svc, execution_id)
    assert run["harness_state"] == HarnessState.SETTLED.value
    assert run["settlement"] == RunSettlement.AUTHORITY_REVOKED.value
    assert run["settlement"] != RunSettlement.FAILED.value
    execution = await svc.repos.executions.get(execution_id)
    assert execution is not None and "no effective capability" in (execution.error or "")
    assert await _stranded_runs(svc) == []


@pytest.mark.asyncio
async def test_an_authority_narrowed_while_the_approval_waits_is_not_a_failure(
    service: MultiplayerService,
) -> None:
    """The same truth on the approval path: revoked, not failed."""
    svc = service
    room_id, _, execution_id, provider, approval_id = await _suspend_at_a_reviewer(svc, "narrowed")

    await svc.set_member_capabilities(room_id, "owner", [], "owner")
    await svc.approve_action(approval_id, "owner")

    run = await _the_run(svc, execution_id)
    assert run["harness_state"] == HarnessState.SETTLED.value
    assert run["settlement"] == RunSettlement.AUTHORITY_REVOKED.value
    assert await _stranded_runs(svc) == []


@pytest.mark.asyncio
async def test_a_mention_run_whose_dispatcher_died_is_orphaned_not_failed(
    service: MultiplayerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing ever picked it up. That is an orphan, not an agent that failed."""
    svc = service
    provider = _AnswersAtOnce()
    room_id, _ = await _room_with_agent(svc, provider, "orphan")

    async def never_dispatched(execution_id: str, prompt: str) -> None:
        return None

    monkeypatch.setattr(svc, "_dispatch_mention_run", never_dispatched)
    await svc.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "@Synthesizer assess the rollback",
        invoke_mentioned_agents=True,
    )
    orphan = (await svc.repos.executions.list_by_room(room_id))[0]

    restarted = MultiplayerService(svc.db, RealtimeHub())
    await restarted._settle_orphaned_mention_runs()

    run = await _the_run(svc, orphan.execution_id)
    assert run["settlement"] == RunSettlement.ORPHANED.value
    assert run["settlement"] != RunSettlement.FAILED.value
    assert await _stranded_runs(svc) == []


# ── The invariant itself, rather than the two instances of it ────────────────


@pytest.mark.asyncio
async def test_no_way_a_turn_can_end_leaves_a_run_nothing_will_prompt(
    service: MultiplayerService,
) -> None:
    """Asserted over every row rather than over the two defects that motivated it.

    "No lease holder" is not a column — a lease is a deadline, not a name — so the
    holder is read off the process instead: once the call driving a turn has returned,
    nothing in this process is carrying that run. What remains checkable, and is
    checked here after every way a turn is known to end, is that no run is left
    non-terminal except the two kinds that something will still reach: one not yet
    dispatched, and one holding at a reviewer with its continuation parked behind the
    approval.
    """
    svc = service

    async def answers(slug: str) -> None:
        provider = _AnswersAtOnce()
        room_id, agent_id = await _room_with_agent(svc, provider, slug)
        await svc.execute_agent_step(await _start(svc, room_id, agent_id), "Assess it.")

    async def approved_and_carried_on(slug: str) -> None:
        _, _, _, _, approval_id = await _suspend_at_a_reviewer(svc, slug)
        await svc.approve_action(approval_id, "owner")

    async def refused_outright(slug: str) -> None:
        _, _, _, _, approval_id = await _suspend_at_a_reviewer(svc, slug)
        await svc.reject_action(approval_id, "owner")

    async def refused_but_carried_on(slug: str) -> None:
        _, _, _, _, approval_id = await _suspend_at_a_reviewer(svc, slug)
        await svc.reject_action(approval_id, "owner", continue_turn=True)

    async def approved_with_the_continuation_gone(slug: str) -> None:
        _, _, execution_id, _, approval_id = await _suspend_at_a_reviewer(svc, slug)
        await svc.repos.suspended_turns.discard(execution_id)
        await svc.approve_action(approval_id, "owner")

    async def refused_with_the_continuation_gone(slug: str) -> None:
        _, _, execution_id, _, approval_id = await _suspend_at_a_reviewer(svc, slug)
        await svc.repos.suspended_turns.discard(execution_id)
        await svc.reject_action(approval_id, "owner", continue_turn=True)

    async def the_agent_was_removed(slug: str) -> None:
        room_id, agent_id, _, _, _ = await _suspend_at_a_reviewer(svc, slug)
        await svc.remove_agent_from_room(agent_id, room_id, "owner")

    async def the_run_was_cancelled(slug: str) -> None:
        room_id, _, execution_id, _, _ = await _suspend_at_a_reviewer(svc, slug)
        await svc.cancel_execution(execution_id, "owner")
        del room_id

    endings = [
        answers,
        approved_and_carried_on,
        refused_outright,
        refused_but_carried_on,
        approved_with_the_continuation_gone,
        refused_with_the_continuation_gone,
        the_agent_was_removed,
        the_run_was_cancelled,
    ]
    for ending in endings:
        await ending(ending.__name__.replace("_", "")[:20])
        assert await _stranded_runs(svc) == [], ending.__name__

    # And nothing the sweep would have had to invent a name for later.
    assert await svc.sweep_expired_run_leases() == 0
