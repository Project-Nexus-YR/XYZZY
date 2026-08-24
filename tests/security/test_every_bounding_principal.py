"""Regression: a run is bounded by every principal it has, not by a list of kinds.

Round eight put the steerers on the authorization and called the series closed. It was
one participant short. The authorization carried the authorizing human and the
steerers, and the human actually DRIVING the run — the delegate who called
``POST /executions/{id}/step`` — had no durable home on it at all. The only record of
that person was ``agent_runs.acting_user_id``, one mutable column documented as
"initiator, then last caller", and every advance overwrote it: by the time an approval
was granted it read as the run's own principal. So a delegate holding only ``writing``
could step somebody else's run, park ``task.create`` at approval, be narrowed to
nothing or removed from the room, and have the task written anyway when the principal
approved. The identical attack by a *steerer* was refused, which is what made round
eight look finished.

The fix is not a fourth identity threaded through the doors. It is that identities are
no longer enumerated at a door at all: ``RunAuthorization`` carries one
``BoundingPrincipals`` set, ``_authorization_for`` takes no principal argument through
which a caller could hand it a short one, and the set is filled by a single union over
every durable row that names a human against the run. ``UnboundedTerms.spend_under``
refuses to produce a spendable set for a run other than the one its terms were read
for, so deriving from a partial set and spending it is an error rather than a quietly
wider grant.

Two false records in the approval paths are closed here too. An approval that gated no
tool call could settle a live run ``APPROVAL_REFUSED`` or put it back on a fresh lease
with nothing to prompt it; and ``cancel_execution`` and ``remove_agent_from_room``
abandoned an approval PENDING for ever, still grantable against a run that had ended.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path
from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
import multiplayer.services.service as service_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import ApprovalStatus, BranchMode, HarnessState
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.capabilities import (
    UNKNOWN_PRINCIPAL,
    BoundingPrincipals,
    CapabilityTerms,
    RunAuthorization,
    UnboundedTerms,
)
from multiplayer.services.service import AuthorizationError, DomainError, MultiplayerService

OWNER = "owner"
# The delegate who drives one step of Owner's run. Everything below turns on his
# grant being read when it is spent rather than when he stepped.
BOB = "bob"
STEERER = "steerer"

# The two tools whose calls stop at a human. The approval is a twelve-hour window in
# front of a spend, which is the whole reason a stale bound is worth anything.
GATED = [
    ("task.create", {"title": "Roll the migration back"}),
    ("artifact.write", {"name": "Rollout plan", "description": "the plan"}),
]


class _AsksForToolsThenAnswers:
    """Asks for each tool in turn, one per prompt, then finishes."""

    def __init__(self, *calls: tuple[str, dict[str, Any]]) -> None:
        self.calls = list(calls)
        self.prompts: list[str] = []

    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del schema
        self.prompts.append(prompt)
        index = len(self.prompts) - 1
        if index < len(self.calls):
            tool, tool_input = self.calls[index]
            return {
                "action": "tool",
                "tool": tool,
                "input": tool_input,
                "output": {"content": f"requesting {tool}"},
            }
        return {"action": "finish", "output": {"content": "here is the answer"}}


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({OWNER, BOB, STEERER}))
    await svc.initialize()
    yield svc
    await db.close()


async def _room_with_synthesizer(
    svc: MultiplayerService, provider: Any, bob_holds: list[str] | None = None
) -> tuple[str, str]:
    org = await svc.create_organization("Bound org", "bound-org", OWNER)
    workspace = await svc.create_workspace(org.org_id, "Main", "main", OWNER)
    room = await svc.create_room(workspace.workspace_id, "Decision", OWNER)
    for member in (BOB, STEERER):
        await svc.invite_room_member(room.room_id, member, "editor", OWNER)
    # Exactly the capability the gated tools need, and nothing else.
    await svc.set_member_capabilities(room.room_id, BOB, bob_holds or ["writing"], OWNER)
    await svc.set_member_capabilities(room.room_id, STEERER, ["writing"], OWNER)
    svc.nexus = NexusAgentBridge(model_provider=provider)
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(
        room.room_id,
        next(t.template_id for t in templates if t.name == "Synthesizer"),
        name="Synthesizer",
        requested_by=OWNER,
    )
    return room.room_id, agent.agent_id


async def _delegated_run_waiting_on_a_reviewer(
    svc: MultiplayerService, provider: Any, bob_holds: list[str] | None = None
) -> tuple[str, str, str]:
    """Bob drives one step of Owner's run, and it stops at the approval he reached."""
    room_id, agent_id = await _room_with_synthesizer(svc, provider, bob_holds)
    session = await svc.start_agent_session(room_id, agent_id)
    execution = await svc.start_execution(session.session_id, OWNER)
    await svc.execute_agent_step(execution.execution_id, "Assess the deploy.", BOB)
    approvals = await svc.list_pending_approvals(room_id)
    assert len(approvals) == 1, approvals
    return room_id, execution.execution_id, approvals[0].approval_id


