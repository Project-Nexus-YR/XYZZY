"""Repository layer: typed data access over the multiplayer database."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime
from typing import Any

from ..domain.events import EventType, RoomEvent
from ..domain.models import (
    AgentInstance,
    AgentStatus,
    AgentTemplate,
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactType,
    ArtifactVersion,
    Decision,
    DecisionStatus,
    Execution,
    ExecutionStatus,
    Memory,
    MemoryScope,
    Message,
    MessageRole,
    Notification,
    NotificationStatus,
    Organization,
    OrgMember,
    Presence,
    Room,
    RoomMember,
    RoomStatus,
    Session,
    SessionStatus,
    Task,
    TaskDependency,
    TaskPriority,
    TaskStatus,
    ToolPermission,
    User,
    UserStatus,
    Workspace,
    WorkspaceMember,
    utcnow,
)
from .connection import Database, serialize_datetime

log = logging.getLogger(__name__)


class Repos:
    """Access point for all repository operations."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.users = UserRepo(db)
        self.orgs = OrgRepo(db)
        self.workspaces = WorkspaceRepo(db)
        self.rooms = RoomRepo(db)
        self.room_members = RoomMemberRepo(db)
        self.agents = AgentRepo(db)
        self.sessions = SessionRepo(db)
        self.executions = ExecutionRepo(db)
        self.tasks = TaskRepo(db)
        self.messages = MessageRepo(db)
        self.events = EventRepo(db)
        self.artifacts = ArtifactRepo(db)
        self.decisions = DecisionRepo(db)
        self.memories = MemoryRepo(db)
        self.approvals = ApprovalRepo(db)
        self.notifications = NotificationRepo(db)
        self.presence = PresenceRepo(db)
        self.tool_permissions = ToolPermissionRepo(db)


class UserRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, user: User) -> User:
        await self.db.execute(
            "INSERT INTO users(user_id, display_name, email, avatar_url, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user.user_id, user.display_name, user.email, user.avatar_url,
             user.status.value, serialize_datetime(user.created_at)),
        )
        await self.db.commit()
        return user

    async def get(self, user_id: str) -> User | None:
        row = await self.db.fetch_one("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return None if row is None else self._from_row(row)

    async def get_by_email(self, email: str) -> User | None:
        row = await self.db.fetch_one("SELECT * FROM users WHERE email = ?", (email,))
        return None if row is None else self._from_row(row)

    async def update_status(self, user_id: str, status: UserStatus) -> None:
        await self.db.execute(
            "UPDATE users SET status = ? WHERE user_id = ?", (status.value, user_id)
        )
        await self.db.commit()

    async def list_all(self) -> list[User]:
        rows = await self.db.fetch_all("SELECT * FROM users ORDER BY created_at")
        return [self._from_row(r) for r in rows]

    def _from_row(self, row: dict[str, Any]) -> User:
        return User(
            user_id=row["user_id"],
            display_name=row["display_name"],
            email=row["email"],
            avatar_url=row["avatar_url"],
            status=UserStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )


class OrgRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, org: Organization) -> Organization:
        await self.db.execute(
            "INSERT INTO organizations(org_id, name, slug, created_at) VALUES (?, ?, ?, ?)",
            (org.org_id, org.name, org.slug, serialize_datetime(org.created_at)),
        )
        await self.db.commit()
        return org

    async def get(self, org_id: str) -> Organization | None:
        row = await self.db.fetch_one("SELECT * FROM organizations WHERE org_id = ?", (org_id,))
        return None if row is None else Organization(
            org_id=row["org_id"], name=row["name"], slug=row["slug"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    async def add_member(self, member: OrgMember) -> None:
        await self.db.execute(
            "INSERT INTO organization_members(org_id, user_id, role, created_at) VALUES (?, ?, ?, ?)",
            (member.org_id, member.user_id, member.role, serialize_datetime(member.created_at)),
        )
        await self.db.commit()

    async def list_members(self, org_id: str) -> list[OrgMember]:
        rows = await self.db.fetch_all(
            "SELECT * FROM organization_members WHERE org_id = ?", (org_id,)
        )
        return [
            OrgMember(org_id=r["org_id"], user_id=r["user_id"], role=r["role"],
                      created_at=datetime.fromisoformat(r["created_at"]))
            for r in rows
        ]


class WorkspaceRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, ws: Workspace) -> Workspace:
        await self.db.execute(
            "INSERT INTO workspaces(workspace_id, org_id, name, slug, created_at) VALUES (?, ?, ?, ?, ?)",
            (ws.workspace_id, ws.org_id, ws.name, ws.slug, serialize_datetime(ws.created_at)),
        )
        await self.db.commit()
        return ws

    async def get(self, workspace_id: str) -> Workspace | None:
        row = await self.db.fetch_one(
            "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
        )
        return None if row is None else Workspace(
            workspace_id=row["workspace_id"], org_id=row["org_id"],
            name=row["name"], slug=row["slug"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    async def list_by_org(self, org_id: str) -> list[Workspace]:
        rows = await self.db.fetch_all(
            "SELECT * FROM workspaces WHERE org_id = ? ORDER BY created_at", (org_id,)
        )
        return [
            Workspace(workspace_id=r["workspace_id"], org_id=r["org_id"],
                      name=r["name"], slug=r["slug"],
                      created_at=datetime.fromisoformat(r["created_at"]))
            for r in rows
        ]

    async def add_member(self, member: WorkspaceMember) -> None:
        await self.db.execute(
            "INSERT INTO workspace_members(workspace_id, user_id, role, created_at) VALUES (?, ?, ?, ?)",
            (member.workspace_id, member.user_id, member.role,
             serialize_datetime(member.created_at)),
        )
        await self.db.commit()


class RoomRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, room: Room) -> Room:
        await self.db.execute(
            "INSERT INTO rooms(room_id, workspace_id, name, description, status, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (room.room_id, room.workspace_id, room.name, room.description,
             room.status.value, room.created_by, serialize_datetime(room.created_at)),
        )
        await self.db.commit()
        return room

    async def get(self, room_id: str) -> Room | None:
        row = await self.db.fetch_one("SELECT * FROM rooms WHERE room_id = ?", (room_id,))
        return None if row is None else self._from_row(row)

    async def list_by_workspace(self, workspace_id: str) -> list[Room]:
        rows = await self.db.fetch_all(
            "SELECT * FROM rooms WHERE workspace_id = ? ORDER BY created_at", (workspace_id,)
        )
        return [self._from_row(r) for r in rows]

    async def update_status(self, room_id: str, status: RoomStatus) -> None:
        await self.db.execute(
            "UPDATE rooms SET status = ? WHERE room_id = ?", (status.value, room_id)
        )
        await self.db.commit()

    def _from_row(self, row: dict[str, Any]) -> Room:
        return Room(
            room_id=row["room_id"], workspace_id=row["workspace_id"],
            name=row["name"], description=row["description"],
            status=RoomStatus(row["status"]),
            created_by=row["created_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )


class RoomMemberRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(self, member: RoomMember) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO room_members(room_id, user_id, role, joined_at) VALUES (?, ?, ?, ?)",
            (member.room_id, member.user_id, member.role,
             serialize_datetime(member.joined_at)),
        )
        await self.db.commit()

    async def remove(self, room_id: str, user_id: str) -> None:
        await self.db.execute(
            "DELETE FROM room_members WHERE room_id = ? AND user_id = ?", (room_id, user_id)
        )
        await self.db.commit()

    async def list(self, room_id: str) -> list[RoomMember]:
        rows = await self.db.fetch_all(
            "SELECT * FROM room_members WHERE room_id = ?", (room_id,)
        )
        return [
            RoomMember(room_id=r["room_id"], user_id=r["user_id"], role=r["role"],
                       joined_at=datetime.fromisoformat(r["joined_at"]))
            for r in rows
        ]

    async def is_member(self, room_id: str, user_id: str) -> bool:
        row = await self.db.fetch_one(
            "SELECT 1 FROM room_members WHERE room_id = ? AND user_id = ?",
            (room_id, user_id),
        )
        return row is not None


class AgentRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create_template(self, template: AgentTemplate) -> AgentTemplate:
        await self.db.execute(
            "INSERT INTO agent_templates(template_id, name, description, role, system_prompt, "
            "capabilities, preferred_tools, avatar_url, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (template.template_id, template.name, template.description, template.role,
             template.system_prompt, json.dumps(sorted(template.capabilities)),
             json.dumps(list(template.preferred_tools)), template.avatar_url,
             serialize_datetime(template.created_at)),
        )
        await self.db.commit()
        return template

    async def get_template(self, template_id: str) -> AgentTemplate | None:
        row = await self.db.fetch_one(
            "SELECT * FROM agent_templates WHERE template_id = ?", (template_id,)
        )
        return None if row is None else self._template_from_row(row)

    async def list_templates(self) -> list[AgentTemplate]:
        rows = await self.db.fetch_all("SELECT * FROM agent_templates ORDER BY created_at")
        return [self._template_from_row(r) for r in rows]

    async def create_instance(self, agent: AgentInstance) -> AgentInstance:
        await self.db.execute(
            "INSERT INTO agent_instances(agent_id, template_id, room_id, name, role, status, "
            "system_prompt, capabilities, model_provider, model_name, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (agent.agent_id, agent.template_id, agent.room_id, agent.name, agent.role,
             agent.status.value, agent.system_prompt,
             json.dumps(sorted(agent.capabilities)),
             agent.model_provider, agent.model_name,
             serialize_datetime(agent.created_at)),
        )
        await self.db.commit()
        return agent

    async def get_instance(self, agent_id: str) -> AgentInstance | None:
        row = await self.db.fetch_one(
            "SELECT * FROM agent_instances WHERE agent_id = ?", (agent_id,)
        )
        return None if row is None else self._instance_from_row(row)

    async def list_instances_by_room(self, room_id: str) -> list[AgentInstance]:
        rows = await self.db.fetch_all(
            "SELECT * FROM agent_instances WHERE room_id = ? ORDER BY created_at", (room_id,)
        )
        return [self._instance_from_row(r) for r in rows]

    async def update_status(self, agent_id: str, status: AgentStatus) -> None:
        await self.db.execute(
            "UPDATE agent_instances SET status = ? WHERE agent_id = ?",
            (status.value, agent_id),
        )
        await self.db.commit()

    async def add_room_membership(self, membership: Any) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO agent_room_memberships(agent_id, room_id, joined_at) "
            "VALUES (?, ?, ?)",
            (membership.agent_id, membership.room_id,
             serialize_datetime(membership.joined_at)),
        )
        await self.db.commit()

    def _template_from_row(self, row: dict[str, Any]) -> AgentTemplate:
        return AgentTemplate(
            template_id=row["template_id"], name=row["name"],
            description=row["description"], role=row["role"],
            system_prompt=row["system_prompt"],
            capabilities=frozenset(json.loads(row["capabilities"])),
            preferred_tools=tuple(json.loads(row["preferred_tools"])),
            avatar_url=row["avatar_url"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def _instance_from_row(self, row: dict[str, Any]) -> AgentInstance:
        return AgentInstance(
            agent_id=row["agent_id"], template_id=row["template_id"],
            room_id=row["room_id"], name=row["name"], role=row["role"],
            status=AgentStatus(row["status"]),
            system_prompt=row["system_prompt"],
            capabilities=frozenset(json.loads(row["capabilities"])),
            model_provider=row["model_provider"],
            model_name=row["model_name"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )


class SessionRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, session: Session) -> Session:
        await self.db.execute(
            "INSERT INTO sessions(session_id, room_id, agent_id, task_id, status, started_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session.session_id, session.room_id, session.agent_id, session.task_id,
             session.status.value, serialize_datetime(session.started_at),
             serialize_datetime(session.ended_at)),
        )
        await self.db.commit()
        return session

    async def get(self, session_id: str) -> Session | None:
        row = await self.db.fetch_one(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        )
        return None if row is None else self._from_row(row)

    async def update_status(self, session_id: str, status: SessionStatus) -> None:
        updates: dict[str, str] = {"status": status.value}
        if status in (SessionStatus.COMPLETED, SessionStatus.FAILED):
            updates["ended_at"] = utcnow().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        await self.db.execute(
            f"UPDATE sessions SET {set_clause} WHERE session_id = ?",
            (*updates.values(), session_id),
        )
        await self.db.commit()

    async def list_by_room(self, room_id: str) -> list[Session]:
        rows = await self.db.fetch_all(
            "SELECT * FROM sessions WHERE room_id = ? ORDER BY started_at DESC", (room_id,)
        )
        return [self._from_row(r) for r in rows]

    def _from_row(self, row: dict[str, Any]) -> Session:
        return Session(
            session_id=row["session_id"], room_id=row["room_id"],
            agent_id=row["agent_id"], task_id=row.get("task_id"),
            status=SessionStatus(row["status"]),
            started_at=datetime.fromisoformat(row["started_at"]),
            ended_at=datetime.fromisoformat(row["ended_at"]) if row.get("ended_at") else None,
        )


class ExecutionRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, execution: Execution) -> Execution:
        await self.db.execute(
            "INSERT INTO executions(execution_id, session_id, agent_id, run_id, status, "
            "input_data, output_data, error, started_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (execution.execution_id, execution.session_id, execution.agent_id,
             execution.run_id, execution.status.value,
             json.dumps(execution.input_data), json.dumps(execution.output_data),
             execution.error, serialize_datetime(execution.started_at),
             serialize_datetime(execution.completed_at)),
        )
        await self.db.commit()
        return execution

    async def get(self, execution_id: str) -> Execution | None:
        row = await self.db.fetch_one(
            "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
        )
        return None if row is None else self._from_row(row)

    async def update_status(
        self, execution_id: str, status: ExecutionStatus,
        output_data: dict[str, Any] | None = None, error: str = "",
    ) -> None:
        updates: dict[str, Any] = {"status": status.value}
        if output_data is not None:
            updates["output_data"] = json.dumps(output_data)
        if error:
            updates["error"] = error
        if status in (ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED):
            updates["completed_at"] = utcnow().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        await self.db.execute(
            f"UPDATE executions SET {set_clause} WHERE execution_id = ?",
            (*updates.values(), execution_id),
        )
        await self.db.commit()

    async def list_by_session(self, session_id: str) -> list[Execution]:
        rows = await self.db.fetch_all(
            "SELECT * FROM executions WHERE session_id = ? ORDER BY started_at", (session_id,)
        )
        return [self._from_row(r) for r in rows]

    def _from_row(self, row: dict[str, Any]) -> Execution:
        try:
            input_data = json.loads(row["input_data"])
        except (json.JSONDecodeError, TypeError):
            input_data = {}
        try:
            output_data = json.loads(row["output_data"])
        except (json.JSONDecodeError, TypeError):
            output_data = {}
        return Execution(
            execution_id=row["execution_id"], session_id=row["session_id"],
            agent_id=row["agent_id"], run_id=row.get("run_id"),
            status=ExecutionStatus(row["status"]),
            input_data=input_data,
            output_data=output_data,
            error=row["error"],
            started_at=datetime.fromisoformat(row["started_at"]),
            completed_at=datetime.fromisoformat(row["completed_at"]) if row.get("completed_at") else None,
        )


class TaskRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, task: Task) -> Task:
        now = utcnow().isoformat()
        await self.db.execute(
            "INSERT INTO tasks(task_id, room_id, title, description, status, priority, "
            "assigned_agent_id, created_by, parent_task_id, delegation_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task.task_id, task.room_id, task.title, task.description,
             task.status.value, task.priority.value,
             task.assigned_agent_id, task.created_by,
             task.parent_task_id, task.delegation_id,
             serialize_datetime(task.created_at), serialize_datetime(task.updated_at)),
        )
        await self.db.commit()
        return task

    async def get(self, task_id: str) -> Task | None:
        row = await self.db.fetch_one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        return None if row is None else self._from_row(row)

    async def update(self, task: Task) -> Task:
        await self.db.execute(
            "UPDATE tasks SET title = ?, description = ?, status = ?, priority = ?, "
            "assigned_agent_id = ?, updated_at = ? WHERE task_id = ?",
            (task.title, task.description, task.status.value, task.priority.value,
             task.assigned_agent_id, serialize_datetime(utcnow()), task.task_id),
        )
        await self.db.commit()
        return task

    async def list_by_room(self, room_id: str) -> list[Task]:
        rows = await self.db.fetch_all(
            "SELECT * FROM tasks WHERE room_id = ? ORDER BY created_at", (room_id,)
        )
        return [self._from_row(r) for r in rows]

    async def list_by_status(self, room_id: str, status: TaskStatus) -> list[Task]:
        rows = await self.db.fetch_all(
            "SELECT * FROM tasks WHERE room_id = ? AND status = ? ORDER BY created_at",
            (room_id, status.value),
        )
        return [self._from_row(r) for r in rows]

    async def add_dependency(self, dep: TaskDependency) -> None:
        await self.db.execute(
            "INSERT INTO task_dependencies(task_id, depends_on_task_id, created_at) VALUES (?, ?, ?)",
            (dep.task_id, dep.depends_on_task_id, serialize_datetime(dep.created_at)),
        )
        await self.db.commit()

    def _from_row(self, row: dict[str, Any]) -> Task:
        return Task(
            task_id=row["task_id"], room_id=row["room_id"],
            title=row["title"], description=row["description"],
            status=TaskStatus(row["status"]),
            priority=TaskPriority(row["priority"]),
            assigned_agent_id=row.get("assigned_agent_id"),
            created_by=row["created_by"],
            parent_task_id=row.get("parent_task_id"),
            delegation_id=row.get("delegation_id"),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


class MessageRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, message: Message) -> Message:
        await self.db.execute(
            "INSERT INTO messages(message_id, room_id, role, sender_id, content, metadata, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message.message_id, message.room_id, message.role.value,
             message.sender_id, message.content,
             json.dumps(message.metadata), serialize_datetime(message.created_at)),
        )
        await self.db.commit()
        return message

    async def list_by_room(self, room_id: str, limit: int = 100, offset: int = 0) -> list[Message]:
        limit = min(limit, 500)
        rows = await self.db.fetch_all(
            "SELECT * FROM messages WHERE room_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (room_id, limit, offset),
        )
        return [
            Message(
                message_id=r["message_id"], room_id=r["room_id"],
                role=MessageRole(r["role"]), sender_id=r["sender_id"],
                content=r["content"], metadata=json.loads(r["metadata"]),
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in reversed(rows)
        ]


class EventRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def append(self, event: RoomEvent) -> RoomEvent:
        await self.db.execute(
            "INSERT INTO room_events(event_id, room_id, sequence, event_type, payload, "
            "actor_id, actor_type, timestamp, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event.event_id, event.room_id, event.sequence, event.event_type.value,
             json.dumps(event.payload, default=str), event.actor_id, event.actor_type,
             serialize_datetime(event.timestamp), event.schema_version),
        )
        await self.db.commit()
        return event

    async def append_with_next_sequence(self, event: RoomEvent) -> RoomEvent:
        """Atomically increment sequence counter and insert event.

        Uses RETURNING on the sequence increment to get the sequence atomically,
        then inserts the event. Since aiosqlite serializes all DB operations
        through a single thread, the sequence is guaranteed unique between
        the RETURNING and the event INSERT.
        """
        cursor = await self.db.execute(
            "INSERT INTO room_sequences(room_id, seq) VALUES (?, 1) "
            "ON CONFLICT(room_id) DO UPDATE SET seq = seq + 1 "
            "RETURNING seq",
            (event.room_id,),
        )
        row = await cursor.fetchone()
        seq = int(row["seq"]) if row else 1
        event = replace(event, sequence=seq)
        await self.db.execute(
            "INSERT INTO room_events(event_id, room_id, sequence, event_type, payload, "
            "actor_id, actor_type, timestamp, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event.event_id, event.room_id, event.sequence, event.event_type.value,
             json.dumps(event.payload, default=str), event.actor_id, event.actor_type,
             serialize_datetime(event.timestamp), event.schema_version),
        )
        await self.db.commit()
        return event

    async def append_batch(self, events: list[RoomEvent]) -> None:
        """Insert multiple events in a single transaction."""
        for event in events:
            await self.db.execute(
                "INSERT INTO room_events(event_id, room_id, sequence, event_type, payload, "
                "actor_id, actor_type, timestamp, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event.event_id, event.room_id, event.sequence, event.event_type.value,
                 json.dumps(event.payload, default=str), event.actor_id, event.actor_type,
                 serialize_datetime(event.timestamp), event.schema_version),
            )
        await self.db.commit()

    async def get_next_sequence(self, room_id: str) -> int:
        """Atomically increment and return the next sequence for a room.

        Uses INSERT ON CONFLICT DO UPDATE which is atomic in SQLite.
        The sequence counter table (room_sequences) guarantees strictly
        monotonically increasing sequences even under concurrent access.
        """
        await self.db.execute(
            "INSERT INTO room_sequences(room_id, seq) VALUES (?, 1) "
            "ON CONFLICT(room_id) DO UPDATE SET seq = seq + 1",
            (room_id,),
        )
        row = await self.db.fetch_one(
            "SELECT seq FROM room_sequences WHERE room_id = ?", (room_id,)
        )
        return int(row["seq"]) if row else 1

    async def list_since(self, room_id: str, after_sequence: int, limit: int = 500) -> list[RoomEvent]:
        rows = await self.db.fetch_all(
            "SELECT * FROM room_events WHERE room_id = ? AND sequence > ? "
            "ORDER BY sequence ASC LIMIT ?",
            (room_id, after_sequence, limit),
        )
        return [
            RoomEvent(
                event_id=r["event_id"], room_id=r["room_id"],
                sequence=r["sequence"],
                event_type=EventType(r["event_type"]),
                payload=json.loads(r["payload"]),
                actor_id=r["actor_id"], actor_type=r["actor_type"],
                timestamp=datetime.fromisoformat(r["timestamp"]),
                schema_version=r["schema_version"],
            )
            for r in rows
        ]

    async def get_latest_sequence(self, room_id: str) -> int:
        row = await self.db.fetch_one(
            "SELECT COALESCE(MAX(sequence), 0) AS seq FROM room_events WHERE room_id = ?",
            (room_id,),
        )
        return int(row["seq"]) if row else 0


class ArtifactRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, artifact: Artifact) -> Artifact:
        await self.db.execute(
            "INSERT INTO artifacts(artifact_id, room_id, name, artifact_type, description, "
            "current_version, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (artifact.artifact_id, artifact.room_id, artifact.name,
             artifact.artifact_type.value, artifact.description,
             artifact.current_version, artifact.created_by,
             serialize_datetime(artifact.created_at), serialize_datetime(artifact.updated_at)),
        )
        await self.db.commit()
        return artifact

    async def get(self, artifact_id: str) -> Artifact | None:
        row = await self.db.fetch_one(
            "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
        )
        return None if row is None else self._from_row(row)

    async def list_by_room(self, room_id: str) -> list[Artifact]:
        rows = await self.db.fetch_all(
            "SELECT * FROM artifacts WHERE room_id = ? ORDER BY created_at", (room_id,)
        )
        return [self._from_row(r) for r in rows]

    async def create_version(self, version: ArtifactVersion) -> ArtifactVersion:
        """Insert version and atomically update artifact's current_version in one transaction."""
        async with self.db.transaction():
            await self.db.execute(
                "INSERT INTO artifact_versions(version_id, artifact_id, version_number, content, "
                "content_hash, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (version.version_id, version.artifact_id, version.version_number,
                 version.content, version.content_hash, version.created_by,
                 serialize_datetime(version.created_at)),
            )
            await self.db.execute(
                "UPDATE artifacts SET current_version = ?, updated_at = ? WHERE artifact_id = ?",
                (version.version_number, utcnow().isoformat(), version.artifact_id),
            )
        return version

    async def list_versions(self, artifact_id: str) -> list[ArtifactVersion]:
        rows = await self.db.fetch_all(
            "SELECT * FROM artifact_versions WHERE artifact_id = ? ORDER BY version_number DESC",
            (artifact_id,),
        )
        return [
            ArtifactVersion(
                version_id=r["version_id"], artifact_id=r["artifact_id"],
                version_number=r["version_number"], content=r["content"],
                content_hash=r["content_hash"], created_by=r["created_by"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    def _from_row(self, row: dict[str, Any]) -> Artifact:
        return Artifact(
            artifact_id=row["artifact_id"], room_id=row["room_id"],
            name=row["name"], artifact_type=ArtifactType(row["artifact_type"]),
            description=row["description"], current_version=row["current_version"],
            created_by=row["created_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


class DecisionRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, decision: Decision) -> Decision:
        await self.db.execute(
            "INSERT INTO decisions(decision_id, room_id, title, content, reason, status, "
            "created_by, reviewed_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (decision.decision_id, decision.room_id, decision.title,
             decision.content, decision.reason, decision.status.value,
             decision.created_by, decision.reviewed_by,
             serialize_datetime(decision.created_at)),
        )
        await self.db.commit()
        return decision

    async def get(self, decision_id: str) -> Decision | None:
        row = await self.db.fetch_one(
            "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
        )
        return None if row is None else self._from_row(row)

    async def list_by_room(self, room_id: str) -> list[Decision]:
        rows = await self.db.fetch_all(
            "SELECT * FROM decisions WHERE room_id = ? ORDER BY created_at", (room_id,)
        )
        return [self._from_row(r) for r in rows]

    async def update_status(self, decision_id: str, status: DecisionStatus) -> None:
        await self.db.execute(
            "UPDATE decisions SET status = ? WHERE decision_id = ?",
            (status.value, decision_id),
        )
        await self.db.commit()

    def _from_row(self, row: dict[str, Any]) -> Decision:
        return Decision(
            decision_id=row["decision_id"], room_id=row["room_id"],
            title=row["title"], content=row["content"], reason=row["reason"],
            status=DecisionStatus(row["status"]),
            created_by=row["created_by"], reviewed_by=row["reviewed_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )


class MemoryRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, memory: Memory) -> Memory:
        await self.db.execute(
            "INSERT INTO memories(memory_id, room_id, workspace_id, org_id, scope, content, "
            "memory_type, is_authoritative, superseded_by, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (memory.memory_id, memory.room_id, memory.workspace_id, memory.org_id,
             memory.scope.value, memory.content, memory.memory_type,
             int(memory.is_authoritative), memory.superseded_by,
             memory.created_by, serialize_datetime(memory.created_at)),
        )
        await self.db.commit()
        return memory

    async def list_by_room(self, room_id: str) -> list[Memory]:
        rows = await self.db.fetch_all(
            "SELECT * FROM memories WHERE room_id = ? AND superseded_by IS NULL ORDER BY created_at",
            (room_id,),
        )
        return [self._from_row(r) for r in rows]

    async def list_by_workspace(self, workspace_id: str) -> list[Memory]:
        rows = await self.db.fetch_all(
            "SELECT * FROM memories WHERE workspace_id = ? AND superseded_by IS NULL ORDER BY created_at",
            (workspace_id,),
        )
        return [self._from_row(r) for r in rows]

    async def supersede(self, memory_id: str, superseded_by: str) -> None:
        await self.db.execute(
            "UPDATE memories SET superseded_by = ? WHERE memory_id = ?",
            (superseded_by, memory_id),
        )
        await self.db.commit()

    def _from_row(self, row: dict[str, Any]) -> Memory:
        return Memory(
            memory_id=row["memory_id"], room_id=row.get("room_id"),
            workspace_id=row.get("workspace_id"), org_id=row.get("org_id"),
            scope=MemoryScope(row["scope"]), content=row["content"],
            memory_type=row["memory_type"],
            is_authoritative=bool(row["is_authoritative"]),
            superseded_by=row.get("superseded_by"),
            created_by=row["created_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )


class ApprovalRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, approval: Approval) -> Approval:
        await self.db.execute(
            "INSERT INTO approvals(approval_id, room_id, execution_id, agent_id, "
            "action_description, status, reviewer_id, review_comment, requested_at, reviewed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (approval.approval_id, approval.room_id, approval.execution_id,
             approval.agent_id, approval.action_description, approval.status.value,
             approval.reviewer_id, approval.review_comment,
             serialize_datetime(approval.requested_at),
             serialize_datetime(approval.reviewed_at)),
        )
        await self.db.commit()
        return approval

    async def get(self, approval_id: str) -> Approval | None:
        row = await self.db.fetch_one(
            "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
        )
        return None if row is None else self._from_row(row)

    async def list_pending_by_room(self, room_id: str) -> list[Approval]:
        rows = await self.db.fetch_all(
            "SELECT * FROM approvals WHERE room_id = ? AND status = 'PENDING' ORDER BY requested_at",
            (room_id,),
        )
        return [self._from_row(r) for r in rows]

    async def update(self, approval: Approval) -> Approval:
        await self.db.execute(
            "UPDATE approvals SET status = ?, reviewer_id = ?, review_comment = ?, reviewed_at = ? "
            "WHERE approval_id = ?",
            (approval.status.value, approval.reviewer_id, approval.review_comment,
             serialize_datetime(approval.reviewed_at), approval.approval_id),
        )
        await self.db.commit()
        return approval

    def _from_row(self, row: dict[str, Any]) -> Approval:
        return Approval(
            approval_id=row["approval_id"], room_id=row["room_id"],
            execution_id=row["execution_id"], agent_id=row["agent_id"],
            action_description=row["action_description"],
            status=ApprovalStatus(row["status"]),
            reviewer_id=row.get("reviewer_id"),
            review_comment=row["review_comment"],
            requested_at=datetime.fromisoformat(row["requested_at"]),
            reviewed_at=datetime.fromisoformat(row["reviewed_at"]) if row.get("reviewed_at") else None,
        )


class NotificationRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, notification: Notification) -> Notification:
        await self.db.execute(
            "INSERT INTO notifications(notification_id, user_id, room_id, title, body, "
            "notification_type, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (notification.notification_id, notification.user_id, notification.room_id,
             notification.title, notification.body, notification.notification_type,
             notification.status.value, serialize_datetime(notification.created_at)),
        )
        await self.db.commit()
        return notification

    async def list_unread(self, user_id: str) -> list[Notification]:
        rows = await self.db.fetch_all(
            "SELECT * FROM notifications WHERE user_id = ? AND status = 'UNREAD' ORDER BY created_at DESC",
            (user_id,),
        )
        return [
            Notification(
                notification_id=r["notification_id"], user_id=r["user_id"],
                room_id=r.get("room_id"), title=r["title"], body=r["body"],
                notification_type=r["notification_type"],
                status=NotificationStatus(r["status"]),
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    async def mark_read(self, notification_id: str) -> None:
        await self.db.execute(
            "UPDATE notifications SET status = 'READ' WHERE notification_id = ?",
            (notification_id,),
        )
        await self.db.commit()


class PresenceRepo:
    def __init__(self, db: Database) -> None:
        pass  # Presence is ephemeral, stored in-memory via PresenceService

    async def set(self, presence: Presence) -> None:
        pass  # Handled by in-memory presence service

    async def get_room_presence(self, room_id: str) -> list[Presence]:
        return []


class ToolPermissionRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, perm: ToolPermission) -> ToolPermission:
        await self.db.execute(
            "INSERT INTO tool_permissions(permission_id, agent_id, room_id, tool_name, "
            "allowed, requires_approval, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (perm.permission_id, perm.agent_id, perm.room_id, perm.tool_name,
             int(perm.allowed), int(perm.requires_approval),
             serialize_datetime(perm.created_at)),
        )
        await self.db.commit()
        return perm

    async def get(self, agent_id: str, room_id: str, tool_name: str) -> ToolPermission | None:
        row = await self.db.fetch_one(
            "SELECT * FROM tool_permissions WHERE agent_id = ? AND room_id = ? AND tool_name = ?",
            (agent_id, room_id, tool_name),
        )
        return None if row is None else ToolPermission(
            permission_id=row["permission_id"], agent_id=row["agent_id"],
            room_id=row["room_id"], tool_name=row["tool_name"],
            allowed=bool(row["allowed"]),
            requires_approval=bool(row["requires_approval"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    async def list_by_agent_room(self, agent_id: str, room_id: str) -> list[ToolPermission]:
        rows = await self.db.fetch_all(
            "SELECT * FROM tool_permissions WHERE agent_id = ? AND room_id = ?",
            (agent_id, room_id),
        )
        return [
            ToolPermission(
                permission_id=r["permission_id"], agent_id=r["agent_id"],
                room_id=r["room_id"], tool_name=r["tool_name"],
                allowed=bool(r["allowed"]),
                requires_approval=bool(r["requires_approval"]),
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]
