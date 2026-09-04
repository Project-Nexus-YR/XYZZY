"""The unit two agents talk about: a task, its states, and what it carries.

The vocabulary is Google's A2A, taken verbatim rather than paraphrased, so that
an implementation written against that specification and this one mean the same
thing by the same word. Where this file adds something the specification does
not have, it says so.

An ``AgentTask`` is not an ``Execution``. The execution is the turn a harness
runs; the task is the request one party made of another, which may outlive
several executions, may be waiting on a person, and may be refused before any
execution exists at all. Collapsing them would mean a task could not exist in a
state where nothing is running, which is most of the interesting ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from .models import DomainError, new_id, utcnow


class AgentTaskState(StrEnum):
    """A2A's task states.

    The values are the specification's own strings — lowercase, hyphenated,
    ``canceled`` with one l — rather than this codebase's usual uppercase enum
    convention, because these ones go on the wire. A private spelling would mean
    a translation table at the boundary, and a translation table is where a state
    goes missing the first time somebody adds one.

    ``INPUT_REQUIRED`` and ``AUTH_REQUIRED`` are interruptible rather than
    terminal: the task is alive and waiting on somebody. The other four end it.

    The specification also has ``unknown``. It is deliberately absent here: it
    describes a client's ignorance of a task on some other server, and a server
    that can reach its own row is never in that position. Accepting it would only
    create a state this state machine has no transition out of.
    """

    SUBMITTED = "submitted"
    WORKING = "working"
    INPUT_REQUIRED = "input-required"
    AUTH_REQUIRED = "auth-required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    REJECTED = "rejected"


TERMINAL_STATES = frozenset(
    {
        AgentTaskState.COMPLETED,
        AgentTaskState.FAILED,
        AgentTaskState.CANCELED,
        AgentTaskState.REJECTED,
    }
)

# Written out rather than derived, because the interesting property of this map
# is the transitions it leaves out. A derived rule would silently gain an edge
# the first time somebody adds a state.
LEGAL_TRANSITIONS: dict[AgentTaskState, frozenset[AgentTaskState]] = {
    AgentTaskState.SUBMITTED: frozenset(
        {
            AgentTaskState.WORKING,
            AgentTaskState.REJECTED,
            AgentTaskState.CANCELED,
            AgentTaskState.AUTH_REQUIRED,
        }
    ),
    AgentTaskState.WORKING: frozenset(
        {
            AgentTaskState.INPUT_REQUIRED,
            AgentTaskState.AUTH_REQUIRED,
            AgentTaskState.COMPLETED,
            AgentTaskState.FAILED,
            AgentTaskState.CANCELED,
        }
    ),
    AgentTaskState.INPUT_REQUIRED: frozenset(
        {AgentTaskState.WORKING, AgentTaskState.CANCELED, AgentTaskState.FAILED}
    ),
    AgentTaskState.AUTH_REQUIRED: frozenset(
        {
            AgentTaskState.WORKING,
            AgentTaskState.CANCELED,
            AgentTaskState.FAILED,
            # A person may answer the escalation with "no", which is a refusal of
            # the task and not a failure of the agent that asked.
            AgentTaskState.REJECTED,
        }
    ),
    AgentTaskState.COMPLETED: frozenset(),
    AgentTaskState.FAILED: frozenset(),
    AgentTaskState.CANCELED: frozenset(),
    AgentTaskState.REJECTED: frozenset(),
}


class A2AError(DomainError):
    """A refusal the specification gives a name to.

    The names are the specification's. A caller that asked for something this
    agent cannot do is owed the reason as a name it can branch on, not a
    sentence, and certainly not a silent degrade into doing something else.
    """

    code = "A2AError"


class UnsupportedOperationError(A2AError):
    code = "UnsupportedOperationError"


class PushNotificationNotSupportedError(A2AError):
    code = "PushNotificationNotSupportedError"


class TaskNotCancelableError(A2AError):
    code = "TaskNotCancelableError"


class TaskNotFoundError(A2AError):
    code = "TaskNotFoundError"


class DelegationCycleError(A2AError):
    """A2A has no name for this, because A2A has no notion of a chain.

    Two agents wired to consult each other is not a hypothetical: it is the
    first afternoon of a room with five agents in it. Left alone the second
    request re-enters the first agent, which asks again, and the failure
    surfaces as a stack overflow or a spend ceiling rather than as a refusal
    anybody can read.
    """

    code = "DelegationCycleError"


class DelegationDepthExceededError(A2AError):
    """Also ours. A chain with no cycle in it can still be too long."""

    code = "DelegationDepthExceededError"


# Depth counts delegations, not agents: the task a human opens is depth 0, so a
# ceiling of four admits a chain of five agents. It is a bound on the shape of
# the chain and not on how much work it may do, and it is here because the thing
# an unbounded chain exhausts is the database rather than the stack — every hop
# is rows, and nothing else stops it. Four is a judgement, not a measurement.
# Raising it is a one-line change; removing it is not.
MAX_DELEGATION_DEPTH = 4


class PartKind(StrEnum):
    TEXT = "text"
    RAW = "raw"
    URL = "url"


@dataclass(frozen=True, slots=True)
class Part:
    """One typed piece of a message. Text, bytes, or a pointer to bytes."""

    kind: PartKind
    content: str
    media_type: str = "text/plain"

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "content": self.content, "media_type": self.media_type}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Part:
        return cls(
            kind=PartKind(raw["kind"]),
            content=str(raw["content"]),
            media_type=str(raw.get("media_type", "text/plain")),
        )


class TaskMessageRole(StrEnum):
    ASKER = "asker"
    DELEGATE = "delegate"


@dataclass(frozen=True, slots=True)
class AgentTaskMessage:
    """A turn in the conversation between two agents about one task."""

    message_id: str
    task_id: str
    sequence: int
    role: TaskMessageRole
    parts: tuple[Part, ...]
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class AgentTask:
    """One agent's request of another.

    ``authorized_by`` is the human whose grant this task ultimately spends. It is
    recorded because a delegated run has to be bounded by the same principals as
    the run that delegated it — and it is recorded as an identity, never as the
    set of things that identity could do. What they may lend is re-read from
    durable rows at the moment it is spent, every time.
    """

    task_id: str
    context_id: str
    room_id: str
    target_agent_id: str
    authorized_by: str
    # Who made the call, which on a delegated task is not who it is authorized by.
    # Both bound what the run may spend; only one of them is the chain's root.
    requested_by: str = ""
    delegating_agent_id: str | None = None
    delegating_run_id: str | None = None
    execution_id: str | None = None
    state: AgentTaskState = AgentTaskState.SUBMITTED
    accepted_output_modes: tuple[str, ...] = ()
    depth: int = 0
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    terminal_at: datetime | None = None
    refusal_reason: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def opened_by_a_human(self) -> bool:
        return self.delegating_agent_id is None


def new_context_id() -> str:
    return new_id("a2actx")


def require_transition(current: AgentTaskState, target: AgentTaskState) -> None:
    """Refuse a transition the state machine does not have.

    Terminal means terminal: a completed task that can be moved back to working
    is not a task with a lifecycle, it is a mutable row with a status column, and
    every reader downstream of it has to defend itself.
    """
    if target not in LEGAL_TRANSITIONS[current]:
        raise DomainError(f"a task cannot go from {current.value} to {target.value}")


def negotiate_output_modes(
    requested: tuple[str, ...], supported: tuple[str, ...]
) -> tuple[str, ...]:
    """The modes both sides accept, or a named refusal.

    A2A's negotiation is declare-and-reject: the caller says what it will take,
    and an agent that cannot produce any of it must say so by name. Returning a
    best-effort substitute would be the silent degrade the specification exists
    to prevent — the caller would receive something it already said it could not
    use, and would have no way to know that is what happened.
    """
    if not requested:
        return supported
    agreed = tuple(mode for mode in requested if mode in supported)
    if not agreed:
        raise UnsupportedOperationError(
            f"this agent produces {', '.join(supported) or 'nothing'}; "
            f"the caller accepts only {', '.join(requested)}"
        )
    return agreed


def require_delegable(
    ancestry: tuple[str, ...],
    target_agent_id: str,
    *,
    max_depth: int = MAX_DELEGATION_DEPTH,
) -> int:
    """Refuse a delegation that would loop or run too deep; return the new depth.

    ``ancestry`` is every agent already in this chain, root first, and the
    delegator is its last entry. Both refusals are read off that one tuple, so
    there is no walk to get wrong and no counter to drift: depth is the length
    of the chain the database holds, and a cycle is membership in it.

    The cycle test is on the whole ancestry rather than on the immediate
    delegator alone. A asks B asks A is the loop people think of; A asks B asks
    C asks A is the loop they hit, and only the first of the two is caught by
    looking one step back.
    """
    if target_agent_id in ancestry:
        raise DelegationCycleError(
            f"{target_agent_id} is already in this chain: "
            f"{' -> '.join((*ancestry, target_agent_id))}"
        )
    depth = len(ancestry)
    if depth > max_depth:
        raise DelegationDepthExceededError(
            f"a delegation chain may be {max_depth} deep; this one would be {depth}"
        )
    return depth