async def _writes(svc: MultiplayerService, room_id: str) -> list[str]:
    tasks = [task.task_id for task in await svc.repos.tasks.list_by_room(room_id)]
    artifacts = [a.artifact_id for a in await svc.repos.artifacts.list_by_room(room_id)]
    return tasks + artifacts


async def _statuses(svc: MultiplayerService) -> set[str]:
    rows = await svc.db.fetch_all("SELECT status FROM tool_requests")
    return {str(row["status"]) for row in rows}


# ── The thirteenth relocation: the caller who drove the step ─────────────────


@pytest.mark.parametrize(("tool", "tool_input"), GATED)
@pytest.mark.asyncio
async def test_narrowing_the_acting_caller_while_the_approval_waits_refuses_the_tool(
    service: MultiplayerService, tool: str, tool_input: dict[str, Any]
) -> None:
    """His grant is read when it is spent, not when he stepped the run."""
    svc = service
    provider = _AsksForToolsThenAnswers((tool, tool_input))
    room_id, _, approval_id = await _delegated_run_waiting_on_a_reviewer(svc, provider)

    await svc.set_member_capabilities(room_id, BOB, [], OWNER)
    await svc.approve_action(approval_id, OWNER)

    assert await _writes(svc, room_id) == []
    assert await _statuses(svc) == {"REJECTED"}
    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert "tool.call_completed" not in types
    assert "tool.call_rejected" in types


@pytest.mark.parametrize(("tool", "tool_input"), GATED)
@pytest.mark.asyncio
async def test_removing_the_acting_caller_while_the_approval_waits_refuses_the_tool(
    service: MultiplayerService, tool: str, tool_input: dict[str, Any]
) -> None:
    """Out of the room is not a narrower grant; it is no grant at all."""
    svc = service
    provider = _AsksForToolsThenAnswers((tool, tool_input))
    room_id, _, approval_id = await _delegated_run_waiting_on_a_reviewer(svc, provider)

    await svc.repos.room_members.remove(room_id, BOB)
    await svc.approve_action(approval_id, OWNER)

    assert await _writes(svc, room_id) == []
    assert await _statuses(svc) == {"REJECTED"}


@pytest.mark.parametrize(("tool", "tool_input"), GATED)
@pytest.mark.asyncio
async def test_narrowing_the_steerer_while_the_approval_waits_is_still_refused(
    service: MultiplayerService, tool: str, tool_input: dict[str, Any]
) -> None:
    """The control that made round eight look complete. It still holds.

    A steerer is now one entry in the same set the acting caller is in, rather than a
    field of its own, so this asserts the collection did not lose what it replaced.
    The steer is recorded without the delegation gate, so her name reaches the bound
    through her intervention row alone: this fails if the union stops reading those,
    rather than passing on the caller arm having recorded her twice.
    """
    svc = service
    provider = _AsksForToolsThenAnswers((tool, tool_input))
    room_id, agent_id = await _room_with_synthesizer(svc, provider)
    session = await svc.start_agent_session(room_id, agent_id)
    execution = await svc.start_execution(session.session_id, OWNER)
    await svc.intervene_execution(execution.execution_id, STEERER, "file it against the rollback")
    await svc.execute_agent_step(execution.execution_id, "Assess the deploy.", OWNER)
    approval = (await svc.list_pending_approvals(room_id))[0]

    await svc.set_member_capabilities(room_id, STEERER, [], OWNER)
    await svc.approve_action(approval.approval_id, OWNER)

    assert await _writes(svc, room_id) == []
    assert await _statuses(svc) == {"REJECTED"}


@pytest.mark.asyncio
async def test_a_legitimate_delegated_step_still_runs_end_to_end(
    service: MultiplayerService,
) -> None:
    """The bound is an authority, not a penalty for not being the run's own principal."""
    svc = service
    provider = _AsksForToolsThenAnswers(GATED[0])
    room_id, execution_id, approval_id = await _delegated_run_waiting_on_a_reviewer(svc, provider)

    await svc.approve_action(approval_id, OWNER)

    assert len(await _writes(svc, room_id)) == 1
    assert await _statuses(svc) == {"EXECUTED"}
    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert "tool.call_completed" in types
    # The turn ran on past its approval to an answer rather than stopping there.
    assert len(provider.prompts) == 2
    run = await svc.repos.agent_runs.get_by_execution(execution_id)
    assert run is not None and run.settlement is not None
    assert run.settlement.value == "END_TURN"


