"""A delegated task spends the human's grant, never the delegating agent's.

test_delegated_authority.py asserts the property on the derivation alone. These
tests assert it on the path a caller actually takes, and specifically at the till
rather than at the door: opening a task put the delegating agent into a bounding
set that the tool calls afterwards never saw, so a delegate was ceilinged where it
was asked and unbounded everywhere it spent. The two are asserted together here,
because the gap between them is where the defect lived.

The rest is the refusals. A chain the asker can decline to mention is a chain the
asker can decline to have, so the parent is derived from the delegator's own open
run and never taken from the argument. An ``agent:`` principal can never become
the human a chain is authorized by. A task that has already ended cannot be taken
back, whoever asks and however the race falls out.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.agent_tasks import (
    MAX_DELEGATION_DEPTH,
    AgentTaskState,
    DelegationCycleError,
    DelegationDepthExceededError,
    Part,
    PartKind,
    TaskMessageRole,
    TaskNotCancelableError,
    TaskNotFoundError,
    UnsupportedOperationError,
)
from multiplayer.domain.models import DomainError, ExecutionStatus, RunSettlement, utcnow
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.authorization import AuthorizationError
from multiplayer.security.capabilities import agent_principal
from multiplayer.services.service import MultiplayerService, _TurnContinuation

OWNER = "owner"
# A full member of the room who never asks for anything: membership is not the
# gate on continuing or cancelling a task, and this is who proves it.
BYSTANDER = "bystander"
# An editor whose lending is bounded to one capability, so that a spend wider
# than one capability is proof their bound was not consulted.
NARROW = "narrow"
ASK = (Part(kind=PartKind.TEXT, content="assess the migration"),)


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({OWNER, BYSTANDER, NARROW}))
    await svc.initialize()
    yield svc
    await db.close()


async def _room(svc: MultiplayerService) -> str:
    org = await svc.create_organization("Delegation org", "deleg-org", OWNER)
    workspace = await svc.create_workspace(org.org_id, "Main", "main", OWNER)
    room = await svc.create_room(workspace.workspace_id, "Decision", OWNER)
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


async def _rows(svc: MultiplayerService, sql: str) -> list[dict]:
    return await svc.db.fetch_all(sql)


async def _effective(svc: MultiplayerService, execution_id: str, agent_id: str, room_id: str):
    """What this run may actually spend, derived the way every tool call derives it."""
    authorization = await svc._authorization_for(execution_id, agent_id, room_id)
    return (await svc._authorized_terms(authorization)).effective


async def _chain(svc: MultiplayerService, room_id: str, agent_ids: list[str]):
    """Walk a real chain: every hop opens its turn before it delegates onward.

    No hop names a parent. The parent is read from the delegator's own open run,
    which is the point — a caller that omits ``parent_task_id`` gets the chain it
    is actually in, not a fresh root with nothing for a cycle test to look at.
    """
    task = await svc.open_agent_task(room_id, agent_ids[0], ASK, requested_by=OWNER)
    for previous, target in zip(agent_ids, agent_ids[1:], strict=False):
        await svc.start_agent_task(task.task_id)
        task = await svc.open_agent_task(
            room_id, target, ASK, requested_by=OWNER, delegating_agent_id=previous
        )
    return task


async def _two_rooms(svc: MultiplayerService) -> tuple[str, str]:
    """One workspace, two rooms, and a caller who is a member of neither."""
    org = await svc.create_organization("Delegation org", "deleg-org", OWNER)
    workspace = await svc.create_workspace(org.org_id, "Main", "main", OWNER)
    here = await svc.create_room(workspace.workspace_id, "Decision", OWNER)
    elsewhere = await svc.create_room(workspace.workspace_id, "Elsewhere", OWNER)
    return here.room_id, elsewhere.room_id


@pytest.mark.asyncio
async def test_the_dispatcher_picks_a_submitted_task_up_and_drives_it_to_an_end(service):
    room_id = await _room(service)
    delegate = await _agent(service, room_id, "Researcher")
    task = await service.open_agent_task(room_id, delegate, ASK, requested_by=OWNER)
    assert task.state is AgentTaskState.SUBMITTED

    await service._dispatch_agent_task_run(task)

    # Whatever the turn did, the task is no longer submitted and no longer working.
    # A delegator waiting on an answer that never arrives is the failure a state
    # machine exists to prevent, so the row says how it ended either way.
    driven = await service.get_agent_task(task.task_id, viewer_id=OWNER)
    assert driven.is_terminal
    assert driven.execution_id is not None
    assert driven.terminal_at is not None


@pytest.mark.asyncio
async def test_the_startup_sweep_dispatches_a_task_stale_since_before_it(service):
    """A crash between accepting an A2A task and scheduling its background
    dispatch is the only way a task sits SUBMITTED past the sweep's staleness
    threshold. `sweep_stale_submitted_agent_tasks` (called from `initialize`,
    same as the run-lease sweep) is the constant-work recovery: it must find
    such a task and dispatch it to a terminal state, without anything else
    ever calling `start_agent_task` for it.
    """
    room_id = await _room(service)
    delegate = await _agent(service, room_id, "Researcher")
    task = await service.open_agent_task(room_id, delegate, ASK, requested_by=OWNER)
    assert task.state is AgentTaskState.SUBMITTED

    long_ago = (utcnow() - timedelta(seconds=service._STALE_SUBMITTED_TASK_SECONDS + 5)).isoformat()
    await service.db.execute(
        "UPDATE agent_tasks SET created_at = ? WHERE task_id = ?", (long_ago, task.task_id)
    )
    await service.db.commit()

    # The drain dispatches inline, one task at a time, so by the time it
    # returns the stranded task has already been driven - a stronger fact
    # than "a background dispatch was scheduled".
    swept = await service.sweep_stale_submitted_agent_tasks()
    assert swept == 1

    driven = await service.get_agent_task(task.task_id, viewer_id=OWNER)
    assert driven.is_terminal


@pytest.mark.asyncio
async def test_only_the_agent_being_asked_may_answer_fail_or_escalate_the_task(service):
    room_id = await _room(service)
    delegate = await _agent(service, room_id, "Researcher")
    impostor = await _agent(service, room_id, "Researcher")
    task = await service.open_agent_task(room_id, delegate, ASK, requested_by=OWNER)
    await service.start_agent_task(task.task_id)

    # Five verbs move a task from the delegate's side, and all five used to take a
    # task id and nothing else — so any principal reaching one could answer, fail
    # or decline somebody else's task. The refusal is the unknown-task one, for the
    # same reason the asker's is.
    answer = (Part(kind=PartKind.TEXT, content="not mine to give"),)
    for refuse in (
        service.complete_agent_task(task.task_id, answer, by_agent_id=impostor),
        service.fail_agent_task(task.task_id, "nope", by_agent_id=impostor),
        service.reject_agent_task(task.task_id, "nope", by_agent_id=impostor),
        service.escalate_agent_task(task.task_id, reason="nope", by_agent_id=impostor),
        service.require_agent_task_input(task.task_id, answer, by_agent_id=impostor),
    ):
        with pytest.raises(TaskNotFoundError) as refusal:
            await refuse
        assert task.task_id not in str(refusal.value)

    assert (await service.get_agent_task(task.task_id, viewer_id=OWNER)).state is (
        AgentTaskState.WORKING
    )
    assert len(await service.list_agent_task_messages(task.task_id, viewer_id=OWNER)) == 1


# ── The refusals give nothing away ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_stranger_cannot_tell_a_real_agent_from_a_misfiled_one_from_a_fiction(service):
    here, elsewhere = await _two_rooms(service)
    real = await _agent(service, here, "Architect")
    misfiled = await _agent(service, elsewhere, "Architect")
    imaginary = "agent_00000000000000000000000000000000"

    # BYSTANDER is a known user and a member of nothing. Three answers — 403 for a
    # real agent here, one refusal for a real agent filed elsewhere, another for an
    # id that was never minted — let a caller confirm an agent id and then find the
    # room it lives in, by walking ids and subtracting the differences.
    refusals = []
    for target in (real, misfiled, imaginary):
        with pytest.raises(AuthorizationError) as refusal:
            await service.open_agent_task(here, target, ASK, requested_by=BYSTANDER)
        refusals.append(str(refusal.value))

    assert len(set(refusals)) == 1, refusals
    assert real not in refusals[0]
    assert misfiled not in refusals[0]
    assert BYSTANDER not in refusals[0]


@pytest.mark.asyncio
async def test_a_member_who_may_not_lend_is_told_nothing_about_who_or_what(service):
    room_id = await _room(service)
    await service.invite_room_member(room_id, BYSTANDER, "viewer", OWNER)
    delegate = await _agent(service, room_id, "Coder")

    # A viewer holds no mutating capability, so they may lend a Coder nothing. The
    # refusal names neither them nor the agent: it crosses an organisational
    # boundary as a JSON-RPC error body and lands in logs on the way.
    with pytest.raises(AuthorizationError) as refusal:
        await service.open_agent_task(room_id, delegate, ASK, requested_by=BYSTANDER)
    assert delegate not in str(refusal.value)
    assert BYSTANDER not in str(refusal.value)


@pytest.mark.asyncio
async def test_a_stranger_cannot_tell_a_real_task_from_an_imaginary_one(service):
    room_id = await _room(service)
    delegate = await _agent(service, room_id, "Architect")
    real = await service.open_agent_task(room_id, delegate, ASK, requested_by=OWNER)
    imaginary = "a2atask_00000000000000000000000000000000"

    for read in (service.get_agent_task, service.list_agent_task_messages):
        refusals = []
        for target in (real.task_id, imaginary):
            with pytest.raises(TaskNotFoundError) as refusal:
                await read(target, viewer_id=BYSTANDER)
            refusals.append(str(refusal.value))
        assert len(set(refusals)) == 1, refusals
        assert real.task_id not in refusals[0]


@pytest.mark.asyncio
async def test_resolving_an_escalation_gives_a_stranger_the_same_answer_either_way(service):
    room_id = await _room(service)
    delegate = await _agent(service, room_id, "Architect")
    real = await service.open_agent_task(room_id, delegate, ASK, requested_by=OWNER)
    await service.escalate_agent_task(real.task_id, reason="needs security", by_agent_id=delegate)

    refusals = []
    for target in (real.task_id, "a2atask_00000000000000000000000000000000"):
        with pytest.raises(TaskNotFoundError) as refusal:
            await service.resolve_agent_task_escalation(target, granted=True, by_user_id=BYSTANDER)
        refusals.append(str(refusal.value))
    assert len(set(refusals)) == 1, refusals
    assert (await service.get_agent_task(real.task_id, viewer_id=OWNER)).state is (
        AgentTaskState.AUTH_REQUIRED
    )


# ── The delegator bounds the spend, not only the door ────────────────────────


@pytest.mark.asyncio
async def test_the_delegating_agent_is_read_at_the_till_and_not_only_at_the_door(service):
    room_id = await _room(service)
    # A narrow delegator and a broad delegate, so the ceiling has something to
    # take away: Researcher and Architect share analysis and nothing else.
    delegator = await _agent(service, room_id, "Researcher")
    delegate = await _agent(service, room_id, "Architect")

    root = await service.open_agent_task(room_id, delegator, ASK, requested_by=OWNER)
    await service.start_agent_task(root.task_id)
    delegated = await service.open_agent_task(
        room_id, delegate, ASK, requested_by=OWNER, delegating_agent_id=delegator
    )
    started = await service.start_agent_task(delegated.task_id)

    principals = await service.repos.executions.bounding_principals(started.execution_id)
    assert agent_principal(delegator) in principals
    # The whole of the delegate's own set was what a tool call used to get, because
    # the delegating agent was declared an arm of that union and never made one.
    assert await _effective(service, started.execution_id, delegate, room_id) == {"analysis"}


@pytest.mark.asyncio
async def test_narrowing_the_delegator_narrows_the_delegate_without_reopening_anything(service):
    room_id = await _room(service)
    delegator = await _agent(service, room_id, "Researcher")
    delegate = await _agent(service, room_id, "Architect")

    root = await service.open_agent_task(room_id, delegator, ASK, requested_by=OWNER)
    await service.start_agent_task(root.task_id)
    delegated = await service.open_agent_task(
        room_id, delegate, ASK, requested_by=OWNER, delegating_agent_id=delegator
    )
    started = await service.start_agent_task(delegated.task_id)
    assert await _effective(service, started.execution_id, delegate, room_id)

    # Re-read from durable rows at the moment of spending, every time. Removing the
    # delegator mid-run stops the delegate, rather than leaving it running on
    # authority its asker no longer has.
    await service.remove_agent_from_room(delegator, room_id, OWNER)
    assert not await _effective(service, started.execution_id, delegate, room_id)

    await service.db.execute(
        "UPDATE agent_instances SET capabilities = '[]' WHERE agent_id = ?", (delegator,)
    )
    await service.db.commit()
    assert not await _effective(service, started.execution_id, delegate, room_id)


@pytest.mark.asyncio
async def test_the_caller_who_asks_bounds_the_spend_as_well_as_the_root(service):
    room_id = await _room(service)
    await service.invite_room_member(room_id, NARROW, "editor", OWNER)
    await service.set_member_capabilities(room_id, NARROW, ["analysis"], OWNER)
    delegator = await _agent(service, room_id, "Architect")
    delegate = await _agent(service, room_id, "Architect")

    root = await service.open_agent_task(room_id, delegator, ASK, requested_by=OWNER)
    await service.start_agent_task(root.task_id)
    # NARROW may lend analysis and nothing else. The task is authorized by OWNER,
    # because that is whose chain it joins — but NARROW is who asked for this hop,
    # and a set built as {authorized_by} swapped them out rather than adding the
    # root in. Both ceilings apply: two Architects intersect to three capabilities,
    # and NARROW's bound takes it back down to the one they can lend.
    delegated = await service.open_agent_task(
        room_id, delegate, ASK, requested_by=NARROW, delegating_agent_id=delegator
    )
    assert delegated.authorized_by == OWNER
    assert delegated.requested_by == NARROW

    started = await service.start_agent_task(delegated.task_id)
    assert NARROW in await service.repos.executions.bounding_principals(started.execution_id)
    assert await _effective(service, started.execution_id, delegate, room_id) == {"analysis"}

    # And having asked, they may say more about it and take it back.
    await service.require_agent_task_input(
        started.task_id,
        (Part(kind=PartKind.TEXT, content="which migration?"),),
        by_agent_id=delegate,
    )
    resumed = await service.continue_agent_task(started.task_id, ASK, requested_by=NARROW)
    assert resumed.state is AgentTaskState.WORKING


@pytest.mark.asyncio
async def test_a_caller_who_can_lend_nothing_here_is_refused_at_the_door(service):
    room_id = await _room(service)
    await service.invite_room_member(room_id, NARROW, "editor", OWNER)
    # research is a capability no Architect has, so NARROW can lend this delegate
    # nothing at all — while OWNER, who the chain is authorized by, could lend it
    # everything. A door that names only the root would open on OWNER's grant and
    # let a member with no overlap at all commission the work.
    await service.set_member_capabilities(room_id, NARROW, ["research"], OWNER)
    delegator = await _agent(service, room_id, "Architect")
    delegate = await _agent(service, room_id, "Architect")

    root = await service.open_agent_task(room_id, delegator, ASK, requested_by=OWNER)
    await service.start_agent_task(root.task_id)
    with pytest.raises(AuthorizationError):
        await service.open_agent_task(
            room_id, delegate, ASK, requested_by=NARROW, delegating_agent_id=delegator
        )
    assert len(await _rows(service, "SELECT task_id FROM agent_tasks")) == 1


@pytest.mark.asyncio
async def test_every_ancestor_bounds_the_spend_not_only_the_immediate_delegator(service):
    room_id = await _room(service)
    # Researcher is the grandparent and the only narrow link in the chain: the two
    # Architects below it intersect to three capabilities between themselves.
    grandparent = await _agent(service, room_id, "Researcher")
    parent = await _agent(service, room_id, "Architect")
    child = await _agent(service, room_id, "Architect")

    deepest = await _chain(service, room_id, [grandparent, parent, child])
    assert deepest.depth == 2
    started = await service.start_agent_task(deepest.task_id)

    principals = await service.repos.executions.bounding_principals(started.execution_id)
    assert agent_principal(grandparent) in principals
    assert agent_principal(parent) in principals
    # Bounding by the immediate delegator alone widens this to all three. Depth one
    # cannot tell the difference, because there the parent is the whole ancestry.
    assert await _effective(service, started.execution_id, child, room_id) == {"analysis"}


@pytest.mark.asyncio
async def test_an_agent_cannot_carry_one_rooms_authority_into_another(service):
    here, elsewhere = await _two_rooms(service)
    mover = await _agent(service, here, "Architect")
    delegate = await _agent(service, elsewhere, "Architect")

    root = await service.open_agent_task(here, mover, ASK, requested_by=OWNER)
    await service.start_agent_task(root.task_id)

    # Moved to the other room with its run still open. Written as rows because that
    # is the state being tested: an agent whose durable membership says `elsewhere`
    # while the run it is serving answers a task in `here`. Nothing above stops the
    # ask now — the room gate reads membership, and membership agrees.
    await service.db.execute(
        "INSERT INTO agent_room_memberships(membership_id, agent_id, room_id, joined_at) "
        "VALUES (?, ?, ?, ?)",
        ("member_moved", mover, elsewhere, "2026-01-01T00:00:00+00:00"),
    )
    await service.db.execute(
        "UPDATE agent_instances SET room_id = ? WHERE agent_id = ?", (elsewhere, mover)
    )
    await service.db.commit()
    assert await service.repos.agents.has_room_membership(mover, elsewhere)

    # The parent is what would have been inherited: its context, its authority, its
    # chain and its depth, all of them belonging to a room this one is sealed from.
    with pytest.raises(AuthorizationError):
        await service.open_agent_task(
            elsewhere, delegate, ASK, requested_by=OWNER, delegating_agent_id=mover
        )
    assert len(await _rows(service, "SELECT task_id FROM agent_tasks")) == 1


@pytest.mark.asyncio
async def test_an_earlier_run_of_the_same_task_is_still_bounded_by_its_chain(service):
    room_id = await _room(service)
    delegator = await _agent(service, room_id, "Researcher")
    delegate = await _agent(service, room_id, "Architect")

    root = await service.open_agent_task(room_id, delegator, ASK, requested_by=OWNER)
    await service.start_agent_task(root.task_id)
    delegated = await service.open_agent_task(
        room_id, delegate, ASK, requested_by=OWNER, delegating_agent_id=delegator
    )
    first = (await service.start_agent_task(delegated.task_id)).execution_id
    assert first is not None
    assert await _effective(service, first, delegate, room_id) == {"analysis"}

    # A task legally opens a second turn: it stops for input and starts again, and
    # attach_execution overwrites the task's one-slot pointer at the newest one.
    await service.require_agent_task_input(
        delegated.task_id,
        (Part(kind=PartKind.TEXT, content="which migration?"),),
        by_agent_id=delegate,
    )
    second = (await service.start_agent_task(delegated.task_id)).execution_id
    assert second is not None and second != first

    # The first run is still PENDING — open, claimable, dispatchable — so its bound
    # has to survive the re-attach. Joined through the task's pointer it did not:
    # the chain arm returned nothing and the delegator's ceiling evaporated from a
    # live run, which is the whole defect in its sixteenth costume.
    execution = await service.repos.executions.get(first)
    assert execution is not None
    assert execution.status is not ExecutionStatus.COMPLETED
    principals = await service.repos.executions.bounding_principals(first)
    assert agent_principal(delegator) in principals
    assert await _effective(service, first, delegate, room_id) == {"analysis"}
    # And the newest turn is bounded too, by the same link rather than despite it.
    assert await _effective(service, second, delegate, room_id) == {"analysis"}


@pytest.mark.asyncio
async def test_a_delegated_run_whose_delegator_was_revoked_is_settled_not_de_tooled(service):
    room_id = await _room(service)
    delegator = await _agent(service, room_id, "Researcher")
    delegate = await _agent(service, room_id, "Architect")

    root = await service.open_agent_task(room_id, delegator, ASK, requested_by=OWNER)
    await service.start_agent_task(root.task_id)
    delegated = await service.open_agent_task(
        room_id, delegate, ASK, requested_by=OWNER, delegating_agent_id=delegator
    )
    started = await service.start_agent_task(delegated.task_id)
    assert started.execution_id is not None
    await service.remove_agent_from_room(delegator, room_id, OWNER)
    assert not await _effective(service, started.execution_id, delegate, room_id)

    # OWNER is still a member, so the liveness gate on the run's own authorizer
    # passes and the turn used to dispatch, derive an empty tool schema and run on
    # tooled with nothing — while the delegator waited for an answer.
    with pytest.raises(AuthorizationError):
        await service._execute_one_agent_step(
            started.execution_id, _TurnContinuation(prompt="go", acting_as="")
        )
    run = await service.repos.agent_runs.get_by_execution(started.execution_id)
    assert run is not None
    assert run.settlement is RunSettlement.AUTHORITY_REVOKED


@pytest.mark.asyncio
async def test_the_chain_is_the_only_place_the_delegator_is_read_from(service):
    room_id = await _room(service)
    delegator = await _agent(service, room_id, "Researcher")
    delegate = await _agent(service, room_id, "Architect")

    root = await service.open_agent_task(room_id, delegator, ASK, requested_by=OWNER)
    await service.start_agent_task(root.task_id)
    delegated = await service.open_agent_task(
        room_id, delegate, ASK, requested_by=OWNER, delegating_agent_id=delegator
    )
    started = await service.start_agent_task(delegated.task_id)
    assert started.execution_id is not None

    # The bound used to have a second arm reading agent_tasks.delegating_agent_id,
    # which is an alias of what the chain already holds: _delegating_task derives
    # the parent from the delegator's own run, so the parent's target_agent_id is
    # the delegating agent and the child is written with it at the chain's end.
    # Two arms returning one principal means either can be deleted with every test
    # still green. Deleting the chain rows must therefore lose the principal.
    assert agent_principal(delegator) in await service.repos.executions.bounding_principals(
        started.execution_id
    )
    assert (await service.repos.agent_tasks.ancestry(delegated.task_id))[-1] == delegator
    assert delegated.delegating_agent_id == delegator

    await service.db.execute("DELETE FROM agent_task_chain WHERE task_id = ?", (delegated.task_id,))
    await service.db.commit()
    principals = await service.repos.executions.bounding_principals(started.execution_id)
    assert agent_principal(delegator) not in principals


@pytest.mark.asyncio
async def test_a_resumed_delegated_run_keeps_its_chain_and_its_root(service):
    room_id = await _room(service)
    await service.invite_room_member(room_id, BYSTANDER, "editor", OWNER)
    delegator = await _agent(service, room_id, "Researcher")
    delegate = await _agent(service, room_id, "Architect")

    root = await service.open_agent_task(room_id, delegator, ASK, requested_by=OWNER)
    await service.start_agent_task(root.task_id)
    delegated = await service.open_agent_task(
        room_id, delegate, ASK, requested_by=OWNER, delegating_agent_id=delegator
    )
    started = await service.start_agent_task(delegated.task_id)
    assert started.execution_id is not None
    first = await service.repos.executions.get(started.execution_id)
    assert first is not None and first.agent_task_id == delegated.task_id
    assert await _effective(service, started.execution_id, delegate, room_id) == {"analysis"}

    await service._settle_undispatched_run(started.execution_id, "stopped", RunSettlement.FAILED)
    run = await service.repos.agent_runs.get_by_execution(started.execution_id)
    assert run is not None
    resumed = await service.resume_agent_run(run.run_id, BYSTANDER)

    # The clone carried triggered_by and input_data — the task id is literally
    # inside that dict — and left the column NULL, so the whole chain fell out of
    # the bound the moment the run came back. And authorized_by was overwritten
    # with whoever pressed resume, re-rooting a delegated chain on them.
    assert resumed.agent_task_id == delegated.task_id
    assert resumed.authorized_by == OWNER
    principals = await service.repos.executions.bounding_principals(resumed.execution_id)
    assert agent_principal(delegator) in principals
    assert OWNER in principals
    # The resumer bounds it too, as a caller — a row, not a replacement.
    assert BYSTANDER in principals
    assert await _effective(service, resumed.execution_id, delegate, room_id) == {"analysis"}


@pytest.mark.asyncio
async def test_the_delegate_is_not_the_asker_of_its_own_task(service):
    room_id = await _room(service)
    delegate = await _agent(service, room_id, "Architect")
    task = await service.open_agent_task(room_id, delegate, ASK, requested_by=OWNER)
    await service.start_agent_task(task.task_id)

    # The asker and the agent being asked are two parties, and only the first may
    # take the task back or add to the asking half of the conversation. An agent
    # that counted as its own asker could cancel the task it was given and forge
    # ASKER-role turns into its own record of what it was told to do.
    with pytest.raises(TaskNotFoundError):
        await service.cancel_agent_task(task.task_id, requested_by=delegate)
    with pytest.raises(TaskNotFoundError):
        await service.continue_agent_task(task.task_id, ASK, requested_by=delegate)
    with pytest.raises(TaskNotFoundError):
        await service.cancel_agent_task(task.task_id, requested_by=agent_principal(delegate))

    assert (await service.get_agent_task(task.task_id, viewer_id=OWNER)).state is (
        AgentTaskState.WORKING
    )
    roles = [m.role for m in await service.list_agent_task_messages(task.task_id, viewer_id=OWNER)]
    assert roles == [TaskMessageRole.ASKER]


# ── The chain is derived, never taken from the caller ────────────────────────


@pytest.mark.asyncio
async def test_a_delegation_that_names_no_parent_still_arrives_inside_its_chain(service):
    room_id = await _room(service)
    first = await _agent(service, room_id, "Architect")
    second = await _agent(service, room_id, "Architect")

    root = await service.open_agent_task(room_id, first, ASK, requested_by=OWNER)
    running_root = await service.start_agent_task(root.task_id)
    # No parent_task_id, here or below. Twelve hops of A asking B asking A used to
    # arrive as twelve separate roots, every one of them depth zero with no chain
    # rows at all, and require_delegable was handed an empty ancestry to find a
    # cycle in. The cycle was real; only the evidence of it was missing.
    delegated = await service.open_agent_task(
        room_id, second, ASK, requested_by=OWNER, delegating_agent_id=first
    )
    assert delegated.depth == 1
    assert await service.repos.agent_tasks.ancestry(delegated.task_id) == (first,)
    assert delegated.context_id == root.context_id
    assert delegated.delegating_run_id == running_root.execution_id

    await service.start_agent_task(delegated.task_id)
    with pytest.raises(DelegationCycleError):
        await service.open_agent_task(
            room_id, first, ASK, requested_by=OWNER, delegating_agent_id=second
        )


@pytest.mark.asyncio
async def test_an_agent_running_under_no_task_has_nothing_to_delegate_from(service):
    room_id = await _room(service)
    first = await _agent(service, room_id, "Architect")
    second = await _agent(service, room_id, "Architect")

    # A refusal, not a fresh root. Rooting it here is what let the chain be opted
    # out of one call at a time.
    with pytest.raises(TaskNotFoundError):
        await service.open_agent_task(
            room_id, second, ASK, requested_by=OWNER, delegating_agent_id=first
        )
    assert not await _rows(service, "SELECT task_id FROM agent_tasks")


@pytest.mark.asyncio
async def test_a_parent_the_delegator_is_not_running_under_is_refused(service):
    room_id = await _room(service)
    first = await _agent(service, room_id, "Architect")
    second = await _agent(service, room_id, "Architect")
    stranger = await _agent(service, room_id, "Architect")

    root = await service.open_agent_task(room_id, first, ASK, requested_by=OWNER)
    await service.start_agent_task(root.task_id)
    elsewhere = await service.open_agent_task(room_id, stranger, ASK, requested_by=OWNER)

    with pytest.raises(AuthorizationError):
        await service.open_agent_task(
            room_id,
            second,
            ASK,
            requested_by=OWNER,
            delegating_agent_id=first,
            parent_task_id=elsewhere.task_id,
        )


# ── Authority never launders upward ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_agent_cannot_become_the_human_a_chain_is_authorized_by(service):
    room_id = await _room(service)
    first = await _agent(service, room_id, "Architect")
    second = await _agent(service, room_id, "Architect")

    # An agent naming itself as the asker of a root task would write agent:… into
    # executions.authorized_by, which is one arm of the union every spend-point
    # bounds by. The chain would then be authorized by an agent, and removing the
    # person from the room would change nothing about what it could spend.
    with pytest.raises(AuthorizationError):
        await service.open_agent_task(room_id, second, ASK, requested_by=agent_principal(first))
    assert not await _rows(service, "SELECT task_id FROM agent_tasks")


@pytest.mark.asyncio
async def test_the_root_human_survives_every_hop_and_is_what_the_run_records(service):
    room_id = await _room(service)
    first = await _agent(service, room_id, "Architect")
    second = await _agent(service, room_id, "Architect")

    root = await service.open_agent_task(room_id, first, ASK, requested_by=OWNER)
    await service.start_agent_task(root.task_id)
    delegated = await service.open_agent_task(
        room_id, second, ASK, requested_by=OWNER, delegating_agent_id=first
    )
    assert delegated.authorized_by == OWNER
    started = await service.start_agent_task(delegated.task_id)
    execution = await service.repos.executions.get(started.execution_id)
    assert execution is not None
    assert execution.authorized_by == OWNER

    # And the person is load-bearing, not decorative: take their membership away
    # and the delegated run can spend nothing.
    await service.db.execute(
        "DELETE FROM room_members WHERE room_id = ? AND user_id = ?", (room_id, OWNER)
    )
    await service.db.commit()
    assert not await _effective(service, started.execution_id, second, room_id)


@pytest.mark.asyncio
async def test_a_person_cannot_inherit_another_chains_authority_by_naming_its_task(service):
    room_id = await _room(service)
    delegate = await _agent(service, room_id, "Architect")
    await service.invite_room_member(room_id, BYSTANDER, "editor", OWNER)

    owners = await service.open_agent_task(room_id, delegate, ASK, requested_by=OWNER)
    with pytest.raises(AuthorizationError):
        await service.open_agent_task(
            room_id, delegate, ASK, requested_by=BYSTANDER, parent_task_id=owners.task_id
        )


# ── Cycles and depth ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_two_step_loop_back_to_the_first_agent_is_refused(service):
    room_id = await _room(service)
    first = await _agent(service, room_id, "Architect")
    second = await _agent(service, room_id, "Architect")

    task = await _chain(service, room_id, [first, second])
    await service.start_agent_task(task.task_id)
    with pytest.raises(DelegationCycleError):
        await service.open_agent_task(
            room_id, first, ASK, requested_by=OWNER, delegating_agent_id=second
        )


@pytest.mark.asyncio
async def test_a_three_step_loop_back_to_the_first_agent_is_refused(service):
    room_id = await _room(service)
    first = await _agent(service, room_id, "Architect")
    second = await _agent(service, room_id, "Architect")
    third = await _agent(service, room_id, "Architect")

    # A asks B asks C is fine; C asking A closes the loop, and only looking at the
    # whole ancestry sees it. One step back sees nothing wrong here at all.
    task = await _chain(service, room_id, [first, second, third])
    await service.start_agent_task(task.task_id)
    with pytest.raises(DelegationCycleError):
        await service.open_agent_task(
            room_id, first, ASK, requested_by=OWNER, delegating_agent_id=third
        )


@pytest.mark.asyncio
async def test_a_chain_exactly_at_the_limit_is_allowed(service):
    room_id = await _room(service)
    agents = [await _agent(service, room_id, "Architect") for _ in range(MAX_DELEGATION_DEPTH + 1)]
    deepest = await _chain(service, room_id, agents)
    assert deepest.depth == MAX_DELEGATION_DEPTH


@pytest.mark.asyncio
async def test_a_chain_one_deeper_than_the_limit_is_refused(service):
    room_id = await _room(service)
    agents = [await _agent(service, room_id, "Architect") for _ in range(MAX_DELEGATION_DEPTH + 1)]
    deepest = await _chain(service, room_id, agents)
    await service.start_agent_task(deepest.task_id)
    one_more = await _agent(service, room_id, "Architect")

    with pytest.raises(DelegationDepthExceededError):
        await service.open_agent_task(
            room_id, one_more, ASK, requested_by=OWNER, delegating_agent_id=agents[-1]
        )


# ── The lifecycle's refusals ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_only_the_asker_may_continue_or_cancel_the_task(service):
    room_id = await _room(service)
    await service.invite_room_member(room_id, BYSTANDER, "editor", OWNER)
    delegate = await _agent(service, room_id, "Architect")

    task = await service.open_agent_task(room_id, delegate, ASK, requested_by=OWNER)
    await service.start_agent_task(task.task_id)
    await service.require_agent_task_input(
        task.task_id, (Part(kind=PartKind.TEXT, content="which migration?"),), by_agent_id=delegate
    )

    # An editor of this room: they may read it and they may write in it, and they
    # did not ask for this. Room membership is not the gate — being the asker is.
    # And the refusal says only that there is no such task, so a caller cannot sort
    # the ids they may not touch from the ids that do not exist.
    for refuse in (
        service.continue_agent_task(task.task_id, ASK, requested_by=BYSTANDER),
        service.cancel_agent_task(task.task_id, requested_by=BYSTANDER),
    ):
        with pytest.raises(TaskNotFoundError) as refusal:
            await refuse
        assert task.task_id not in str(refusal.value)

    task_now = await service.get_agent_task(task.task_id, viewer_id=OWNER)
    assert task_now.state is AgentTaskState.INPUT_REQUIRED
    assert len(await service.list_agent_task_messages(task.task_id, viewer_id=OWNER)) == 2


@pytest.mark.asyncio
async def test_the_interruptible_states_are_reachable_and_leavable(service):
    room_id = await _room(service)
    delegate = await _agent(service, room_id, "Researcher")

    task = await service.open_agent_task(room_id, delegate, ASK, requested_by=OWNER)
    await service.start_agent_task(task.task_id)

    asked = await service.require_agent_task_input(
        task.task_id, (Part(kind=PartKind.TEXT, content="which migration?"),), by_agent_id=delegate
    )
    assert asked.state is AgentTaskState.INPUT_REQUIRED
    resumed = await service.continue_agent_task(
        task.task_id, (Part(kind=PartKind.TEXT, content="035"),), requested_by=OWNER
    )
    assert resumed.state is AgentTaskState.WORKING

    escalated = await service.escalate_agent_task(
        task.task_id, reason="needs security", by_agent_id=delegate
    )
    assert escalated.state is AgentTaskState.AUTH_REQUIRED
    # "No" ends the task as a refusal, not as a failure of the agent that asked.
    declined = await service.resolve_agent_task_escalation(
        task.task_id, granted=False, by_user_id=OWNER
    )
    assert declined.state is AgentTaskState.REJECTED
    assert OWNER in declined.refusal_reason

    roles = [m.role for m in await service.list_agent_task_messages(task.task_id, viewer_id=OWNER)]
    assert roles == [TaskMessageRole.ASKER, TaskMessageRole.DELEGATE, TaskMessageRole.ASKER]


@pytest.mark.asyncio
async def test_a_completed_task_cannot_be_canceled(service):
    room_id = await _room(service)
    delegate = await _agent(service, room_id, "Researcher")

    task = await service.open_agent_task(room_id, delegate, ASK, requested_by=OWNER)
    await service.start_agent_task(task.task_id)
    done = await service.complete_agent_task(
        task.task_id, (Part(kind=PartKind.TEXT, content="assessed"),), by_agent_id=delegate
    )
    assert done.state is AgentTaskState.COMPLETED

    with pytest.raises(TaskNotCancelableError):
        await service.cancel_agent_task(task.task_id, requested_by=OWNER)
    assert (await service.get_agent_task(task.task_id, viewer_id=OWNER)).state is (
        AgentTaskState.COMPLETED
    )


@pytest.mark.asyncio
async def test_the_loser_of_two_cancels_is_told_the_task_cannot_be_canceled(service):
    room_id = await _room(service)
    delegate = await _agent(service, room_id, "Architect")
    task = await service.open_agent_task(room_id, delegate, ASK, requested_by=OWNER)

    outcomes = await asyncio.gather(
        service.cancel_agent_task(task.task_id, requested_by=OWNER),
        service.cancel_agent_task(task.task_id, requested_by=OWNER),
        return_exceptions=True,
    )
    # The task ended between the loser's read and its write. It is owed the name it
    # would have been given a moment later, not a description of the write that
    # touched no rows.
    assert sum(isinstance(o, TaskNotCancelableError) for o in outcomes) == 1
    assert (await service.get_agent_task(task.task_id, viewer_id=OWNER)).state is (
        AgentTaskState.CANCELED
    )


@pytest.mark.asyncio
async def test_two_concurrent_starts_leave_no_orphaned_run(service):
    room_id = await _room(service)
    delegate = await _agent(service, room_id, "Architect")
    task = await service.open_agent_task(room_id, delegate, ASK, requested_by=OWNER)

    outcomes = await asyncio.gather(
        service.start_agent_task(task.task_id),
        service.start_agent_task(task.task_id),
        return_exceptions=True,
    )
    assert sum(isinstance(o, DomainError) for o in outcomes) == 1

    started = await service.get_agent_task(task.task_id, viewer_id=OWNER)
    executions = [r["execution_id"] for r in await _rows(service, "SELECT * FROM executions")]
    # The loser refused before writing anything. It used to build a session, an
    # execution and a run envelope, and then lose the attach — leaving the run
    # holding a live credential and a lease nothing would come back to close.
    assert executions == [started.execution_id]
    assert len(await _rows(service, "SELECT run_id FROM agent_runs")) == 1
    assert len(await _rows(service, "SELECT session_id FROM sessions")) == 1


@pytest.mark.asyncio
async def test_a_failure_preparing_the_run_leaves_no_working_orphan(service, monkeypatch):
    """The WORKING transition used to commit on its own, ahead of the
    session/execution/run-creation transaction: a failure between the two
    left the task WORKING with no run behind it forever, because the orphan
    settler only scans MENTION triggers. Now the whole handoff is one
    transaction, so a failure anywhere in it must leave the task exactly
    where it started and write no execution or session row at all.
    """
    room_id = await _room(service)
    delegate = await _agent(service, room_id, "Architect")
    task = await service.open_agent_task(room_id, delegate, ASK, requested_by=OWNER)
    before = await service.get_agent_task(task.task_id, viewer_id=OWNER)

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("run preparation failed")

    monkeypatch.setattr(service, "_prepare_agent_run", _boom)

    with pytest.raises(RuntimeError):
        await service.start_agent_task(task.task_id)

    after = await service.get_agent_task(task.task_id, viewer_id=OWNER)
    assert after == before
    assert after.state is AgentTaskState.SUBMITTED
    assert after.execution_id is None
    assert await _rows(service, "SELECT execution_id FROM executions") == []
    assert await _rows(service, "SELECT session_id FROM sessions") == []
    assert await _rows(service, "SELECT run_id FROM agent_runs") == []


@pytest.mark.asyncio
async def test_an_illegal_transition_leaves_the_row_exactly_as_it_was(service):
    room_id = await _room(service)
    delegate = await _agent(service, room_id, "Researcher")

    task = await service.open_agent_task(room_id, delegate, ASK, requested_by=OWNER)
    before = await service.get_agent_task(task.task_id, viewer_id=OWNER)

    # SUBMITTED has no edge to COMPLETED: a task cannot finish before it started.
    with pytest.raises(DomainError):
        await service.complete_agent_task(
            task.task_id, (Part(kind=PartKind.TEXT, content="done"),), by_agent_id=delegate
        )

    assert await service.get_agent_task(task.task_id, viewer_id=OWNER) == before
    # And the message that would have carried the answer was never written either.
    assert len(await service.list_agent_task_messages(task.task_id, viewer_id=OWNER)) == 1


@pytest.mark.asyncio
async def test_a_mode_this_deployment_cannot_produce_creates_no_task_at_all(service):
    room_id = await _room(service)
    delegate = await _agent(service, room_id, "Researcher")

    with pytest.raises(UnsupportedOperationError):
        await service.open_agent_task(
            room_id,
            delegate,
            ASK,
            requested_by=OWNER,
            accepted_output_modes=("audio/ogg",),
        )
    # Refused before anything was written, rather than opened and then abandoned:
    # a task row in a state nobody will ever move is worse than no row.
    assert not await _rows(service, "SELECT task_id FROM agent_tasks")
