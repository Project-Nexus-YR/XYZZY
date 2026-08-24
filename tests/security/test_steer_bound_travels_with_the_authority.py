"""Regression: the steerer bound is carried by the authority, not by remembering it.

Twelve rounds relocated one defect. A steerer's grant bounds what a run may spend; the
bound was applied by whichever spend-point had last been taught the rule, so each round
closed one place a capability set could go stale and the next round found another. It
has been a persisted column on the intervention row, an in-memory set cached on the
turn, and — reproduced twice here — the approval door, where ``_current_tool_decision``
and ``_require_run_authority_in_transaction`` both re-derived the five durable terms
correctly and neither applied the bound. A steerer holding ``["writing"]`` caused a
``task.create``, was narrowed to nothing while the approval was pending, and the tool
executed on the grant, twelve hours after she stopped holding it.

What ends the series is that no spend-point applies the bound any more. Every one of
them re-derives from a ``RunAuthorization``; the authorization names the steerers, read
from the run's own intervention rows by the single factory that builds it; and
``_authorized_terms`` is the only thing in the service that produces a spendable
``CapabilityTerms``. The raw five-way derivation returns ``UnboundedTerms``, which has
no ``effective`` at all, so a spend-point that derives terms and forgets the bound has
nothing to hand the gateway. The last section pins that shape, because a thirteenth
relocation would have to break it first.
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
from multiplayer.domain.models import HarnessState, MessageRole, RunSettlement
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security import boundary
from multiplayer.security.capabilities import RunAuthorization, UnboundedTerms
from multiplayer.services.service import MultiplayerService

OWNER = "owner"
STEERER = "steerer"

# The two tools whose calls stop at a human. They are the ones the approval door
# holds, so they are the ones a stale bound had twelve hours to be spent on.
GATED = [
    ("task.create", {"title": "Roll the migration back"}),
    ("artifact.write", {"name": "Rollout plan", "description": "the plan"}),
]


class _AsksForAToolThenAnswers:
    def __init__(self, tool: str, tool_input: dict[str, Any]) -> None:
        self.tool = tool
        self.tool_input = tool_input
        self.prompts: list[str] = []

    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del schema
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            return {
                "action": "tool",
                "tool": self.tool,
                "input": self.tool_input,
                "output": {"content": f"requesting {self.tool}"},
            }
        return {"action": "finish", "output": {"content": "here is the answer"}}


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({OWNER, STEERER}))
    await svc.initialize()
    yield svc
    await db.close()


async def _room_with_synthesizer(svc: MultiplayerService, provider: Any) -> tuple[str, str]:
    org = await svc.create_organization("Bound org", "bound-org", OWNER)
    workspace = await svc.create_workspace(org.org_id, "Main", "main", OWNER)
    room = await svc.create_room(workspace.workspace_id, "Decision", OWNER)
    await svc.invite_room_member(room.room_id, STEERER, "editor", OWNER)
    # She may lend writing and nothing else, which is exactly the gated tools.
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


async def _steered_run_waiting_on_a_reviewer(
    svc: MultiplayerService, provider: _AsksForAToolThenAnswers
) -> tuple[str, str, str]:
    """A turn the steerer shaped, stopped at the approval her grant let it reach."""
    room_id, agent_id = await _room_with_synthesizer(svc, provider)
    session = await svc.start_agent_session(room_id, agent_id)
    execution = await svc.start_execution(session.session_id, OWNER)
    await svc.intervene_execution(
        execution.execution_id, STEERER, "file it against the rollback", require_member=True
    )
    await svc.execute_agent_step(execution.execution_id, "Assess the deploy.", OWNER)
    approvals = await svc.list_pending_approvals(room_id)
    assert len(approvals) == 1, approvals
    return room_id, execution.execution_id, approvals[0].approval_id


async def _writes(svc: MultiplayerService, room_id: str) -> list[str]:
    tasks = [task.task_id for task in await svc.repos.tasks.list_by_room(room_id)]
    artifacts = [a.artifact_id for a in await svc.repos.artifacts.list_by_room(room_id)]
    return tasks + artifacts


# ── The twelfth relocation: the approval door ────────────────────────────────


@pytest.mark.parametrize(("tool", "tool_input"), GATED)
@pytest.mark.asyncio
async def test_narrowing_the_steerer_while_the_approval_waits_refuses_the_tool(
    service: MultiplayerService, tool: str, tool_input: dict[str, Any]
) -> None:
    """Her grant is read when it is spent, not when the reviewer was asked."""
    svc = service
    provider = _AsksForAToolThenAnswers(tool, tool_input)
    room_id, _, approval_id = await _steered_run_waiting_on_a_reviewer(svc, provider)

    await svc.set_member_capabilities(room_id, STEERER, [], OWNER)
    await svc.approve_action(approval_id, OWNER)

    assert await _writes(svc, room_id) == []
    statuses = [row["status"] for row in await svc.db.fetch_all("SELECT status FROM tool_requests")]
    assert statuses and set(statuses) == {"REJECTED"}
    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert "tool.call_completed" not in types
    assert "tool.call_rejected" in types


@pytest.mark.parametrize(("tool", "tool_input"), GATED)
@pytest.mark.asyncio
async def test_removing_the_steerer_while_the_approval_waits_refuses_the_tool(
    service: MultiplayerService, tool: str, tool_input: dict[str, Any]
) -> None:
    """The same refusal the gateway gives for a removal, at the door that outlives it.

    The identical removal against a non-approval tool was already refused at the
    gateway. An approval is a twelve-hour lease in front of the same spend, and it
    laundered the removal for as long as the reviewer took.
    """
    svc = service
    provider = _AsksForAToolThenAnswers(tool, tool_input)
    room_id, _, approval_id = await _steered_run_waiting_on_a_reviewer(svc, provider)

    await svc.repos.room_members.remove(room_id, STEERER)
    await svc.approve_action(approval_id, OWNER)

    assert await _writes(svc, room_id) == []
    statuses = [row["status"] for row in await svc.db.fetch_all("SELECT status FROM tool_requests")]
    assert statuses and set(statuses) == {"REJECTED"}


@pytest.mark.parametrize(("tool", "tool_input"), GATED)
@pytest.mark.asyncio
async def test_the_writer_transaction_carries_the_steerer_bound_too(
    service: MultiplayerService, tool: str, tool_input: dict[str, Any], monkeypatch: Any
) -> None:
    """The last leg: the authority goes after the gateway agreed and _run_tool began."""
    svc = service
    provider = _AsksForAToolThenAnswers(tool, tool_input)
    room_id, _, approval_id = await _steered_run_waiting_on_a_reviewer(svc, provider)
    real_authorization = svc._run_authorization

    async def narrow_the_steerer_after_dispatch(request: Any) -> RunAuthorization:
        authorization = await real_authorization(request)
        # A concurrent human request runs with no turn context; the patch
        # injects it mid-turn, so it steps outside the boundary explicitly.
        token = boundary._agent_turn.set(None)
        try:
            await svc.set_member_capabilities(room_id, STEERER, [], OWNER)
        finally:
            boundary._agent_turn.reset(token)
        return authorization

    monkeypatch.setattr(svc, "_run_authorization", narrow_the_steerer_after_dispatch)
    await svc.approve_action(approval_id, OWNER)

    assert await _writes(svc, room_id) == []
    revoked = [
        event.payload
        for event in await svc.get_room_events(room_id)
        if event.event_type.value == "agent.run.authority_revoked"
    ]
    assert [payload["stage"] for payload in revoked] == [tool]
    run = (await svc.db.fetch_all("SELECT harness_state, settlement FROM agent_runs"))[0]
    assert run["harness_state"] == HarnessState.SETTLED.value
    assert run["settlement"] == RunSettlement.AUTHORITY_REVOKED.value


@pytest.mark.parametrize(("tool", "tool_input"), GATED)
@pytest.mark.asyncio
async def test_a_steerer_who_still_holds_it_still_gets_the_tool(
    service: MultiplayerService, tool: str, tool_input: dict[str, Any]
) -> None:
    """The control. The bound is her authority, not a penalty for having steered."""
    svc = service
    provider = _AsksForAToolThenAnswers(tool, tool_input)
    room_id, _, approval_id = await _steered_run_waiting_on_a_reviewer(svc, provider)

    await svc.approve_action(approval_id, OWNER)

    assert len(await _writes(svc, room_id)) == 1
    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert "tool.call_completed" in types


@pytest.mark.asyncio
async def test_a_steer_is_read_from_the_rows_rather_than_carried_by_the_caller(
    service: MultiplayerService,
) -> None:
    """A second process decides the approval and is bounded by a steer it never saw.

    The turn's steerers used to be a set cached on the continuation object and copied
    into the suspended-turn row. Nothing carries them now: they are the run's own
    intervention rows, so a process that never held the turn reads the same bound.
    """
    svc = service
    provider = _AsksForAToolThenAnswers(*GATED[0])
    room_id, execution_id, approval_id = await _steered_run_waiting_on_a_reviewer(svc, provider)
    assert STEERER in await svc.repos.executions.bounding_principals(execution_id)
    columns = await svc.db.fetch_all("SELECT name FROM pragma_table_info('suspended_turns')")
    assert "steerers" not in {str(row["name"]) for row in columns}

    other = MultiplayerService(svc.db, RealtimeHub(), known_users=frozenset({OWNER, STEERER}))
    other.nexus = NexusAgentBridge(model_provider=provider)
    await svc.set_member_capabilities(room_id, STEERER, [], OWNER)

    await other.approve_action(approval_id, OWNER)

    assert await _writes(svc, room_id) == []
    statuses = [row["status"] for row in await svc.db.fetch_all("SELECT status FROM tool_requests")]
    assert statuses and set(statuses) == {"REJECTED"}


@pytest.mark.asyncio
async def test_a_mention_run_carries_the_bound_through_the_approval_door(
    service: MultiplayerService,
) -> None:
    """The same door, reached the way the product reaches it."""
    svc = service
    provider = _AsksForAToolThenAnswers(*GATED[0])
    room_id, _ = await _room_with_synthesizer(svc, provider)
    await svc.send_message(
        room_id,
        MessageRole.HUMAN,
        OWNER,
        "@Synthesizer file the rollback",
        invoke_mentioned_agents=True,
    )
    execution = (await svc.repos.executions.list_by_room(room_id))[0]
    approval = (await svc.list_pending_approvals(room_id))[0]
    await svc.intervene_execution(
        execution.execution_id, STEERER, "and say why", require_member=True
    )

    await svc.set_member_capabilities(room_id, STEERER, [], OWNER)
    await svc.approve_action(approval.approval_id, OWNER)

    assert await _writes(svc, room_id) == []


# ── What makes a thirteenth relocation something you cannot write by accident ──


def _service_ast() -> ast.Module:
    source = inspect.getsourcefile(service_module)
    assert source is not None
    return ast.parse(Path(source).read_text(encoding="utf-8"))


def _callers_of(name: str) -> set[str]:
    """Every function in the service whose body mentions ``name`` as a call."""
    callers: set[str] = set()
    for node in ast.walk(_service_ast()):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if called == name:
                callers.add(node.name)
    return callers


def test_the_raw_five_way_derivation_produces_nothing_a_gateway_can_spend() -> None:
    """``UnboundedTerms`` has no ``effective``. That is the wall, not a convention."""
    assert not hasattr(UnboundedTerms, "effective")
    # No default, so no construction can leave the bounding principals out by
    # omission. The steerers are inside that set now; see
    # test_every_bounding_principal.py for what else is.
    bounding = RunAuthorization.__dataclass_fields__["bounding"]
    assert bounding.default is dataclasses.MISSING
    assert bounding.default_factory is dataclasses.MISSING


def test_only_one_factory_builds_a_run_authorization() -> None:
    """The steerers are read there, which is why no caller has to remember them."""
    assert _callers_of("RunAuthorization") == {"_authorization_for"}


def test_the_unbounded_derivation_has_only_gates_and_a_preview_for_callers() -> None:
    """A new caller of the raw derivation is a decision, recorded here or not made.

    ``_authorized_terms`` is the spend. The rest ask what a principal may lend — a
    launch gate, a steer gate — or show a member what a run they have not opened yet
    would be able to do. None of them decides a tool call, and the assertion below is
    what a thirteenth relocation would have to edit out of the suite first.
    """
    assert _callers_of("_lendable_terms") == {
        "_authorized_terms",  # the one spend, which applies the bound
        "_require_delegated_authority",  # may this caller steer somebody else's run
        "_require_agent_run_authority",  # may this caller steer this agent at all
        "_invoke_mentioned_agent_in_transaction",  # may this member open a turn here
        "_execute_one_agent_step_inner",  # may the run's own principal still be spoken for
        "agent_capability_terms",  # a preview for a run that does not exist
    }


def test_no_function_derives_terms_and_decides_a_tool_call_in_the_same_breath() -> None:
    """Deriving beside a gateway decision is precisely how the bound got skipped."""
    assert _callers_of("decide") & _callers_of("_lendable_terms") == set()
    # And every gateway decision in the service comes from the bounded derivation.
    assert _callers_of("decide") <= _callers_of("_authorized_terms")