@pytest.mark.asyncio
async def test_a_resumed_turn_honours_the_acting_callers_current_grant(
    service: MultiplayerService,
) -> None:
    """Not the grant he held when he stepped, and not a blanket lockout either.

    Bob steps the turn holding ``writing`` and ``analysis``, and is cut back to
    ``analysis`` while the approval waits. He can still act on the run — the delegation
    gate passes on ``analysis`` — so what refuses the parked ``task.create`` and the
    ``message.react`` the resumed turn asks for next is his current grant, capability
    by capability, rather than his absence.
    """
    svc = service
    provider = _AsksForToolsThenAnswers(
        GATED[0], ("message.react", {"message_id": "", "emoji": "+1"})
    )
    room_id, execution_id, approval_id = await _delegated_run_waiting_on_a_reviewer(
        svc, provider, bob_holds=["writing", "analysis"]
    )

    await svc.set_member_capabilities(room_id, BOB, ["analysis"], OWNER)
    await svc.approve_action(approval_id, OWNER)

    assert await _writes(svc, room_id) == []
    # Both calls were reached and both were refused: the resumed prompt happened.
    assert len(provider.prompts) >= 2
    rejected = [
        event.payload
        for event in await svc.get_room_events(room_id)
        if event.event_type.value == "tool.call_rejected"
    ]
    assert {payload["tool"] for payload in rejected} == {"task.create", "message.react"}
    assert await _statuses(svc) == {"REJECTED"}


@pytest.mark.asyncio
async def test_the_acting_caller_has_a_durable_home_that_cannot_be_edited(
    service: MultiplayerService,
) -> None:
    """The column that lost him is last-writer-wins; the record that keeps him is a set."""
    svc = service
    provider = _AsksForToolsThenAnswers(GATED[0])
    _, execution_id, _ = await _delegated_run_waiting_on_a_reviewer(svc, provider)

    # The mutable column has already been overwritten with the run's own principal —
    # which is exactly how the caller used to be lost.
    run = await svc.repos.agent_runs.get_by_execution(execution_id)
    assert run is not None and run.acting_user_id == OWNER
    assert BOB in await svc.repos.executions.bounding_principals(execution_id)

    with pytest.raises(Exception, match="never rewritten"):
        await svc.db.execute(
            "UPDATE execution_callers SET caller_id = ? WHERE execution_id = ?",
            (OWNER, execution_id),
        )
    with pytest.raises(Exception, match="never deleted"):
        await svc.db.execute(
            "DELETE FROM execution_callers WHERE execution_id = ?", (execution_id,)
        )
    assert BOB in await svc.repos.executions.bounding_principals(execution_id)


@pytest.mark.asyncio
async def test_a_run_cannot_be_advanced_by_somebody_the_records_do_not_name(
    service: MultiplayerService,
) -> None:
    """The backstop, and the reason a fourteenth cannot enter through a new door.

    Every service door that moves a run on a human's behalf writes that human into the
    run's callers, but that is a discipline, and a discipline is what failed thirteen
    times. The database keeps the same record from the statement that does the moving:
    a repository advance that no service door has heard of still names its caller, and
    naming one records it. So a path written next year cannot advance a run under
    somebody whose grant then bounds nothing.
    """
    svc = service
    provider = _AsksForToolsThenAnswers(GATED[0])
    _, execution_id, _ = await _delegated_run_waiting_on_a_reviewer(svc, provider)
    run = await svc.repos.agent_runs.get_by_execution(execution_id)
    assert run is not None
    unnamed = "a_door_nobody_wrote_yet"
    assert unnamed not in await svc.repos.executions.bounding_principals(execution_id)

    await svc.repos.agent_runs.advance(
        run.run_id, HarnessState.STREAMING, run.lease_expires_at, unnamed
    )

    assert unnamed in await svc.repos.executions.bounding_principals(execution_id)


