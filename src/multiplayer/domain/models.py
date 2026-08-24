"""Core domain models for the multiplayer AI workspace."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def utcnow() -> datetime:
    return datetime.now(UTC)


class DomainError(ValueError):
    pass


class IdempotencyConflict(DomainError):
    """An idempotency key was replayed for a different or unfinished request."""


# ── User & Auth ──────────────────────────────────────────────────────────────


class UserStatus(StrEnum):
    ONLINE = "ONLINE"
    AWAY = "AWAY"
    OFFLINE = "OFFLINE"


@dataclass(frozen=True, slots=True)
class User:
    user_id: str
    display_name: str
    email: str
    avatar_url: str = ""
    status: UserStatus = UserStatus.OFFLINE
    created_at: datetime = field(default_factory=utcnow)


# ── The session a signed-in human holds ──────────────────────────────────────


@dataclass(frozen=True, slots=True)
class UserSession:
    """One sign-in, alive only while both of its clocks still hold.

    ``idle_expires_at`` moves forward while the session is used; an abandoned
    browser stops being a way in. ``absolute_expires_at`` never moves; a session
    used every minute still ends. Neither clock alone expresses both rules, and a
    reader that consults one of them has decided the other does not apply.

    ``subject`` and ``idp_session_id`` are the provider's ``sub`` and ``sid``.
    They exist so a back-channel logout naming either can find what to kill —
    without them the provider can say "this person is signed out" and be ignored.
    """

    session_id: str
    user_id: str
    issuer: str
    subject: str
    idp_session_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    idle_expires_at: datetime = field(default_factory=utcnow)
    absolute_expires_at: datetime = field(default_factory=utcnow)
    revoked_at: datetime | None = None
    revoked_reason: str = ""
    # Held only to be replayed to the provider as id_token_hint when signing out.
    # It is the provider's assertion that a login happened, never a credential
    # this API accepts.
    idp_id_token: str = ""
    # Spent against the provider on every refresh, so a person disabled there
    # loses this session at the next rotation rather than at the absolute clock.
    idp_refresh_token: str = ""

    def alive_at(self, moment: datetime) -> bool:
        """Both clocks and the revocation, answered together.

        Every caller needs all three, so there is one place that knows that, and
        no caller is trusted to remember the third.
        """
        if self.revoked_at is not None:
            return False
        return moment < self.idle_expires_at and moment < self.absolute_expires_at


@dataclass(frozen=True, slots=True)
class SessionRefreshToken:
    """A refresh credential, spendable once.

    ``consumed_at`` is the whole mechanism: a token presented with it already set
    is a replay, and a replay is either theft or a bug in the client. Both are
    answered by revoking the session rather than the token, because a token
    family that keeps working after one of its members leaked is a family that
    has not actually been contained.
    """

    token_hash: str
    session_id: str
    issued_at: datetime = field(default_factory=utcnow)
    expires_at: datetime = field(default_factory=utcnow)
    consumed_at: datetime | None = None
    replaced_by_hash: str | None = None


@dataclass(frozen=True, slots=True)
class OidcAuthorization:
    """The half of a login the browser is not trusted to carry.

    ``state`` answers cross-site request forgery, ``nonce`` answers replay of an
    ID token, and ``code_verifier`` answers an intercepted authorization code.
    All three are read from this row rather than from the request that comes
    back, and the row is consumable exactly once.
    """

    state: str
    nonce: str
    code_verifier: str
    # The digest of a cookie set on the browser that started this login. State
    # alone lives on the server and proves nothing about who came back.
    browser_binding_hash: str = ""
    created_at: datetime = field(default_factory=utcnow)
    expires_at: datetime = field(default_factory=utcnow)
    consumed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Organization:
    org_id: str
    name: str
    slug: str
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class OrgMember:
    org_id: str
    user_id: str
    role: str = "member"
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class Workspace:
    workspace_id: str
    org_id: str
    name: str
    slug: str
    created_at: datetime = field(default_factory=utcnow)
    allowed_capabilities: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceMember:
    workspace_id: str
    user_id: str
    role: str = "member"
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class BootstrapContext:
    """Principal-owned identity for the idempotent first workspace hierarchy."""

    user_id: str
    org_id: str
    workspace_id: str
    room_id: str
    created_at: datetime = field(default_factory=utcnow)


# ── Room ─────────────────────────────────────────────────────────────────────


class RoomStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


@dataclass(frozen=True, slots=True)
class Room:
    room_id: str
    workspace_id: str
    name: str
    description: str = ""
    status: RoomStatus = RoomStatus.ACTIVE
    created_by: str = ""
    created_at: datetime = field(default_factory=utcnow)
    allowed_capabilities: str | None = None


@dataclass(frozen=True, slots=True)
class RoomMember:
    room_id: str
    user_id: str
    role: str = "member"
    joined_at: datetime = field(default_factory=utcnow)
    allowed_capabilities: str | None = None


@dataclass(frozen=True, slots=True)
class ToolRequest:
    """A durable gateway decision for one agent tool request (PRD §14)."""

    request_id: str
    room_id: str
    execution_id: str
    agent_id: str
    requested_by: str
    tool: str
    # requested_by holds the agent id — the actor. authorized_by names the human
    # under whose authority the request acts; '' means authority was not recorded.
    authorized_by: str = ""
    input_json: str = "{}"
    required_capability: str | None = None
    effective_json: str = "[]"
    status: str = "PENDING_APPROVAL"
    reason: str = ""
    approval_id: str | None = None
    result_json: str = "{}"
    created_at: datetime = field(default_factory=utcnow)
    resolved_at: datetime | None = None


# ── Agent ────────────────────────────────────────────────────────────────────


class AgentStatus(StrEnum):
    IDLE = "IDLE"
    THINKING = "THINKING"
    WORKING = "WORKING"
    REVIEWING = "REVIEWING"
    DELEGATING = "DELEGATING"
    WAITING_INPUT = "WAITING_INPUT"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    BLOCKED = "BLOCKED"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    OFFLINE = "OFFLINE"


@dataclass(frozen=True, slots=True)
class AgentTemplate:
    template_id: str
    name: str
    description: str
    role: str
    system_prompt: str = ""
    capabilities: frozenset[str] = frozenset()
    preferred_tools: tuple[str, ...] = ()
    avatar_url: str = ""
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class AgentInstance:
    agent_id: str
    template_id: str
    room_id: str
    name: str
    role: str
    status: AgentStatus = AgentStatus.IDLE
    system_prompt: str = ""
    capabilities: frozenset[str] = frozenset()
    model_provider: str = ""
    model_name: str = ""
    # Which harness runs this agent's turns. An unknown id refuses to launch.
    harness_id: str = "nexus"
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class AgentRoomMembership:
    """One spell in a room. An agent that leaves and returns has two of these.

    A rejoin names the departure it follows, as a resumed run names the run it
    continues, so returning never erases the fact of having left.
    """

    agent_id: str
    room_id: str
    membership_id: str = field(default_factory=lambda: new_id("member"))
    joined_at: datetime = field(default_factory=utcnow)
    removed_at: datetime | None = None
    rejoined_from_membership_id: str | None = None


# ── Agent identity, addressing, and the run envelope ─────────────────────────


class ProofMode(StrEnum):
    """How an agent instance proves it is the one the identity row names.

    A key exists exactly when there is an untrusted transport to prove authorship
    across. An in-process harness has none, so it holds no key.
    """

    IN_PROCESS = "IN_PROCESS"
    SIGNED_CHALLENGE = "SIGNED_CHALLENGE"


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    """One immutable identity per agent instance, revoked once rather than per run."""

    identity_id: str
    agent_id: str
    proof_mode: ProofMode = ProofMode.IN_PROCESS
    public_key: str | None = None
    key_fingerprint: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    revoked_at: datetime | None = None


class AddressingMode(StrEnum):
    OWNER_ONLY = "OWNER_ONLY"
    ALLOWLIST = "ALLOWLIST"
    ANYONE = "ANYONE"
    NOBODY = "NOBODY"


@dataclass(frozen=True, slots=True)
class AgentAddressing:
    """Who may point this agent, stored here so a harness cannot widen its audience."""

    agent_id: str
    room_id: str
    mode: AddressingMode
    owner_user_id: str
    allowlist: frozenset[str] = frozenset()
    updated_at: datetime = field(default_factory=utcnow)
    updated_by: str = ""


class HarnessState(StrEnum):
    """Transport state of one turn. The domain state stays on executions.status."""

    STARTING = "STARTING"
    STREAMING = "STREAMING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    SETTLED = "SETTLED"


class RunSettlement(StrEnum):
    END_TURN = "END_TURN"
    CANCELLED = "CANCELLED"
    MAX_TOKENS = "MAX_TOKENS"
    FAILED = "FAILED"
    ORPHANED = "ORPHANED"
    AUTHORITY_REVOKED = "AUTHORITY_REVOKED"
    AGENT_REMOVED = "AGENT_REMOVED"
    APPROVAL_REFUSED = "APPROVAL_REFUSED"
    # A reviewer never answered. Not ORPHANED — nothing was orphaned, the run was
    # exactly where it said it was — and not PARKED, which is about a run that kept
    # dying. The only thing that happened is that nobody decided, and a reader of
    # this run is owed that rather than a borrowed name for something else.
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    # A run picked up max_attempts times that died every time. Parking it is what
    # keeps a stuck run from being swept forever without ever being describable.
    PARKED = "PARKED"


@dataclass(frozen=True, slots=True)
class AgentRun:
    """The identity-and-authority envelope around one executions row."""

    run_id: str
    execution_id: str
    agent_id: str
    identity_id: str
    room_id: str
    authorized_by: str
    acting_user_id: str
    harness_id: str
    credential_hash: str
    lease_expires_at: datetime
    harness_state: HarnessState = HarnessState.STARTING
    settlement: RunSettlement | None = None
    resumed_from_run_id: str | None = None
    challenge_verified_at: datetime | None = None
    attempts: int = 1
    max_attempts: int = 3
    created_at: datetime = field(default_factory=utcnow)
    settled_at: datetime | None = None


# ── Branch ───────────────────────────────────────────────────────────────────


class BranchMode(StrEnum):
    TURN_LOCKED_SINGLE = "TURN_LOCKED_SINGLE"
    PARALLEL = "PARALLEL"


class BranchStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class Branch:
    """One immutable AI-work context inside a durable room/channel."""

    branch_id: str
    room_id: str
    mode: BranchMode
    status: BranchStatus
    initiated_by: str
    initiating_prompt: str
    context_event_sequence: int
    context_message_ids: tuple[str, ...]
    context_snapshot: dict[str, Any]
    context_hash: str
    lifecycle_managed: bool = True
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    completed_at: datetime | None = None


# ── Session & Execution ──────────────────────────────────────────────────────


class SessionStatus(StrEnum):
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class Session:
    session_id: str
    room_id: str
    agent_id: str
    task_id: str | None = None
    status: SessionStatus = SessionStatus.CREATED
    started_at: datetime = field(default_factory=utcnow)
    ended_at: datetime | None = None


class ExecutionStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    PAUSED = "PAUSED"


class AgentTrigger(StrEnum):
    """Why an agent turn happened, recorded on the turn itself."""

    MENTION = "MENTION"
    DIRECT = "DIRECT"
    SCHEDULE = "SCHEDULE"


@dataclass(frozen=True, slots=True)
class Execution:
    execution_id: str
    session_id: str
    agent_id: str
    # The human whose authority this run carries. Capability terms are derived
    # from this principal at execution time, never from the agent or the branch.
    authorized_by: str = ""
    # The agent task this run answers, if it answers one. On the run rather than
    # on the task because a task opens a fresh run every time it resumes.
    agent_task_id: str | None = None
    branch_id: str = ""
    run_id: str | None = None
    triggered_by: AgentTrigger = AgentTrigger.DIRECT
    status: ExecutionStatus = ExecutionStatus.PENDING
    input_data: dict[str, Any] = field(default_factory=dict)
    output_data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    started_at: datetime = field(default_factory=utcnow)
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        """Carry the task link on every clone, rather than trusting five writers to.

        ``agent_task_id`` is a column so the bound can join on it, and it was set at
        exactly one of the five places an ``Execution`` is built. ``resume_agent_run``
        clones ``triggered_by`` and ``input_data`` from the earlier run — the task id
        is literally inside that dict — and left the column NULL, so a resumed
        delegated run lost its entire chain from the bound the moment it came back.

        Deriving it here is what makes that unrepeatable: a writer that carries the
        input data cannot drop the link, and a writer that means to set the column
        outright still can. A column four of five writers forget is a column that
        gets forgotten again.
        """
        if self.agent_task_id is None:
            carried = self.input_data.get("agent_task_id")
            if isinstance(carried, str) and carried:
                object.__setattr__(self, "agent_task_id", carried)


@dataclass(frozen=True, slots=True)
class ExecutionIntervention:
    """One human steer, kept beside the identity of whoever produced it.

    The row records who steered, never what they held. The step that consumes this
    instruction re-derives that person's effective set from durable records and runs
    under the run's terms intersected with it, so injected text can never widen a run
    beyond what its author holds *now* — a set stored here would say what they held
    then, and outlive being narrowed.
    """

    intervention_id: str
    execution_id: str
    intervened_by: str
    instruction: str
    consumed_at: datetime | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class AgentOutput:
    """An immutable, inspectable result produced by one agent execution."""

    output_id: str
    room_id: str
    session_id: str
    execution_id: str
    agent_id: str
    content: str
    branch_id: str = ""
    output_data: dict[str, Any] = field(default_factory=dict)
    # The human-authored prompt and the exact rendered provider request are
    # intentionally separate. The latter includes template instructions and
    # any interventions that actually reached the model.
    source_prompt: str = ""
    provider_input: str = ""
    provider_name: str = ""
    provider_model: str = ""
    provider_response_id: str = ""
    provider_interventions: tuple[str, ...] = ()
    provider_evidence: str = ""
    created_at: datetime = field(default_factory=utcnow)


class OutputDisposition(StrEnum):
    INCLUDED = "INCLUDED"
    EXCLUDED = "EXCLUDED"


@dataclass(frozen=True, slots=True)
class OutputSelection:
    """The room's durable, human-governed review decision for one output."""

    room_id: str
    output_id: str
    disposition: OutputDisposition
    decided_by: str
    branch_id: str = ""
    updated_at: datetime = field(default_factory=utcnow)


class BranchSynthesisStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class BranchSynthesis:
    synthesis_id: str
    branch_id: str
    room_id: str
    title: str
    initiated_by: str
    status: BranchSynthesisStatus = BranchSynthesisStatus.PENDING
    synthesis_type: str = "DECISION_BRIEF"
    provider_input: str = ""
    provider_name: str = ""
    provider_model: str = ""
    provider_response_id: str = ""
    provider_evidence: str = ""
    simulated: bool = False
    content: str = ""
    error: str = ""
    artifact_version_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BranchSynthesisInput:
    synthesis_id: str
    output_id: str
    ordinal: int


class TurnLockScopeType(StrEnum):
    ROOM = "ROOM"


class TurnLockStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"


@dataclass(frozen=True, slots=True)
class TurnLock:
    lock_id: str
    scope_type: TurnLockScopeType
    scope_id: str
    branch_id: str
    status: TurnLockStatus
    acquired_by: str
    acquired_at: datetime = field(default_factory=utcnow)
    released_at: datetime | None = None
    release_reason: str = ""


# ── Idempotency ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """Durable claim that one principal's keyed write already produced a result."""

    scope_id: str
    user_id: str
    idempotency_key: str
    operation: str
    request_hash: str
    result_ref: str
    created_at: datetime = field(default_factory=utcnow)


