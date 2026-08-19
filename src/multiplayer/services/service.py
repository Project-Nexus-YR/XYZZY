"""Core service layer: orchestrates domain operations across repos, events, and NEXUS."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any

from ..db.connection import Database
from ..db.repositories import Repos
from ..domain.events import EventType, RoomEvent
from ..domain.models import (
    AgentInstance,
    AgentRoomMembership,
    AgentStatus,
    AgentTemplate,
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactType,
    ArtifactVersion,
    Decision,
    DomainError,
    Execution,
    ExecutionStatus,
    Memory,
    MemoryScope,
    Message,
    MessageRole,
    Notification,
    Organization,
    OrgMember,
    Room,
    RoomMember,
    Session,
    SessionStatus,
    Task,
    TaskPriority,
    TaskStatus,
    Workspace,
    WorkspaceMember,
    new_id,
    utcnow,
)
from ..nexus_bridge.agent_bridge import NexusAgentBridge
from ..realtime.hub import RealtimeHub
from ..services.presence import PresenceService

log = logging.getLogger(__name__)

# ── State machine transition tables ──────────────────────────────────────────

VALID_TASK_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.CREATED: {TaskStatus.ASSIGNED, TaskStatus.CANCELLED},
    TaskStatus.ASSIGNED: {TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.IN_PROGRESS: {TaskStatus.BLOCKED, TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.BLOCKED: {TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: {TaskStatus.ASSIGNED, TaskStatus.CREATED},
    TaskStatus.CANCELLED: {TaskStatus.CREATED},
}

VALID_AGENT_TRANSITIONS: dict[AgentStatus, set[AgentStatus]] = {
    AgentStatus.IDLE: {AgentStatus.THINKING, AgentStatus.WORKING, AgentStatus.OFFLINE},
    AgentStatus.THINKING: {AgentStatus.WORKING, AgentStatus.WAITING_INPUT, AgentStatus.FAILED},
    AgentStatus.WORKING: {
        AgentStatus.THINKING, AgentStatus.REVIEWING, AgentStatus.DELEGATING,
        AgentStatus.WAITING_INPUT, AgentStatus.WAITING_APPROVAL,
        AgentStatus.BLOCKED, AgentStatus.COMPLETED, AgentStatus.FAILED, AgentStatus.PAUSED,
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
    ExecutionStatus.PENDING: {ExecutionStatus.RUNNING, ExecutionStatus.CANCELLED},
    ExecutionStatus.RUNNING: {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED, ExecutionStatus.PAUSED},
    ExecutionStatus.PAUSED: {ExecutionStatus.RUNNING, ExecutionStatus.CANCELLED},
    ExecutionStatus.COMPLETED: set(),
    ExecutionStatus.FAILED: set(),
    ExecutionStatus.CANCELLED: set(),
}


def _validate_transition(
    current: Any, target: Any,
    valid: dict[Any, set[Any]], entity_name: str,
) -> None:
    """Raise DomainError if the transition is not valid."""
    allowed = valid.get(current, set())
    if target not in allowed:
        raise DomainError(
            f"invalid {entity_name} transition: {current.value} -> {target.value}"
        )


class MultiplayerService:
    def __init__(self, db: Database, hub: RealtimeHub) -> None:
        self.db = db
        self.repos = Repos(db)
        self.hub = hub
        self.presence = PresenceService()
        self.nexus = NexusAgentBridge(db_path=":memory:")
        self._running_executions: dict[str, asyncio.Task[None]] = {}

    async def initialize(self) -> None:
        migrations_dir = Path(__file__).parent.parent / "migrations"
        migration_file = migrations_dir / "001_initial.sql"
        if migration_file.exists():
            sql = migration_file.read_text()
            await self.db.execute_script(sql)
        await self._seed_default_templates()

    async def _seed_default_templates(self) -> None:
        templates = await self.repos.agents.list_templates()
        if templates:
            return
        defaults = [
            AgentTemplate(template_id=new_id("tmpl"), name="Architect",
                description="Plans system architecture", role="Architect",
                system_prompt="You are an architect.",
                capabilities=frozenset({"planning", "analysis", "decision_making"})),
            AgentTemplate(template_id=new_id("tmpl"), name="Researcher",
                description="Investigates questions", role="Researcher",
                system_prompt="You are a researcher.",
                capabilities=frozenset({"research", "analysis", "retrieval"})),
            AgentTemplate(template_id=new_id("tmpl"), name="Coder",
                description="Writes and reviews code", role="Coder",
                system_prompt="You are a software engineer.",
                capabilities=frozenset({"coding", "testing", "review"})),
            AgentTemplate(template_id=new_id("tmpl"), name="Security Reviewer",
                description="Reviews for security issues", role="Security Reviewer",
                system_prompt="You are a security expert.",
                capabilities=frozenset({"security", "review", "analysis"})),
            AgentTemplate(template_id=new_id("tmpl"), name="Synthesizer",
                description="Combines multi-agent outputs", role="Synthesizer",
                system_prompt="You are a synthesizer.",
                capabilities=frozenset({"synthesis", "writing", "analysis"})),
        ]
        for t in defaults:
            await self.repos.agents.create_template(t)

    # ── Event helpers ────────────────────────────────────────────────────────

    async def _append_room_event(
        self, room_id: str, event_type: EventType,
        payload: dict[str, Any], actor_id: str, actor_type: str,
    ) -> RoomEvent:
        """Append a durable room event with atomic sequence generation."""
        event = RoomEvent(
            room_id=room_id, sequence=0, event_type=event_type,
            payload=payload, actor_id=actor_id, actor_type=actor_type,
        )
        event = await self.repos.events.append_with_next_sequence(event)
        # Realtime broadcast is best-effort; failures must not roll back the event
        try:
            await self.hub.broadcast_room_event(event)
        except Exception:
            log.exception("Failed to broadcast event %s for room %s", event_type.value, room_id)
        return event

    # ── Input validation ─────────────────────────────────────────────────────

    @staticmethod
    def _validate_non_empty(value: str, field_name: str) -> str:
        value = value.strip()
        if not value:
            raise DomainError(f"{field_name} must not be empty")
        if len(value) > 10000:
            raise DomainError(f"{field_name} must not exceed 10000 characters")
        return value

    @staticmethod
    def _validate_id(value: str, field_name: str) -> str:
        value = value.strip()
        if not value:
            raise DomainError(f"{field_name} must not be empty")
        if len(value) > 256:
            raise DomainError(f"{field_name} is too long")
        return value

    @staticmethod
    def _validate_limit(limit: int) -> int:
        return max(1, min(limit, 500))

    # ── Authorization helpers ────────────────────────────────────────────────

    async def _check_room_membership(self, room_id: str, user_id: str) -> None:
        """Verify user is a member of the room. Raises DomainError if not."""
        if not await self.repos.room_members.is_member(room_id, user_id):
            raise DomainError(f"user {user_id} is not a member of room {room_id}")

    async def _check_workspace_membership(self, workspace_id: str, user_id: str) -> None:
        """Basic authorization check - verify user can access the workspace's room."""
        ws = await self.repos.workspaces.get(workspace_id)
        if not ws:
            raise DomainError(f"workspace not found: {workspace_id}")

    # ── Organization ─────────────────────────────────────────────────────────

    async def create_organization(self, name: str, slug: str, creator_id: str) -> Organization:
        name = self._validate_non_empty(name, "organization name")
        slug = self._validate_non_empty(slug, "organization slug")
        org = Organization(org_id=new_id("org"), name=name, slug=slug)
        await self.repos.orgs.create(org)
        await self.repos.orgs.add_member(OrgMember(org_id=org.org_id, user_id=creator_id, role="admin"))
        return org

    # ── Workspace ────────────────────────────────────────────────────────────

    async def create_workspace(self, org_id: str, name: str, slug: str, creator_id: str) -> Workspace:
        name = self._validate_non_empty(name, "workspace name")
        slug = self._validate_non_empty(slug, "workspace slug")
        ws = Workspace(workspace_id=new_id("ws"), org_id=org_id, name=name, slug=slug)
        await self.repos.workspaces.create(ws)
        await self.repos.workspaces.add_member(
            WorkspaceMember(workspace_id=ws.workspace_id, user_id=creator_id, role="admin"))
        return ws

    async def list_workspaces(self, org_id: str) -> list[Workspace]:
        return await self.repos.workspaces.list_by_org(org_id)

    # ── Room ─────────────────────────────────────────────────────────────────

    async def create_room(self, workspace_id: str, name: str, creator_id: str, description: str = "") -> Room:
        name = self._validate_non_empty(name, "room name")
        room = Room(room_id=new_id("room"), workspace_id=workspace_id, name=name,
                    description=description, created_by=creator_id)
        await self.repos.rooms.create(room)
        await self.repos.room_members.add(RoomMember(room_id=room.room_id, user_id=creator_id, role="admin"))
        await self._append_room_event(room.room_id, EventType.ROOM_CREATED,
            {"name": name, "description": description}, creator_id, "user")
        return room

    async def get_room(self, room_id: str) -> Room:
        room = await self.repos.rooms.get(room_id)
        if not room:
            raise DomainError(f"room not found: {room_id}")
        return room

    async def list_rooms(self, workspace_id: str) -> list[Room]:
        return await self.repos.rooms.list_by_workspace(workspace_id)

    async def join_room(self, room_id: str, user_id: str) -> None:
        await self.get_room(room_id)
        await self.repos.room_members.add(RoomMember(room_id=room_id, user_id=user_id))
        await self.presence.user_joined(user_id, room_id)
        await self._append_room_event(room_id, EventType.USER_JOINED_ROOM, {"user_id": user_id}, user_id, "user")

    async def leave_room(self, room_id: str, user_id: str) -> None:
        await self.presence.user_left(user_id, room_id)
        await self._append_room_event(room_id, EventType.USER_LEFT_ROOM, {"user_id": user_id}, user_id, "user")

    async def get_room_members(self, room_id: str) -> list[RoomMember]:
        return await self.repos.room_members.list(room_id)

    # ── Agents ───────────────────────────────────────────────────────────────

    async def list_agent_templates(self) -> list[AgentTemplate]:
        return await self.repos.agents.list_templates()

    async def spawn_agent(self, room_id: str, template_id: str, name: str | None = None,
                          system_prompt: str | None = None, model_provider: str = "",
                          model_name: str = "") -> AgentInstance:
        template = await self.repos.agents.get_template(template_id)
        if not template:
            raise DomainError(f"agent template not found: {template_id}")
        agent = AgentInstance(
            agent_id=new_id("agent"), template_id=template_id, room_id=room_id,
            name=name or template.name, role=template.role,
            system_prompt=system_prompt or template.system_prompt,
            capabilities=template.capabilities, model_provider=model_provider, model_name=model_name)
        await self.repos.agents.create_instance(agent)
        await self.repos.agents.add_room_membership(AgentRoomMembership(agent_id=agent.agent_id, room_id=room_id))
        await self._append_room_event(room_id, EventType.AGENT_JOINED_ROOM,
            {"agent_id": agent.agent_id, "name": agent.name, "role": agent.role}, agent.agent_id, "agent")
        return agent

    async def get_agent(self, agent_id: str) -> AgentInstance:
        agent = await self.repos.agents.get_instance(agent_id)
        if not agent:
            raise DomainError(f"agent not found: {agent_id}")
        return agent

    async def list_room_agents(self, room_id: str) -> list[AgentInstance]:
        return await self.repos.agents.list_instances_by_room(room_id)

    async def update_agent_status(self, agent_id: str, status: AgentStatus) -> None:
        agent = await self.get_agent(agent_id)
        _validate_transition(agent.status, status, VALID_AGENT_TRANSITIONS, "agent")
        await self.repos.agents.update_status(agent_id, status)
        await self._append_room_event(agent.room_id, EventType.AGENT_STATUS_CHANGED,
            {"agent_id": agent_id, "status": status.value}, agent_id, "agent")

    # ── Session & Execution ──────────────────────────────────────────────────

    async def start_agent_session(self, room_id: str, agent_id: str, task_id: str | None = None) -> Session:
        agent = await self.get_agent(agent_id)
        if agent.room_id != room_id:
            raise DomainError("agent is not in this room")
        session = Session(session_id=new_id("sess"), room_id=room_id, agent_id=agent_id, task_id=task_id)
        await self.repos.sessions.create(session)
        await self._append_room_event(room_id, EventType.SESSION_STARTED,
            {"session_id": session.session_id, "agent_id": agent_id}, agent_id, "agent")
        return session

    async def start_execution(self, session_id: str, input_data: dict[str, Any] | None = None) -> Execution:
        session = await self.repos.sessions.get(session_id)
        if not session:
            raise DomainError(f"session not found: {session_id}")
        _validate_transition(session.status, SessionStatus.ACTIVE, VALID_SESSION_TRANSITIONS, "session")
        await self.repos.sessions.update_status(session_id, SessionStatus.ACTIVE)
        execution = Execution(execution_id=new_id("exec"), session_id=session_id,
                              agent_id=session.agent_id, input_data=input_data or {})
        await self.repos.executions.create(execution)
        await self._append_room_event(session.room_id, EventType.EXECUTION_STARTED,
            {"execution_id": execution.execution_id, "session_id": session_id,
             "agent_id": session.agent_id}, session.agent_id, "agent")
        await self._set_agent_status_safe(session.agent_id, AgentStatus.WORKING)
        return execution

    async def execute_agent_step(self, execution_id: str, prompt: str) -> dict[str, Any]:
        execution = await self.repos.executions.get(execution_id)
        if not execution:
            raise DomainError(f"execution not found: {execution_id}")
        session = await self.repos.sessions.get(execution.session_id)
        if not session:
            raise DomainError("session not found")
        agent = await self.get_agent(execution.agent_id)

        if not execution.run_id:
            nexus_agent, budget = await self.nexus.create_execution(agent, session, prompt, execution)
            execution = Execution(execution_id=execution.execution_id, session_id=execution.session_id,
                                  agent_id=execution.agent_id, run_id=f"run_{execution.execution_id}",
                                  status=ExecutionStatus.RUNNING, input_data=execution.input_data)
            await self.repos.executions.update_status(execution.execution_id, ExecutionStatus.RUNNING)

        result = await self.nexus.execute_step(execution_id, prompt)
        if result.get("status") == "error":
            await self.repos.executions.update_status(execution.execution_id, ExecutionStatus.FAILED, error=result.get("error", ""))
            await self._set_agent_status_safe(execution.agent_id, AgentStatus.FAILED)
            return result
        if result.get("action") == "finish":
            await self.repos.executions.update_status(
                execution.execution_id, ExecutionStatus.COMPLETED, output_data=result.get("result") or {})
            await self.repos.sessions.update_status(execution.session_id, SessionStatus.COMPLETED)
            await self._set_agent_status_safe(execution.agent_id, AgentStatus.IDLE)
        return result

    async def _set_agent_status_safe(self, agent_id: str, status: AgentStatus) -> None:
        """Set agent status, skipping validation if transition is invalid (best-effort)."""
        try:
            await self.update_agent_status(agent_id, status)
        except DomainError:
            log.debug("Skipping invalid agent transition for %s: -> %s", agent_id, status.value)

    # ── Tasks ────────────────────────────────────────────────────────────────

    async def create_task(self, room_id: str, title: str, description: str = "",
                          priority: TaskPriority = TaskPriority.NORMAL,
                          created_by: str = "", parent_task_id: str | None = None) -> Task:
        title = self._validate_non_empty(title, "task title")
        task = Task(task_id=new_id("task"), room_id=room_id, title=title,
                    description=description, priority=priority, created_by=created_by,
                    parent_task_id=parent_task_id)
        await self.repos.tasks.create(task)
        await self._append_room_event(room_id, EventType.TASK_CREATED,
            {"task_id": task.task_id, "title": title}, created_by, "user")
        return task

    async def assign_task(self, task_id: str, agent_id: str) -> Task:
        task = await self.repos.tasks.get(task_id)
        if not task:
            raise DomainError(f"task not found: {task_id}")
        _validate_transition(task.status, TaskStatus.ASSIGNED, VALID_TASK_TRANSITIONS, "task")
        task = Task(task_id=task.task_id, room_id=task.room_id, title=task.title,
                    description=task.description, status=TaskStatus.ASSIGNED,
                    priority=task.priority, assigned_agent_id=agent_id,
                    created_by=task.created_by, parent_task_id=task.parent_task_id,
                    delegation_id=task.delegation_id)
        await self.repos.tasks.update(task)
        await self._append_room_event(task.room_id, EventType.TASK_ASSIGNED,
            {"task_id": task_id, "agent_id": agent_id}, agent_id, "agent")
        return task

    async def delegate_task(self, task_id: str, from_agent_id: str, to_agent_id: str, description: str = "") -> Task:
        task = await self.repos.tasks.get(task_id)
        if not task:
            raise DomainError(f"task not found: {task_id}")
        delegation_id = new_id("deleg")
        child = Task(task_id=new_id("task"), room_id=task.room_id, title=f"Delegated: {task.title}",
                     description=description or task.description, status=TaskStatus.ASSIGNED,
                     priority=task.priority, assigned_agent_id=to_agent_id,
                     created_by=from_agent_id, parent_task_id=task_id, delegation_id=delegation_id)
        await self.repos.tasks.create(child)
        await self._append_room_event(task.room_id, EventType.TASK_DELEGATED,
            {"parent_task_id": task_id, "child_task_id": child.task_id,
             "from_agent": from_agent_id, "to_agent": to_agent_id}, from_agent_id, "agent")
        return child

    async def complete_task(self, task_id: str) -> Task:
        task = await self.repos.tasks.get(task_id)
        if not task:
            raise DomainError(f"task not found: {task_id}")
        _validate_transition(task.status, TaskStatus.COMPLETED, VALID_TASK_TRANSITIONS, "task")
        task = Task(task_id=task.task_id, room_id=task.room_id, title=task.title,
                    description=task.description, status=TaskStatus.COMPLETED,
                    priority=task.priority, assigned_agent_id=task.assigned_agent_id,
                    created_by=task.created_by, parent_task_id=task.parent_task_id,
                    delegation_id=task.delegation_id)
        await self.repos.tasks.update(task)
        await self._append_room_event(task.room_id, EventType.TASK_COMPLETED,
            {"task_id": task_id}, task.assigned_agent_id or "system", "agent")
        return task

    async def cancel_task(self, task_id: str) -> Task:
        task = await self.repos.tasks.get(task_id)
        if not task:
            raise DomainError(f"task not found: {task_id}")
        _validate_transition(task.status, TaskStatus.CANCELLED, VALID_TASK_TRANSITIONS, "task")
        task = Task(task_id=task.task_id, room_id=task.room_id, title=task.title,
                    description=task.description, status=TaskStatus.CANCELLED,
                    priority=task.priority, assigned_agent_id=task.assigned_agent_id,
                    created_by=task.created_by, parent_task_id=task.parent_task_id,
                    delegation_id=task.delegation_id)
        await self.repos.tasks.update(task)
        await self._append_room_event(task.room_id, EventType.TASK_CANCELLED,
            {"task_id": task_id}, task.created_by, "user")
        return task

    async def list_room_tasks(self, room_id: str) -> list[Task]:
        return await self.repos.tasks.list_by_room(room_id)

    # ── Messages ─────────────────────────────────────────────────────────────

    async def send_message(self, room_id: str, role: MessageRole, sender_id: str, content: str,
                           metadata: dict[str, Any] | None = None) -> Message:
        content = self._validate_non_empty(content, "message content")
        msg = Message(message_id=new_id("msg"), room_id=room_id, role=role,
                      sender_id=sender_id, content=content, metadata=metadata or {})
        await self.repos.messages.create(msg)
        await self._append_room_event(room_id, EventType.MESSAGE_CREATED,
            {"message_id": msg.message_id, "role": role.value, "sender_id": sender_id,
             "content": content[:500]}, sender_id, role.value.lower())
        return msg

    async def list_room_messages(self, room_id: str, limit: int = 100) -> list[Message]:
        return await self.repos.messages.list_by_room(room_id, limit=self._validate_limit(limit))

    # ── Artifacts ────────────────────────────────────────────────────────────

    async def create_artifact(self, room_id: str, name: str, artifact_type: ArtifactType,
                              description: str = "", created_by: str = "",
                              content: str = "") -> Artifact:
        name = self._validate_non_empty(name, "artifact name")
        artifact = Artifact(artifact_id=new_id("art"), room_id=room_id, name=name,
                            artifact_type=artifact_type, description=description,
                            current_version=1 if content else 0, created_by=created_by)
        await self.repos.artifacts.create(artifact)
        if content:
            version = ArtifactVersion(version_id=new_id("ver"), artifact_id=artifact.artifact_id,
                                      version_number=1, content=content,
                                      content_hash=hashlib.sha256(content.encode()).hexdigest(),
                                      created_by=created_by)
            await self.repos.artifacts.create_version(version)
        await self._append_room_event(room_id, EventType.ARTIFACT_CREATED,
            {"artifact_id": artifact.artifact_id, "name": name, "type": artifact_type.value},
            created_by, "user")
        return artifact

    async def update_artifact(self, artifact_id: str, content: str, updated_by: str = "") -> ArtifactVersion:
        artifact = await self.repos.artifacts.get(artifact_id)
        if not artifact:
            raise DomainError(f"artifact not found: {artifact_id}")
        new_ver = artifact.current_version + 1
        version = ArtifactVersion(version_id=new_id("ver"), artifact_id=artifact_id,
                                  version_number=new_ver, content=content,
                                  content_hash=hashlib.sha256(content.encode()).hexdigest(),
                                  created_by=updated_by)
        await self.repos.artifacts.create_version(version)
        await self._append_room_event(artifact.room_id, EventType.ARTIFACT_VERSION_CREATED,
            {"artifact_id": artifact_id, "version": new_ver}, updated_by, "user")
        return version

    async def list_room_artifacts(self, room_id: str) -> list[Artifact]:
        return await self.repos.artifacts.list_by_room(room_id)

    # ── Decisions ────────────────────────────────────────────────────────────

    async def create_decision(self, room_id: str, title: str, content: str,
                              reason: str = "", created_by: str = "") -> Decision:
        title = self._validate_non_empty(title, "decision title")
        decision = Decision(decision_id=new_id("dec"), room_id=room_id, title=title,
                            content=content, reason=reason, created_by=created_by)
        await self.repos.decisions.create(decision)
        await self._append_room_event(room_id, EventType.DECISION_CREATED,
            {"decision_id": decision.decision_id, "title": title}, created_by, "user")
        return decision

    async def list_room_decisions(self, room_id: str) -> list[Decision]:
        return await self.repos.decisions.list_by_room(room_id)

    # ── Memory ───────────────────────────────────────────────────────────────

    async def create_memory(self, room_id: str | None, workspace_id: str | None,
                            org_id: str | None, scope: MemoryScope, content: str,
                            memory_type: str = "fact", created_by: str = "") -> Memory:
        content = self._validate_non_empty(content, "memory content")
        memory = Memory(memory_id=new_id("mem"), room_id=room_id, workspace_id=workspace_id,
                        org_id=org_id, scope=scope, content=content,
                        memory_type=memory_type, created_by=created_by)
        await self.repos.memories.create(memory)
        if room_id:
            await self._append_room_event(room_id, EventType.MEMORY_CREATED,
                {"memory_id": memory.memory_id, "type": memory_type}, created_by, "user")
        return memory

    async def list_room_memories(self, room_id: str) -> list[Memory]:
        return await self.repos.memories.list_by_room(room_id)

    # ── Approvals ────────────────────────────────────────────────────────────

    async def request_approval(self, room_id: str, execution_id: str, agent_id: str,
                               action_description: str) -> Approval:
        approval = Approval(approval_id=new_id("appr"), room_id=room_id,
                            execution_id=execution_id, agent_id=agent_id,
                            action_description=action_description)
        await self.repos.approvals.create(approval)
        await self._set_agent_status_safe(agent_id, AgentStatus.WAITING_APPROVAL)
        await self._append_room_event(room_id, EventType.APPROVAL_REQUESTED,
            {"approval_id": approval.approval_id, "agent_id": agent_id,
             "action": action_description}, agent_id, "agent")
        return approval

    async def approve_action(self, approval_id: str, reviewer_id: str, comment: str = "") -> Approval:
        approval = await self.repos.approvals.get(approval_id)
        if not approval:
            raise DomainError(f"approval not found: {approval_id}")
        if approval.status != ApprovalStatus.PENDING:
            raise DomainError(f"approval {approval_id} is not pending (current: {approval.status.value})")
        approval = Approval(approval_id=approval.approval_id, room_id=approval.room_id,
                            execution_id=approval.execution_id, agent_id=approval.agent_id,
                            action_description=approval.action_description,
                            status=ApprovalStatus.APPROVED, reviewer_id=reviewer_id,
                            review_comment=comment, requested_at=approval.requested_at,
                            reviewed_at=utcnow())
        await self.repos.approvals.update(approval)
        await self._set_agent_status_safe(approval.agent_id, AgentStatus.WORKING)
        await self._append_room_event(approval.room_id, EventType.APPROVAL_GRANTED,
            {"approval_id": approval_id, "reviewer_id": reviewer_id}, reviewer_id, "user")
        return approval

    async def reject_action(self, approval_id: str, reviewer_id: str, comment: str = "") -> Approval:
        approval = await self.repos.approvals.get(approval_id)
        if not approval:
            raise DomainError(f"approval not found: {approval_id}")
        if approval.status != ApprovalStatus.PENDING:
            raise DomainError(f"approval {approval_id} is not pending (current: {approval.status.value})")
        approval = Approval(approval_id=approval.approval_id, room_id=approval.room_id,
                            execution_id=approval.execution_id, agent_id=approval.agent_id,
                            action_description=approval.action_description,
                            status=ApprovalStatus.REJECTED, reviewer_id=reviewer_id,
                            review_comment=comment, requested_at=approval.requested_at,
                            reviewed_at=utcnow())
        await self.repos.approvals.update(approval)
        await self._append_room_event(approval.room_id, EventType.APPROVAL_REJECTED,
            {"approval_id": approval_id, "reviewer_id": reviewer_id}, reviewer_id, "user")
        return approval

    async def list_pending_approvals(self, room_id: str) -> list[Approval]:
        return await self.repos.approvals.list_pending_by_room(room_id)

    # ── Human Intervention ───────────────────────────────────────────────────

    async def interrupt_agent(self, agent_id: str, user_id: str, reason: str = "") -> None:
        agent = await self.get_agent(agent_id)
        execution_id = await self.nexus.get_execution_for_agent(agent_id)
        if execution_id:
            await self.nexus.pause_execution(execution_id)
        await self._set_agent_status_safe(agent_id, AgentStatus.PAUSED)
        await self._append_room_event(agent.room_id, EventType.HUMAN_INTERRUPTED_AGENT,
            {"agent_id": agent_id, "reason": reason}, user_id, "user")

    async def redirect_agent(self, agent_id: str, user_id: str, instruction: str) -> None:
        agent = await self.get_agent(agent_id)
        execution_id = await self.nexus.get_execution_for_agent(agent_id)
        if execution_id:
            run_id = await self.nexus.get_run_id_for_execution(execution_id)
            if run_id:
                await self.nexus.add_intervention(run_id, instruction)
        await self._append_room_event(agent.room_id, EventType.HUMAN_REDIRECTED_AGENT,
            {"agent_id": agent_id, "instruction": instruction}, user_id, "user")

    # ── Notifications ────────────────────────────────────────────────────────

    async def create_notification(self, user_id: str, title: str, body: str,
                                  room_id: str | None = None,
                                  notification_type: str = "info") -> Notification:
        notif = Notification(notification_id=new_id("notif"), user_id=user_id, room_id=room_id,
                             title=title, body=body, notification_type=notification_type)
        await self.repos.notifications.create(notif)
        return notif

    async def list_notifications(self, user_id: str) -> list[Notification]:
        return await self.repos.notifications.list_unread(user_id)

    # ── Event History ────────────────────────────────────────────────────────

    async def get_room_events(self, room_id: str, after_sequence: int = 0) -> list[RoomEvent]:
        return await self.repos.events.list_since(room_id, max(0, after_sequence))

    # ── Room State (for reconnect) ───────────────────────────────────────────

    async def get_room_state(self, room_id: str, last_sequence: int = 0) -> dict[str, Any]:
        room = await self.get_room(room_id)
        events = await self.get_room_events(room_id, last_sequence)
        members = await self.get_room_members(room_id)
        agents = await self.list_room_agents(room_id)
        tasks = await self.list_room_tasks(room_id)
        messages = await self.list_room_messages(room_id, limit=50)
        artifacts = await self.list_room_artifacts(room_id)
        decisions = await self.list_room_decisions(room_id)
        memories = await self.list_room_memories(room_id)
        pending_approvals = await self.list_pending_approvals(room_id)
        presence = await self.presence.get_room_presence(room_id)
        return {
            "room": {"room_id": room.room_id, "name": room.name, "description": room.description,
                     "status": room.status.value, "workspace_id": room.workspace_id},
            "events_since": [{"event_id": e.event_id, "sequence": e.sequence,
                              "event_type": e.event_type.value, "payload": e.payload,
                              "actor_id": e.actor_id, "actor_type": e.actor_type,
                              "timestamp": e.timestamp.isoformat()} for e in events],
            "members": [{"user_id": m.user_id, "role": m.role} for m in members],
            "agents": [{"agent_id": a.agent_id, "name": a.name, "role": a.role,
                        "status": a.status.value} for a in agents],
            "tasks": [{"task_id": t.task_id, "title": t.title, "status": t.status.value,
                       "priority": t.priority.value, "assigned_agent_id": t.assigned_agent_id}
                      for t in tasks],
            "messages": [{"message_id": m.message_id, "role": m.role.value,
                          "sender_id": m.sender_id, "content": m.content,
                          "created_at": m.created_at.isoformat()} for m in messages],
            "artifacts": [{"artifact_id": a.artifact_id, "name": a.name,
                           "type": a.artifact_type.value, "version": a.current_version}
                          for a in artifacts],
            "decisions": [{"decision_id": d.decision_id, "title": d.title, "status": d.status.value}
                          for d in decisions],
            "memories": [{"memory_id": m.memory_id, "content": m.content, "type": m.memory_type}
                         for m in memories],
            "pending_approvals": [{"approval_id": a.approval_id, "action": a.action_description,
                                   "agent_id": a.agent_id} for a in pending_approvals],
            "presence": [{"user_id": p.user_id, "status": p.status.value} for p in presence],
        }


# Needed for hashlib import in create_artifact