@pytest.mark.asyncio
async def test_a_refused_caller_does_not_become_a_bound_on_the_run(
    service: MultiplayerService,
) -> None:
    """Only a caller the gate admits is written down. A refusal is not participation."""
    svc = service
    provider = _AsksForToolsThenAnswers(GATED[0])
    room_id, agent_id = await _room_with_synthesizer(svc, provider)
    await svc.set_member_capabilities(room_id, BOB, [], OWNER)
    session = await svc.start_agent_session(room_id, agent_id)
    execution = await svc.start_execution(session.session_id, OWNER)

    with pytest.raises(AuthorizationError):
        await svc.execute_agent_step(execution.execution_id, "Assess the deploy.", BOB)

    assert BOB not in await svc.repos.executions.bounding_principals(execution.execution_id)
    # And the run Owner authorized is still able to spend what Owner may lend.
    await svc.execute_agent_step(execution.execution_id, "Assess the deploy.", OWNER)
    assert await svc.list_pending_approvals(room_id)


@pytest.mark.asyncio
async def test_the_author_of_a_branch_prompt_is_the_run_that_prompt_authorizes(
    service: MultiplayerService,
) -> None:
    """The fifth candidate, and why it is not a fifth kind.

    A branch's ``initiating_prompt`` drives every run on it, which makes its author
    look like a steerer who is not in the set. She is not one, because she is the
    authorizing principal of those runs: ``create_branch`` is the only maker of a
    lifecycle-managed branch and it opens each execution with
    ``authorized_by=initiated_by``, and a run on any other branch is prompted with the
    text its own caller passed. That identity is the whole of the argument, so it is
    asserted here rather than reasoned about — if it ever stops holding, an author's
    text will be driving a run her grant does not bound, and this says so.
    """
    svc = service
    provider = _AsksForToolsThenAnswers()
    room_id, agent_id = await _room_with_synthesizer(svc, provider)

    branch, executions = await svc.start_branch(
        room_id, BranchMode.TURN_LOCKED_SINGLE, "Compare the rollback options.", BOB, [agent_id]
    )

    assert executions
    for execution in executions:
        assert execution.authorized_by == branch.initiated_by == BOB
        assert BOB in await svc.repos.executions.bounding_principals(execution.execution_id)


# ── An approval only ends the run whose tool it actually gated ───────────────


async def _live_run_and_an_approval_that_gates_nothing(
    svc: MultiplayerService,
) -> tuple[str, str, str]:
    """An approval opened through the approvals route against a run that asked for none."""
    provider = _AsksForToolsThenAnswers()
    room_id, agent_id = await _room_with_synthesizer(svc, provider)
    session = await svc.start_agent_session(room_id, agent_id)
    execution = await svc.start_execution(session.session_id, OWNER)
    approval = await svc.request_approval(
        room_id,
        execution.execution_id,
        agent_id,
        "something nobody's turn is waiting on",
        requested_by=OWNER,
        require_member=True,
    )
    return room_id, execution.execution_id, approval.approval_id


@pytest.mark.asyncio
async def test_an_approval_that_gated_nothing_cannot_settle_the_run(
    service: MultiplayerService,
) -> None:
    """Refusing a question nobody asked is not an account of why a run ended."""
    svc = service
    room_id, execution_id, approval_id = await _live_run_and_an_approval_that_gates_nothing(svc)
    before = await svc.repos.agent_runs.get_by_execution(execution_id)
    assert before is not None

    rejected = await svc.reject_action(approval_id, OWNER, require_member=True)

    assert rejected.status is ApprovalStatus.REJECTED
    after = await svc.repos.agent_runs.get_by_execution(execution_id)
    assert after is not None
    assert after.settlement is None
    assert after.harness_state is before.harness_state
    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert "agent.run.settled" not in types


@pytest.mark.asyncio
async def test_an_approval_that_gated_nothing_cannot_resume_the_run(
    service: MultiplayerService,
) -> None:
    """The worse half: a fresh lease with nothing suspended to prompt the run again."""
    svc = service
    _, execution_id, approval_id = await _live_run_and_an_approval_that_gates_nothing(svc)
    before = await svc.repos.agent_runs.get_by_execution(execution_id)
    assert before is not None

    await svc.reject_action(approval_id, OWNER, require_member=True, continue_turn=True)

    after = await svc.repos.agent_runs.get_by_execution(execution_id)
    assert after is not None
    assert after.settlement is None
    # Untouched: not settled, and not put back on a lease with nothing to prompt it.
    assert after.harness_state is before.harness_state
    assert after.lease_expires_at == before.lease_expires_at
    parked = await svc.db.fetch_all("SELECT execution_id FROM suspended_turns")
    assert parked == []