# ── Task ─────────────────────────────────────────────────────────────────────


class TaskStatus(StrEnum):
    CREATED = "CREATED"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskPriority(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class Task:
    task_id: str
    room_id: str
    title: str
    description: str = ""
    status: TaskStatus = TaskStatus.CREATED
    priority: TaskPriority = TaskPriority.NORMAL
    assigned_agent_id: str | None = None
    created_by: str = ""
    parent_task_id: str | None = None
    delegation_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class TaskDependency:
    task_id: str
    depends_on_task_id: str
    created_at: datetime = field(default_factory=utcnow)


# ── Message ──────────────────────────────────────────────────────────────────


class MessageRole(StrEnum):
    HUMAN = "HUMAN"
    AGENT = "AGENT"
    SYSTEM = "SYSTEM"


@dataclass(frozen=True, slots=True)
class Message:
    message_id: str
    room_id: str
    role: MessageRole
    sender_id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    # The sequence of the canonical event that created this message, so a client
    # resumes a message listing on the same cursor it resumes the event log on.
    event_sequence: int = 0
    parent_message_id: str | None = None
    root_message_id: str | None = None
    thread_depth: int = 0
    broadcast_to_room: bool = True
    created_at: datetime = field(default_factory=utcnow)


class ParticipantType(StrEnum):
    """The two kinds of principal a room can address or attribute an action to."""

    USER = "USER"
    AGENT = "AGENT"


# A handle is drawn from this alphabet, and _MENTION_PATTERN in the service reads
# exactly the same one, so every handle the system issues is a handle a mention can
# spell. Anything else in a display name becomes a separator.
_HANDLE_ALLOWED = "abcdefghijklmnopqrstuvwxyz0123456789_."


def handle_from_display_name(name: str) -> str:
    """The address derived from a display name, before the room makes it unique.

    Lowercased so that a mention is not a spelling test, and every run of
    characters outside the handle alphabet becomes a single hyphen, which is what
    turns "Security Reviewer" into a name a mention can actually carry. A name with
    nothing usable in it still has to be addressable, so it falls back rather than
    returning an empty handle nobody could type.
    """
    out: list[str] = []
    for char in name.strip().lower():
        if char in _HANDLE_ALLOWED:
            out.append(char)
        elif out and out[-1] != "-":
            out.append("-")
    handle = "".join(out).strip("-.")
    # The mention pattern requires an alphanumeric first character.
    while handle and not handle[0].isalnum():
        handle = handle[1:]
    return handle or "participant"


@dataclass(frozen=True, slots=True)
class RoomParticipantHandle:
    """One participant's durable address inside one room."""

    room_id: str
    participant_type: ParticipantType
    participant_id: str
    handle: str
    created_at: datetime = field(default_factory=utcnow)


class MentionTargetType(StrEnum):
    USER = "USER"
    AGENT = "AGENT"


@dataclass(frozen=True, slots=True)
class MessageMention:
    """One addressed target, derived from the message text, never client-supplied."""

    message_id: str
    room_id: str
    target_type: MentionTargetType
    target_id: str
    handle: str
    invoked_execution_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class MessageReaction:
    message_id: str
    room_id: str
    actor_id: str
    emoji: str
    # Which kind of principal reacted, so the reader is told whether the eyes on
    # their message are a teammate's or an agent's.
    actor_type: ParticipantType = ParticipantType.USER
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    removed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReadCursor:
    """A member's durable read position on the room's canonical event sequence."""

    room_id: str
    user_id: str
    last_read_sequence: int
    updated_at: datetime = field(default_factory=utcnow)


class SearchObjectKind(StrEnum):
    """Kinds that opted in to indexing. Anything absent here is never searchable.

    A kind earns a place here only when a member of the room can already read the
    indexed text through an existing endpoint under the same RoomCapability.READ
    that the search query joins on, so indexing widens what a reader can find and
    never what they are allowed to see.
    """

    MESSAGE = "MESSAGE"
    ARTIFACT_VERSION = "ARTIFACT_VERSION"
    TASK = "TASK"
    AGENT_OUTPUT = "AGENT_OUTPUT"
    DECISION = "DECISION"


@dataclass(frozen=True, slots=True)
class SearchHit:
    object_kind: SearchObjectKind
    object_id: str
    # The id a client needs alongside object_id to reach the object itself, empty
    # when object_id and room_id already address it. An artifact version is read
    # through its artifact; a message, task, output and decision are not.
    container_id: str
    room_id: str
    # The room's name travels with the hit: a result the reader cannot place is a
    # result they cannot act on, and re-reading rooms client-side would leak which
    # rooms exist beyond the ones the query already authorized.
    room_name: str
    author_id: str
    excerpt: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ThreadReply:
    """A reply and the number of replies it has, counted at read time."""

    message: Message
    reply_count: int


# A thread is a conversation, not a tree to recurse forever. Bounding the depth
# bounds every read that walks it and keeps the rendered indent finite.
MAX_THREAD_DEPTH = 8


@dataclass(frozen=True, slots=True)
class ThreadSummary:
    """What a channel needs to describe a thread, every field counted on read.

    Nothing here is stored: a counter maintained on the write path drifts from the
    reply rows it claims to summarise and nothing detects the drift.
    """

    root_message_id: str
    descendant_count: int
    participant_count: int
    last_reply_at: datetime | None


# ── Artifact ─────────────────────────────────────────────────────────────────


class ArtifactType(StrEnum):
    DOCUMENT = "DOCUMENT"
    CODE = "CODE"
    FILE = "FILE"
    DATASET = "DATASET"
    DESIGN = "DESIGN"
    CONFIGURATION = "CONFIGURATION"
    TASK = "TASK"


@dataclass(frozen=True, slots=True)
class Artifact:
    artifact_id: str
    room_id: str
    name: str
    artifact_type: ArtifactType
    description: str = ""
    current_version: int = 0
    created_by: str = ""
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class ArtifactVersion:
    version_id: str
    artifact_id: str
    version_number: int
    content: str = ""
    content_hash: str = ""
    provenance_hash: str = ""
    branch_synthesis_id: str | None = None
    created_by: str = ""
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class ArtifactClaim:
    """A final artifact claim whose evidence is exact immutable agent output text."""

    claim_id: str
    version_id: str
    ordinal: int
    text: str
    is_ai_derived: bool = True
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class ClaimSource:
    claim_id: str
    output_id: str
    evidence: str
    agent_id: str = ""
    execution_id: str = ""
    source_prompt: str = ""
    provider_input: str = ""
    provider_name: str = ""
    provider_model: str = ""
    provider_response_id: str = ""
    provider_interventions: tuple[str, ...] = ()
    provider_evidence: str = ""


# ── Decision ─────────────────────────────────────────────────────────────────


class OntologyEntityKind(StrEnum):
    PERSON = "Person"
    PROJECT = "Project"
    TASK = "Task"
    DECISION = "Decision"
    ARTIFACT = "Artifact"
    CLAIM = "Claim"
    AGENT_OUTPUT = "AgentOutput"


class OntologyRelationshipKind(StrEnum):
    OWNS = "OWNS"
    BLOCKS = "BLOCKS"
    DEPENDS_ON = "DEPENDS_ON"
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    REFERENCES = "REFERENCES"
    DERIVED_FROM = "DERIVED_FROM"


class OntologyDerivationKind(StrEnum):
    SYSTEM_MATERIALIZED = "SYSTEM_MATERIALIZED"
    AI_DERIVED = "AI_DERIVED"


class OntologyExtractor(StrEnum):
    """The three shipped extraction timings. There is no QUERY_TIME: reads never write."""

    IMMEDIATE = "IMMEDIATE"
    ASYNC = "ASYNC"
    SCHEDULED = "SCHEDULED"


class OntologyReviewStatus(StrEnum):
    UNCONFIRMED = "UNCONFIRMED"
    CONFIRMED = "CONFIRMED"
    CORRECTED = "CORRECTED"


class OntologyReviewAction(StrEnum):
    CONFIRM = "CONFIRM"
    CORRECT = "CORRECT"


class OntologyReviewTarget(StrEnum):
    ENTITY = "ENTITY"
    RELATIONSHIP = "RELATIONSHIP"


@dataclass(frozen=True, slots=True)
class OntologyEntity:
    """A room-scoped projection with explicit derivation and exact evidence IDs."""

    entity_id: str
    room_id: str
    kind: OntologyEntityKind
    source_object_id: str
    label: str
    properties: dict[str, Any] = field(default_factory=dict)
    derivation_kind: OntologyDerivationKind = OntologyDerivationKind.SYSTEM_MATERIALIZED
    confidence: float = 1.0
    evidence_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    review_status: OntologyReviewStatus = OntologyReviewStatus.UNCONFIRMED
    extractor: OntologyExtractor = OntologyExtractor.IMMEDIATE
    # Where in the room's total order this assertion was written. Currency is
    # derived from it per read; nothing stamps an assertion current.
    asserted_at_sequence: int = 0
    evidence_event_sequences: tuple[int, ...] = ()
    stale_at_sequence: int | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class OntologyRelationship:
    relationship_id: str
    room_id: str
    kind: OntologyRelationshipKind
    from_entity_id: str
    to_entity_id: str
    derivation_kind: OntologyDerivationKind = OntologyDerivationKind.SYSTEM_MATERIALIZED
    confidence: float = 1.0
    evidence_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    review_status: OntologyReviewStatus = OntologyReviewStatus.UNCONFIRMED
    # The durable row whose content states the relation — not automatically an
    # endpoint. Without it a relationship-centric answer cannot drill down.
    source_object_kind: str = ""
    source_object_id: str = ""
    extractor: OntologyExtractor = OntologyExtractor.IMMEDIATE
    asserted_at_sequence: int = 0
    evidence_event_sequences: tuple[int, ...] = ()
    stale_at_sequence: int | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class OntologyReview:
    review_id: str
    room_id: str
    target_type: OntologyReviewTarget
    target_id: str
    action: OntologyReviewAction
    before_value: dict[str, Any]
    after_value: dict[str, Any]
    reason: str
    reviewed_by: str
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class OntologyExtractionCursor:
    """One extractor's resume hint for one room. It decides nothing a reader sees."""

    room_id: str
    extractor: OntologyExtractor
    last_sequence: int
    last_run_at: str


# AI_DERIVED < SYSTEM_MATERIALIZED, UNCONFIRMED < CORRECTED < CONFIRMED. A derived
# assertion is only as good as its weakest input, so these orders decide what a
# consolidation edge over two unconfirmed entities may claim to be.
_DERIVATION_STRENGTH: dict[OntologyDerivationKind, int] = {
    OntologyDerivationKind.AI_DERIVED: 0,
    OntologyDerivationKind.SYSTEM_MATERIALIZED: 1,
}
_REVIEW_STRENGTH: dict[OntologyReviewStatus, int] = {
    OntologyReviewStatus.UNCONFIRMED: 0,
    OntologyReviewStatus.CORRECTED: 1,
    OntologyReviewStatus.CONFIRMED: 2,
}


def weakest_derivation_kind(
    kinds: Iterable[OntologyDerivationKind],
) -> OntologyDerivationKind | None:
    """The weakest derivation among some inputs; None when there are no inputs."""
    return min(kinds, key=lambda kind: _DERIVATION_STRENGTH[kind], default=None)


def weakest_review_status(
    statuses: Iterable[OntologyReviewStatus],
) -> OntologyReviewStatus | None:
    """The weakest review status among some inputs; None when there are no inputs."""
    return min(statuses, key=lambda status: _REVIEW_STRENGTH[status], default=None)


class DecisionStatus(StrEnum):
    PROPOSED = "PROPOSED"
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class Decision:
    decision_id: str
    room_id: str
    title: str
    content: str
    reason: str = ""
    status: DecisionStatus = DecisionStatus.PROPOSED
    created_by: str = ""
    reviewed_by: str = ""
    created_at: datetime = field(default_factory=utcnow)


# ── Memory ───────────────────────────────────────────────────────────────────


class MemoryScope(StrEnum):
    ROOM = "ROOM"
    WORKSPACE = "WORKSPACE"
    ORGANIZATION = "ORGANIZATION"
    AGENT_PRIVATE = "AGENT_PRIVATE"
    USER_PRIVATE = "USER_PRIVATE"


@dataclass(frozen=True, slots=True)
class Memory:
    memory_id: str
    room_id: str | None
    workspace_id: str | None
    org_id: str | None
    scope: MemoryScope
    content: str
    memory_type: str = "fact"
    is_authoritative: bool = False
    superseded_by: str | None = None
    created_by: str = ""
    created_at: datetime = field(default_factory=utcnow)


# ── Approval ─────────────────────────────────────────────────────────────────


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class Approval:
    approval_id: str
    room_id: str
    execution_id: str
    agent_id: str
    action_description: str
    authorized_by: str = ""
    status: ApprovalStatus = ApprovalStatus.PENDING
    reviewer_id: str | None = None
    review_comment: str = ""
    requested_at: datetime = field(default_factory=utcnow)
    reviewed_at: datetime | None = None


# ── Notification ─────────────────────────────────────────────────────────────


class NotificationStatus(StrEnum):
    UNREAD = "UNREAD"
    READ = "READ"
    DISMISSED = "DISMISSED"


@dataclass(frozen=True, slots=True)
class Notification:
    notification_id: str
    user_id: str
    room_id: str | None
    title: str
    body: str
    notification_type: str = "info"
    status: NotificationStatus = NotificationStatus.UNREAD
    created_at: datetime = field(default_factory=utcnow)


# ── Presence ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Presence:
    user_id: str
    room_id: str
    status: UserStatus = UserStatus.ONLINE
    last_seen: datetime = field(default_factory=utcnow)


# ── Credential & Tool Permission ─────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Credential:
    credential_id: str
    org_id: str
    name: str
    credential_type: str
    encrypted_data: str
    created_by: str = ""
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class ToolPermission:
    permission_id: str
    agent_id: str
    room_id: str
    tool_name: str
    allowed: bool = True
    requires_approval: bool = False
    created_at: datetime = field(default_factory=utcnow)
