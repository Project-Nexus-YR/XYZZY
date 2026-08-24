"""Event model for the multiplayer workspace."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from .models import new_id, utcnow


class EventType(StrEnum):
    # Room lifecycle
    ROOM_CREATED = "room.created"
    ROOM_UPDATED = "room.updated"
    ROOM_ARCHIVED = "room.archived"

    # Member events
    USER_JOINED_ROOM = "user.joined_room"
    USER_INVITED_ROOM = "user.invited_room"
    USER_LEFT_ROOM = "user.left_room"
    USER_ROLE_CHANGED = "user.role_changed"
    USER_REMOVED_ROOM = "user.removed_room"
    AGENT_JOINED_ROOM = "agent.joined_room"
    AGENT_LEFT_ROOM = "agent.left_room"
    # A distinct event from a first join: the room's record shows the agent left and
    # came back, rather than showing it joining twice with nothing in between.
    AGENT_REJOINED_ROOM = "agent.rejoined_room"

    # Messages
    MESSAGE_CREATED = "message.created"
    MESSAGE_EDITED = "message.edited"
    MESSAGE_REACTION_ADDED = "message.reaction_added"
    MESSAGE_REACTION_REMOVED = "message.reaction_removed"

    # Tasks
    TASK_CREATED = "task.created"
    TASK_ASSIGNED = "task.assigned"
    TASK_UNASSIGNED = "task.unassigned"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"
    TASK_DELEGATED = "task.delegated"

    # Agent lifecycle
    AGENT_STATUS_CHANGED = "agent.status_changed"
    AGENT_STARTED_EXECUTION = "agent.started_execution"
    AGENT_FINISHED_EXECUTION = "agent.finished_execution"
    AGENT_REQUESTED_INPUT = "agent.requested_input"
    AGENT_REQUESTED_APPROVAL = "agent.requested_approval"
    AGENT_BLOCKED = "agent.blocked"

    # Execution
    EXECUTION_STARTED = "execution.started"
    EXECUTION_COMPLETED = "execution.completed"
    EXECUTION_FAILED = "execution.failed"
    EXECUTION_PAUSED = "execution.paused"
    EXECUTION_CANCELLED = "execution.cancelled"

    # Agent identity, addressing, and the run envelope.
    AGENT_IDENTITY_REGISTERED = "agent.identity.registered"
    AGENT_IDENTITY_REVOKED = "agent.identity.revoked"
    AGENT_LAUNCH_REFUSED = "agent.launch.refused"
    AGENT_ADDRESSING_UPDATED = "agent.addressing.updated"
    AGENT_ADDRESSING_REFUSED = "agent.addressing.refused"
    AGENT_RUN_SETTLED = "agent.run.settled"
    AGENT_RUN_ORPHANED = "agent.run.orphaned"
    AGENT_RUN_AUTHORITY_REVOKED = "agent.run.authority_revoked"

    # Canonical agent run/output events used by reconnect and provenance.
    AGENT_RUN_STARTED = "agent.run.started"
    AGENT_OUTPUT_CREATED = "agent.output.created"
    AGENT_RUN_COMPLETED = "agent.run.completed"
    OUTPUT_SELECTION_UPDATED = "output.selection.updated"

    # First-class branch and turn ownership lifecycle.
    BRANCH_STARTED = "branch.started"
    BRANCH_COMPLETED = "branch.completed"
    BRANCH_PARTIAL = "branch.partial"
    BRANCH_FAILED = "branch.failed"
    BRANCH_CANCELLED = "branch.cancelled"
    TURN_LOCK_ACQUIRED = "turn_lock.acquired"
    TURN_LOCK_RELEASED = "turn_lock.released"
    BRANCH_SYNTHESIS_STARTED = "branch.synthesis.started"
    BRANCH_SYNTHESIS_COMPLETED = "branch.synthesis.completed"
    BRANCH_SYNTHESIS_FAILED = "branch.synthesis.failed"

    # Artifacts
    ARTIFACT_CREATED = "artifact.created"
    ARTIFACT_UPDATED = "artifact.updated"
    ARTIFACT_VERSION_CREATED = "artifact.version_created"
    DECISION_BRIEF_SYNTHESIZED = "artifact.decision_brief_synthesized"
    SYNTHESIS_PUBLISHED = "artifact.synthesis_published"

    # Evidence-backed ontology projection and human governance.
    ONTOLOGY_MATERIALIZED = "ontology.materialized"
    ONTOLOGY_ASSERTION_CONFIRMED = "ontology.assertion_confirmed"
    ONTOLOGY_ASSERTION_CORRECTED = "ontology.assertion_corrected"
    ONTOLOGY_ASSERTION_SUPERSEDED = "ontology.assertion_superseded"
    ONTOLOGY_EXTRACTION_ADVANCED = "ontology.extraction.advanced"

    # Decisions
    DECISION_CREATED = "decision.created"
    DECISION_UPDATED = "decision.updated"
    DECISION_SUPERSEDED = "decision.superseded"

    # Memory
    MEMORY_CREATED = "memory.created"
    MEMORY_UPDATED = "memory.updated"

    # Approvals
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_REJECTED = "approval.rejected"
    # Closed with the run it gated, because nobody decided it.
    APPROVAL_EXPIRED = "approval.expired"

    # Notifications
    NOTIFICATION_CREATED = "notification.created"

    # Human intervention
    HUMAN_INTERRUPTED_AGENT = "human.interrupted_agent"
    HUMAN_REDIRECTED_AGENT = "human.redirected_agent"
    HUMAN_APPROVED_ACTION = "human.approved_action"
    HUMAN_REJECTED_ACTION = "human.rejected_action"
    HUMAN_TOOK_OVER_TASK = "human.took_over_task"
    HUMAN_HANDED_BACK_TASK = "human.handed_back_task"

    # Presence
    PRESENCE_CHANGED = "presence.changed"

    # Tool calls
    TOOL_CALL_STARTED = "tool.call_started"
    TOOL_CALL_COMPLETED = "tool.call_completed"
    TOOL_CALL_FAILED = "tool.call_failed"
    TOOL_CALL_REJECTED = "tool.call_rejected"
    ROOM_POLICY_UPDATED = "room.policy_updated"
    WORKSPACE_POLICY_UPDATED = "workspace.policy_updated"

    # Session
    SESSION_STARTED = "session.started"
    SESSION_PAUSED = "session.paused"
    SESSION_RESUMED = "session.resumed"
    SESSION_COMPLETED = "session.completed"


@dataclass(frozen=True, slots=True)
class RoomEvent:
    """A durable, ordered event within a room."""

    room_id: str
    sequence: int
    event_type: EventType
    payload: dict[str, Any]
    actor_id: str
    actor_type: str  # "user" | "agent" | "system"
    event_id: str = field(default_factory=lambda: new_id("evt"))
    timestamp: datetime = field(default_factory=utcnow)
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class OrgEvent:
    """A durable event within an organization."""

    org_id: str
    sequence: int
    event_type: EventType
    payload: dict[str, Any]
    actor_id: str
    actor_type: str
    event_id: str = field(default_factory=lambda: new_id("evt"))
    timestamp: datetime = field(default_factory=utcnow)
    schema_version: int = 1
