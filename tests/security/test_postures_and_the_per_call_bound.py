"""A channel may say what pauses; a reviewer answers for one call and not the run.

Two things, and they meet at the same door.

The first is a posture. A channel could already say what its agents may do —
``rooms.allowed_capabilities``, one of the five terms the intersection reads — and
could not say what stops at a human. That was fixed per tool, at the moment somebody
registered it, with nothing above it: no way to say "in this room, everything pauses",
and no way afterwards to show which rule was in force. ``STRICT`` is that sentence, and
``GUARDED`` is every channel as it stands. What a posture may do is bounded by
:func:`under_posture` rather than by anybody's discipline — it raises whether a
permitted call pauses and cannot reach ``allowed`` — so the assertions below pair every
pause with the effective set, byte for byte, under both.

The second is a reach taken back. Releasing a parked tool call wrote the reviewer into
``execution_callers``, and everything in that table bounds the whole run: an
administrator scoped to ``retrieval`` who approved a single read stripped ``writing``
from every later call of that run. It failed closed, so nobody ever obtained anything
they did not hold — it is over-reach rather than escalation, and the kind that teaches
people not to answer approvals. She is recorded against the call she released instead.

The bound itself is untouched, and that is the point the last four tests exist to make.
A reviewer still cannot approve herself past her own grant on the call she is
answering for, at the approval door and again inside the transaction that writes it.
What changed is which decisions she is over, not whether she is over any.
"""

from __future__ import annotations

from itertools import chain, combinations
from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import AgentTemplate, HarnessState, new_id
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.capabilities import (
    CAPABILITIES,
    TOOLS,
    BoundingPrincipals,
    Posture,
    decide,
    under_posture,
)
from multiplayer.services.service import AuthorizationError, MultiplayerService

OWNER = "owner"
# A member who answers approvals and holds less than the run's own principal. Every
# question below is about how far what she holds reaches.
REVIEWER = "reviewer"

# Reads and writes from one agent, so a posture can be watched changing the fate of a
# call the floor says nothing about while the floor's own calls stay where they are.
DEPUTY_CAPABILITIES = frozenset({"retrieval", "writing"})

READ = ("channel.read_context", {})
WRITE = ("task.create", {"title": "Roll the migration back"})


class _AsksThenAnswers:
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
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({OWNER, REVIEWER}))
    await svc.initialize()
    yield svc
    await db.close()


async def _workspace(svc: MultiplayerService) -> str:
    org = await svc.create_organization("Posture org", "posture-org", OWNER)
    workspace = await svc.create_workspace(org.org_id, "Main", "main", OWNER)
    return workspace.workspace_id


async def _deputy_template(svc: MultiplayerService) -> str:
    template = AgentTemplate(
        template_id=new_id("tmpl"),
        name="Deputy",
        description="Reads the channel and opens work in it",
        role="Deputy",
        system_prompt="You are a deputy.",
        capabilities=DEPUTY_CAPABILITIES,
    )
    await svc.repos.agents.create_template(template)
    return template.template_id


async def _room(svc: MultiplayerService, workspace_id: str, name: str = "Decision") -> str:
    room = await svc.create_room(workspace_id, name, OWNER)
    await svc.invite_room_member(room.room_id, REVIEWER, "editor", OWNER)
    return room.room_id


async def _turn(
    svc: MultiplayerService, room_id: str, template_id: str, provider: _AsksThenAnswers
) -> str:
    """One agent turn, driven to wherever the gateway leaves it."""
    svc.nexus = NexusAgentBridge(model_provider=provider)
    agent = await svc.spawn_agent(room_id, template_id, name="Deputy", requested_by=OWNER)
    session = await svc.start_agent_session(room_id, agent.agent_id)
    execution = await svc.start_execution(session.session_id, OWNER)
    await svc.execute_agent_step(execution.execution_id, "Assess the deploy.", OWNER)
    return execution.execution_id


async def _tool_rows(svc: MultiplayerService, room_id: str = "") -> list[dict[str, Any]]:
    where = "WHERE room_id = ? " if room_id else ""
    params = (room_id,) if room_id else ()
    return list(
        await svc.db.fetch_all(
            "SELECT request_id, tool, status, reason, effective_json FROM tool_requests "
            f"{where}ORDER BY created_at, request_id",
            params,
        )
    )


