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
    USER_LEFT_ROOM = "user.left_room"
    AGENT_JOINED_ROOM = "agent.joined_room"
    AGENT_LEFT_ROOM = "agent.left_room"

    # Messages
    MESSAGE_CREATED = "message.created"
    MESSAGE_EDITED = "message.edited"

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

    # Artifacts
    ARTIFACT_CREATED = "artifact.created"
    ARTIFACT_UPDATED = "artifact.updated"
    ARTIFACT_VERSION_CREATED = "artifact.version_created"

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
