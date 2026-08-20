"""Core domain models for the multiplayer AI workspace."""

from __future__ import annotations

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


@dataclass(frozen=True, slots=True)
class RoomMember:
    room_id: str
    user_id: str
    role: str = "member"
    joined_at: datetime = field(default_factory=utcnow)


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
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True, slots=True)
class AgentRoomMembership:
    agent_id: str
    room_id: str
    joined_at: datetime = field(default_factory=utcnow)


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


@dataclass(frozen=True, slots=True)
class Execution:
    execution_id: str
    session_id: str
    agent_id: str
    run_id: str | None = None
    status: ExecutionStatus = ExecutionStatus.PENDING
    input_data: dict[str, Any] = field(default_factory=dict)
    output_data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    started_at: datetime = field(default_factory=utcnow)
    completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AgentOutput:
    """An immutable, inspectable result produced by one agent execution."""

    output_id: str
    room_id: str
    session_id: str
    execution_id: str
    agent_id: str
    content: str
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
    updated_at: datetime = field(default_factory=utcnow)


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
    created_at: datetime = field(default_factory=utcnow)


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