@pytest.mark.asyncio
async def test_the_approval_a_run_really_did_stop_at_still_ends_it(
    service: MultiplayerService,
) -> None:
    """The control for both tests above: a real gate still settles what it gated."""
    svc = service
    provider = _AsksForToolsThenAnswers(GATED[0])
    _, execution_id, approval_id = await _delegated_run_waiting_on_a_reviewer(svc, provider)

    await svc.reject_action(approval_id, OWNER, require_member=True)

    run = await svc.repos.agent_runs.get_by_execution(execution_id)
    assert run is not None
    assert run.harness_state is HarnessState.SETTLED
    assert run.settlement is not None and run.settlement.value == "APPROVAL_REFUSED"


# ── No approval outlives the run it was holding ──────────────────────────────


@pytest.mark.asyncio
async def test_cancelling_a_run_closes_the_approval_it_was_holding(
    service: MultiplayerService,
) -> None:
    """A settled run is never swept again, so expiry alone abandoned this for ever."""
    svc = service
    provider = _AsksForToolsThenAnswers(GATED[0])
    room_id, execution_id, approval_id = await _delegated_run_waiting_on_a_reviewer(svc, provider)

    await svc.cancel_execution(execution_id, OWNER, require_member=True)

    approval = await svc.repos.approvals.get(approval_id)
    assert approval is not None and approval.status is ApprovalStatus.EXPIRED
    assert await _statuses(svc) == {"REJECTED"}
    with pytest.raises(DomainError, match="is not pending"):
        await svc.approve_action(approval_id, OWNER)
    assert await _writes(svc, room_id) == []


@pytest.mark.asyncio
async def test_removing_the_agent_closes_the_approval_it_was_holding(
    service: MultiplayerService,
) -> None:
    """The other path that settles a run without ever reaching the expiry sweep."""
    svc = service
    provider = _AsksForToolsThenAnswers(GATED[0])
    room_id, _, approval_id = await _delegated_run_waiting_on_a_reviewer(svc, provider)
    approval = await svc.repos.approvals.get(approval_id)
    assert approval is not None

    await svc.remove_agent_from_room(approval.agent_id, room_id, OWNER, require_member=True)

    closed = await svc.repos.approvals.get(approval_id)
    assert closed is not None and closed.status is ApprovalStatus.EXPIRED
    assert await _statuses(svc) == {"REJECTED"}
    with pytest.raises(DomainError, match="is not pending"):
        await svc.approve_action(approval_id, OWNER)
    assert await _writes(svc, room_id) == []


# ── What makes a fourteenth relocation something you cannot write by accident ──


def _service_ast() -> ast.Module:
    source = inspect.getsourcefile(service_module)
    assert source is not None
    return ast.parse(Path(source).read_text(encoding="utf-8"))


def _functions_mentioning(name: str, tree: ast.Module | None = None) -> set[str]:
    """Every function in the service — or in ``tree`` — whose body names ``name`` at all."""
    mentions: set[str] = set()
    for node in ast.walk(tree if tree is not None else _service_ast()):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name) and inner.id == name:
                mentions.add(node.name)
            elif isinstance(inner, ast.Attribute) and inner.attr == name:
                mentions.add(node.name)
    return mentions


def _functions_constructing_a_bounding_set(tree: ast.Module | None = None) -> set[str]:
    """Every function that ends up holding one, by either route that makes one.

    Naming the class is one route and it was the only one this guard could see. The
    other is ``also_bounded_by``, which takes principals from its caller and hands
    back a set built from them — the exact shape of all fourteen relocations, and
    invisible to a guard that reads for the class name. Both are counted here so a
    door that adds a principal it happens to know about has to be on the list below.
    """
    return _functions_mentioning("BoundingPrincipals", tree) | _functions_mentioning(
        "also_bounded_by", tree
    )


def test_a_run_authorization_names_its_principals_only_as_one_set() -> None:
    """There is no field to pick a single principal out of, so no door can pick one."""
    fields = RunAuthorization.__dataclass_fields__
    assert set(fields) == {"run_id", "agent_id", "room_id", "bounding", "required_capability"}
    assert fields["bounding"].default is dataclasses.MISSING
    assert fields["bounding"].default_factory is dataclasses.MISSING


def test_the_authorization_factory_takes_no_principal_from_its_caller() -> None:
    """A caller cannot hand it a set that is one short, because it takes none at all.

    This is the assertion a fourteenth relocation would have to edit out first. Every
    previous round's defect entered through an identity a caller passed to this
    factory — the authorizing human, then the steerers, then the acting caller — and
    the parameter list is where the shortfall was expressible.
    """
    params = set(inspect.signature(MultiplayerService._authorization_for).parameters)
    assert params == {"self", "execution_id", "agent_id", "room_id", "required_capability"}