async def _event_types(svc: MultiplayerService, room_id: str) -> list[str]:
    return [event.event_type.value for event in await svc.get_room_events(room_id)]


# ── The two invariants, over every value they have rather than a sample ──────


def test_no_posture_and_no_added_principal_can_widen_anything() -> None:
    """Exhaustive, because these are the two claims everything else rests on.

    A posture over every tool, every posture and every subset of the vocabulary:
    ``allowed`` is whatever ``GUARDED`` decided, and ``requires_approval`` only ever
    rises. Never a denial turned into a question for a human either — that would be a
    widening through the back door, permitting the call by whoever answered.

    And the reviewer's half: adding principals to a bound is a union, so the set only
    grows, and since the terms are an intersection over it the grant only shrinks.
    That is why a per-call bound cannot become a way to spend more than the run holds.
    """
    vocabulary = sorted(CAPABILITIES)
    subsets = chain.from_iterable(
        combinations(vocabulary, size) for size in range(len(vocabulary) + 1)
    )
    for subset in subsets:
        effective = frozenset(subset)
        for tool in TOOLS:
            baseline = decide(tool, effective)
            for posture in Posture:
                decision = under_posture(baseline, posture)
                assert decision.allowed == baseline.allowed
                assert decision.requires_approval >= baseline.requires_approval
                assert decision.allowed or not decision.requires_approval

    held = BoundingPrincipals(frozenset({OWNER}))
    for added in ((), (OWNER,), (REVIEWER,), (OWNER, REVIEWER)):
        assert held.also_bounded_by(added).principals >= held.principals


# ── A posture raises what pauses, and reaches nothing else ───────────────────


@pytest.mark.asyncio
async def test_a_strict_room_pauses_a_call_a_guarded_room_would_not(
    service: MultiplayerService,
) -> None:
    """The whole of what a posture does, and the whole of what it must not do.

    ``channel.read_context`` is the call the floor says nothing about: durable in
    nobody's records, so under ``GUARDED`` it runs unasked. Under ``STRICT`` the same
    call in the same workspace, by the same agent, on the same grant, stops at a human.

    And the effective set is compared byte for byte, because that is the invariant the
    pause is allowed to sit next to: a posture may change the fate of a call, never the
    authority behind it. If ``STRICT`` ever reads as a sixth term — widening or
    narrowing — these two strings stop matching before anything else notices.
    """
    svc = service
    workspace_id = await _workspace(svc)
    template_id = await _deputy_template(svc)

    guarded = await _room(svc, workspace_id, "Guarded")
    await _turn(svc, guarded, template_id, _AsksThenAnswers(READ))

    strict = await _room(svc, workspace_id, "Strict")
    await svc.declare_room_posture(strict, Posture.STRICT, OWNER)
    await _turn(svc, strict, template_id, _AsksThenAnswers(READ))

    [under_guarded] = await _tool_rows(svc, guarded)
    [under_strict] = await _tool_rows(svc, strict)
    assert under_guarded["status"] == "EXECUTED"
    assert under_strict["status"] == "PENDING_APPROVAL"
    assert "strict posture" in under_strict["reason"]
    # The authority behind the call is untouched, to the byte.
    assert under_strict["effective_json"] == under_guarded["effective_json"]
    assert await svc.list_pending_approvals(strict)
    assert await svc.list_pending_approvals(guarded) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("posture", list(Posture))
