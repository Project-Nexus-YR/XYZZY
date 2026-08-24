"""The task state machine, and the negotiation that decides whether one opens.

These are the properties a second implementation of the same protocol would
check against ours: that the state names are the ones the specification uses,
that terminal means terminal, and that an agent which cannot produce what the
caller asked for refuses by name instead of substituting something.
"""

from __future__ import annotations

import pytest

from multiplayer.domain.agent_tasks import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    AgentTask,
    AgentTaskState,
    Part,
    PartKind,
    UnsupportedOperationError,
    negotiate_output_modes,
    require_transition,
)
from multiplayer.domain.models import DomainError


def test_the_state_names_are_the_ones_the_protocol_uses():
    # Pinned deliberately, and copied from the v0.3.0 JSON schema rather than
    # from the prose: lowercase, hyphenated, and `canceled` with one l. An
    # earlier version of this test pinned the same names in this codebase's own
    # uppercase house style, which is how a file whose docstring promises the
    # protocol's vocabulary verbatim came to hold eight strings no conformant
    # client would recognise.
    assert {state.value for state in AgentTaskState} == {
        "submitted",
        "working",
        "input-required",
        "auth-required",
        "completed",
        "failed",
        "canceled",
        "rejected",
    }


def test_the_states_this_server_never_reports_are_absent():
    # The specification also has `unknown`, for a client that has lost track of a
    # task on somebody else's server. A server reading its own row is never in
    # that position, and a state with no transition out of it is a trap for
    # whoever adds the ninth.
    assert "unknown" not in {state.value for state in AgentTaskState}


def test_the_four_end_states_have_no_way_out():
    assert TERMINAL_STATES == {
        AgentTaskState.COMPLETED,
        AgentTaskState.FAILED,
        AgentTaskState.CANCELED,
        AgentTaskState.REJECTED,
    }
    for state in TERMINAL_STATES:
        assert LEGAL_TRANSITIONS[state] == frozenset()
        with pytest.raises(DomainError):
            require_transition(state, AgentTaskState.WORKING)


def test_the_two_waiting_states_are_interruptions_rather_than_endings():
    for waiting in (AgentTaskState.INPUT_REQUIRED, AgentTaskState.AUTH_REQUIRED):
        assert waiting not in TERMINAL_STATES
        # Whoever was being waited on answered, and the work resumes.
        require_transition(waiting, AgentTaskState.WORKING)


def test_a_person_may_refuse_an_escalation_without_it_being_a_failure():
    # AUTH_REQUIRED means a named human is being asked to lend one capability.
    # "No" is a refusal of the task, not a fault in the agent that asked, and the
    # two are different things to everyone reading the log afterwards.
    require_transition(AgentTaskState.AUTH_REQUIRED, AgentTaskState.REJECTED)
    with pytest.raises(DomainError):
        require_transition(AgentTaskState.WORKING, AgentTaskState.REJECTED)


def test_work_cannot_begin_again_once_it_has_finished():
    with pytest.raises(DomainError):
        require_transition(AgentTaskState.COMPLETED, AgentTaskState.INPUT_REQUIRED)
    with pytest.raises(DomainError):
        require_transition(AgentTaskState.SUBMITTED, AgentTaskState.COMPLETED)


def test_a_task_that_nobody_delegated_knows_it_was_opened_by_a_person():
    opened = AgentTask(
        task_id="t1", context_id="c1", room_id="r1", target_agent_id="a1", authorized_by="alice"
    )
    assert opened.opened_by_a_human
    assert not opened.is_terminal

    delegated = AgentTask(
        task_id="t2",
        context_id="c1",
        room_id="r1",
        target_agent_id="a2",
        authorized_by="alice",
        delegating_agent_id="a1",
        depth=1,
    )
    assert not delegated.opened_by_a_human


def test_negotiation_returns_what_both_sides_accept():
    assert negotiate_output_modes(("text/plain", "image/png"), ("text/plain",)) == ("text/plain",)
    # A caller that states no preference takes whatever the agent produces.
    assert negotiate_output_modes((), ("text/plain", "text/html")) == ("text/plain", "text/html")


def test_an_agent_that_cannot_produce_what_was_asked_for_refuses_by_name():
    with pytest.raises(UnsupportedOperationError) as refusal:
        negotiate_output_modes(("audio/wav",), ("text/plain",))
    # The name is the point: a caller branches on it. Substituting text for the
    # audio it asked for would hand back something it already said it cannot use.
    assert refusal.value.code == "UnsupportedOperationError"


def test_a_part_survives_the_round_trip_through_storage():
    for part in (
        Part(kind=PartKind.TEXT, content="hello"),
        Part(kind=PartKind.RAW, content="AAEC", media_type="application/octet-stream"),
        Part(kind=PartKind.URL, content="https://example/artifact", media_type="text/csv"),
    ):
        assert Part.from_dict(part.as_dict()) == part
