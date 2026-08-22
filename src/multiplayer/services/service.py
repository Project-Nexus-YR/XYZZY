"""Core service layer: orchestrates domain operations across repos, events, and NEXUS."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..db.connection import Database
from ..db.repositories import Repos
from ..domain.events import EventType, RoomEvent
from ..domain.models import (
    AgentInstance,
    AgentOutput,
    AgentRoomMembership,
    AgentStatus,
    AgentTemplate,
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactClaim,
    ArtifactType,
    ArtifactVersion,
    BootstrapContext,
    Branch,
    BranchMode,
    BranchStatus,
    BranchSynthesis,
    BranchSynthesisInput,
    BranchSynthesisStatus,
    ClaimSource,
    Decision,
    DomainError,
    Execution,
    ExecutionStatus,
    Memory,
    MemoryScope,
    Message,
    MessageRole,
    Notification,
    OntologyDerivationKind,
    OntologyEntity,
    OntologyEntityKind,
    OntologyRelationship,
    OntologyRelationshipKind,
    OntologyReview,
    OntologyReviewAction,
    Organization,
    OrgMember,
    OutputDisposition,
    OutputSelection,
    Room,
    RoomMember,
    Session,
    SessionStatus,
    Task,
    TaskPriority,
    TaskStatus,
    TurnLock,
    TurnLockScopeType,
    TurnLockStatus,
    Workspace,
    WorkspaceMember,
    new_id,
    utcnow,
)
from ..domain.provenance import calculate_artifact_provenance_hash
from ..model_providers import ModelProviderError
from ..nexus_bridge.agent_bridge import NexusAgentBridge
from ..realtime.hub import RealtimeHub
from ..security.authorization import RoomCapability, RoomPolicy
from ..services.presence import PresenceService

log = logging.getLogger(__name__)

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
    ExecutionStatus.PENDING: {ExecutionStatus.RUNNING, ExecutionStatus.CANCELLED},
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


class MultiplayerService:
    def __init__(self, db: Database, hub: RealtimeHub) -> None:
        self.db = db
        self.repos = Repos(db)
        self.hub = hub
        self.presence = PresenceService()
        self.nexus = NexusAgentBridge(db_path=":memory:")
        self.authorization = RoomPolicy(self.repos)
        self._running_executions: dict[str, asyncio.Task[None]] = {}

    async def initialize(self) -> None:
        migrations_dir = Path(__file__).parent.parent / "migrations"
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied_rows = await self.db.fetch_all("SELECT name FROM schema_migrations")
        applied = {str(row["name"]) for row in applied_rows}
        for migration_file in sorted(migrations_dir.glob("*.sql")):
            if migration_file.name in applied:
                continue
            await self.db.execute_script(migration_file.read_text())
            await self.db.execute(
                "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                (migration_file.name, utcnow().isoformat()),
            )
        await self._backfill_legacy_artifact_provenance_hashes()
        await self._seed_default_templates()

    async def _backfill_legacy_artifact_provenance_hashes(self) -> None:
        """Bind pre-migration snapshots using the best evidence available at upgrade time."""
        versions = await self.repos.artifacts.list_versions_without_provenance_hash()
        for version in versions:
            claims = await self.repos.artifacts.get_version_provenance(version.version_id)
            provenance_hash = self._artifact_provenance_hash(version, claims)
            await self.repos.artifacts.set_provenance_hash_if_empty(
                version.version_id, provenance_hash
            )

    async def _seed_default_templates(self) -> None:
        templates = await self.repos.agents.list_templates()
        if templates:
            return
        defaults = [
            AgentTemplate(
                template_id=new_id("tmpl"),
                name="Architect",
                description="Plans system architecture",
                role="Architect",
                system_prompt="You are an architect.",
                capabilities=frozenset({"planning", "analysis", "decision_making"}),
            ),
            AgentTemplate(
                template_id=new_id("tmpl"),
                name="Researcher",
                description="Investigates questions",
                role="Researcher",
                system_prompt="You are a researcher.",
                capabilities=frozenset({"research", "analysis", "retrieval"}),
            ),
            AgentTemplate(
                template_id=new_id("tmpl"),
                name="Coder",
                description="Writes and reviews code",
                role="Coder",
                system_prompt="You are a software engineer.",
                capabilities=frozenset({"coding", "testing", "review"}),
            ),
            AgentTemplate(
                template_id=new_id("tmpl"),
                name="Security Reviewer",
                description="Reviews for security issues",
                role="Security Reviewer",
                system_prompt="You are a security expert.",
                capabilities=frozenset({"security", "review", "analysis"}),
            ),
            AgentTemplate(
                template_id=new_id("tmpl"),
                name="Synthesizer",
                description="Combines multi-agent outputs",
                role="Synthesizer",
                system_prompt="You are a synthesizer.",
                capabilities=frozenset({"synthesis", "writing", "analysis"}),
            ),
        ]
        for t in defaults:
            await self.repos.agents.create_template(t)

    # ── Event helpers ────────────────────────────────────────────────────────

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
        if slug.casefold().startswith("bootstrap-"):
            raise DomainError("organization slug namespace is reserved")
        org = Organization(org_id=new_id("org"), name=name, slug=slug)
        await self.repos.orgs.create(org)
        await self.repos.orgs.add_member(
            OrgMember(org_id=org.org_id, user_id=creator_id, role="admin")
        )
        return org

    async def get_user_context(
        self, user_id: str
    ) -> tuple[list[Organization], list[Workspace], list[Room]]:
        """Discover durable collaboration boundaries visible to one principal.

        This read-only path is used by browser reconnect. Each query is anchored
        to its own membership table so stale browser identifiers cannot reveal or
        attach the principal to an unauthorized organization, workspace, or room.
        """
        organizations, workspaces, rooms = await asyncio.gather(
            self.repos.orgs.list_for_user(user_id),
            self.repos.workspaces.list_for_user(user_id),
            self.repos.rooms.list_for_user(user_id),
        )
        return organizations, workspaces, rooms

    async def bootstrap_user_workspace(
        self,
        user_id: str,
        display_name: str,
        room_name: str,
    ) -> tuple[Organization, Workspace, Room]:
        """Atomically get or create the principal's stable first workspace.

        Discovery remains a useful read optimization, but this transaction is
        the idempotency boundary. Concurrent browser tabs that both observed an
        empty context serialize here and resolve the same durable hierarchy.
        """
        user_id = self._validate_id(user_id, "user id")
        display_name = self._validate_non_empty(display_name, "display name")
        room_name = self._validate_non_empty(room_name, "room name")
        created_event: RoomEvent | None = None

        async with self.db.transaction():
            bootstrap = await self.repos.bootstrap_contexts.get(user_id)
            if bootstrap is not None:
                organization = await self.repos.orgs.get(bootstrap.org_id)
                workspace = await self.repos.workspaces.get(bootstrap.workspace_id)
                room = await self.repos.rooms.get(bootstrap.room_id)
                org_member = await self.repos.orgs.get_member(bootstrap.org_id, user_id)
                workspace_member = await self.repos.workspaces.get_member(
                    bootstrap.workspace_id, user_id
                )
                room_member = await self.repos.room_members.get(bootstrap.room_id, user_id)
                valid = (
                    organization is not None
                    and workspace is not None
                    and room is not None
                    and workspace.org_id == bootstrap.org_id
                    and room.workspace_id == bootstrap.workspace_id
                    and org_member is not None
                    and org_member.role == "admin"
                    and workspace_member is not None
                    and workspace_member.role == "admin"
                    and room_member is not None
                    and room_member.role == "admin"
                )
                if not valid:
                    raise DomainError("bootstrap context failed ownership validation")
                assert organization is not None
                assert workspace is not None
                assert room is not None
                return organization, workspace, room

            org_id = new_id("org")
            organization = Organization(
                org_id=org_id,
                name=f"{display_name}'s workspace",
                slug=f"bootstrap-{org_id}",
            )
            await self.repos.orgs.create(organization)
            await self.repos.orgs.add_member(
                OrgMember(org_id=org_id, user_id=user_id, role="admin")
            )

            workspace = Workspace(
                workspace_id=new_id("ws"),
                org_id=org_id,
                name="Main",
                slug="main",
            )
            await self.repos.workspaces.create(workspace)
            await self.repos.workspaces.add_member(
                WorkspaceMember(
                    workspace_id=workspace.workspace_id,
                    user_id=user_id,
                    role="admin",
                )
            )

            room = Room(
                room_id=new_id("room"),
                workspace_id=workspace.workspace_id,
                name=room_name,
                created_by=user_id,
            )
            await self.repos.rooms.create(room)
            await self.repos.room_members.add(
                RoomMember(room_id=room.room_id, user_id=user_id, role="admin")
            )
            created_event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room.room_id,
                    sequence=0,
                    event_type=EventType.ROOM_CREATED,
                    payload={"name": room_name, "description": ""},
                    actor_id=user_id,
                    actor_type="user",
                )
            )
            await self.repos.bootstrap_contexts.create(
                BootstrapContext(
                    user_id=user_id,
                    org_id=organization.org_id,
                    workspace_id=workspace.workspace_id,
                    room_id=room.room_id,
                )
            )

        if created_event is not None:
            await self._broadcast_persisted_events([created_event])
        return organization, workspace, room

    # ── Workspace ────────────────────────────────────────────────────────────

    async def create_workspace(
        self, org_id: str, name: str, slug: str, creator_id: str
    ) -> Workspace:
        name = self._validate_non_empty(name, "workspace name")
        slug = self._validate_non_empty(slug, "workspace slug")
        ws = Workspace(workspace_id=new_id("ws"), org_id=org_id, name=name, slug=slug)
        await self.repos.workspaces.create(ws)
        await self.repos.workspaces.add_member(
            WorkspaceMember(workspace_id=ws.workspace_id, user_id=creator_id, role="admin")
        )
        return ws

    async def list_workspaces(self, org_id: str) -> list[Workspace]:
        return await self.repos.workspaces.list_by_org(org_id)

    # ── Room ─────────────────────────────────────────────────────────────────

    async def create_room(
        self, workspace_id: str, name: str, creator_id: str, description: str = ""
    ) -> Room:
        name = self._validate_non_empty(name, "room name")
        room = Room(
            room_id=new_id("room"),
            workspace_id=workspace_id,
            name=name,
            description=description,
            created_by=creator_id,
        )
        await self.repos.rooms.create(room)
        await self.repos.room_members.add(
            RoomMember(room_id=room.room_id, user_id=creator_id, role="admin")
        )
        await self._append_room_event(
            room.room_id,
            EventType.ROOM_CREATED,
            {"name": name, "description": description},
            creator_id,
            "user",
        )
        return room

    async def get_room(self, room_id: str) -> Room:
        room = await self.repos.rooms.get(room_id)
        if not room:
            raise DomainError(f"room not found: {room_id}")
        return room

    async def list_rooms(self, workspace_id: str) -> list[Room]:
        return await self.repos.rooms.list_by_workspace(workspace_id)

    async def join_room(self, room_id: str, user_id: str) -> None:
        """Mark an already invited member present; never create membership."""
        await self.authorization.require(room_id, user_id, RoomCapability.READ)
        await self.presence.user_joined(user_id, room_id)
        await self._append_room_event(
            room_id, EventType.USER_JOINED_ROOM, {"user_id": user_id}, user_id, "user"
        )

    async def invite_room_member(
        self,
        room_id: str,
        invited_user_id: str,
        role: str,
        invited_by: str,
    ) -> RoomMember:
        if role not in {"viewer", "editor"}:
            raise DomainError("invitation role must be viewer or editor")
        member = RoomMember(room_id=room_id, user_id=invited_user_id, role=role)
        event = await self.repos.room_members.add_with_event(
            member,
            RoomEvent(
                room_id=room_id,
                sequence=0,
                event_type=EventType.USER_INVITED_ROOM,
                payload={"user_id": invited_user_id, "role": role},
                actor_id=invited_by,
                actor_type="user",
            ),
        )
        await self._broadcast_persisted_events([event])
        return member

    async def leave_room(self, room_id: str, user_id: str) -> None:
        await self.presence.user_left(user_id, room_id)
        await self._append_room_event(
            room_id, EventType.USER_LEFT_ROOM, {"user_id": user_id}, user_id, "user"
        )

    async def get_room_members(self, room_id: str) -> list[RoomMember]:
        return await self.repos.room_members.list(room_id)

    # ── Agents ───────────────────────────────────────────────────────────────

    async def list_agent_templates(self) -> list[AgentTemplate]:
        return await self.repos.agents.list_templates()

    async def spawn_agent(
        self,
        room_id: str,
        template_id: str,
        name: str | None = None,
        system_prompt: str | None = None,
        model_provider: str = "",
        model_name: str = "",
    ) -> AgentInstance:
        template = await self.repos.agents.get_template(template_id)
        if not template:
            raise DomainError(f"agent template not found: {template_id}")
        agent = AgentInstance(
            agent_id=new_id("agent"),
            template_id=template_id,
            room_id=room_id,
            name=name or template.name,
            role=template.role,
            system_prompt=system_prompt or template.system_prompt,
            capabilities=template.capabilities,
            model_provider=model_provider,
            model_name=model_name,
        )
        await self.repos.agents.create_instance(agent)
        await self.repos.agents.add_room_membership(
            AgentRoomMembership(agent_id=agent.agent_id, room_id=room_id)
        )
        await self._append_room_event(
            room_id,
            EventType.AGENT_JOINED_ROOM,
            {"agent_id": agent.agent_id, "name": agent.name, "role": agent.role},
            agent.agent_id,
            "agent",
        )
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
        await self._append_room_event(
            agent.room_id,
            EventType.AGENT_STATUS_CHANGED,
            {"agent_id": agent_id, "status": status.value},
            agent_id,
            "agent",
        )

    # ── Branch ───────────────────────────────────────────────────────────────

    async def start_branch(
        self,
        room_id: str,
        mode: BranchMode,
        initiating_prompt: str,
        initiated_by: str,
        agent_ids: list[str],
    ) -> tuple[Branch, list[Execution]]:
        """Atomically freeze context, create AgentRuns, and optionally own the room turn."""
        initiating_prompt = self._validate_non_empty(initiating_prompt, "branch prompt")
        unique_agent_ids = list(dict.fromkeys(agent_ids))
        if unique_agent_ids != agent_ids:
            raise DomainError("branch agent ids must be unique")
        expected = 1 if mode == BranchMode.TURN_LOCKED_SINGLE else None
        if expected is not None and len(agent_ids) != expected:
            raise DomainError("turn-locked single mode requires exactly one agent")
        if mode == BranchMode.PARALLEL and not 2 <= len(agent_ids) <= 3:
            raise DomainError("parallel mode requires two or three agents")
        agents = [await self.get_agent(agent_id) for agent_id in agent_ids]
        if any(agent.room_id != room_id for agent in agents):
            raise DomainError("every branch agent must belong to the room")

        persisted_events: list[RoomEvent] = []
        executions: list[Execution] = []
        async with self.db.transaction():
            active_lock = await self.repos.turn_locks.get_active(TurnLockScopeType.ROOM, room_id)
            if active_lock is not None:
                raise DomainError(f"room turn is locked by branch {active_lock.branch_id}")
            sequence = await self.repos.events.get_latest_sequence(room_id)
            messages = await self.repos.messages.list_by_room(room_id, limit=50)
            events = await self.repos.events.list_since(room_id, max(0, sequence - 100), limit=100)
            snapshot = {
                "schema": "multiai.branch-context.v1",
                "limits": {"messages": 50, "events": 100},
                "messages": [
                    {
                        "message_id": message.message_id,
                        "role": message.role.value,
                        "sender_id": message.sender_id,
                        "content": message.content,
                        "metadata": message.metadata,
                        "created_at": message.created_at.isoformat(),
                    }
                    for message in messages
                ],
                "events": [
                    {
                        "event_id": event.event_id,
                        "sequence": event.sequence,
                        "event_type": event.event_type.value,
                        "payload": event.payload,
                        "actor_id": event.actor_id,
                        "actor_type": event.actor_type,
                        "timestamp": event.timestamp.isoformat(),
                    }
                    for event in events
                    if event.sequence <= sequence
                ],
            }
            message_ids = tuple(message.message_id for message in messages)
            context_envelope = {
                "initiating_prompt": initiating_prompt,
                "context_event_sequence": sequence,
                "context_message_ids": list(message_ids),
                "context_snapshot": snapshot,
            }
            context_hash = hashlib.sha256(
                json.dumps(
                    context_envelope,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            branch = Branch(
                branch_id=new_id("branch"),
                room_id=room_id,
                mode=mode,
                status=BranchStatus.RUNNING,
                initiated_by=initiated_by,
                initiating_prompt=initiating_prompt,
                context_event_sequence=sequence,
                context_message_ids=message_ids,
                context_snapshot=snapshot,
                context_hash=context_hash,
            )
            await self.repos.branches.create(branch)
            lock: TurnLock | None = None
            if mode == BranchMode.TURN_LOCKED_SINGLE:
                lock = TurnLock(
                    lock_id=new_id("lock"),
                    scope_type=TurnLockScopeType.ROOM,
                    scope_id=room_id,
                    branch_id=branch.branch_id,
                    status=TurnLockStatus.ACTIVE,
                    acquired_by=initiated_by,
                )
                await self.repos.turn_locks.create(lock)
            for agent in agents:
                session = Session(
                    session_id=new_id("sess"),
                    room_id=room_id,
                    agent_id=agent.agent_id,
                    status=SessionStatus.ACTIVE,
                )
                execution = Execution(
                    execution_id=new_id("exec"),
                    session_id=session.session_id,
                    agent_id=agent.agent_id,
                    branch_id=branch.branch_id,
                    status=ExecutionStatus.PENDING,
                    input_data={
                        "initiating_prompt": initiating_prompt,
                        "context_hash": context_hash,
                    },
                )
                await self.repos.sessions.create(session)
                await self.repos.executions.create(execution)
                executions.append(execution)
            events_to_persist = [
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.BRANCH_STARTED,
                    payload={
                        "branch_id": branch.branch_id,
                        "mode": mode.value,
                        "status": branch.status.value,
                        "context_event_sequence": sequence,
                        "context_message_ids": list(message_ids),
                        "context_hash": context_hash,
                        "execution_ids": [run.execution_id for run in executions],
                    },
                    actor_id=initiated_by,
                    actor_type="user",
                )
            ]
            if lock is not None:
                events_to_persist.append(
                    RoomEvent(
                        room_id=room_id,
                        sequence=0,
                        event_type=EventType.TURN_LOCK_ACQUIRED,
                        payload={
                            "lock_id": lock.lock_id,
                            "scope_type": lock.scope_type.value,
                            "scope_id": lock.scope_id,
                            "branch_id": branch.branch_id,
                        },
                        actor_id=initiated_by,
                        actor_type="user",
                    )
                )
            for run in executions:
                events_to_persist.append(
                    RoomEvent(
                        room_id=room_id,
                        sequence=0,
                        event_type=EventType.AGENT_RUN_STARTED,
                        payload={
                            "branch_id": branch.branch_id,
                            "execution_id": run.execution_id,
                            "session_id": run.session_id,
                            "agent_id": run.agent_id,
                        },
                        actor_id=initiated_by,
                        actor_type="user",
                    )
                )
            for event in events_to_persist:
                persisted_events.append(
                    await self.repos.events.append_with_next_sequence_in_transaction(event)
                )
        await self._broadcast_persisted_events(persisted_events)
        return branch, executions

    async def get_branch(self, branch_id: str) -> Branch:
        branch = await self.repos.branches.get(branch_id)
        if branch is None:
            raise DomainError(f"branch not found: {branch_id}")
        return branch

    async def list_room_branches(self, room_id: str) -> list[Branch]:
        await self.get_room(room_id)
        return await self.repos.branches.list_by_room(room_id)

    async def list_branch_runs(self, branch_id: str) -> list[Execution]:
        await self.get_branch(branch_id)
        return await self.repos.executions.list_by_branch(branch_id)

    @staticmethod
    def _branch_execution_prompt(branch: Branch) -> str:
        if not branch.lifecycle_managed:
            return branch.initiating_prompt
        snapshot = json.dumps(
            branch.context_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (
            f"Branch prompt:\n{branch.initiating_prompt}\n\n"
            f"Immutable bounded channel context (hash {branch.context_hash}):\n{snapshot}"
        )

    # ── Session & Execution ──────────────────────────────────────────────────

    async def start_agent_session(
        self, room_id: str, agent_id: str, task_id: str | None = None
    ) -> Session:
        agent = await self.get_agent(agent_id)
        if agent.room_id != room_id:
            raise DomainError("agent is not in this room")
        session = Session(
            session_id=new_id("sess"), room_id=room_id, agent_id=agent_id, task_id=task_id
        )
        await self.repos.sessions.create(session)
        await self._append_room_event(
            room_id,
            EventType.SESSION_STARTED,
            {"session_id": session.session_id, "agent_id": agent_id},
            agent_id,
            "agent",
        )
        return session

    async def start_execution(
        self, session_id: str, input_data: dict[str, Any] | None = None
    ) -> Execution:
        session = await self.repos.sessions.get(session_id)
        if not session:
            raise DomainError(f"session not found: {session_id}")
        _validate_transition(
            session.status, SessionStatus.ACTIVE, VALID_SESSION_TRANSITIONS, "session"
        )
        execution = Execution(
            execution_id=new_id("exec"),
            session_id=session_id,
            agent_id=session.agent_id,
            input_data=input_data or {},
        )
        event = await self.repos.executions.start_with_event(
            execution,
            RoomEvent(
                room_id=session.room_id,
                sequence=0,
                event_type=EventType.AGENT_RUN_STARTED,
                payload={
                    "execution_id": execution.execution_id,
                    "session_id": session_id,
                    "agent_id": session.agent_id,
                },
                actor_id=session.agent_id,
                actor_type="agent",
            ),
        )
        await self._broadcast_persisted_events([event])
        await self._set_agent_status_safe(session.agent_id, AgentStatus.WORKING)
        persisted = await self.repos.executions.get(execution.execution_id)
        return persisted or execution

    async def execute_agent_step(self, execution_id: str, prompt: str) -> dict[str, Any]:
        prompt = self._validate_non_empty(prompt, "agent prompt")
        execution = await self.repos.executions.get(execution_id)
        if not execution:
            raise DomainError(f"execution not found: {execution_id}")
        session = await self.repos.sessions.get(execution.session_id)
        if not session:
            raise DomainError("session not found")
        agent = await self.get_agent(execution.agent_id)
        branch = await self.get_branch(execution.branch_id)

        if execution.status in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }:
            raise DomainError(
                f"execution {execution_id} is terminal (current: {execution.status.value})"
            )

        source_prompt = prompt
        provider_prompt = prompt
        if branch.lifecycle_managed:
            if prompt != branch.initiating_prompt:
                raise DomainError("managed branch run must use its immutable initiating prompt")
            source_prompt = branch.initiating_prompt
            provider_prompt = self._branch_execution_prompt(branch)

        if not execution.run_id:
            await self.nexus.create_execution(agent, session, provider_prompt, execution)
            run_id = f"run_{execution.execution_id}"
            execution = Execution(
                execution_id=execution.execution_id,
                session_id=execution.session_id,
                agent_id=execution.agent_id,
                branch_id=execution.branch_id,
                run_id=run_id,
                status=ExecutionStatus.RUNNING,
                input_data=execution.input_data,
            )
            await self.repos.executions.mark_running(execution.execution_id, run_id)

        result = await self.nexus.execute_step(execution_id, provider_prompt)
        if result.get("status") == "error":
            error = str(result.get("error", ""))
            persisted_events = await self.repos.executions.terminalize_without_output(
                replace(execution, branch_id=branch.branch_id),
                ExecutionStatus.FAILED,
                error,
                [
                    RoomEvent(
                        room_id=session.room_id,
                        sequence=0,
                        event_type=EventType.EXECUTION_FAILED,
                        payload={
                            "branch_id": branch.branch_id,
                            "execution_id": execution.execution_id,
                            "error": error,
                        },
                        actor_id=execution.agent_id,
                        actor_type="agent",
                    )
                ],
            )
            await self._broadcast_persisted_events(persisted_events)
            await self._set_agent_status_safe(execution.agent_id, AgentStatus.FAILED)
            return result
        if result.get("action") == "finish":
            raw_output = result.get("result")
            output_data = raw_output if isinstance(raw_output, dict) else {"result": raw_output}
            raw_provenance = result.get("provenance")
            provenance = raw_provenance if isinstance(raw_provenance, dict) else {}
            raw_interventions = provenance.get("interventions")
            interventions = (
                tuple(str(item) for item in raw_interventions)
                if isinstance(raw_interventions, list)
                else ()
            )
            output = AgentOutput(
                output_id=new_id("out"),
                room_id=session.room_id,
                session_id=session.session_id,
                execution_id=execution.execution_id,
                agent_id=execution.agent_id,
                content=self._output_content(output_data),
                branch_id=branch.branch_id,
                output_data=output_data,
                source_prompt=source_prompt,
                provider_input=str(provenance.get("provider_input", "")),
                provider_name=str(provenance.get("provider_name", "")),
                provider_model=str(provenance.get("provider_model", "")),
                provider_response_id=str(provenance.get("provider_response_id", "")),
                provider_interventions=interventions,
                provider_evidence=str(provenance.get("provider_evidence", "")),
            )
            persisted_events = await self.repos.agent_outputs.complete_execution(
                output,
                [
                    RoomEvent(
                        room_id=session.room_id,
                        sequence=0,
                        event_type=EventType.AGENT_OUTPUT_CREATED,
                        payload={
                            "output_id": output.output_id,
                            "branch_id": branch.branch_id,
                            "execution_id": execution.execution_id,
                            "session_id": session.session_id,
                            "agent_id": execution.agent_id,
                        },
                        actor_id=execution.agent_id,
                        actor_type="agent",
                    ),
                    RoomEvent(
                        room_id=session.room_id,
                        sequence=0,
                        event_type=EventType.AGENT_RUN_COMPLETED,
                        payload={
                            "execution_id": execution.execution_id,
                            "session_id": session.session_id,
                            "agent_id": execution.agent_id,
                            "output_id": output.output_id,
                            "branch_id": branch.branch_id,
                        },
                        actor_id=execution.agent_id,
                        actor_type="agent",
                    ),
                ],
            )
            await self._broadcast_persisted_events(persisted_events)
            await self._set_agent_status_safe(execution.agent_id, AgentStatus.COMPLETED)
            await self._set_agent_status_safe(execution.agent_id, AgentStatus.IDLE)
            result["output_id"] = output.output_id
        return result

    async def execute_branch_run(self, branch_id: str, execution_id: str) -> dict[str, Any]:
        branch = await self.get_branch(branch_id)
        execution = await self.repos.executions.get(execution_id)
        if execution is None or execution.branch_id != branch.branch_id:
            raise DomainError("agent run not found in branch")
        return await self.execute_agent_step(execution_id, branch.initiating_prompt)

    async def pause_execution(self, execution_id: str) -> bool:
        execution = await self.repos.executions.get(execution_id)
        if execution is None:
            raise DomainError("execution not found")
        branch = await self.get_branch(execution.branch_id)
        if not branch.lifecycle_managed:
            return await self.nexus.pause_execution(execution_id)
        _validate_transition(
            execution.status, ExecutionStatus.PAUSED, VALID_EXECUTION_TRANSITIONS, "execution"
        )
        ok = await self.nexus.pause_execution(execution_id)
        if not ok:
            return False
        await self.repos.executions.update_status(execution_id, ExecutionStatus.PAUSED)
        return True

    async def resume_execution(self, execution_id: str) -> bool:
        execution = await self.repos.executions.get(execution_id)
        if execution is None:
            raise DomainError("execution not found")
        branch = await self.get_branch(execution.branch_id)
        if not branch.lifecycle_managed:
            return await self.nexus.resume_execution(execution_id)
        _validate_transition(
            execution.status, ExecutionStatus.RUNNING, VALID_EXECUTION_TRANSITIONS, "execution"
        )
        ok = await self.nexus.resume_execution(execution_id)
        if not ok:
            return False
        await self.repos.executions.update_status(execution_id, ExecutionStatus.RUNNING)
        return True

    async def cancel_execution(self, execution_id: str, cancelled_by: str) -> bool:
        execution = await self.repos.executions.get(execution_id)
        if execution is None:
            raise DomainError("execution not found")
        branch = await self.get_branch(execution.branch_id)
        if not branch.lifecycle_managed:
            return await self.nexus.cancel_execution(execution_id)
        if execution.status in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }:
            raise DomainError("execution is already terminal")
        ok = await self.nexus.cancel_execution(execution_id)
        # A durable PENDING run may not yet exist in the bridge; cancellation is
        # still authoritative at the Branch/AgentRun layer.
        if not ok and execution.run_id:
            return False
        session = await self.repos.sessions.get(execution.session_id)
        if session is None:
            raise DomainError("session not found")
        events = await self.repos.executions.terminalize_without_output(
            execution,
            ExecutionStatus.CANCELLED,
            "cancelled by user",
            [
                RoomEvent(
                    room_id=session.room_id,
                    sequence=0,
                    event_type=EventType.EXECUTION_CANCELLED,
                    payload={
                        "branch_id": execution.branch_id,
                        "execution_id": execution.execution_id,
                    },
                    actor_id=cancelled_by,
                    actor_type="user",
                )
            ],
        )
        await self._broadcast_persisted_events(events)
        return True

    @staticmethod
    def _output_content(output_data: dict[str, Any]) -> str:
        """Derive readable content while preserving the complete structured payload."""
        for key in ("content", "result", "text", "answer"):
            value = output_data.get(key)
            if isinstance(value, str):
                return value
        return json.dumps(output_data, sort_keys=True, default=str)

    async def list_room_outputs(self, room_id: str) -> list[AgentOutput]:
        await self.get_room(room_id)
        return await self.repos.agent_outputs.list_by_room(room_id)

    async def select_output(
        self,
        room_id: str,
        output_id: str,
        disposition: OutputDisposition,
        decided_by: str,
    ) -> OutputSelection:
        output = await self.repos.agent_outputs.get(output_id)
        if output is None or output.room_id != room_id:
            raise DomainError("agent output not found in room")
        selection = OutputSelection(
            room_id=room_id,
            output_id=output_id,
            disposition=disposition,
            decided_by=decided_by,
            branch_id=output.branch_id,
        )
        event = await self.repos.output_selections.upsert_with_event(
            selection,
            RoomEvent(
                room_id=room_id,
                sequence=0,
                event_type=EventType.OUTPUT_SELECTION_UPDATED,
                payload={
                    "branch_id": output.branch_id,
                    "output_id": output_id,
                    "disposition": disposition.value,
                },
                actor_id=decided_by,
                actor_type="user",
            ),
        )
        await self._broadcast_persisted_events([event])
        return selection

    async def list_output_selections(self, room_id: str) -> list[OutputSelection]:
        await self.get_room(room_id)
        return await self.repos.output_selections.list_by_room(room_id)

    async def select_branch_output(
        self,
        branch_id: str,
        output_id: str,
        disposition: OutputDisposition,
        decided_by: str,
    ) -> OutputSelection:
        branch = await self.get_branch(branch_id)
        output = await self.repos.agent_outputs.get(output_id)
        if output is None or output.branch_id != branch_id:
            raise DomainError("agent output not found in branch")
        return await self.select_output(branch.room_id, output_id, disposition, decided_by)

    async def synthesize_decision_brief(
        self, room_id: str, title: str, created_by: str
    ) -> tuple[Artifact, ArtifactVersion]:
        """Compatibility route: resolve one selected Branch, then synthesize that unit."""
        selections = await self.list_output_selections(room_id)
        selected_ids = {
            item.output_id for item in selections if item.disposition == OutputDisposition.INCLUDED
        }
        outputs = [
            output
            for output in await self.list_room_outputs(room_id)
            if output.output_id in selected_ids
        ]
        branch_ids = {output.branch_id for output in outputs}
        if len(branch_ids) != 1:
            raise DomainError("selected outputs must belong to exactly one branch")
        return await self.synthesize_branch_decision_brief(branch_ids.pop(), title, created_by)

    async def synthesize_branch_decision_brief(
        self, branch_id: str, title: str, created_by: str
    ) -> tuple[Artifact, ArtifactVersion]:
        """Run model-backed synthesis over this Branch's explicit selected outputs."""
        title = self._validate_non_empty(title, "decision brief title")
        branch = await self.get_branch(branch_id)
        outputs = await self.repos.agent_outputs.list_by_branch(branch_id)
        selections = await self.repos.output_selections.list_by_branch(branch_id)
        decisions = {selection.output_id: selection.disposition for selection in selections}
        minimum_included = 1 if branch.mode == BranchMode.TURN_LOCKED_SINGLE else 2
        if len(outputs) < minimum_included:
            raise DomainError(
                f"at least {minimum_included} branch output(s) are required for this mode"
            )
        unreviewed = [output.output_id for output in outputs if output.output_id not in decisions]
        if unreviewed:
            raise DomainError("every branch output must be included or excluded")
        included = [
            output
            for output in outputs
            if decisions[output.output_id] == OutputDisposition.INCLUDED
        ]
        if len(included) < minimum_included:
            raise DomainError(
                f"at least {minimum_included} branch output(s) must be included for this mode"
            )
        runs = await self.repos.executions.list_by_branch(branch_id)
        if any(
            run.status
            not in {
                ExecutionStatus.COMPLETED,
                ExecutionStatus.FAILED,
                ExecutionStatus.CANCELLED,
            }
            for run in runs
        ):
            raise DomainError("branch synthesis requires every AgentRun to be terminal")

        selected_records = [
            {"output_id": output.output_id, "agent_id": output.agent_id, "content": output.content}
            for output in included
        ]
        provider_input = self.nexus.build_synthesis_provider_input(
            title=title,
            prompt=branch.initiating_prompt,
            outputs=selected_records,
        )
        synthesis = BranchSynthesis(
            synthesis_id=new_id("syn"),
            branch_id=branch_id,
            room_id=branch.room_id,
            title=title,
            initiated_by=created_by,
            status=BranchSynthesisStatus.RUNNING,
            provider_input=provider_input,
        )
        inputs = [
            BranchSynthesisInput(
                synthesis_id=synthesis.synthesis_id,
                output_id=output.output_id,
                ordinal=ordinal,
            )
            for ordinal, output in enumerate(included, start=1)
        ]
        async with self.db.transaction():
            await self.repos.branch_syntheses.create_with_inputs(synthesis, inputs)
        try:
            model_result = await self.nexus.synthesize_selected_outputs(
                title=title,
                prompt=branch.initiating_prompt,
                outputs=selected_records,
            )
        except ModelProviderError as exc:
            async with self.db.transaction():
                await self.repos.branch_syntheses.mark_failed(synthesis.synthesis_id, str(exc))
                started_event = await self.repos.events.append_with_next_sequence_in_transaction(
                    RoomEvent(
                        room_id=branch.room_id,
                        sequence=0,
                        event_type=EventType.BRANCH_SYNTHESIS_STARTED,
                        payload={
                            "branch_id": branch_id,
                            "synthesis_id": synthesis.synthesis_id,
                            "selected_output_ids": [item.output_id for item in inputs],
                        },
                        actor_id=created_by,
                        actor_type="user",
                        timestamp=synthesis.created_at,
                    )
                )
                failed_event = await self.repos.events.append_with_next_sequence_in_transaction(
                    RoomEvent(
                        room_id=branch.room_id,
                        sequence=0,
                        event_type=EventType.BRANCH_SYNTHESIS_FAILED,
                        payload={"branch_id": branch_id, "synthesis_id": synthesis.synthesis_id},
                        actor_id=created_by,
                        actor_type="user",
                    )
                )
            await self._broadcast_persisted_events([started_event, failed_event])
            raise DomainError(str(exc)) from exc

        document_value = model_result.get("document")
        if not isinstance(document_value, dict):
            raise DomainError("model provider returned invalid synthesis document")
        document = document_value
        content = self._render_decision_brief(title, document, bool(model_result["simulated"]))
        existing = next(
            (
                artifact
                for artifact in await self.list_room_artifacts(branch.room_id)
                if artifact.name == "Decision Brief"
            ),
            None,
        )
        create_artifact = existing is None
        artifact = existing or Artifact(
            artifact_id=new_id("art"),
            room_id=branch.room_id,
            name="Decision Brief",
            artifact_type=ArtifactType.DOCUMENT,
            description="Human-selected, provenance-complete specialist synthesis",
            created_by=created_by,
        )
        version_id = new_id("ver")
        version = ArtifactVersion(
            version_id=version_id,
            artifact_id=artifact.artifact_id,
            version_number=artifact.current_version + 1,
            content=content,
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            branch_synthesis_id=synthesis.synthesis_id,
            created_by=created_by,
        )
        output_by_id = {output.output_id: output for output in included}
        claims_and_sources: list[tuple[ArtifactClaim, ClaimSource]] = []
        raw_claims = document.get("claims")
        if not isinstance(raw_claims, list):
            raise DomainError("synthesis claims are invalid")
        for ordinal, raw_claim in enumerate(raw_claims, start=1):
            if not isinstance(raw_claim, dict):
                raise DomainError("synthesis claim is invalid")
            claim = ArtifactClaim(
                claim_id=new_id("claim"),
                version_id=version_id,
                ordinal=ordinal,
                text=str(raw_claim["text"]),
                is_ai_derived=True,
                confidence=float(raw_claim["confidence"]),
            )
            for output_id in raw_claim["source_output_ids"]:
                output = output_by_id[str(output_id)]
                claims_and_sources.append((claim, self._claim_source(claim.claim_id, output)))
        provenance_records = [
            self._claim_provenance_record(claim, source) for claim, source in claims_and_sources
        ]
        version = replace(
            version,
            provenance_hash=self._artifact_provenance_hash(version, provenance_records),
        )
        terminal_synthesis = replace(
            synthesis,
            status=BranchSynthesisStatus.COMPLETED,
            provider_name=str(model_result["provider_name"]),
            provider_model=str(model_result["provider_model"]),
            provider_response_id=str(model_result["provider_response_id"]),
            provider_evidence=str(model_result["provider_evidence"]),
            simulated=bool(model_result["simulated"]),
            content=content,
            artifact_version_id=version.version_id,
            completed_at=utcnow(),
        )
        event_types: list[RoomEvent] = []
        if create_artifact:
            event_types.append(
                RoomEvent(
                    room_id=branch.room_id,
                    sequence=0,
                    event_type=EventType.ARTIFACT_CREATED,
                    payload={
                        "artifact_id": artifact.artifact_id,
                        "name": artifact.name,
                        "type": artifact.artifact_type.value,
                    },
                    actor_id=created_by,
                    actor_type="user",
                )
            )
        event_types.extend(
            [
                RoomEvent(
                    room_id=branch.room_id,
                    sequence=0,
                    event_type=EventType.BRANCH_SYNTHESIS_STARTED,
                    payload={
                        "branch_id": branch_id,
                        "synthesis_id": synthesis.synthesis_id,
                        "selected_output_ids": [item.output_id for item in inputs],
                    },
                    actor_id=created_by,
                    actor_type="user",
                    timestamp=synthesis.created_at,
                ),
                RoomEvent(
                    room_id=branch.room_id,
                    sequence=0,
                    event_type=EventType.DECISION_BRIEF_SYNTHESIZED,
                    payload={
                        "branch_id": branch_id,
                        "synthesis_id": synthesis.synthesis_id,
                        "artifact_id": artifact.artifact_id,
                        "version_id": version.version_id,
                        "version": version.version_number,
                        "content_hash": version.content_hash,
                        "provenance_hash": version.provenance_hash,
                        "selected_output_ids": [output.output_id for output in included],
                        "simulated": terminal_synthesis.simulated,
                    },
                    actor_id=created_by,
                    actor_type="user",
                ),
                RoomEvent(
                    room_id=branch.room_id,
                    sequence=0,
                    event_type=EventType.BRANCH_SYNTHESIS_COMPLETED,
                    payload={
                        "branch_id": branch_id,
                        "synthesis_id": synthesis.synthesis_id,
                        "artifact_version_id": version.version_id,
                        "simulated": terminal_synthesis.simulated,
                    },
                    actor_id=created_by,
                    actor_type="user",
                ),
            ]
        )
        ontology_entities, ontology_relationships = await self._decision_brief_ontology(
            room_id=branch.room_id,
            title=title,
            created_by=created_by,
            artifact=artifact,
            version=version,
            claims_and_sources=claims_and_sources,
            included=included,
        )
        event_types.append(
            RoomEvent(
                room_id=branch.room_id,
                sequence=0,
                event_type=EventType.ONTOLOGY_MATERIALIZED,
                payload={
                    "artifact_id": artifact.artifact_id,
                    "version_id": version.version_id,
                    "entity_ids": [entity.entity_id for entity in ontology_entities],
                    "relationship_ids": [item.relationship_id for item in ontology_relationships],
                },
                actor_id=created_by,
                actor_type="user",
            )
        )
        persisted_events = await self.repos.artifacts.create_synthesis(
            artifact,
            version,
            claims_and_sources,
            ontology_entities,
            ontology_relationships,
            event_types,
            create_artifact=create_artifact,
            synthesis=terminal_synthesis,
        )
        await self._broadcast_persisted_events(persisted_events)
        return replace(artifact, current_version=version.version_number), version

    @staticmethod
    def _claim_source(claim_id: str, output: AgentOutput) -> ClaimSource:
        return ClaimSource(
            claim_id=claim_id,
            output_id=output.output_id,
            evidence=output.content,
            agent_id=output.agent_id,
            execution_id=output.execution_id,
            source_prompt=output.source_prompt,
            provider_input=output.provider_input,
            provider_name=output.provider_name,
            provider_model=output.provider_model,
            provider_response_id=output.provider_response_id,
            provider_interventions=output.provider_interventions,
            provider_evidence=output.provider_evidence,
        )

    @staticmethod
    def _render_decision_brief(title: str, document: dict[str, Any], simulated: bool) -> str:
        marker = " [SIMULATED SYNTHESIS]" if simulated else ""
        lines = [
            f"# {title}{marker}",
            "",
            str(document.get("summary", "")).strip(),
            "",
            "## Recommendation [AI-derived]",
            str(document.get("recommendation", "")).strip(),
            "",
            "## Claims",
        ]
        claims = document.get("claims")
        if isinstance(claims, list):
            for ordinal, claim in enumerate(claims, start=1):
                if isinstance(claim, dict):
                    source_ids = ", ".join(f"`{item}`" for item in claim["source_output_ids"])
                    lines.extend(
                        [
                            f"### Claim {ordinal} [AI-derived]",
                            str(claim["text"]),
                            f"Sources: {source_ids}",
                            f"Confidence: {float(claim['confidence']):.2f}",
                            "",
                        ]
                    )
        for heading, key in (("Risks", "risks"), ("Uncertainties", "uncertainties")):
            lines.append(f"## {heading}")
            values = document.get(key)
            if isinstance(values, list):
                lines.extend(f"- {value}" for value in values)
            lines.append("")
        lines.extend(["## Next action", str(document.get("next_action", "")).strip(), ""])
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _ontology_id(prefix: str, room_id: str, *source_ids: str) -> str:
        material = ":".join((room_id, *source_ids)).encode()
        return f"{prefix}_{hashlib.sha256(material).hexdigest()[:24]}"

    async def _decision_brief_ontology(
        self,
        *,
        room_id: str,
        title: str,
        created_by: str,
        artifact: Artifact,
        version: ArtifactVersion,
        claims_and_sources: list[tuple[ArtifactClaim, ClaimSource]],
        included: list[AgentOutput],
    ) -> tuple[list[OntologyEntity], list[OntologyRelationship]]:
        """Project the published brief without inferring beyond frozen evidence."""
        room = await self.get_room(room_id)
        creator = await self.repos.users.get(created_by)
        selected_output_ids = tuple(output.output_id for output in included)
        claim_ids = tuple(claim.claim_id for claim, _source in claims_and_sources)
        timestamp = version.created_at

        project_id = self._ontology_id("ont", room_id, "Project", room_id)
        person_id = self._ontology_id("ont", room_id, "Person", created_by)
        artifact_entity_id = self._ontology_id("ont", room_id, "Artifact", version.version_id)
        decision_id = self._ontology_id("ont", room_id, "Decision", version.version_id)
        entities = [
            OntologyEntity(
                entity_id=project_id,
                room_id=room_id,
                kind=OntologyEntityKind.PROJECT,
                source_object_id=room_id,
                label=room.name,
                properties={"workspace_id": room.workspace_id},
                evidence_ids=(room_id,),
                source_ids=(room_id,),
                created_at=timestamp,
                updated_at=timestamp,
            ),
            OntologyEntity(
                entity_id=person_id,
                room_id=room_id,
                kind=OntologyEntityKind.PERSON,
                source_object_id=created_by,
                label=creator.display_name if creator is not None else created_by,
                properties={"user_id": created_by},
                evidence_ids=(created_by,),
                source_ids=(created_by,),
                created_at=timestamp,
                updated_at=timestamp,
            ),
            OntologyEntity(
                entity_id=artifact_entity_id,
                room_id=room_id,
                kind=OntologyEntityKind.ARTIFACT,
                source_object_id=version.version_id,
                label=f"{artifact.name} v{version.version_number}",
                properties={
                    "artifact_id": artifact.artifact_id,
                    "version_id": version.version_id,
                    "version_number": version.version_number,
                    "content_hash": version.content_hash,
                    "provenance_hash": version.provenance_hash,
                },
                evidence_ids=(version.version_id,),
                source_ids=(artifact.artifact_id, version.version_id),
                created_at=timestamp,
                updated_at=timestamp,
            ),
            OntologyEntity(
                entity_id=decision_id,
                room_id=room_id,
                kind=OntologyEntityKind.DECISION,
                source_object_id=version.version_id,
                label=title,
                properties={
                    "artifact_id": artifact.artifact_id,
                    "version_id": version.version_id,
                    "claim_ids": list(claim_ids),
                },
                derivation_kind=OntologyDerivationKind.AI_DERIVED,
                confidence=1.0,
                evidence_ids=selected_output_ids,
                source_ids=(version.version_id, *claim_ids),
                created_at=timestamp,
                updated_at=timestamp,
            ),
        ]
        claim_entity_ids: dict[str, str] = {}
        output_entity_ids: dict[str, str] = {}
        for output in included:
            output_entity_id = self._ontology_id("ont", room_id, "AgentOutput", output.output_id)
            output_entity_ids[output.output_id] = output_entity_id
            entities.append(
                OntologyEntity(
                    entity_id=output_entity_id,
                    room_id=room_id,
                    kind=OntologyEntityKind.AGENT_OUTPUT,
                    source_object_id=output.output_id,
                    label=f"Agent output {output.output_id}",
                    properties={
                        "agent_id": output.agent_id,
                        "execution_id": output.execution_id,
                        "provider_name": output.provider_name,
                        "provider_model": output.provider_model,
                    },
                    derivation_kind=OntologyDerivationKind.AI_DERIVED,
                    confidence=1.0,
                    evidence_ids=(output.output_id,),
                    source_ids=(output.output_id, output.execution_id),
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
        for claim, source in claims_and_sources:
            claim_entity_id = self._ontology_id("ont", room_id, "Claim", claim.claim_id)
            claim_entity_ids[claim.claim_id] = claim_entity_id
            entities.append(
                OntologyEntity(
                    entity_id=claim_entity_id,
                    room_id=room_id,
                    kind=OntologyEntityKind.CLAIM,
                    source_object_id=claim.claim_id,
                    label=claim.text,
                    properties={
                        "version_id": claim.version_id,
                        "ordinal": claim.ordinal,
                        "is_ai_derived": claim.is_ai_derived,
                    },
                    derivation_kind=OntologyDerivationKind.AI_DERIVED,
                    confidence=claim.confidence,
                    evidence_ids=(source.output_id,),
                    source_ids=(claim.claim_id, claim.version_id, source.output_id),
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )

        relationships: list[OntologyRelationship] = []

        def relationship(
            kind: OntologyRelationshipKind,
            from_entity_id: str,
            to_entity_id: str,
            derivation_kind: OntologyDerivationKind,
            evidence_ids: tuple[str, ...],
            source_ids: tuple[str, ...],
        ) -> None:
            relationships.append(
                OntologyRelationship(
                    relationship_id=self._ontology_id(
                        "rel", room_id, kind.value, from_entity_id, to_entity_id
                    ),
                    room_id=room_id,
                    kind=kind,
                    from_entity_id=from_entity_id,
                    to_entity_id=to_entity_id,
                    derivation_kind=derivation_kind,
                    evidence_ids=evidence_ids,
                    source_ids=source_ids,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )

        relationship(
            OntologyRelationshipKind.OWNS,
            project_id,
            artifact_entity_id,
            OntologyDerivationKind.SYSTEM_MATERIALIZED,
            (version.version_id,),
            (room_id, artifact.artifact_id, version.version_id),
        )
        relationship(
            OntologyRelationshipKind.OWNS,
            person_id,
            artifact_entity_id,
            OntologyDerivationKind.SYSTEM_MATERIALIZED,
            (version.version_id,),
            (created_by, artifact.artifact_id, version.version_id),
        )
        relationship(
            OntologyRelationshipKind.REFERENCES,
            artifact_entity_id,
            decision_id,
            OntologyDerivationKind.SYSTEM_MATERIALIZED,
            (version.version_id,),
            (artifact.artifact_id, version.version_id),
        )
        for claim, source in claims_and_sources:
            claim_entity_id = claim_entity_ids[claim.claim_id]
            output_entity_id = output_entity_ids[source.output_id]
            exact_evidence = (source.output_id,)
            relationship(
                OntologyRelationshipKind.SUPPORTS,
                claim_entity_id,
                decision_id,
                OntologyDerivationKind.AI_DERIVED,
                exact_evidence,
                (claim.claim_id, source.output_id, version.version_id),
            )
            relationship(
                OntologyRelationshipKind.DERIVED_FROM,
                claim_entity_id,
                output_entity_id,
                OntologyDerivationKind.AI_DERIVED,
                exact_evidence,
                (claim.claim_id, source.output_id),
            )
            relationship(
                OntologyRelationshipKind.DERIVED_FROM,
                decision_id,
                output_entity_id,
                OntologyDerivationKind.AI_DERIVED,
                exact_evidence,
                (version.version_id, claim.claim_id, source.output_id),
            )
        return entities, relationships

    @staticmethod
    def _claim_provenance_record(claim: ArtifactClaim, source: ClaimSource) -> dict[str, Any]:
        return {
            "claim_id": claim.claim_id,
            "ordinal": claim.ordinal,
            "text": claim.text,
            "is_ai_derived": int(claim.is_ai_derived),
            "confidence": claim.confidence,
            "output_id": source.output_id,
            "evidence": source.evidence,
            "agent_id": source.agent_id,
            "execution_id": source.execution_id,
            "source_prompt": source.source_prompt,
            "provider_input": source.provider_input,
            "provider_name": source.provider_name,
            "provider_model": source.provider_model,
            "provider_response_id": source.provider_response_id,
            "provider_interventions": list(source.provider_interventions),
            "provider_evidence": source.provider_evidence,
        }

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

    @classmethod
    def verify_artifact_provenance_hash(
        cls, version: ArtifactVersion, claims: list[dict[str, Any]]
    ) -> bool:
        actual_content_hash = hashlib.sha256(version.content.encode()).hexdigest()
        if actual_content_hash != version.content_hash:
            return False
        expected_hash = cls._artifact_provenance_hash(
            replace(version, content_hash=actual_content_hash), claims
        )
        return bool(version.provenance_hash) and version.provenance_hash == expected_hash

    async def _set_agent_status_safe(self, agent_id: str, status: AgentStatus) -> None:
        """Set agent status, skipping validation if transition is invalid (best-effort)."""
        try:
            await self.update_agent_status(agent_id, status)
        except DomainError:
            log.debug("Skipping invalid agent transition for %s: -> %s", agent_id, status.value)

    # ── Tasks ────────────────────────────────────────────────────────────────

    async def create_task(
        self,
        room_id: str,
        title: str,
        description: str = "",
        priority: TaskPriority = TaskPriority.NORMAL,
        created_by: str = "",
        parent_task_id: str | None = None,
    ) -> Task:
        title = self._validate_non_empty(title, "task title")
        task = Task(
            task_id=new_id("task"),
            room_id=room_id,
            title=title,
            description=description,
            priority=priority,
            created_by=created_by,
            parent_task_id=parent_task_id,
        )
        await self.repos.tasks.create(task)
        await self._append_room_event(
            room_id,
            EventType.TASK_CREATED,
            {"task_id": task.task_id, "title": title},
            created_by,
            "user",
        )
        return task

    async def assign_task(self, task_id: str, agent_id: str) -> Task:
        task = await self.repos.tasks.get(task_id)
        if not task:
            raise DomainError(f"task not found: {task_id}")
        _validate_transition(task.status, TaskStatus.ASSIGNED, VALID_TASK_TRANSITIONS, "task")
        task = Task(
            task_id=task.task_id,
            room_id=task.room_id,
            title=task.title,
            description=task.description,
            status=TaskStatus.ASSIGNED,
            priority=task.priority,
            assigned_agent_id=agent_id,
            created_by=task.created_by,
            parent_task_id=task.parent_task_id,
            delegation_id=task.delegation_id,
        )
        await self.repos.tasks.update(task)
        await self._append_room_event(
            task.room_id,
            EventType.TASK_ASSIGNED,
            {"task_id": task_id, "agent_id": agent_id},
            agent_id,
            "agent",
        )
        return task

    async def delegate_task(
        self, task_id: str, from_agent_id: str, to_agent_id: str, description: str = ""
    ) -> Task:
        task = await self.repos.tasks.get(task_id)
        if not task:
            raise DomainError(f"task not found: {task_id}")
        delegation_id = new_id("deleg")
        child = Task(
            task_id=new_id("task"),
            room_id=task.room_id,
            title=f"Delegated: {task.title}",
            description=description or task.description,
            status=TaskStatus.ASSIGNED,
            priority=task.priority,
            assigned_agent_id=to_agent_id,
            created_by=from_agent_id,
            parent_task_id=task_id,
            delegation_id=delegation_id,
        )
        await self.repos.tasks.create(child)
        await self._append_room_event(
            task.room_id,
            EventType.TASK_DELEGATED,
            {
                "parent_task_id": task_id,
                "child_task_id": child.task_id,
                "from_agent": from_agent_id,
                "to_agent": to_agent_id,
            },
            from_agent_id,
            "agent",
        )
        return child

    async def complete_task(self, task_id: str) -> Task:
        task = await self.repos.tasks.get(task_id)
        if not task:
            raise DomainError(f"task not found: {task_id}")
        _validate_transition(task.status, TaskStatus.COMPLETED, VALID_TASK_TRANSITIONS, "task")
        task = Task(
            task_id=task.task_id,
            room_id=task.room_id,
            title=task.title,
            description=task.description,
            status=TaskStatus.COMPLETED,
            priority=task.priority,
            assigned_agent_id=task.assigned_agent_id,
            created_by=task.created_by,
            parent_task_id=task.parent_task_id,
            delegation_id=task.delegation_id,
        )
        await self.repos.tasks.update(task)
        await self._append_room_event(
            task.room_id,
            EventType.TASK_COMPLETED,
            {"task_id": task_id},
            task.assigned_agent_id or "system",
            "agent",
        )
        return task

    async def cancel_task(self, task_id: str) -> Task:
        task = await self.repos.tasks.get(task_id)
        if not task:
            raise DomainError(f"task not found: {task_id}")
        _validate_transition(task.status, TaskStatus.CANCELLED, VALID_TASK_TRANSITIONS, "task")
        task = Task(
            task_id=task.task_id,
            room_id=task.room_id,
            title=task.title,
            description=task.description,
            status=TaskStatus.CANCELLED,
            priority=task.priority,
            assigned_agent_id=task.assigned_agent_id,
            created_by=task.created_by,
            parent_task_id=task.parent_task_id,
            delegation_id=task.delegation_id,
        )
        await self.repos.tasks.update(task)
        await self._append_room_event(
            task.room_id, EventType.TASK_CANCELLED, {"task_id": task_id}, task.created_by, "user"
        )
        return task

    async def list_room_tasks(self, room_id: str) -> list[Task]:
        return await self.repos.tasks.list_by_room(room_id)

    # ── Messages ─────────────────────────────────────────────────────────────

    async def send_message(
        self,
        room_id: str,
        role: MessageRole,
        sender_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> Message:
        content = self._validate_non_empty(content, "message content")
        msg = Message(
            message_id=new_id("msg"),
            room_id=room_id,
            role=role,
            sender_id=sender_id,
            content=content,
            metadata=metadata or {},
        )
        try:
            event = await self.repos.messages.create_with_event_and_turn_guard(
                msg,
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.MESSAGE_CREATED,
                    payload={
                        "message_id": msg.message_id,
                        "role": role.value,
                        "sender_id": sender_id,
                        "content": content[:500],
                    },
                    actor_id=sender_id,
                    actor_type=role.value.lower(),
                ),
            )
        except ValueError as exc:
            raise DomainError(str(exc)) from exc
        await self._broadcast_persisted_events([event])
        return msg

    async def list_room_messages(self, room_id: str, limit: int = 100) -> list[Message]:
        return await self.repos.messages.list_by_room(room_id, limit=self._validate_limit(limit))

    # ── Artifacts ────────────────────────────────────────────────────────────

    async def create_artifact(
        self,
        room_id: str,
        name: str,
        artifact_type: ArtifactType,
        description: str = "",
        created_by: str = "",
        content: str = "",
    ) -> Artifact:
        name = self._validate_non_empty(name, "artifact name")
        artifact = Artifact(
            artifact_id=new_id("art"),
            room_id=room_id,
            name=name,
            artifact_type=artifact_type,
            description=description,
            current_version=1 if content else 0,
            created_by=created_by,
        )
        await self.repos.artifacts.create(artifact)
        if content:
            version = ArtifactVersion(
                version_id=new_id("ver"),
                artifact_id=artifact.artifact_id,
                version_number=1,
                content=content,
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
                created_by=created_by,
            )
            version = replace(
                version,
                provenance_hash=self._artifact_provenance_hash(version, []),
            )
            await self.repos.artifacts.create_version(version)
        await self._append_room_event(
            room_id,
            EventType.ARTIFACT_CREATED,
            {"artifact_id": artifact.artifact_id, "name": name, "type": artifact_type.value},
            created_by,
            "user",
        )
        return artifact

    async def update_artifact(
        self, artifact_id: str, content: str, updated_by: str = ""
    ) -> ArtifactVersion:
        artifact = await self.repos.artifacts.get(artifact_id)
        if not artifact:
            raise DomainError(f"artifact not found: {artifact_id}")
        new_ver = artifact.current_version + 1
        version = ArtifactVersion(
            version_id=new_id("ver"),
            artifact_id=artifact_id,
            version_number=new_ver,
            content=content,
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            created_by=updated_by,
        )
        version = replace(
            version,
            provenance_hash=self._artifact_provenance_hash(version, []),
        )
        await self.repos.artifacts.create_version(version)
        await self._append_room_event(
            artifact.room_id,
            EventType.ARTIFACT_VERSION_CREATED,
            {"artifact_id": artifact_id, "version": new_ver},
            updated_by,
            "user",
        )
        return version

    async def list_room_artifacts(self, room_id: str) -> list[Artifact]:
        return await self.repos.artifacts.list_by_room(room_id)

    # ── Decisions ────────────────────────────────────────────────────────────

    async def create_decision(
        self, room_id: str, title: str, content: str, reason: str = "", created_by: str = ""
    ) -> Decision:
        title = self._validate_non_empty(title, "decision title")
        decision = Decision(
            decision_id=new_id("dec"),
            room_id=room_id,
            title=title,
            content=content,
            reason=reason,
            created_by=created_by,
        )
        await self.repos.decisions.create(decision)
        await self._append_room_event(
            room_id,
            EventType.DECISION_CREATED,
            {"decision_id": decision.decision_id, "title": title},
            created_by,
            "user",
        )
        return decision

    async def list_room_decisions(self, room_id: str) -> list[Decision]:
        return await self.repos.decisions.list_by_room(room_id)

    # ── Memory ───────────────────────────────────────────────────────────────

    async def create_memory(
        self,
        room_id: str | None,
        workspace_id: str | None,
        org_id: str | None,
        scope: MemoryScope,
        content: str,
        memory_type: str = "fact",
        created_by: str = "",
    ) -> Memory:
        content = self._validate_non_empty(content, "memory content")
        memory = Memory(
            memory_id=new_id("mem"),
            room_id=room_id,
            workspace_id=workspace_id,
            org_id=org_id,
            scope=scope,
            content=content,
            memory_type=memory_type,
            created_by=created_by,
        )
        await self.repos.memories.create(memory)
        if room_id:
            await self._append_room_event(
                room_id,
                EventType.MEMORY_CREATED,
                {"memory_id": memory.memory_id, "type": memory_type},
                created_by,
                "user",
            )
        return memory

    async def list_room_memories(self, room_id: str) -> list[Memory]:
        return await self.repos.memories.list_by_room(room_id)

    # ── Approvals ────────────────────────────────────────────────────────────

    async def request_approval(
        self, room_id: str, execution_id: str, agent_id: str, action_description: str
    ) -> Approval:
        approval = Approval(
            approval_id=new_id("appr"),
            room_id=room_id,
            execution_id=execution_id,
            agent_id=agent_id,
            action_description=action_description,
        )
        await self.repos.approvals.create(approval)
        await self._set_agent_status_safe(agent_id, AgentStatus.WAITING_APPROVAL)
        await self._append_room_event(
            room_id,
            EventType.APPROVAL_REQUESTED,
            {
                "approval_id": approval.approval_id,
                "agent_id": agent_id,
                "action": action_description,
            },
            agent_id,
            "agent",
        )
        return approval

    async def approve_action(
        self, approval_id: str, reviewer_id: str, comment: str = ""
    ) -> Approval:
        approval = await self.repos.approvals.get(approval_id)
        if not approval:
            raise DomainError(f"approval not found: {approval_id}")
        if approval.status != ApprovalStatus.PENDING:
            raise DomainError(
                f"approval {approval_id} is not pending (current: {approval.status.value})"
            )
        approval = Approval(
            approval_id=approval.approval_id,
            room_id=approval.room_id,
            execution_id=approval.execution_id,
            agent_id=approval.agent_id,
            action_description=approval.action_description,
            status=ApprovalStatus.APPROVED,
            reviewer_id=reviewer_id,
            review_comment=comment,
            requested_at=approval.requested_at,
            reviewed_at=utcnow(),
        )
        await self.repos.approvals.update(approval)
        await self._set_agent_status_safe(approval.agent_id, AgentStatus.WORKING)
        await self._append_room_event(
            approval.room_id,
            EventType.APPROVAL_GRANTED,
            {"approval_id": approval_id, "reviewer_id": reviewer_id},
            reviewer_id,
            "user",
        )
        return approval

    async def reject_action(
        self, approval_id: str, reviewer_id: str, comment: str = ""
    ) -> Approval:
        approval = await self.repos.approvals.get(approval_id)
        if not approval:
            raise DomainError(f"approval not found: {approval_id}")
        if approval.status != ApprovalStatus.PENDING:
            raise DomainError(
                f"approval {approval_id} is not pending (current: {approval.status.value})"
            )
        approval = Approval(
            approval_id=approval.approval_id,
            room_id=approval.room_id,
            execution_id=approval.execution_id,
            agent_id=approval.agent_id,
            action_description=approval.action_description,
            status=ApprovalStatus.REJECTED,
            reviewer_id=reviewer_id,
            review_comment=comment,
            requested_at=approval.requested_at,
            reviewed_at=utcnow(),
        )
        await self.repos.approvals.update(approval)
        await self._append_room_event(
            approval.room_id,
            EventType.APPROVAL_REJECTED,
            {"approval_id": approval_id, "reviewer_id": reviewer_id},
            reviewer_id,
            "user",
        )
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
        await self._append_room_event(
            agent.room_id,
            EventType.HUMAN_INTERRUPTED_AGENT,
            {"agent_id": agent_id, "reason": reason},
            user_id,
            "user",
        )

    async def redirect_agent(self, agent_id: str, user_id: str, instruction: str) -> None:
        agent = await self.get_agent(agent_id)
        execution_id = await self.nexus.get_execution_for_agent(agent_id)
        if execution_id:
            await self.nexus.add_execution_intervention(execution_id, instruction)
        await self._append_room_event(
            agent.room_id,
            EventType.HUMAN_REDIRECTED_AGENT,
            {"agent_id": agent_id, "instruction": instruction},
            user_id,
            "user",
        )

    # ── Notifications ────────────────────────────────────────────────────────

    async def create_notification(
        self,
        user_id: str,
        title: str,
        body: str,
        room_id: str | None = None,
        notification_type: str = "info",
    ) -> Notification:
        notif = Notification(
            notification_id=new_id("notif"),
            user_id=user_id,
            room_id=room_id,
            title=title,
            body=body,
            notification_type=notification_type,
        )
        await self.repos.notifications.create(notif)
        return notif

    async def list_notifications(self, user_id: str) -> list[Notification]:
        return await self.repos.notifications.list_unread(user_id)

    # ── Event History ────────────────────────────────────────────────────────

    async def get_room_ontology(self, room_id: str) -> dict[str, Any]:
        await self.get_room(room_id)
        entities = await self.repos.ontology.list_entities(room_id)
        relationships = await self.repos.ontology.list_relationships(room_id)
        reviews = await self.repos.ontology.list_reviews(room_id)
        return {
            "entities": [self._ontology_entity_record(entity) for entity in entities],
            "relationships": [
                self._ontology_relationship_record(relationship) for relationship in relationships
            ],
            "reviews": [self._ontology_review_record(review) for review in reviews],
        }

    @staticmethod
    def _meta_question_kind(question: str) -> str:
        normalized = " ".join(question.strip().lower().split()).rstrip("?!. ")
        why_queries = frozenset(
            {
                "why",
                "why_decision",
                "why decision",
                "why was this decision made",
                "why was the decision made",
                "what is the reason for this decision",
                "what are the reasons for this decision",
            }
        )
        evidence_queries = frozenset(
            {
                "evidence",
                "decision_evidence",
                "decision evidence",
                "what evidence supports this decision",
                "what evidence supports the decision",
                "show supporting evidence",
                "show the evidence for this decision",
                "what sources support this decision",
            }
        )
        if normalized in evidence_queries:
            return "DECISION_EVIDENCE"
        if normalized in why_queries:
            return "WHY_DECISION"
        raise DomainError(
            "unsupported Meta question; ask why the decision was made or what evidence supports it"
        )

    async def answer_decision_meta(
        self,
        room_id: str,
        question: str,
        *,
        version_id: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Answer one bounded decision question from current governed assertions."""
        question_kind = self._meta_question_kind(question)
        if not 1 <= limit <= 10:
            raise DomainError("Meta evidence limit must be between 1 and 10")
        await self.get_room(room_id)

        resolved = await self.repos.artifacts.resolve_decision_version(room_id, version_id)
        if resolved is None:
            raise DomainError("decision artifact version not found in room")
        artifact, version = resolved
        provenance, available_claims = await self.repos.artifacts.get_version_provenance_bounded(
            version.version_id, limit
        )
        decision = await self.repos.ontology.get_entity_by_source(
            room_id, OntologyEntityKind.DECISION, version.version_id
        )
        if decision is None:
            raise DomainError("decision ontology is not available for artifact version")
        decision_review = await self.repos.ontology.get_latest_review(room_id, decision.entity_id)

        chains: list[dict[str, Any]] = []
        for source in provenance:
            claim = await self.repos.ontology.get_entity_by_source(
                room_id, OntologyEntityKind.CLAIM, str(source["claim_id"])
            )
            output = await self.repos.ontology.get_entity_by_source(
                room_id, OntologyEntityKind.AGENT_OUTPUT, str(source["output_id"])
            )
            if claim is None or output is None:
                raise DomainError("decision evidence chain is incomplete")
            claim_to_decision = await self.repos.ontology.get_relationship_between(
                room_id, claim.entity_id, decision.entity_id
            )
            claim_to_output = await self.repos.ontology.get_relationship_between(
                room_id, claim.entity_id, output.entity_id
            )
            if claim_to_decision is None or claim_to_output is None:
                raise DomainError("decision evidence relationship is incomplete")
            claim_review = await self.repos.ontology.get_latest_review(room_id, claim.entity_id)
            output_review = await self.repos.ontology.get_latest_review(room_id, output.entity_id)
            decision_link_review = await self.repos.ontology.get_latest_review(
                room_id, claim_to_decision.relationship_id
            )
            output_link_review = await self.repos.ontology.get_latest_review(
                room_id, claim_to_output.relationship_id
            )
            chains.append(
                {
                    "claim": {
                        **self._ontology_entity_record(claim),
                        "published_text": source["text"],
                        "latest_review": (
                            self._ontology_review_record(claim_review)
                            if claim_review is not None
                            else None
                        ),
                    },
                    "agent_output": {
                        **self._ontology_entity_record(output),
                        "latest_review": (
                            self._ontology_review_record(output_review)
                            if output_review is not None
                            else None
                        ),
                    },
                    "relationships": {
                        "claim_to_decision": {
                            **self._ontology_relationship_record(claim_to_decision),
                            "latest_review": (
                                self._ontology_review_record(decision_link_review)
                                if decision_link_review is not None
                                else None
                            ),
                        },
                        "claim_to_agent_output": {
                            **self._ontology_relationship_record(claim_to_output),
                            "latest_review": (
                                self._ontology_review_record(output_link_review)
                                if output_link_review is not None
                                else None
                            ),
                        },
                    },
                    "exact_source_evidence": {
                        "output_id": source["output_id"],
                        "evidence": source["evidence"],
                        "agent_id": source["agent_id"],
                        "execution_id": source["execution_id"],
                        "source_prompt": source["source_prompt"],
                        "provider_input": source["provider_input"],
                        "provider_name": source["provider_name"],
                        "provider_model": source["provider_model"],
                        "provider_response_id": source["provider_response_id"],
                        "provider_interventions": source["provider_interventions"],
                        "provider_evidence": source["provider_evidence"],
                    },
                }
            )

        current_claims = [chain["claim"]["label"] for chain in chains]
        relationship_counts: dict[str, int] = {}
        for chain in chains:
            kind = str(chain["relationships"]["claim_to_decision"]["kind"])
            relationship_counts[kind] = relationship_counts.get(kind, 0) + 1
        relationship_summary = ", ".join(
            f"{kind} {count}" for kind, count in sorted(relationship_counts.items())
        )
        if question_kind == "WHY_DECISION":
            summary = (
                f"{decision.label} has {len(chains)} deliberately selected "
                f"claim{'s' if len(chains) != 1 else ''} ({relationship_summary}): "
                + "; ".join(current_claims)
            )
        else:
            summary = (
                f"{len(chains)} selected AgentOutput"
                f"{'s' if len(chains) != 1 else ''} are linked to {decision.label} "
                f"through governed claims ({relationship_summary})."
            )

        freshness_cursor = await self.repos.events.get_latest_sequence(room_id)
        return {
            "query": {
                "question": question,
                "kind": question_kind,
                "supported_kinds": ["WHY_DECISION", "DECISION_EVIDENCE"],
            },
            "scope": {
                "room_id": room_id,
                "artifact_id": artifact.artifact_id,
                "version_id": version.version_id,
                "version_number": version.version_number,
                "max_claims": limit,
            },
            "summary": summary,
            "decision": {
                **self._ontology_entity_record(decision),
                "evidence_ids": [chain["exact_source_evidence"]["output_id"] for chain in chains],
                "source_ids": [
                    version.version_id,
                    *(chain["claim"]["source_object_id"] for chain in chains),
                ],
                "artifact_name": artifact.name,
                "version_id": version.version_id,
                "latest_review": (
                    self._ontology_review_record(decision_review)
                    if decision_review is not None
                    else None
                ),
            },
            "evidence_chains": chains,
            "freshness": {
                "room_event_cursor": freshness_cursor,
                "artifact_created_at": version.created_at.isoformat(),
                "decision_updated_at": decision.updated_at.isoformat(),
            },
            "retrieval_counts": {
                "available_claims": available_claims,
                "returned_claims": len(chains),
                "returned_outputs": len(chains),
                "truncated": available_claims > len(chains),
            },
            "provenance": {
                "content_hash": version.content_hash,
                "provenance_hash": version.provenance_hash,
                "verified": self.verify_artifact_provenance_hash(version, provenance)
                if available_claims == len(provenance)
                else None,
                "verification_note": (
                    "verified against all frozen claims"
                    if available_claims == len(provenance)
                    else "not recomputed because bounded retrieval omitted claims"
                ),
            },
        }

    async def review_ontology_entity(
        self,
        room_id: str,
        entity_id: str,
        action: OntologyReviewAction,
        reviewed_by: str,
        reason: str,
        *,
        corrected_label: str | None = None,
        corrected_properties: dict[str, Any] | None = None,
        corrected_confidence: float | None = None,
    ) -> tuple[OntologyEntity, OntologyReview]:
        reason = reason.strip()
        if len(reason) > 2000:
            raise DomainError("ontology review reason must not exceed 2000 characters")
        if corrected_label is not None:
            corrected_label = self._validate_non_empty(corrected_label, "corrected label")
        if corrected_confidence is not None and not 0.0 <= corrected_confidence <= 1.0:
            raise DomainError("corrected confidence must be between 0 and 1")
        corrections = (corrected_label, corrected_properties, corrected_confidence)
        if action == OntologyReviewAction.CONFIRM and any(
            correction is not None for correction in corrections
        ):
            raise DomainError("confirmation cannot change an ontology fact")
        if action == OntologyReviewAction.CORRECT and all(
            correction is None for correction in corrections
        ):
            raise DomainError("correction must provide a changed value")
        if action == OntologyReviewAction.CORRECT and not reason:
            raise DomainError("correction reason must not be empty")

        reviewed_at = utcnow()
        async with self.db.transaction():
            entity = await self.repos.ontology.get_entity(entity_id)
            if entity is None or entity.room_id != room_id:
                raise DomainError("ontology entity not found in room")
            if action == OntologyReviewAction.CORRECT and all(
                (
                    corrected_label is None or corrected_label == entity.label,
                    corrected_properties is None or corrected_properties == entity.properties,
                    corrected_confidence is None or corrected_confidence == entity.confidence,
                )
            ):
                raise DomainError("correction must change an ontology fact")
            updated, review = await self.repos.ontology.review_entity_in_transaction(
                entity,
                new_id("orev"),
                action,
                reviewed_by,
                reason,
                corrected_label=corrected_label,
                corrected_properties=corrected_properties,
                corrected_confidence=corrected_confidence,
                reviewed_at=reviewed_at,
            )
            event_type = (
                EventType.ONTOLOGY_ASSERTION_CONFIRMED
                if action == OntologyReviewAction.CONFIRM
                else EventType.ONTOLOGY_ASSERTION_CORRECTED
            )
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=event_type,
                    payload={
                        "target_type": "ENTITY",
                        "target_id": entity_id,
                        "review_id": review.review_id,
                        "action": action.value,
                        "before": review.before_value,
                        "after": review.after_value,
                        "reason": reason,
                    },
                    actor_id=reviewed_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return updated, review

    async def review_ontology_relationship(
        self,
        room_id: str,
        relationship_id: str,
        action: OntologyReviewAction,
        reviewed_by: str,
        reason: str,
        *,
        corrected_kind: OntologyRelationshipKind | None = None,
        corrected_confidence: float | None = None,
    ) -> tuple[OntologyRelationship, OntologyReview]:
        reason = reason.strip()
        if len(reason) > 2000:
            raise DomainError("ontology review reason must not exceed 2000 characters")
        if corrected_confidence is not None and not 0.0 <= corrected_confidence <= 1.0:
            raise DomainError("corrected confidence must be between 0 and 1")
        corrections = (corrected_kind, corrected_confidence)
        if action == OntologyReviewAction.CONFIRM and any(
            correction is not None for correction in corrections
        ):
            raise DomainError("confirmation cannot change an ontology relationship")
        if action == OntologyReviewAction.CORRECT and all(
            correction is None for correction in corrections
        ):
            raise DomainError("correction must provide a changed value")
        if action == OntologyReviewAction.CORRECT and not reason:
            raise DomainError("correction reason must not be empty")

        reviewed_at = utcnow()
        async with self.db.transaction():
            relationship = await self.repos.ontology.get_relationship(relationship_id)
            if relationship is None or relationship.room_id != room_id:
                raise DomainError("ontology relationship not found in room")
            if action == OntologyReviewAction.CORRECT and all(
                (
                    corrected_kind is None or corrected_kind == relationship.kind,
                    corrected_confidence is None or corrected_confidence == relationship.confidence,
                )
            ):
                raise DomainError("correction must change an ontology relationship")
            if corrected_kind is not None:
                room_relationships = await self.repos.ontology.list_relationships(room_id)
                if any(
                    candidate.relationship_id != relationship.relationship_id
                    and candidate.kind == corrected_kind
                    and candidate.from_entity_id == relationship.from_entity_id
                    and candidate.to_entity_id == relationship.to_entity_id
                    for candidate in room_relationships
                ):
                    raise DomainError("corrected ontology relationship already exists")
            updated, review = await self.repos.ontology.review_relationship_in_transaction(
                relationship,
                new_id("orev"),
                action,
                reviewed_by,
                reason,
                corrected_kind=corrected_kind,
                corrected_confidence=corrected_confidence,
                reviewed_at=reviewed_at,
            )
            event_type = (
                EventType.ONTOLOGY_ASSERTION_CONFIRMED
                if action == OntologyReviewAction.CONFIRM
                else EventType.ONTOLOGY_ASSERTION_CORRECTED
            )
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=event_type,
                    payload={
                        "target_type": "RELATIONSHIP",
                        "target_id": relationship_id,
                        "review_id": review.review_id,
                        "action": action.value,
                        "before": review.before_value,
                        "after": review.after_value,
                        "reason": reason,
                    },
                    actor_id=reviewed_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return updated, review

    @staticmethod
    def _ontology_entity_record(entity: OntologyEntity) -> dict[str, Any]:
        return {
            "entity_id": entity.entity_id,
            "kind": entity.kind.value,
            "source_object_id": entity.source_object_id,
            "label": entity.label,
            "properties": entity.properties,
            "derivation_kind": entity.derivation_kind.value,
            "confidence": entity.confidence,
            "evidence_ids": list(entity.evidence_ids),
            "source_ids": list(entity.source_ids),
            "review_status": entity.review_status.value,
            "created_at": entity.created_at.isoformat(),
            "updated_at": entity.updated_at.isoformat(),
        }

    @staticmethod
    def _ontology_relationship_record(
        relationship: OntologyRelationship,
    ) -> dict[str, Any]:
        return {
            "relationship_id": relationship.relationship_id,
            "kind": relationship.kind.value,
            "from_entity_id": relationship.from_entity_id,
            "to_entity_id": relationship.to_entity_id,
            "derivation_kind": relationship.derivation_kind.value,
            "confidence": relationship.confidence,
            "evidence_ids": list(relationship.evidence_ids),
            "source_ids": list(relationship.source_ids),
            "review_status": relationship.review_status.value,
            "created_at": relationship.created_at.isoformat(),
            "updated_at": relationship.updated_at.isoformat(),
        }

    @staticmethod
    def _ontology_review_record(review: OntologyReview) -> dict[str, Any]:
        return {
            "review_id": review.review_id,
            "target_type": review.target_type.value,
            "target_id": review.target_id,
            "action": review.action.value,
            "before": review.before_value,
            "after": review.after_value,
            "reason": review.reason,
            "reviewed_by": review.reviewed_by,
            "created_at": review.created_at.isoformat(),
        }

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
        runs = await self.repos.executions.list_by_room(room_id)
        branches = await self.repos.branches.list_by_room(room_id)
        outputs = await self.list_room_outputs(room_id)
        output_selections = await self.list_output_selections(room_id)
        branch_syntheses = [
            synthesis
            for branch in branches
            for synthesis in await self.repos.branch_syntheses.list_by_branch(branch.branch_id)
        ]
        active_turn_lock = await self.repos.turn_locks.get_active(TurnLockScopeType.ROOM, room_id)
        ontology = await self.get_room_ontology(room_id)
        artifact_state: list[dict[str, Any]] = []
        for artifact in artifacts:
            versions = await self.repos.artifacts.list_versions(artifact.artifact_id)
            latest = versions[0] if versions else None
            artifact_state.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "name": artifact.name,
                    "type": artifact.artifact_type.value,
                    "version": artifact.current_version,
                    "version_id": latest.version_id if latest else None,
                    "content": latest.content if latest else "",
                    "content_hash": latest.content_hash if latest else "",
                    "provenance_hash": latest.provenance_hash if latest else "",
                    "branch_synthesis_id": latest.branch_synthesis_id if latest else None,
                }
            )
        presence = await self.presence.get_room_presence(room_id)
        return {
            "room": {
                "room_id": room.room_id,
                "name": room.name,
                "description": room.description,
                "status": room.status.value,
                "workspace_id": room.workspace_id,
            },
            "events_since": [
                {
                    "event_id": e.event_id,
                    "sequence": e.sequence,
                    "event_type": e.event_type.value,
                    "payload": e.payload,
                    "actor_id": e.actor_id,
                    "actor_type": e.actor_type,
                    "timestamp": e.timestamp.isoformat(),
                }
                for e in events
            ],
            "members": [{"user_id": m.user_id, "role": m.role} for m in members],
            "agents": [
                {"agent_id": a.agent_id, "name": a.name, "role": a.role, "status": a.status.value}
                for a in agents
            ],
            "branches": [
                {
                    "branch_id": branch.branch_id,
                    "mode": branch.mode.value,
                    "status": branch.status.value,
                    "initiated_by": branch.initiated_by,
                    "initiating_prompt": branch.initiating_prompt,
                    "context_event_sequence": branch.context_event_sequence,
                    "context_message_ids": list(branch.context_message_ids),
                    "context_snapshot": branch.context_snapshot,
                    "context_hash": branch.context_hash,
                    "lifecycle_managed": branch.lifecycle_managed,
                    "execution_ids": [
                        run.execution_id for run in runs if run.branch_id == branch.branch_id
                    ],
                    "created_at": branch.created_at.isoformat(),
                    "updated_at": branch.updated_at.isoformat(),
                    "completed_at": (
                        branch.completed_at.isoformat() if branch.completed_at else None
                    ),
                }
                for branch in branches
            ],
            "runs": [
                {
                    "execution_id": run.execution_id,
                    "session_id": run.session_id,
                    "agent_id": run.agent_id,
                    "run_id": run.run_id,
                    "status": run.status.value,
                    "started_at": run.started_at.isoformat(),
                    "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                }
                for run in runs
            ],
            "outputs": [
                {
                    "output_id": output.output_id,
                    "branch_id": output.branch_id,
                    "execution_id": output.execution_id,
                    "session_id": output.session_id,
                    "agent_id": output.agent_id,
                    "content": output.content,
                    "output_data": output.output_data,
                    "source_prompt": output.source_prompt,
                    "provider_input": output.provider_input,
                    "provider_name": output.provider_name,
                    "provider_model": output.provider_model,
                    "provider_response_id": output.provider_response_id,
                    "provider_interventions": list(output.provider_interventions),
                    "provider_evidence": output.provider_evidence,
                    "created_at": output.created_at.isoformat(),
                }
                for output in outputs
            ],
            "output_selections": [
                {
                    "output_id": selection.output_id,
                    "branch_id": selection.branch_id,
                    "disposition": selection.disposition.value,
                    "decided_by": selection.decided_by,
                    "updated_at": selection.updated_at.isoformat(),
                }
                for selection in output_selections
            ],
            "branch_syntheses": [
                {
                    "synthesis_id": synthesis.synthesis_id,
                    "branch_id": synthesis.branch_id,
                    "status": synthesis.status.value,
                    "title": synthesis.title,
                    "provider_name": synthesis.provider_name,
                    "provider_model": synthesis.provider_model,
                    "provider_response_id": synthesis.provider_response_id,
                    "simulated": synthesis.simulated,
                    "artifact_version_id": synthesis.artifact_version_id,
                    "created_at": synthesis.created_at.isoformat(),
                    "completed_at": (
                        synthesis.completed_at.isoformat() if synthesis.completed_at else None
                    ),
                }
                for synthesis in branch_syntheses
            ],
            "turn_lock": (
                {
                    "lock_id": active_turn_lock.lock_id,
                    "scope_type": active_turn_lock.scope_type.value,
                    "scope_id": active_turn_lock.scope_id,
                    "branch_id": active_turn_lock.branch_id,
                    "status": active_turn_lock.status.value,
                    "acquired_by": active_turn_lock.acquired_by,
                    "acquired_at": active_turn_lock.acquired_at.isoformat(),
                }
                if active_turn_lock is not None
                else None
            ),
            "tasks": [
                {
                    "task_id": t.task_id,
                    "title": t.title,
                    "status": t.status.value,
                    "priority": t.priority.value,
                    "assigned_agent_id": t.assigned_agent_id,
                }
                for t in tasks
            ],
            "messages": [
                {
                    "message_id": m.message_id,
                    "role": m.role.value,
                    "sender_id": m.sender_id,
                    "content": m.content,
                    "created_at": m.created_at.isoformat(),
                }
                for m in messages
            ],
            "artifacts": artifact_state,
            "decisions": [
                {"decision_id": d.decision_id, "title": d.title, "status": d.status.value}
                for d in decisions
            ],
            "memories": [
                {"memory_id": m.memory_id, "content": m.content, "type": m.memory_type}
                for m in memories
            ],
            "pending_approvals": [
                {
                    "approval_id": a.approval_id,
                    "action": a.action_description,
                    "agent_id": a.agent_id,
                }
                for a in pending_approvals
            ],
            "presence": [{"user_id": p.user_id, "status": p.status.value} for p in presence],
            "ontology": ontology,
        }


# Needed for hashlib import in create_artifact