async def test_the_floor_still_pauses_under_every_posture(
    service: MultiplayerService, posture: Posture
) -> None:
    """There is no tier that guarantees nothing, which is why there are only two.

    ``GUARDED`` is the bottom and it is not a lowering: ``task.create`` creates work a
    human has to dispose of, so it pauses under the most permissive posture this
    channel can declare. A posture raises the pause and never sets it — reverse that
    one word and the guarded half of this parametrisation starts writing tasks nobody
    was asked about.
    """
    svc = service
    room_id = await _room(svc, await _workspace(svc))
    template_id = await _deputy_template(svc)
    await svc.declare_room_posture(room_id, posture, OWNER)

    execution_id = await _turn(svc, room_id, template_id, _AsksThenAnswers(WRITE))

    [row] = await _tool_rows(svc, room_id)
    assert row["status"] == "PENDING_APPROVAL"
    assert await svc.repos.tasks.list_by_room(room_id) == []
    run = await svc.repos.agent_runs.get_by_execution(execution_id)
    assert run is not None and run.harness_state is HarnessState.AWAITING_APPROVAL
    if posture is Posture.GUARDED:
        # The floor's own cause, not a posture's: nothing was raised here.
        assert "strict posture" not in row["reason"]


@pytest.mark.asyncio
async def test_declaring_a_posture_needs_the_administering_capability(
    service: MultiplayerService,
) -> None:
    """Raising the bar and lowering it are one act, and both are governance.

    The refused half leaves nothing behind — no declaration row, no event, and a
    channel still reading ``GUARDED`` — because a rule somebody was refused is not a
    rule that was in force for a moment.
    """
    svc = service
    room_id = await _room(svc, await _workspace(svc))

    with pytest.raises(AuthorizationError):
        await svc.declare_room_posture(room_id, Posture.STRICT, REVIEWER)

    assert await svc.repos.room_postures.current(room_id) is Posture.GUARDED
    assert await svc.db.fetch_all("SELECT * FROM room_postures") == []
    assert "room.posture_declared" not in await _event_types(svc, room_id)

    declaration_id = await svc.declare_room_posture(room_id, Posture.STRICT, OWNER)

    assert await svc.repos.room_postures.current(room_id) is Posture.STRICT
    [declared] = [
        event.payload
        for event in await svc.get_room_events(room_id)
        if event.event_type.value == "room.posture_declared"
    ]
    assert declared == {
        "declaration_id": declaration_id,
        "posture": "STRICT",
        "declared_by": OWNER,
    }


@pytest.mark.asyncio
async def test_a_declaration_cannot_be_rewritten_deleted_or_replaced(
    service: MultiplayerService,
) -> None:
    """Which rule governed an action stays answerable from rows nobody can revise.

    ``INSERT OR REPLACE`` is the third of these because it is a rewrite wearing an
    insert's clothes, and SQLite does not run a delete trigger for it unless
    ``recursive_triggers`` happens to be on. The refusal is stated on the insert.
    """
    svc = service
    room_id = await _room(svc, await _workspace(svc))
    declaration_id = await svc.declare_room_posture(room_id, Posture.STRICT, OWNER)

    with pytest.raises(Exception, match="never rewritten"):
        await svc.db.execute(
            "UPDATE room_postures SET posture = 'GUARDED' WHERE declaration_id = ?",
            (declaration_id,),
        )
    with pytest.raises(Exception, match="never deleted"):
        await svc.db.execute(
            "DELETE FROM room_postures WHERE declaration_id = ?", (declaration_id,)
        )
    with pytest.raises(Exception, match="never rewritten"):
        await svc.db.execute(
            "INSERT OR REPLACE INTO room_postures"
            "(declaration_id, room_id, posture, declared_by, declared_at) "
            "VALUES (?, ?, 'GUARDED', ?, '2020-01-01T00:00:00Z')",
            (declaration_id, room_id, REVIEWER),
        )

    assert await svc.repos.room_postures.current(room_id) is Posture.STRICT


