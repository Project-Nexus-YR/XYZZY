"""Shared core for the service mixins: attributes, cross cluster helpers, and stubs.

_ServiceCore declares every attribute MultiplayerService sets in __init__, so
that mypy sees them on self from any mixin without Any and without a type
ignore. It also declares a typed stub for every method one mixin calls on
another: the real body lives in the module that owns the method, and the stub
here is only what a caller needs to type check against.

_SharedMixin extends _ServiceCore with the handful of helpers and read paths
that more than one cluster genuinely depends on: validation, the room and
workspace capability guards, idempotency, run leases and settlement, and a few
small read accessors. Every domain mixin inherits from _SharedMixin, so these
are available on self everywhere, with one real implementation.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from ..db.connection import Database
from ..db.repositories import Repos
from ..domain.events import EventType, RoomEvent
from ..domain.models import (
    AddressingMode,
    AgentInstance,
    AgentOutput,
    AgentRun,
    AgentStatus,
    AgentTemplate,
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactType,
    ArtifactVersion,
    Branch,
    BranchMode,
    Decision,
    DecisionStatus,
    DomainError,
    Execution,
    ExecutionStatus,
    HarnessState,
    IdempotencyConflict,
    IdempotencyRecord,
    Memory,
    Message,
    MessageReaction,
    MessageRole,
    OntologyEntity,
    OntologyRelationship,
    OntologyReview,
    OntologyReviewStatus,
    Organization,
    OutputDisposition,
    OutputSelection,
    ParticipantType,
    ProofMode,
    Room,
    RoomMember,
    RoomParticipantHandle,
    RunSettlement,
    Session,
    SessionStatus,
    Task,
    TaskPriority,
    TaskStatus,
    ToolRequest,
    Workspace,
    handle_from_display_name,
    new_id,
    utcnow,
)
from ..domain.provenance import calculate_artifact_provenance_hash
from ..harness import (
    KNOWN_HARNESS_IDS,
    NEXUS_HARNESS_ID,
    AgentHarness,
    ModelProviderHarness,
    NexusHarness,
    NexusLaunch,
    SessionUpdate,
)
from ..harness.adapters import MODEL_PROVIDER_HARNESS_ID
from ..metrics import Metrics
from ..nexus_bridge.agent_bridge import NexusAgentBridge
from ..realtime.hub import RealtimeHub
from ..security.authorization import (
    AuthorizationError,
    RoomCapability,
    RoomPolicy,
    capabilities_for_role,
)
from ..security.capabilities import (
    CAPABILITIES,
    BoundingPrincipals,
    CapabilityTerms,
    GatewayDecision,
    RunAuthorization,
    UnboundedTerms,
    may_address,
    policy_capabilities,
)
from ..security.identity import (
    credential_hash,
    new_launch_challenge,
    new_run_credential,
    verify_challenge_answer,
)
from ..services.presence import PresenceService

log = logging.getLogger(__name__)

# The two human identities a demo deployment seeds. Fixed rather than generated,
# so the one bearer token XYZZY_DEMO issues (see server.py) always resolves to
# the same workspace on every restart of the same database.
DEMO_USER_ID = "user_demo"
DEMO_SECOND_USER_ID = "user_demo_second"

# A mention addresses a handle: one whitespace-free token, drawn from the same
# alphabet handle_from_display_name issues into, so every handle in the room is a
# handle this pattern can read back.
_MENTION_PATTERN = re.compile(r"(?<![\w@])@([A-Za-z0-9][A-Za-z0-9_.\-]*)")
# Two people called "Sam" is ordinary; a hundred is not. The bound keeps handle
# issuance from becoming an unbounded scan on a write path.
_MAX_HANDLE_ATTEMPTS = 100
# How much of an output the conversation shows before it defers to the record.
# Long enough that scrolling the thread is still reading, short enough that the
# message is plainly a pointer and not a second copy of the output.
_AGENT_MESSAGE_EXCERPT_CHARS = 280
# One wording for every way of not being allowed to reach an agent task, so that
# no pair of refusals can be subtracted from each other to learn what exists. The
# room gate's own words, repeated rather than paraphrased: a caller who cannot act
# in the room must not be able to tell which check turned them away.
_ROOM_ACCESS_FORBIDDEN = "room access forbidden"
_NO_SUCH_AGENT_TASK = "no such agent task"
# FTS5 reads its own query syntax, so user input becomes quoted phrases instead.
_SEARCH_TERM_PATTERN = re.compile(r"\w+")
# Every non-settled run holds a heartbeat lease. No state is exempt: an exemption is
# not a longer deadline but no deadline, and manufactures the fourth case the
# guarantee denies � a run is settled, holds a live lease, or is swept. A reviewer may
# take hours, so AWAITING_APPROVAL gets a long lease rather than none.
_STREAMING_LEASE = timedelta(minutes=15)
_APPROVAL_LEASE = timedelta(hours=12)
# How many times a run may be picked up before it is parked instead of swept again.
# Without it a run whose dispatcher keeps dying is re-orphaned forever and never
# reaches a state a reader can describe.
_RUN_MAX_ATTEMPTS = 3
# Set around the one call to _execute_one_agent_step_inner that is an actual turn
# entrance, read inside it rather than passed as a parameter, so that a caller
# substituting the inner step (as several tests do) keeps its own two argument
# shape and this claim stays invisible to it rather than becoming its problem.
_require_idle_entrance: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_require_idle_entrance", default=False
)

# get_room_events' own default and its hard ceiling. Reconnect wants
# everything missed since a given sequence, and a room's history is bounded
# by practice at far fewer than this many events, so the default is really
# the ceiling: high enough that no legitimate reconnect ever meets it, low
# enough that a room with an unbounded number of events cannot make this
# method build an unbounded list in memory before anything gets to truncate
# it. A caller past this cap is the one that needs to paginate.
_ROOM_EVENTS_DEFAULT_LIMIT = 5000
_ROOM_EVENTS_MAX_LIMIT = 5000

# ── State machine transition tables ──────────────────────────────────────────

VALID_TASK_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.CREATED: {TaskStatus.ASSIGNED, TaskStatus.CANCELLED},
    TaskStatus.ASSIGNED: {
        TaskStatus.IN_PROGRESS,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.IN_PROGRESS: {
        TaskStatus.BLOCKED,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.BLOCKED: {TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: {TaskStatus.ASSIGNED, TaskStatus.CREATED},
    TaskStatus.CANCELLED: {TaskStatus.CREATED},
}

VALID_AGENT_TRANSITIONS: dict[AgentStatus, set[AgentStatus]] = {
    AgentStatus.IDLE: {AgentStatus.THINKING, AgentStatus.WORKING, AgentStatus.OFFLINE},
    AgentStatus.THINKING: {AgentStatus.WORKING, AgentStatus.WAITING_INPUT, AgentStatus.FAILED},
    AgentStatus.WORKING: {
        AgentStatus.THINKING,
        AgentStatus.REVIEWING,
        AgentStatus.DELEGATING,
        AgentStatus.WAITING_INPUT,
        AgentStatus.WAITING_APPROVAL,
        AgentStatus.BLOCKED,
        AgentStatus.COMPLETED,
        AgentStatus.FAILED,
        AgentStatus.PAUSED,
    },
    AgentStatus.REVIEWING: {AgentStatus.WORKING, AgentStatus.COMPLETED, AgentStatus.FAILED},
    AgentStatus.DELEGATING: {AgentStatus.WORKING, AgentStatus.WAITING_INPUT},
    AgentStatus.WAITING_INPUT: {AgentStatus.WORKING, AgentStatus.THINKING},
    AgentStatus.WAITING_APPROVAL: {AgentStatus.WORKING, AgentStatus.BLOCKED, AgentStatus.FAILED},
    AgentStatus.BLOCKED: {AgentStatus.WORKING, AgentStatus.FAILED},
    AgentStatus.PAUSED: {AgentStatus.WORKING, AgentStatus.IDLE, AgentStatus.FAILED},
    AgentStatus.COMPLETED: {AgentStatus.IDLE},
    AgentStatus.FAILED: {AgentStatus.IDLE, AgentStatus.OFFLINE},
    AgentStatus.OFFLINE: {AgentStatus.IDLE},
}

VALID_SESSION_TRANSITIONS: dict[SessionStatus, set[SessionStatus]] = {
    SessionStatus.CREATED: {SessionStatus.ACTIVE, SessionStatus.FAILED},
    SessionStatus.ACTIVE: {SessionStatus.PAUSED, SessionStatus.COMPLETED, SessionStatus.FAILED},
    SessionStatus.PAUSED: {SessionStatus.ACTIVE, SessionStatus.COMPLETED, SessionStatus.FAILED},
    SessionStatus.COMPLETED: set(),
    SessionStatus.FAILED: set(),
}

VALID_EXECUTION_TRANSITIONS: dict[ExecutionStatus, set[ExecutionStatus]] = {
    # A run that was opened but never started can still fail: its dispatcher may
    # have died, and a run nothing will pick up must reach a terminal state.
    ExecutionStatus.PENDING: {
        ExecutionStatus.RUNNING,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.FAILED,
    },
    ExecutionStatus.RUNNING: {
        ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.PAUSED,
    },
    ExecutionStatus.PAUSED: {ExecutionStatus.RUNNING, ExecutionStatus.CANCELLED},
    ExecutionStatus.COMPLETED: set(),
    ExecutionStatus.FAILED: set(),
    ExecutionStatus.CANCELLED: set(),
}


# A decision is proposed, then taken or refused; a taken one is only ever
# displaced by a later decision. Nothing returns to PROPOSED: reopening a settled
# call is a new proposal, and rewriting the old row would erase that it was made.
VALID_DECISION_TRANSITIONS: dict[DecisionStatus, set[DecisionStatus]] = {
    DecisionStatus.PROPOSED: {DecisionStatus.ACTIVE, DecisionStatus.REJECTED},
    DecisionStatus.ACTIVE: {DecisionStatus.SUPERSEDED},
    DecisionStatus.SUPERSEDED: set(),
    DecisionStatus.REJECTED: set(),
}


def _validate_transition(
    current: Any,
    target: Any,
    valid: dict[Any, set[Any]],
    entity_name: str,
) -> None:
    """Raise DomainError if the transition is not valid."""
    allowed = valid.get(current, set())
    if target not in allowed:
        raise DomainError(f"invalid {entity_name} transition: {current.value} -> {target.value}")


def _policy_json(allowed: list[str] | None) -> str | None:
    """Store a policy as JSON; None means the policy is not set."""
    return None if allowed is None else json.dumps(sorted(set(allowed)))


def _policy_list(raw: str | None) -> list[str] | None:
    """A stored policy is a JSON list; never set means no restriction."""
    if raw is None:
        return None
    parsed = json.loads(raw)
    return [str(item) for item in parsed] if isinstance(parsed, list) else None


_ASYNC_PASS_LIMIT = 200
# What an unreviewed extraction is worth before a human has looked at it. No
# threshold ever promotes it: only human review does.
_INFERRED_CONFIDENCE = 0.6
_UNCONFIRMED_TEMPLATE = "an unreviewed extraction suggests"
# A confirmed assertion is a person's account and is never rewritten, so when the
# row it describes moves the reader is told both, not only the older one.
# States the comparison, not a history. The code knows only that the reviewed
# assertion and the source row differ now - not which of them moved, nor when.
# Correcting only the assertion leaves the row untouched, and saying the record
# "has since changed" asserts an edit that never happened.
_DISAGREEMENT_TEMPLATE = "confirmed by a person; the source record does not agree"
# The cap the Meta route already applies to free text, restated where the text is
# kept, because a service caller reaches the audit record without the route.
_MAX_AUDITED_QUESTION = 500


class AgentLaunchRefused(AuthorizationError):
    """A launch the workspace refused before any run row existed.

    The reason is one of a closed set so the refusal event says which door closed:
    not_a_member, no_identity, revoked, challenge_failed, unknown_harness,
    not_addressable.
    """

    def __init__(self, agent_id: str, room_id: str, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.agent_id = agent_id
        self.room_id = room_id
        self.reason = reason


class RunAuthorityRevoked(AuthorizationError):
    """A re-check inside the writing transaction found the authority gone.

    Raised from inside a tool writer's own transaction, so the write it guards rolls
    back with it. The run is settled AUTHORITY_REVOKED by whoever catches it, after
    that rollback, because a settlement written inside the doomed transaction would
    roll back too.
    """

    def __init__(self, authorization: RunAuthorization, stage: str) -> None:
        super().__init__(
            f"run {authorization.run_id} may no longer "
            f"{authorization.required_capability} at {stage}"
        )
        self.authorization = authorization
        self.stage = stage


@dataclass
class _TurnContinuation:
    """What one agent turn carries across the prompts that make it up.

    A turn is not one provider call. A model that asks for a tool is prompted again
    with what the tool returned, so ``observations`` are the gateway's own records of
    those calls, in order, and they are what the next prompt reasons over.

    It carries no authority and no names to derive authority from. Who has steered
    this turn used to be cached here and copied into the suspended-turn row beside
    it, which made two more places a bound could be dropped on the way to a
    spend-point; it is read from the run's own intervention rows instead, by the one
    derivation every spend-point uses.
    """

    prompt: str
    acting_as: str
    observations: list[str] = field(default_factory=list)


class _ServiceCore(ABC):
    """Declares what MultiplayerService.__init__ sets, for every mixin to read.

    A mixin module is type checked on its own, so without this base mypy would
    not know that ``self.db`` or ``self.repos`` exist on ``self`` inside a
    method defined in, say, agents.py. Nothing here is instantiated on its own:
    it exists only so ``self`` has a type every mixin can share.
    """

    db: Database
    repos: Repos
    hub: RealtimeHub
    presence: PresenceService
    metrics: Metrics | None
    nexus: NexusAgentBridge
    authorization: RoomPolicy
    known_users: frozenset[str]
    _running_executions: dict[str, asyncio.Task[None]]
    _run_credentials: dict[str, str]
    _dispatch_claim: str
    _ontology_drains: set[str]
    _background_tasks: set[asyncio.Task[None]]
    _event_chain_migration_is_new: bool

    @abstractmethod
    async def _advance_run_for_execution(
        self,
        execution_id: str,
        state: HarnessState,
        acting_user_id: str,
        lease: timedelta,
        expected: HarnessState | None = None,
    ) -> bool: ...
    @abstractmethod
    async def _agent_message_for_mention(
        self, execution: Execution, session: Session, output: AgentOutput
    ) -> tuple[Message | None, RoomEvent | None]: ...
    @abstractmethod
    async def _agent_template_usable_in_workspace(
        self, template: AgentTemplate, workspace_id: str
    ) -> bool: ...
    @abstractmethod
    async def _authorized_terms(self, authorization: RunAuthorization) -> CapabilityTerms: ...
    @staticmethod
    @abstractmethod
    def _branch_execution_prompt(branch: Branch) -> str: ...
    @staticmethod
    @abstractmethod
    async def _currency(
        positions: list[tuple[str, int, tuple[str, ...]]],
        invalidating: Callable[[tuple[str, ...], int], Awaitable[list[int]]],
    ) -> dict[str, tuple[bool, int]]: ...
    @abstractmethod
    async def _current_tool_decision(
        self, request: ToolRequest
    ) -> tuple[GatewayDecision, frozenset[str]]: ...
    @abstractmethod
    async def _execute_one_agent_step(
        self, execution_id: str, continuation: _TurnContinuation, *, require_idle: bool = False
    ) -> dict[str, Any]: ...
    @abstractmethod
    async def _execute_tool_request(self, request: ToolRequest) -> ToolRequest: ...
    @abstractmethod
    async def _is_published_synthesis(self, artifact_id: str) -> bool: ...
    @abstractmethod
    async def _ontology_entity_record(self, entity: OntologyEntity) -> dict[str, Any]: ...
    @staticmethod
    @abstractmethod
    def _ontology_id(prefix: str, room_id: str, *source_ids: str) -> str: ...
    @staticmethod
    @abstractmethod
    def _ontology_relationship_record(relationship: OntologyRelationship) -> dict[str, Any]: ...
    @staticmethod
    @abstractmethod
    def _ontology_review_record(review: OntologyReview) -> dict[str, Any]: ...
    @staticmethod
    @abstractmethod
    def _output_content(output_data: dict[str, Any]) -> str: ...
    @abstractmethod
    async def _principal_term(self, room_id: str, principal: str) -> frozenset[str]: ...
    @abstractmethod
    async def _request_approval_in_transaction(
        self,
        room_id: str,
        execution_id: str,
        agent_id: str,
        action_description: str,
        authorized_by: str,
    ) -> tuple[Approval, RoomEvent]: ...
    @abstractmethod
    async def _require_delegated_authority(self, execution: Execution, acting_as: str) -> None: ...
    @abstractmethod
    def _resolve_model_identity(self, model_provider: str, model_name: str) -> tuple[str, str]: ...
    @abstractmethod
    async def _source_account(self, entity: OntologyEntity) -> dict[str, Any] | None: ...
    @staticmethod
    @abstractmethod
    def _source_disagreement(
        label: str,
        properties: dict[str, Any],
        review_status: OntologyReviewStatus,
        source_account: dict[str, Any] | None,
    ) -> dict[str, Any] | None: ...
    @abstractmethod
    async def _spawn_agent_writes_in_transaction(
        self,
        room_id: str,
        template: AgentTemplate,
        template_system_prompt: str,
        name: str | None,
        system_prompt: str | None,
        model_provider: str,
        model_name: str,
        requested_by: str,
        harness_id: str,
        addressing_mode: AddressingMode,
        room: Room | None,
    ) -> tuple[AgentInstance, list[RoomEvent]]: ...
    @staticmethod
    @abstractmethod
    def _tool_response(request: ToolRequest) -> dict[str, Any]: ...
    @staticmethod
    @abstractmethod
    def _validate_limit(limit: int) -> int: ...
    @staticmethod
    @abstractmethod
    def _with_currency(record: dict[str, Any], currency: tuple[bool, int]) -> dict[str, Any]: ...
    @abstractmethod
    async def add_agent_reaction(
        self,
        message_id: str,
        agent_id: str,
        emoji: str,
        *,
        authorization: RunAuthorization | None = None,
    ) -> MessageReaction: ...
    @abstractmethod
    async def add_reaction(self, message_id: str, actor_id: str, emoji: str) -> MessageReaction: ...
    @abstractmethod
    async def bootstrap_user_workspace(
        self, user_id: str, display_name: str, room_name: str
    ) -> tuple[Organization, Workspace, Room]: ...
    @abstractmethod
    async def create_artifact(
        self,
        room_id: str,
        name: str,
        artifact_type: ArtifactType,
        description: str = "",
        created_by: str = "",
        content: str = "",
        *,
        require_member: bool = False,
        authorization: RunAuthorization | None = None,
    ) -> Artifact: ...
    @abstractmethod
    async def create_task(
        self,
        room_id: str,
        title: str,
        description: str = "",
        priority: TaskPriority = TaskPriority.NORMAL,
        created_by: str = "",
        parent_task_id: str | None = None,
        *,
        require_member: bool = False,
        authorization: RunAuthorization | None = None,
    ) -> Task: ...
    @abstractmethod
    async def execute_branch_run(
        self, branch_id: str, execution_id: str, acting_as: str = ""
    ) -> dict[str, Any]: ...
    @abstractmethod
    async def get_message(self, message_id: str) -> Message: ...
    @abstractmethod
    async def get_read_cursor(self, room_id: str, user_id: str) -> dict[str, Any]: ...
    @abstractmethod
    async def get_room_members(self, room_id: str) -> list[RoomMember]: ...
    @abstractmethod
    async def get_room_ontology(self, room_id: str) -> dict[str, Any]: ...
    @abstractmethod
    async def invite_room_member(
        self, room_id: str, invited_user_id: str, role: str, invited_by: str
    ) -> RoomMember: ...
    @abstractmethod
    async def list_agent_templates(self) -> list[AgentTemplate]: ...
    @abstractmethod
    async def list_output_selections(self, room_id: str) -> list[OutputSelection]: ...
    @abstractmethod
    async def list_pending_approvals(self, room_id: str) -> list[Approval]: ...
    @abstractmethod
    async def list_room_agents(self, room_id: str) -> list[AgentInstance]: ...
    @abstractmethod
    async def list_room_decisions(self, room_id: str) -> list[Decision]: ...
    @abstractmethod
    async def list_room_memories(self, room_id: str) -> list[Memory]: ...
    @abstractmethod
    async def list_room_messages(
        self, room_id: str, limit: int = 100, after_sequence: int | None = None
    ) -> list[Message]: ...
    @abstractmethod
    async def list_room_tasks(self, room_id: str) -> list[Task]: ...
    @abstractmethod
    async def select_output(
        self, room_id: str, output_id: str, disposition: OutputDisposition, decided_by: str
    ) -> OutputSelection: ...
    @abstractmethod
    async def send_message(
        self,
        room_id: str,
        role: MessageRole,
        sender_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        parent_message_id: str | None = None,
        broadcast_to_room: bool = True,
        invoke_mentioned_agents: bool = False,
        attachment_ids: list[str] | None = None,
    ) -> Message: ...
    @abstractmethod
    async def spawn_agent(
        self,
        room_id: str,
        template_id: str,
        name: str | None = None,
        system_prompt: str | None = None,
        model_provider: str = "",
        model_name: str = "",
        *,
        requested_by: str = "",
        require_member: bool = False,
        harness_id: str = NEXUS_HARNESS_ID,
        addressing_mode: AddressingMode = AddressingMode.ANYONE,
    ) -> AgentInstance: ...
    @abstractmethod
    async def start_branch(
        self,
        room_id: str,
        mode: BranchMode,
        initiating_prompt: str,
        initiated_by: str,
        agent_ids: list[str],
        idempotency_key: str | None = None,
    ) -> tuple[Branch, list[Execution]]: ...
    @abstractmethod
    async def sweep_stranded_working_agent_tasks(self) -> int: ...
    @abstractmethod
    async def synthesize_branch_decision_brief(
        self, branch_id: str, title: str | None, created_by: str, idempotency_key: str | None = None
    ) -> tuple[Artifact, ArtifactVersion]: ...
    @abstractmethod
    async def update_agent_status(self, agent_id: str, status: AgentStatus) -> None: ...
    @classmethod
    @abstractmethod
    def verify_artifact_provenance_hash(
        cls, version: ArtifactVersion, claims: list[dict[str, Any]]
    ) -> bool: ...
    @abstractmethod
    async def _renew_run_lease(self, update: SessionUpdate) -> None: ...
    @abstractmethod
    async def _resolve_nexus_launch(self, run_id: str) -> NexusLaunch: ...


class _SharedMixin(_ServiceCore):
    """Cross cluster helpers and read paths with exactly one implementation.

    Validation, the room and workspace capability guards, idempotency, run
    settlement and leases, and the handful of read accessors more than one
    domain cluster calls. Every domain mixin inherits this, directly or
    through the MRO, so a shared helper is written once and read everywhere.
    """

    async def _append_room_event(
        self,
        room_id: str,
        event_type: EventType,
        payload: dict[str, Any],
        actor_id: str,
        actor_type: str,
    ) -> RoomEvent:
        """Append a durable room event with atomic sequence generation."""
        event = RoomEvent(
            room_id=room_id,
            sequence=0,
            event_type=event_type,
            payload=payload,
            actor_id=actor_id,
            actor_type=actor_type,
        )
        event = await self.repos.events.append_with_next_sequence(event)
        # Realtime broadcast is best-effort; failures must not roll back the event
        try:
            await self.hub.broadcast_room_event(event)
        except Exception:
            log.exception("Failed to broadcast event %s for room %s", event_type.value, room_id)
        return event

    @staticmethod
    def _artifact_provenance_hash(version: ArtifactVersion, claims: list[dict[str, Any]]) -> str:
        return calculate_artifact_provenance_hash(
            version_id=version.version_id,
            artifact_id=version.artifact_id,
            version_number=version.version_number,
            content_hash=version.content_hash,
            created_by=version.created_by,
            created_at=version.created_at,
            claims=claims,
        )

    async def _broadcast_persisted_events(self, events: list[RoomEvent]) -> None:
        """Broadcast already-committed events without changing durable state."""
        for event in events:
            try:
                await self.hub.broadcast_room_event(event)
            except Exception:
                log.exception(
                    "Failed to broadcast event %s for room %s",
                    event.event_type.value,
                    event.room_id,
                )

    async def _claim_idempotency(
        self,
        scope_id: str,
        user_id: str,
        idempotency_key: str,
        operation: str,
        request: dict[str, Any],
    ) -> IdempotencyRecord | None:
        """Within a transaction: the prior claim on replay, None when the write is fresh."""
        existing = await self.repos.idempotency.get(scope_id, user_id, idempotency_key)
        if existing is None:
            return None
        if existing.operation != operation or existing.request_hash != self._request_hash(
            operation, request
        ):
            raise IdempotencyConflict("idempotency key was already used for a different request")
        return existing

    async def _continue_agent_turn(
        self, execution_id: str, turn: _TurnContinuation, *, require_idle: bool = False
    ) -> dict[str, Any]:
        """Prompt, feed the tool result back, prompt again, until something ends it.

        Everything that ends it leaves a state a reader can name: the model answering,
        a tool that needs a human, which suspends the turn in the run's approval
        state, the run spending its last attempt, which parks it, a cancellation,
        which settles it CANCELLED, and a step that neither answered nor called a
        tool, which settles it FAILED. None of them leaves the run RUNNING with
        nobody about to prompt it.

        ``require_idle`` gates only the first prompt of this call: entering the loop
        is claiming the run for the whole turn, so every prompt after this one is the
        loop's own, not a second entrance to check.
        """
        first = require_idle
        while True:
            result = await self._execute_one_agent_step(execution_id, turn, require_idle=first)
            first = False
            request = result.get("tool_request")
            if not isinstance(request, dict):
                return result
            if str(request.get("status")) == "PENDING_APPROVAL":
                # The reviewer holds the turn now, parked by the gate that opened the
                # approval, in that same transaction. Saving it here instead was a
                # later, separate write, and a decision that found nothing to resume
                # left the run on a fresh STREAMING lease with nobody about to prompt
                # it.
                return result
            turn.observations.append(self._tool_observation(request))
            parked = await self._park_if_attempts_spent(execution_id, turn.acting_as, result)
            if parked is not None:
                return parked

    async def _expire_undecided_approvals(self, execution_id: str, reason: str) -> None:
        """Close the approvals of a settled run, so no row outlives what it gated.

        A PENDING approval against a settled run is an invitation to decide something
        that can no longer happen, and the tool request behind it is a call that
        started and never ended. Both are resolved here, in one transaction, with the
        events that say so.

        This used to hang off the lease sweep alone, and a settled run is never swept
        again: a run ended by ``cancel_execution`` or by its agent being removed left
        its approval PENDING and its tool request PENDING_APPROVAL for ever, and that
        approval could still be granted afterwards against a run that had ended long
        before. It belongs to settlement, not to expiry, so every settlement calls it.
        """
        pending_approvals = await self.repos.approvals.list_pending_by_execution(execution_id)
        if not pending_approvals:
            return
        run = await self.repos.agent_runs.get_by_execution(execution_id)
        settlement = run.settlement.value if run is not None and run.settlement is not None else ""
        events: list[RoomEvent] = []
        async with self.db.transaction():
            for approval in pending_approvals:
                await self.repos.approvals.update(
                    replace(approval, status=ApprovalStatus.EXPIRED, reviewed_at=utcnow())
                )
                events.append(
                    await self.repos.events.append_with_next_sequence_in_transaction(
                        RoomEvent(
                            room_id=approval.room_id,
                            sequence=0,
                            event_type=EventType.APPROVAL_EXPIRED,
                            payload={
                                "approval_id": approval.approval_id,
                                "execution_id": execution_id,
                                "settlement": settlement,
                                "reason": reason,
                            },
                            actor_id="system",
                            actor_type="system",
                        )
                    )
                )
                request = await self.repos.tool_requests.get_by_approval(approval.approval_id)
                if request is None or request.status != "PENDING_APPROVAL":
                    continue
                await self.repos.tool_requests.resolve_in_transaction(
                    request.request_id, "REJECTED", reason, "{}"
                )
                events.append(
                    await self.repos.events.append_with_next_sequence_in_transaction(
                        RoomEvent(
                            room_id=request.room_id,
                            sequence=0,
                            event_type=EventType.TOOL_CALL_REJECTED,
                            payload={
                                "request_id": request.request_id,
                                "tool": request.tool,
                                "required_capability": request.required_capability,
                                "reason": reason,
                            },
                            actor_id=request.agent_id,
                            actor_type="agent",
                        )
                    )
                )
        await self._broadcast_persisted_events(events)

    def _harness(self, harness_id: str) -> AgentHarness:
        """The harness that runs this agent's turns. An unknown id has none."""
        if harness_id == NEXUS_HARNESS_ID:
            return NexusHarness(self.nexus, self._resolve_nexus_launch)
        if harness_id == MODEL_PROVIDER_HARNESS_ID:
            return ModelProviderHarness(self.nexus.model_provider)
        raise KeyError(harness_id)

    async def _issue_handle(
        self,
        room_id: str,
        participant_type: ParticipantType,
        participant_id: str,
        display_name: str,
    ) -> str:
        """Give a participant the room's spelling of their name, once and durably.

        The display name only seeds the handle. After that the two are independent:
        renaming an agent leaves every mention that already addressed it pointing at
        the same participant, which is the whole reason the handle is stored rather
        than recomputed on each read.

        Suffixes come from the database refusing the insert, never from counting the
        room first. Two participants joining at the same moment would both read the
        same handle as free, and only the unique index can break that tie.
        """
        existing = await self.repos.handles.get_for_participant(
            room_id, participant_type, participant_id
        )
        if existing is not None:
            return existing.handle
        base = handle_from_display_name(display_name)
        for attempt in range(1, _MAX_HANDLE_ATTEMPTS + 1):
            candidate = base if attempt == 1 else f"{base}-{attempt}"
            claimed = await self.repos.handles.claim(
                RoomParticipantHandle(
                    room_id=room_id,
                    participant_type=participant_type,
                    participant_id=participant_id,
                    handle=candidate,
                )
            )
            if claimed:
                return candidate
        raise DomainError(f"could not find a free handle for {display_name} in this room")

    async def _lendable_terms(
        self,
        agent: AgentInstance,
        room_id: str,
        bounding: BoundingPrincipals,
    ) -> UnboundedTerms:
        """The five durable terms of PRD §13, read from records alone.

        The user term is every named principal's grant intersected. Nobody obtains
        through somebody else's run more than they hold themselves, and no principal's
        grant is a substitute for another's: each is a ceiling, and the ceiling is the
        lowest of them.

        What comes back is deliberately not spendable. It answers "what may these
        principals lend this agent here" — the question a launch gate asks. What a
        tool call is decided against is :meth:`_authorized_terms`, and only that.
        """
        user = CAPABILITIES
        for principal in bounding:
            user &= await self._principal_term(room_id, principal)
        template = await self.repos.agents.get_template(agent.template_id)
        room = await self.repos.rooms.get(room_id)
        workspace = await self.repos.workspaces.get(room.workspace_id) if room is not None else None
        return UnboundedTerms(
            bounding,
            CapabilityTerms(
                user=user,
                agent=frozenset(agent.capabilities),
                skill=frozenset(template.capabilities) if template else frozenset(),
                channel=policy_capabilities(
                    _policy_list(room.allowed_capabilities if room else None)
                ),
                workspace=policy_capabilities(
                    _policy_list(workspace.allowed_capabilities if workspace else None)
                ),
            ),
        )

    async def _park_if_attempts_spent(
        self, execution_id: str, acting_as: str, last: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Charge the next prompt to the run's attempts, or park it. Never neither.

        The bound is the run's own ``max_attempts`` — the counter the lease sweep
        already parks a run on — rather than a second limit invented beside it. A
        turn that keeps asking for tools without answering therefore ends PARKED,
        which is terminal, which a reader can name, and which ``resume_agent_run``
        already refuses to reopen.

        The refusal carries the step the turn stopped on, because where a turn ran
        out is the part of it worth reading.
        """
        run = await self.repos.agent_runs.get_by_execution(execution_id)
        if run is not None and await self.repos.agent_runs.spend_attempt(run.run_id, run.attempts):
            return None
        if run is None:
            error = f"execution {execution_id} has no run envelope to bound its turn"
            return {**(last or {}), "status": "error", "error": error}
        if run.harness_state is HarnessState.SETTLED:
            # Something already ended this run and said why. Parking it on top would
            # replace that account with a less accurate one.
            error = f"run {run.run_id} is settled ({run.settlement})"
            return {**(last or {}), "status": "error", "error": error}
        error = f"turn stopped after {run.attempts} step(s) without an answer"
        await self._settle_run(run, RunSettlement.PARKED, acting_as or "system", error)
        await self._set_agent_status_safe(run.agent_id, AgentStatus.FAILED)
        return {
            **(last or {}),
            "status": "error",
            "error": error,
            "settlement": RunSettlement.PARKED.value,
        }

    async def _prepare_agent_run(
        self,
        agent: AgentInstance,
        room_id: str,
        authorized_by: str,
        acting_user_id: str = "",
        *,
        resumed_from_run_id: str | None = None,
        attempts: int = 1,
    ) -> AgentRun:
        """Every gate that must close before a run row exists, in order.

        The membership, identity and challenge legs are repeated by BEFORE INSERT
        triggers on agent_runs, so a future code path that forgets this method still
        cannot launch an anonymous or a removed agent. Running them here first only
        makes the refusal describable.
        """
        # Removal is a gate, exactly as revocation below is one. Stamping
        # agent_room_memberships.removed_at and checking it nowhere is what let a
        # removed agent answer the next mention as if it had never left.
        if not await self.repos.agents.has_room_membership(agent.agent_id, room_id):
            raise AgentLaunchRefused(
                agent.agent_id,
                room_id,
                "not_a_member",
                f"agent {agent.agent_id} is not in room {room_id}",
            )
        identity = await self.repos.agent_identities.get_for_agent(agent.agent_id)
        if identity is None:
            raise AgentLaunchRefused(
                agent.agent_id, room_id, "no_identity", f"agent {agent.agent_id} has no identity"
            )
        if identity.revoked_at is not None:
            raise AgentLaunchRefused(
                agent.agent_id, room_id, "revoked", f"identity {identity.identity_id} is revoked"
            )
        if agent.harness_id not in KNOWN_HARNESS_IDS:
            raise AgentLaunchRefused(
                agent.agent_id,
                room_id,
                "unknown_harness",
                f"no harness is registered as {agent.harness_id!r}",
            )
        harness = self._harness(agent.harness_id)
        challenge = (
            new_launch_challenge() if identity.proof_mode is ProofMode.SIGNED_CHALLENGE else None
        )
        _, answer = await harness.initialize(challenge)
        verified_at: datetime | None = None
        if challenge is not None:
            if not verify_challenge_answer(identity.public_key or "", challenge, answer):
                raise AgentLaunchRefused(
                    agent.agent_id,
                    room_id,
                    "challenge_failed",
                    f"agent {agent.agent_id} did not answer its launch challenge",
                )
            verified_at = utcnow()
        credential = new_run_credential()
        run = AgentRun(
            run_id=new_id("arun"),
            execution_id="",
            agent_id=agent.agent_id,
            identity_id=identity.identity_id,
            room_id=room_id,
            authorized_by=authorized_by,
            acting_user_id=acting_user_id or authorized_by,
            harness_id=agent.harness_id,
            credential_hash=credential_hash(credential),
            lease_expires_at=utcnow() + _STREAMING_LEASE,
            challenge_verified_at=verified_at,
            resumed_from_run_id=resumed_from_run_id,
            attempts=attempts,
            max_attempts=_RUN_MAX_ATTEMPTS,
        )
        # The workspace stores only the hash. The plaintext lives here until the
        # harness is handed it at session_new, and nowhere else, ever.
        self._run_credentials[run.run_id] = credential
        return run

    async def _record_idempotency(
        self,
        scope_id: str,
        user_id: str,
        idempotency_key: str,
        operation: str,
        request: dict[str, Any],
        result_ref: str,
    ) -> None:
        await self.repos.idempotency.create_in_transaction(
            IdempotencyRecord(
                scope_id=scope_id,
                user_id=user_id,
                idempotency_key=idempotency_key,
                operation=operation,
                request_hash=self._request_hash(operation, request),
                result_ref=result_ref,
            )
        )

    async def _record_launch_refusal(self, refusal: AgentLaunchRefused) -> None:
        """Append the refusal after the rollback that discarded the launch."""
        event_type = (
            EventType.AGENT_ADDRESSING_REFUSED
            if refusal.reason == "not_addressable"
            else EventType.AGENT_LAUNCH_REFUSED
        )
        await self._append_room_event(
            refusal.room_id,
            event_type,
            {"agent_id": refusal.agent_id, "reason": refusal.reason},
            refusal.agent_id,
            "agent",
        )

    def _record_model_tokens(self, payload: dict[str, Any]) -> None:
        """Count what a provider said it spent; a missing or odd value counts nothing."""
        if self.metrics is None:
            return
        tokens = payload.get("token_usage", 0)
        if isinstance(tokens, int):
            self.metrics.record_model_tokens(tokens)

    @staticmethod
    def _request_hash(operation: str, request: dict[str, Any]) -> str:
        canonical = json.dumps(
            {"operation": operation, "request": request},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    async def _require_addressable(self, agent: AgentInstance, room_id: str, user_id: str) -> None:
        """Addressing gates who may point an agent, not what it does.

        A missing record grants nothing: the record is the grant, so an agent the
        workspace has no addressing row for is addressable by nobody.
        """
        addressing = await self.repos.agent_addressing.get(agent.agent_id)
        allowed = addressing is not None and may_address(
            addressing.mode.value, addressing.owner_user_id, addressing.allowlist, user_id
        )
        if not allowed:
            raise AgentLaunchRefused(
                agent.agent_id,
                room_id,
                "not_addressable",
                f"{user_id or 'an unknown principal'} may not address agent {agent.agent_id}",
            )

    async def _require_capability_in_transaction(
        self, room_id: str, user_id: str, capability: RoomCapability
    ) -> None:
        """Re-check durable membership inside the write's own transaction.

        The route authorized the request before the transaction began. A role change
        or removal committing in between is serialized by BEGIN IMMEDIATE, so checking
        again here means the ordered log never records a write by someone who had
        already lost the capability.
        """
        member = await self.repos.room_members.get(room_id, user_id)
        if capability not in capabilities_for_role(member.role if member else None):
            raise AuthorizationError("room access forbidden")

    async def _require_mutate_in_transaction(self, room_id: str, user_id: str) -> None:
        """The common case: the actor must still hold MUTATE when the write commits."""
        await self._require_capability_in_transaction(room_id, user_id, RoomCapability.MUTATE)

    async def _require_run_authority_in_transaction(
        self, authorization: RunAuthorization, stage: str
    ) -> None:
        """Re-derive the effective terms inside the transaction that writes.

        No capability set is ever an input to a later decision. Reading them here,
        beside the write, makes the check and the write one transaction by
        construction; a re-check finding the authorizing human gone, the caller
        narrowed, or either holding a role that no longer yields the capability,
        rolls the write back and settles the run AUTHORITY_REVOKED.

        A settled run is refused the same way and in the same place. Settlement is
        terminal, so no capability makes it writable again, and an approval granted
        before it settled is not a door back in: complete_execution already refuses a
        settled run's output, and this is the same refusal for the tool writers.

        Room membership is re-read here too. It is the gate every launch door already
        consults, and a turn outlives the moment it was launched: an agent removed
        between two prompts of the same turn is no longer in the room its next tool
        call would act in, whether that call reads or writes.
        """
        run = await self.repos.agent_runs.get(authorization.run_id)
        if run is None or run.harness_state is HarnessState.SETTLED:
            raise RunAuthorityRevoked(authorization, stage)
        agent = await self.repos.agents.get_instance(authorization.agent_id)
        if agent is None:
            raise RunAuthorityRevoked(authorization, stage)
        if not await self.repos.agents.has_room_membership(
            authorization.agent_id, authorization.room_id
        ):
            raise RunAuthorityRevoked(authorization, stage)
        terms = await self._authorized_terms(authorization)
        if authorization.required_capability not in terms.effective:
            raise RunAuthorityRevoked(authorization, stage)

    async def _resolve_tool_request_terminal(
        self,
        request: ToolRequest,
        status: str,
        reason: str,
        result_json: str,
        event_type: EventType,
        payload: dict[str, Any],
    ) -> RoomEvent:
        """Move a tool request into a terminal state and record the event that
        explains it, as one fact: `resolve` used to self-commit ahead of a
        second, separate commit for the event, so a crash between the two
        left a terminal status (or EXECUTED) with no event to account for it.
        """
        async with self.db.transaction():
            await self.repos.tool_requests.resolve_in_transaction(
                request.request_id, status, reason, result_json
            )
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=request.room_id,
                    sequence=0,
                    event_type=event_type,
                    payload=payload,
                    actor_id=request.agent_id,
                    actor_type="agent",
                )
            )
        try:
            await self.hub.broadcast_room_event(event)
        except Exception:
            log.exception(
                "Failed to broadcast event %s for room %s", event_type.value, request.room_id
            )
        return event

    async def _set_agent_status_safe(self, agent_id: str, status: AgentStatus) -> None:
        """Set agent status, skipping validation if transition is invalid (best-effort)."""
        try:
            await self.update_agent_status(agent_id, status)
        except DomainError:
            log.debug("Skipping invalid agent transition for %s: -> %s", agent_id, status.value)

    async def _settle_run(
        self, run: AgentRun, settlement: RunSettlement, decided_by: str, error: str
    ) -> bool:
        """Bring one run and the execution it envelopes to a terminal state together."""
        execution = await self.repos.executions.get(run.execution_id)
        if execution is None:
            return False
        terminal = {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }
        try:
            if execution.status in terminal:
                async with self.db.transaction():
                    events = [
                        await self.repos.events.append_with_next_sequence_in_transaction(event)
                        for event in await self.repos.agent_runs.settle_in_transaction(
                            run.execution_id, settlement, decided_by
                        )
                    ]
            else:
                cancelled = settlement in {RunSettlement.CANCELLED, RunSettlement.AGENT_REMOVED}
                status = ExecutionStatus.CANCELLED if cancelled else ExecutionStatus.FAILED
                events = await self.repos.executions.terminalize_without_output(
                    execution,
                    status,
                    error,
                    [
                        RoomEvent(
                            room_id=run.room_id,
                            sequence=0,
                            event_type=EventType.EXECUTION_CANCELLED
                            if cancelled
                            else EventType.EXECUTION_FAILED,
                            payload={
                                "execution_id": run.execution_id,
                                "agent_id": run.agent_id,
                                "error": error,
                            },
                            actor_id=decided_by or run.agent_id,
                            actor_type="system",
                        )
                    ],
                    settlement,
                    decided_by,
                )
        except DomainError:
            # The run moved on between the read and this write. Settling a run somebody
            # else is advancing is the damage this guard exists to prevent.
            log.info("Run %s advanced while being settled; leaving it alone", run.run_id)
            return False
        # Nothing will prompt a settled run again, so the turn held for a reviewer is
        # not waiting any more either — and neither is the approval it was held for.
        await self.repos.suspended_turns.discard(run.execution_id)
        await self._expire_undecided_approvals(run.execution_id, error)
        await self._broadcast_persisted_events(events)
        return True

    async def _settle_undispatched_run(
        self, execution_id: str, error: str, settlement: RunSettlement = RunSettlement.FAILED
    ) -> None:
        """Bring a run that will never produce a result to a described terminal state.

        The settlement says what became of it, and it defaulted to FAILED for every
        caller — so a run stopped because its authorizing human was removed was
        recorded as an agent that failed, and one whose dispatcher died before it
        started was recorded the same way. Neither agent failed. Callers that know a
        truer name pass it; the default is kept for the one caller a failure really
        is.
        """
        execution = await self.repos.executions.get(execution_id)
        if execution is None or execution.status in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }:
            return
        session = await self.repos.sessions.get(execution.session_id)
        if session is None:
            return
        try:
            events = await self.repos.executions.terminalize_without_output(
                execution,
                ExecutionStatus.FAILED,
                error,
                [
                    RoomEvent(
                        room_id=session.room_id,
                        sequence=0,
                        event_type=EventType.EXECUTION_FAILED,
                        payload={
                            "execution_id": execution.execution_id,
                            "agent_id": execution.agent_id,
                            "triggered_by": execution.triggered_by.value,
                            "error": error,
                        },
                        actor_id=execution.agent_id,
                        actor_type="agent",
                    )
                ],
                settlement,
            )
        except DomainError:
            # The run moved on between the read above and this write. Settling a run
            # somebody else is advancing is exactly the damage this guard exists to
            # prevent, so this pass loses the race and writes nothing.
            log.info("Run %s advanced while being settled; leaving it alone", execution_id)
            return
        await self._broadcast_persisted_events(events)
        await self._set_agent_status_safe(execution.agent_id, AgentStatus.FAILED)

    @staticmethod
    def _tool_observation(request: dict[str, Any]) -> str:
        """What the next prompt is told about the tool the last one asked for.

        The gateway's own record and nothing beside it: which tool, what the gateway
        decided, and the output it produced under this run's authority. A refusal is
        fed back too, so the model learns it was refused rather than asking again
        into silence.
        """
        return json.dumps(
            {
                "tool": request.get("tool", ""),
                "status": request.get("status", ""),
                "reason": request.get("reason", ""),
                "result": request.get("result", {}),
            },
            default=str,
        )

    @staticmethod
    def _validate_id(value: str, field_name: str) -> str:
        value = value.strip()
        if not value:
            raise DomainError(f"{field_name} must not be empty")
        if len(value) > 256:
            raise DomainError(f"{field_name} is too long")
        return value

    @staticmethod
    def _validate_idempotency_key(value: str) -> str:
        value = value.strip()
        if not value or len(value) > 128:
            raise DomainError("idempotency key must be 1-128 characters")
        return value

    @staticmethod
    def _validate_non_empty(value: str, field_name: str) -> str:
        value = value.strip()
        if not value:
            raise DomainError(f"{field_name} must not be empty")
        if len(value) > 10000:
            raise DomainError(f"{field_name} must not exceed 10000 characters")
        return value

    async def execute_agent_step(
        self, execution_id: str, prompt: str, acting_as: str = ""
    ) -> dict[str, Any]:
        """Run one agent turn to its end, however many provider calls that takes.

        A model that asked for a tool used to end the turn at the gateway: the call
        ran and was audited, and then nothing prompted the model again. The run held
        its lease in silence, no agent message reached the thread, and the sweep
        eventually stamped it ORPHANED — a false account of a dispatcher that had
        returned normally. The tool result is fed back here instead.

        A turn started from outside enters from idle, and only from idle. A second
        step reaching an execution already streaming or already parked at a reviewer
        finds the entrance closed, rather than prompting the model again on top of a
        turn already in flight; a step that resumes one after an approval decision
        is not this entrance, so it is not gated here.
        """
        return await self._continue_agent_turn(
            execution_id,
            _TurnContinuation(self._validate_non_empty(prompt, "agent prompt"), acting_as),
            require_idle=True,
        )

    async def get_agent(self, agent_id: str) -> AgentInstance:
        agent = await self.repos.agents.get_instance(agent_id)
        if not agent:
            raise DomainError(f"agent not found: {agent_id}")
        return agent

    async def get_branch(self, branch_id: str) -> Branch:
        branch = await self.repos.branches.get(branch_id)
        if branch is None:
            raise DomainError(f"branch not found: {branch_id}")
        return branch

    async def get_room(self, room_id: str) -> Room:
        room = await self.repos.rooms.get(room_id)
        if not room:
            raise DomainError(f"room not found: {room_id}")
        return room

    async def list_room_artifacts(self, room_id: str) -> list[Artifact]:
        return await self.repos.artifacts.list_by_room(room_id)

    async def list_room_outputs(self, room_id: str) -> list[AgentOutput]:
        await self.get_room(room_id)
        return await self.repos.agent_outputs.list_by_room(room_id)