def test_one_durable_union_is_the_only_thing_that_fills_the_bounding_set() -> None:
    """A new kind of participant is a new arm of that union and nothing else."""
    assert _functions_mentioning("bounding_principals") == {"_authorization_for"}


def test_every_construction_of_a_bounding_set_is_a_recorded_decision() -> None:
    """The run's own set is read; the rest are gates asking about named principals."""
    assert _functions_constructing_a_bounding_set() == {
        "_lendable_terms",  # the parameter every one of these hands it
        "_authorization_for",  # the run's own principals, read whole from the rows
        "_require_delegated_authority",  # may this caller act on somebody else's run
        "_require_agent_run_authority",  # may this caller steer this agent at all
        "_invoke_mentioned_agent_in_transaction",  # may this member open a turn here
        "open_agent_task",  # may this asker — human or delegating agent — open a task here
        "_execute_one_agent_step_inner",  # is the run's own principal still spoken for
        "agent_capability_terms",  # a preview for a run that does not exist yet
        "_bounded_by_this_calls_reviewers",  # the humans who released this one call
    }


def test_the_second_maker_of_a_bounding_set_takes_no_principal_from_its_caller_either() -> None:
    """``also_bounded_by`` is a construction site, so its callers are pinned like the factory's.

    The factory is safe because there is no parameter through which a caller can hand
    it a short set. This method is the opposite — principals are exactly what it takes
    — so what makes it safe has to be that only one function reaches it, and that that
    function takes no principal from *its* caller either: it is handed a request and
    reads the reviewers of that request from the durable rows. Both halves are asserted,
    because either one alone leaves the shape that lost fourteen rounds expressible.
    """
    assert _functions_mentioning("also_bounded_by") == {"_bounded_by_this_calls_reviewers"}
    params = set(inspect.signature(MultiplayerService._bounded_by_this_calls_reviewers).parameters)
    assert params == {"self", "request", "authorization"}


_A_DOOR_THAT_NEVER_NAMES_THE_CLASS = """
class MultiplayerService:
    async def _a_door_written_next_year(self, request, authorization, reviewer_id):
        return replace(
            authorization, bounding=authorization.bounding.also_bounded_by([reviewer_id])
        )
"""


def test_the_guard_sees_a_construction_that_never_names_the_class() -> None:
    """The blind spot itself, pinned: this is how the reviewer's bound got in unseen.

    A door can hold a bounding set built from principals it was handed without the
    class name appearing anywhere in it, so a guard that reads for the class name
    passes over it in silence. That is not hypothetical — it is what happened, and
    reversing the fix makes this fail rather than making the suite quietly wider.
    """
    tree = ast.parse(_A_DOOR_THAT_NEVER_NAMES_THE_CLASS)

    assert _functions_mentioning("BoundingPrincipals", tree) == set()
    assert _functions_constructing_a_bounding_set(tree) == {"_a_door_written_next_year"}


def test_a_bounding_set_with_nobody_in_it_is_refused() -> None:
    """The intersection over no principals is the whole vocabulary. Never that."""
    with pytest.raises(ValueError, match="bounded by nothing"):
        BoundingPrincipals(frozenset())
    # A run whose rows named nobody is bounded by an unknown, which lends nothing.
    assert BoundingPrincipals.read_from([]) == BoundingPrincipals(frozenset({UNKNOWN_PRINCIPAL}))


def test_terms_read_for_a_partial_set_cannot_be_spent_under_the_whole_run() -> None:
    """The wall. Round eight's mistake, in the shape the type system can refuse."""
    assert not hasattr(UnboundedTerms, "effective")
    whole = frozenset({"analysis"})
    terms = CapabilityTerms(whole, whole, whole, whole, whole)
    partial = UnboundedTerms(BoundingPrincipals(frozenset({OWNER})), terms)
    authorization = RunAuthorization(
        run_id="arun_1",
        agent_id="agent_1",
        room_id="room_1",
        bounding=BoundingPrincipals(frozenset({OWNER, BOB})),
        required_capability="analysis",
    )

    with pytest.raises(ValueError, match="may not be spent under run"):
        partial.spend_under(authorization)

    complete = UnboundedTerms(authorization.bounding, terms)
    assert complete.spend_under(authorization).effective == whole