@pytest.mark.asyncio
async def test_loosening_never_reaches_a_call_that_already_paused(
    service: MultiplayerService,
) -> None:
    """Why loosening is permitted at all: it cannot reach backwards.

    A posture that could only rise would make one mistaken ``STRICT`` permanent and the
    channel disposable. The harm that would buy is not available to be bought: the
    posture is read once, at the moment a call is decided, so a call already parked at
    a reviewer is released by that reviewer or by nobody, and dropping the channel to
    ``GUARDED`` underneath it changes nothing about it.
    """
    svc = service
    room_id = await _room(svc, await _workspace(svc))
    template_id = await _deputy_template(svc)
    await svc.declare_room_posture(room_id, Posture.STRICT, OWNER)
    execution_id = await _turn(svc, room_id, template_id, _AsksThenAnswers(READ))
    [parked] = await _tool_rows(svc, room_id)
    assert parked["status"] == "PENDING_APPROVAL"

    await svc.declare_room_posture(room_id, Posture.GUARDED, OWNER)

    [still_parked] = await _tool_rows(svc, room_id)
    assert still_parked["status"] == "PENDING_APPROVAL"
    run = await svc.repos.agent_runs.get_by_execution(execution_id)
    assert run is not None and run.harness_state is HarnessState.AWAITING_APPROVAL
    # A human releases it, exactly as one would have had to before the loosening.
    approval = (await svc.list_pending_approvals(room_id))[0]
    await svc.approve_action(approval.approval_id, OWNER)
    [released] = await _tool_rows(svc, room_id)
    assert released["status"] == "EXECUTED"


# ── A reviewer bounds the call she answered for, and only that one ───────────


@pytest.mark.asyncio
async def test_a_reviewer_cannot_exceed_their_own_grant_on_the_call_they_approve(
    service: MultiplayerService,
) -> None:
    """The rule that ended fourteen escalations, on the decision she is making.

    Narrowing her bound to one call is not removing it. She holds ``retrieval`` and
    releases a ``task.create``: an approval is not a way to lend what the reviewer does
    not hold, so the call is refused and nothing is written. This is the assertion that
    says the per-call scope below is a scope and not an exemption.
    """
    svc = service
    room_id = await _room(svc, await _workspace(svc))
    template_id = await _deputy_template(svc)
    await svc.set_member_capabilities(room_id, REVIEWER, ["retrieval"], OWNER)
    await _turn(svc, room_id, template_id, _AsksThenAnswers(WRITE))
    approval = (await svc.list_pending_approvals(room_id))[0]

    await svc.approve_action(approval.approval_id, REVIEWER)

    [row] = await _tool_rows(svc, room_id)
    assert row["status"] == "REJECTED"
    assert await svc.repos.tasks.list_by_room(room_id) == []
    types = await _event_types(svc, room_id)
    assert "tool.call_rejected" in types
    assert "tool.call_completed" not in types


@pytest.mark.asyncio
async def test_a_reviewers_narrower_grant_does_not_bound_a_later_call_in_the_same_run(
    service: MultiplayerService,
) -> None:
    """The over-reach, reproduced and closed.

    She holds ``writing`` and nothing else, and she answers for a ``task.create`` —
    which is hers to answer for, and it runs. The turn then asks for a
    ``channel.read_context`` she was never consulted about. Recording her as a caller
    of the run put her grant over that call too and refused it; recording her against
    the call she released leaves it alone.

    Reverse either half — ``record_reviewer`` back to ``record_caller``, or the
    reviewer's name back onto the advance that releases the run — and the read below
    comes back ``REJECTED``.
    """
    svc = service
    room_id = await _room(svc, await _workspace(svc))
    template_id = await _deputy_template(svc)
    await svc.set_member_capabilities(room_id, REVIEWER, ["writing"], OWNER)
    execution_id = await _turn(svc, room_id, template_id, _AsksThenAnswers(WRITE, READ))
    approval = (await svc.list_pending_approvals(room_id))[0]

    await svc.approve_action(approval.approval_id, REVIEWER)

    write_row, read_row = await _tool_rows(svc, room_id)
    assert write_row["tool"] == "task.create"
    assert write_row["status"] == "EXECUTED"
    # The call she answered for is bounded by her: it spent only what she holds.
    assert write_row["effective_json"] == '["writing"]'
    assert read_row["tool"] == "channel.read_context"
    assert read_row["status"] == "EXECUTED"
    # And the call she was never asked about is bounded by the run's own principals.
    assert read_row["effective_json"] == '["retrieval", "writing"]'
    assert len(await svc.repos.tasks.list_by_room(room_id)) == 1
    assert REVIEWER not in await svc.repos.executions.bounding_principals(execution_id)
    assert await svc.repos.tool_requests.reviewers(write_row["request_id"]) == frozenset({REVIEWER})
    assert await svc.repos.tool_requests.reviewers(read_row["request_id"]) == frozenset()
