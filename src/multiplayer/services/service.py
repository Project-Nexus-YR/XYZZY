"""Core service layer: orchestrates domain operations across repos, events, and NEXUS."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..db.connection import Database
from ..db.repositories import Repos
from ..domain.events import EventType, RoomEvent
from ..domain.meta import (
    DECISION_KINDS,
    MetaAnswerStatus,
    MetaQuestionKind,
    MetaRefusalReason,
    OntologyAssurance,
    classify_meta_question,
    invalidation_class,
)
from ..domain.models import (
    MAX_THREAD_DEPTH,
    AddressingMode,
    AgentAddressing,
    AgentIdentity,
    AgentInstance,
    AgentOutput,
    AgentRoomMembership,
    AgentRun,
    AgentStatus,
    AgentTemplate,
    AgentTrigger,
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
    ExecutionIntervention,
    ExecutionStatus,
    HarnessState,
    IdempotencyConflict,
    IdempotencyRecord,
    Memory,
    MemoryScope,
    MentionTargetType,
    Message,
    MessageMention,
    MessageReaction,
    MessageRole,
    Notification,
    OntologyDerivationKind,
    OntologyEntity,
    OntologyEntityKind,
    OntologyExtractor,
    OntologyRelationship,
    OntologyRelationshipKind,
    OntologyReview,
    OntologyReviewAction,
    OntologyReviewStatus,
    Organization,
    OrgMember,
    OutputDisposition,
    OutputSelection,
    ParticipantType,
    ProofMode,
    ReadCursor,
    Room,
    RoomMember,
    RoomParticipantHandle,
    RunSettlement,
    SearchHit,
    Session,
    SessionStatus,
    Task,
    TaskPriority,
    TaskStatus,
    ThreadReply,
    ThreadSummary,
    ToolRequest,
    TurnLock,
    TurnLockScopeType,
    TurnLockStatus,
    Workspace,
    WorkspaceMember,
    handle_from_display_name,
    new_id,
    utcnow,
)
from ..domain.provenance import calculate_artifact_provenance_hash
from ..domain.synthesis import (
    RESERVED_ARTIFACT_NAMES,
    SynthesisSpec,
    SynthesisType,
    spec_for,
)
from ..domain.synthesis import (
    render as render_synthesis,
)
from ..harness import (
    KNOWN_HARNESS_IDS,
    NEXUS_HARNESS_ID,
    AgentHarness,
    HarnessError,
    ModelProviderHarness,
    NexusHarness,
    NexusLaunch,
    PromptRequest,
    RunContext,
    SessionHandle,
    SessionUpdate,
    StopReason,
)
from ..harness.adapters import MODEL_PROVIDER_HARNESS_ID
from ..model_providers import ModelProviderError
from ..nexus_bridge.agent_bridge import NexusAgentBridge
from ..realtime.hub import RealtimeHub
from ..security.authorization import (
    AuthorizationError,
    RoomCapability,
    RoomPolicy,
    capabilities_for_role,
)
from ..security.capabilities import (
    CapabilityTerms,
    GatewayDecision,
    RunAuthorization,
    allowed_tools,
    decide,
    may_address,
    policy_capabilities,
    user_capabilities,
)
from ..security.identity import (
    credential_hash,
    credential_matches,
    new_launch_challenge,
    new_run_credential,
    verify_challenge_answer,
)
from ..services.presence import PresenceService

log = logging.getLogger(__name__)

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


class MultiplayerService:
    def __init__(
        self, db: Database, hub: RealtimeHub, known_users: frozenset[str] | None = None
    ) -> None:
        self.db = db
        self.repos = Repos(db)
        self.hub = hub
        self.presence = PresenceService()
        self.nexus = NexusAgentBridge(db_path=":memory:")
        self.authorization = RoomPolicy(self.repos)
        # Principals the server authenticates; an invitation must name one of them
        # or a user row that bootstrapping already created.
        self.known_users = known_users or frozenset()
        self._running_executions: dict[str, asyncio.Task[None]] = {}
        # Per-run bearer credentials in plaintext, held only until the harness that
        # will use them is opened. The durable row keeps a SHA-256 hash and nothing
        # else, so a credential never outlives the process that issued it.
        self._run_credentials: dict[str, str] = {}
        # Identifies this dispatcher's claims on runs, so another process can tell
        # a run somebody is dispatching from one nobody ever picked up.
        self._dispatch_claim = new_id("dispatch")
        # One in-process lease per room, so two drains never do the same pass twice.
        self._ontology_drains: set[str] = set()

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
        await self._backfill_participant_handles()
        # Objects written before their kind joined the search allowlist.
        await self.repos.search.backfill()
        await self._seed_default_templates()
        await self._settle_orphaned_mention_runs()
        await self.sweep_expired_run_leases()

    async def _settle_orphaned_mention_runs(self) -> None:
        """Settle mention runs whose dispatcher died before it could claim them.

        A mention run is committed PENDING and claimed by its dispatcher immediately
        after that commit. A process that dies in between leaves a run nothing will
        ever pick up, and only that run is an orphan: a claimed run belongs to a
        dispatcher that is working on it, and settling it here would destroy healthy
        work another process is doing. The sweep therefore reads only unclaimed runs
        and writes conditionally, so it loses every race it enters rather than
        winning one it should not. Restarting the turn instead would replay a
        question the room has probably moved past; the author can address the agent
        again.
        """
        orphans = await self.repos.executions.list_unclaimed_pending_by_trigger(
            AgentTrigger.MENTION
        )
        for orphan in orphans:
            await self._settle_undispatched_run(
                orphan.execution_id, "dispatcher stopped before the run started"
            )

    async def _backfill_legacy_artifact_provenance_hashes(self) -> None:
        """Bind pre-migration snapshots using the best evidence available at upgrade time."""
        versions = await self.repos.artifacts.list_versions_without_provenance_hash()
        for version in versions:
            claims = await self.repos.artifacts.get_version_provenance(version.version_id)
            provenance_hash = self._artifact_provenance_hash(version, claims)
            await self.repos.artifacts.set_provenance_hash_if_empty(
                version.version_id, provenance_hash
            )

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

    async def _backfill_participant_handles(self) -> None:
        """Address the participants who joined before handles existed.

        Rows arrive in a fixed order so that two rooms upgrading from the same state
        end up with the same handles, including which of two colliding names got the
        bare one.
        """
        for row in await self.repos.handles.list_participants_without_handles():
            await self._issue_handle(
                str(row["room_id"]),
                ParticipantType(str(row["participant_type"])),
                str(row["participant_id"]),
                str(row["display_name"]),
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
            await self._issue_handle(room.room_id, ParticipantType.USER, user_id, user_id)
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
        await self._issue_handle(room.room_id, ParticipantType.USER, creator_id, creator_id)
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

    async def _is_known_user(self, user_id: str) -> bool:
        """Invitations name accounts: a configured principal or a bootstrapped user row."""
        if user_id in self.known_users:
            return True
        return await self.repos.users.get(user_id) is not None

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
        invited_user_id = self._validate_id(invited_user_id, "user id")
        member = RoomMember(room_id=room_id, user_id=invited_user_id, role=role)
        async with self.db.transaction():
            # Serializing the read and the insert turns a concurrent duplicate invite
            # into a clean rejection rather than a UNIQUE-constraint failure, and the
            # recheck fences out an inviter demoted after the route authorized them.
            await self._require_mutate_in_transaction(room_id, invited_by)
            if not await self._is_known_user(invited_user_id):
                raise DomainError("no account with that user id")
            if await self.repos.room_members.get(room_id, invited_user_id) is not None:
                raise DomainError("user is already a channel member")
            await self.repos.room_members.add(member)
            await self._issue_handle(
                room_id, ParticipantType.USER, invited_user_id, invited_user_id
            )
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.USER_INVITED_ROOM,
                    payload={"user_id": invited_user_id, "role": role},
                    actor_id=invited_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        # The invitee is not subscribed to this room yet; tell their open sockets
        # directly so the channel appears in their sidebar without a reload.
        room = await self.repos.rooms.get(room_id)
        await self.hub.send_to_user(
            invited_user_id,
            {
                "type": "room_invited",
                "room_id": room_id,
                "room_name": room.name if room else room_id,
                "role": role,
            },
        )
        return member

    async def leave_room(self, room_id: str, user_id: str) -> None:
        """Give up membership durably: the row and the event commit together."""
        async with self.db.transaction():
            member = await self.repos.room_members.get(room_id, user_id)
            if member is None:
                raise DomainError("user is not a channel member")
            if member.role == "admin":
                others = [
                    other
                    for other in await self.repos.room_members.list(room_id)
                    if other.user_id != user_id
                ]
                if others and not any(other.role == "admin" for other in others):
                    raise DomainError("the last admin cannot leave while others remain")
            await self.repos.room_members.remove(room_id, user_id)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.USER_LEFT_ROOM,
                    payload={"user_id": user_id, "role": member.role},
                    actor_id=user_id,
                    actor_type="user",
                )
            )
        await self.hub.revoke_room_access(user_id, room_id)
        await self.presence.user_left(user_id, room_id)
        await self._broadcast_persisted_events([event])

    async def get_room_members(self, room_id: str) -> list[RoomMember]:
        return await self.repos.room_members.list(room_id)

    async def update_room_member_role(
        self, room_id: str, user_id: str, role: str, changed_by: str
    ) -> RoomMember:
        """Change a non-admin member's access; admins are immutable here and use leave_room."""
        if role not in {"viewer", "editor"}:
            raise DomainError("member role must be viewer or editor")
        if user_id == changed_by:
            raise DomainError("use leave to change your own membership")
        async with self.db.transaction():
            member = await self.repos.room_members.get(room_id, user_id)
            if member is None:
                raise DomainError("user is not a channel member")
            if member.role == "admin":
                raise DomainError("admin membership cannot be changed here")
            if member.role == role:
                return member
            await self.repos.room_members.update_role(room_id, user_id, role)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.USER_ROLE_CHANGED,
                    payload={"user_id": user_id, "role": role, "previous_role": member.role},
                    actor_id=changed_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return replace(member, role=role)

    async def set_room_policy(
        self, room_id: str, allowed: list[str] | None, changed_by: str
    ) -> None:
        """Bound every run in this channel to a capability list. None lifts the bound."""
        stored = _policy_json(allowed)
        async with self.db.transaction():
            await self._require_capability_in_transaction(
                room_id, changed_by, RoomCapability.ADMINISTER
            )
            await self.repos.rooms.set_allowed_capabilities(room_id, stored)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.ROOM_POLICY_UPDATED,
                    payload={"allowed_capabilities": allowed},
                    actor_id=changed_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])

    async def set_member_capabilities(
        self, room_id: str, user_id: str, allowed: list[str] | None, changed_by: str
    ) -> None:
        """Bound what one member may lend to the agents they run. None restores the role default."""
        stored = _policy_json(allowed)
        async with self.db.transaction():
            await self._require_capability_in_transaction(
                room_id, changed_by, RoomCapability.ADMINISTER
            )
            member = await self.repos.room_members.get(room_id, user_id)
            if member is None:
                raise DomainError("user is not a channel member")
            await self.repos.room_members.set_allowed_capabilities(room_id, user_id, stored)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.ROOM_POLICY_UPDATED,
                    payload={"user_id": user_id, "allowed_capabilities": allowed},
                    actor_id=changed_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])

    async def set_workspace_policy(
        self, workspace_id: str, allowed: list[str] | None, changed_by: str
    ) -> None:
        """Bound every channel in the workspace. Logged in each of its rooms."""
        await self.authorization.require_workspace_member(workspace_id, changed_by)
        stored = _policy_json(allowed)
        await self.repos.workspaces.set_allowed_capabilities(workspace_id, stored)
        for room in await self.repos.rooms.list_by_workspace(workspace_id):
            await self._append_room_event(
                room.room_id,
                EventType.WORKSPACE_POLICY_UPDATED,
                {"workspace_id": workspace_id, "allowed_capabilities": allowed},
                changed_by,
                "user",
            )

    async def remove_room_member(self, room_id: str, user_id: str, removed_by: str) -> None:
        """Revoke a non-admin member's access, including any live realtime subscription."""
        if user_id == removed_by:
            raise DomainError("use leave to remove yourself")
        async with self.db.transaction():
            member = await self.repos.room_members.get(room_id, user_id)
            if member is None:
                raise DomainError("user is not a channel member")
            if member.role == "admin":
                raise DomainError("admin membership cannot be removed here")
            await self.repos.room_members.remove(room_id, user_id)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.USER_REMOVED_ROOM,
                    payload={"user_id": user_id, "role": member.role},
                    actor_id=removed_by,
                    actor_type="user",
                )
            )
        await self.hub.revoke_room_access(user_id, room_id)
        # Their subscriptions to this room are gone; reach their other open sockets.
        await self.hub.send_to_user(user_id, {"type": "room_removed", "room_id": room_id})
        await self.presence.user_left(user_id, room_id)
        await self._broadcast_persisted_events([event])

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
        *,
        requested_by: str = "",
        require_member: bool = False,
        harness_id: str = NEXUS_HARNESS_ID,
        addressing_mode: AddressingMode = AddressingMode.ANYONE,
    ) -> AgentInstance:
        template = await self.repos.agents.get_template(template_id)
        if not template:
            raise DomainError(f"agent template not found: {template_id}")
        if harness_id not in KNOWN_HARNESS_IDS:
            raise DomainError(f"no harness is registered as {harness_id!r}")
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
            harness_id=harness_id,
        )
        room = await self.repos.rooms.get(room_id)
        identity = AgentIdentity(
            identity_id=new_id("ident"),
            agent_id=agent.agent_id,
            proof_mode=ProofMode.IN_PROCESS,
        )
        # An agent spawned into a shared channel answers that channel; narrowing it is
        # an explicit ADMINISTER act. The room membership and the capability
        # intersection still bound what any of them can make it do.
        addressing = AgentAddressing(
            agent_id=agent.agent_id,
            room_id=room_id,
            mode=addressing_mode,
            owner_user_id=requested_by or (room.created_by if room is not None else ""),
            updated_by=requested_by or "system",
        )
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(room_id, requested_by)
            await self.repos.agents.create_instance(agent)
            await self.repos.agents.add_room_membership(
                AgentRoomMembership(agent_id=agent.agent_id, room_id=room_id)
            )
            await self.repos.agent_identities.create_in_transaction(identity)
            await self.repos.agent_addressing.upsert_in_transaction(addressing)
            handle = await self._issue_handle(
                room_id, ParticipantType.AGENT, agent.agent_id, agent.name
            )
            events = [
                await self.repos.events.append_with_next_sequence_in_transaction(
                    RoomEvent(
                        room_id=room_id,
                        sequence=0,
                        event_type=EventType.AGENT_JOINED_ROOM,
                        payload={
                            "agent_id": agent.agent_id,
                            "name": agent.name,
                            "handle": handle,
                            "role": agent.role,
                        },
                        actor_id=agent.agent_id,
                        actor_type="agent",
                    )
                ),
                await self.repos.events.append_with_next_sequence_in_transaction(
                    RoomEvent(
                        room_id=room_id,
                        sequence=0,
                        event_type=EventType.AGENT_IDENTITY_REGISTERED,
                        payload={
                            "agent_id": agent.agent_id,
                            "identity_id": identity.identity_id,
                            "proof_mode": identity.proof_mode.value,
                            "harness_id": harness_id,
                        },
                        actor_id=agent.agent_id,
                        actor_type="agent",
                    )
                ),
            ]
        await self._broadcast_persisted_events(events)
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

    # ── Agent identity, addressing, and the run envelope ─────────────────────

    def _harness(self, harness_id: str) -> AgentHarness:
        """The harness that runs this agent's turns. An unknown id has none."""
        if harness_id == NEXUS_HARNESS_ID:
            return NexusHarness(self.nexus, self._resolve_nexus_launch)
        if harness_id == MODEL_PROVIDER_HARNESS_ID:
            return ModelProviderHarness(self.nexus.model_provider)
        raise KeyError(harness_id)

    async def _resolve_nexus_launch(self, run_id: str) -> NexusLaunch:
        """The durable records a bridge run is opened from, read by run id."""
        run = await self.repos.agent_runs.get(run_id)
        # A run written before this envelope existed is addressed by its execution id.
        execution_id = run.execution_id if run is not None else run_id
        execution = await self.repos.executions.get(execution_id)
        if execution is None:
            raise DomainError(f"agent run {run_id} names no execution")
        session = await self.repos.sessions.get(execution.session_id)
        if session is None:
            raise DomainError("session not found")
        return NexusLaunch(await self.get_agent(execution.agent_id), session, execution)

    async def get_agent_identity(self, agent_id: str) -> AgentIdentity:
        identity = await self.repos.agent_identities.get_for_agent(agent_id)
        if identity is None:
            raise DomainError(f"agent identity not found: {agent_id}")
        return identity

    async def get_agent_addressing(self, agent_id: str) -> AgentAddressing:
        addressing = await self.repos.agent_addressing.get(agent_id)
        if addressing is None:
            raise DomainError(f"agent addressing not found: {agent_id}")
        return addressing

    async def set_agent_addressing(
        self,
        agent_id: str,
        mode: AddressingMode,
        updated_by: str,
        *,
        owner_user_id: str | None = None,
        allowlist: frozenset[str] = frozenset(),
        require_member: bool = False,
    ) -> AgentAddressing:
        """Who may point this agent. Room ADMINISTER, because it is a grant."""
        agent = await self.get_agent(agent_id)
        current = await self.repos.agent_addressing.get(agent_id)
        addressing = AgentAddressing(
            agent_id=agent_id,
            room_id=agent.room_id,
            mode=mode,
            owner_user_id=owner_user_id
            or (current.owner_user_id if current is not None else updated_by),
            allowlist=allowlist,
            updated_by=updated_by,
        )
        async with self.db.transaction():
            if require_member:
                await self._require_capability_in_transaction(
                    agent.room_id, updated_by, RoomCapability.ADMINISTER
                )
            await self.repos.agent_addressing.upsert_in_transaction(addressing)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=agent.room_id,
                    sequence=0,
                    event_type=EventType.AGENT_ADDRESSING_UPDATED,
                    payload={
                        "agent_id": agent_id,
                        "mode": mode.value,
                        "owner_user_id": addressing.owner_user_id,
                        "allowlist": sorted(allowlist),
                    },
                    actor_id=updated_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return addressing

    async def revoke_agent_identity(
        self, agent_id: str, revoked_by: str, *, require_member: bool = False
    ) -> None:
        """Revoke once, not per run: no later run of this agent may launch."""
        agent = await self.get_agent(agent_id)
        if require_member:
            await self.authorization.require(agent.room_id, revoked_by, RoomCapability.ADMINISTER)
        if not await self.repos.agent_identities.revoke(agent_id, utcnow()):
            return
        await self._append_room_event(
            agent.room_id,
            EventType.AGENT_IDENTITY_REVOKED,
            {"agent_id": agent_id, "revoked_by": revoked_by},
            revoked_by,
            "user",
        )

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

    async def _renew_run_lease(self, update: SessionUpdate) -> None:
        """The streaming callback is the run's heartbeat: every update renews its lease."""
        run = await self.repos.agent_runs.get(update.run_id)
        if run is None or run.harness_state is HarnessState.SETTLED:
            return
        await self.repos.agent_runs.advance(
            run.run_id, run.harness_state, utcnow() + _STREAMING_LEASE, run.acting_user_id
        )

    async def _advance_run_for_execution(
        self, execution_id: str, state: HarnessState, acting_user_id: str, lease: timedelta
    ) -> None:
        """Move the envelope and renew its lease. A settled run never moves."""
        run = await self.repos.agent_runs.get_by_execution(execution_id)
        if run is None or run.harness_state is HarnessState.SETTLED:
            return
        await self.repos.agent_runs.advance(
            run.run_id, state, utcnow() + lease, acting_user_id or run.acting_user_id
        )

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
        await self._broadcast_persisted_events(events)
        return True

    async def sweep_expired_run_leases(self) -> int:
        """Settle every run whose lease ran out, so none sits unclaimed by anything.

        A run picked up its full allowance of attempts that died every time is PARKED
        rather than ORPHANED. Both are terminal; the difference is what a reader is
        told about why nothing is coming, which is the whole point of settling it.
        """
        settled = 0
        for run in await self.repos.agent_runs.list_expired(utcnow()):
            settlement = (
                RunSettlement.PARKED if run.attempts >= run.max_attempts else RunSettlement.ORPHANED
            )
            if await self._settle_run(
                run, settlement, "system", f"lease expired after {run.attempts} attempt(s)"
            ):
                settled += 1
        return settled

    async def remove_agent_from_room(
        self, agent_id: str, room_id: str, removed_by: str, *, require_member: bool = False
    ) -> None:
        """Take an agent out of a room and settle everything it had in flight.

        Settlement is decided here and telling the harness is best-effort, so an
        in-flight turn can still land. What stops it writing is the settled-run refusal
        inside complete_execution, not the credential.
        """
        agent = await self.get_agent(agent_id)
        if agent.room_id != room_id:
            raise DomainError("agent is not in this room")
        events: list[RoomEvent] = []
        settled: list[AgentRun] = []
        async with self.db.transaction():
            if require_member:
                await self._require_capability_in_transaction(
                    room_id, removed_by, RoomCapability.ADMINISTER
                )
            await self.repos.agents.remove_room_membership_in_transaction(
                agent_id, room_id, utcnow()
            )
            # The handle is the address, so it goes back to the room with the
            # membership: a later @mention of a removed agent resolves to nobody
            # rather than opening a fresh run for it.
            await self.repos.handles.release_in_transaction(
                room_id, ParticipantType.AGENT, agent_id
            )
            for run in await self.repos.agent_runs.list_open_by_agent_room(agent_id, room_id):
                execution = await self.repos.executions.get(run.execution_id)
                if execution is None:
                    continue
                # Through CANCEL_REQUESTED to SETTLED: the settlement is decided before
                # the harness is told, and the record says so even if it is never told.
                await self.repos.agent_runs.advance(
                    run.run_id, HarnessState.CANCEL_REQUESTED, utcnow(), removed_by
                )
                if execution.status in {
                    ExecutionStatus.COMPLETED,
                    ExecutionStatus.FAILED,
                    ExecutionStatus.CANCELLED,
                }:
                    for event in await self.repos.agent_runs.settle_in_transaction(
                        run.execution_id, RunSettlement.AGENT_REMOVED, removed_by
                    ):
                        events.append(
                            await self.repos.events.append_with_next_sequence_in_transaction(event)
                        )
                    settled.append(run)
                    continue
                events.extend(
                    await self.repos.executions.terminalize_without_output_in_transaction(
                        execution,
                        ExecutionStatus.CANCELLED,
                        "agent removed from room",
                        [],
                        RunSettlement.AGENT_REMOVED,
                        removed_by,
                    )
                )
                settled.append(run)
            events.append(
                await self.repos.events.append_with_next_sequence_in_transaction(
                    RoomEvent(
                        room_id=room_id,
                        sequence=0,
                        event_type=EventType.AGENT_LEFT_ROOM,
                        payload={
                            "agent_id": agent_id,
                            "removed_by": removed_by,
                            "settled_run_ids": [run.run_id for run in settled],
                        },
                        actor_id=removed_by,
                        actor_type="user",
                    )
                )
            )
        await self._broadcast_persisted_events(events)
        harness = self._harness(agent.harness_id) if agent.harness_id in KNOWN_HARNESS_IDS else None
        for run in settled:
            if harness is None:
                continue
            try:
                await harness.session_cancel(
                    SessionHandle(run_id=run.run_id, harness_session_id=run.execution_id),
                    "agent removed from room",
                )
            except Exception:
                log.exception("Could not tell the harness that run %s was settled", run.run_id)

    async def record_session_update(
        self, run_id: str, credential: str, update: SessionUpdate
    ) -> None:
        """Accept one harness-originated update, or refuse it.

        The per-run credential is compared as an opaque token, and a settled run is
        refused whatever it presents: the turn it belonged to is over.
        """
        run = await self.repos.agent_runs.get(run_id)
        if run is None or not credential_matches(credential, run.credential_hash):
            raise AuthorizationError("run credential rejected")
        if run.harness_state is HarnessState.SETTLED:
            raise DomainError(f"run {run_id} is settled ({run.settlement}) and accepts no updates")
        await self.repos.agent_runs.advance(
            run.run_id, HarnessState.STREAMING, utcnow() + _STREAMING_LEASE, run.acting_user_id
        )
        del update

    # ── Branch ───────────────────────────────────────────────────────────────

    async def start_branch(
        self,
        room_id: str,
        mode: BranchMode,
        initiating_prompt: str,
        initiated_by: str,
        agent_ids: list[str],
        idempotency_key: str | None = None,
    ) -> tuple[Branch, list[Execution]]:
        """Atomically freeze context, create AgentRuns, and optionally own the room turn."""
        initiating_prompt = self._validate_non_empty(initiating_prompt, "branch prompt")
        if idempotency_key is not None:
            idempotency_key = self._validate_idempotency_key(idempotency_key)
        request = {"mode": mode.value, "prompt": initiating_prompt, "agent_ids": list(agent_ids)}
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
        # Addressing and identity are gates that close before a run row exists. The
        # BEFORE INSERT triggers below repeat the identity leg, so a revocation racing
        # this preparation is still refused at the write.
        prepared: dict[str, AgentRun] = {}
        for agent in agents:
            try:
                await self._require_addressable(agent, room_id, initiated_by)
                prepared_run = await self._prepare_agent_run(agent, room_id, initiated_by)
            except AgentLaunchRefused as refusal:
                await self._record_launch_refusal(refusal)
                raise
            prepared[agent.agent_id] = prepared_run

        persisted_events: list[RoomEvent] = []
        executions: list[Execution] = []
        async with self.db.transaction():
            await self._require_mutate_in_transaction(room_id, initiated_by)
            if idempotency_key is not None:
                prior = await self._claim_idempotency(
                    room_id, initiated_by, idempotency_key, "branch.start", request
                )
                if prior is not None:
                    replay = await self.repos.branches.get(prior.result_ref)
                    if replay is None:
                        raise DomainError("idempotent branch replay lost its result")
                    return replay, await self.repos.executions.list_by_branch(replay.branch_id)
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
                    authorized_by=initiated_by,
                    branch_id=branch.branch_id,
                    status=ExecutionStatus.PENDING,
                    input_data={
                        "initiating_prompt": initiating_prompt,
                        "context_hash": context_hash,
                    },
                )
                await self.repos.sessions.create(session)
                await self.repos.executions.create(execution)
                await self.repos.agent_runs.create_in_transaction(
                    replace(prepared[agent.agent_id], execution_id=execution.execution_id)
                )
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
            if idempotency_key is not None:
                await self._record_idempotency(
                    room_id,
                    initiated_by,
                    idempotency_key,
                    "branch.start",
                    request,
                    branch.branch_id,
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
        # The instance column says where the agent was created; membership says whether
        # it is still there. Every other launch door reads membership, and this one did
        # not, so a removed agent still got a durable session row and a room event
        # announcing that it had started work.
        if not await self.repos.agents.has_room_membership(agent_id, room_id):
            raise AgentLaunchRefused(
                agent_id, room_id, "not_a_member", f"agent {agent_id} is not in room {room_id}"
            )
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
        self, session_id: str, authorized_by: str, input_data: dict[str, Any] | None = None
    ) -> Execution:
        session = await self.repos.sessions.get(session_id)
        if not session:
            raise DomainError(f"session not found: {session_id}")
        _validate_transition(
            session.status, SessionStatus.ACTIVE, VALID_SESSION_TRANSITIONS, "session"
        )
        agent = await self.get_agent(session.agent_id)
        try:
            await self._require_addressable(agent, session.room_id, authorized_by)
            run = await self._prepare_agent_run(agent, session.room_id, authorized_by)
        except AgentLaunchRefused as refusal:
            await self._record_launch_refusal(refusal)
            raise
        execution = Execution(
            execution_id=new_id("exec"),
            session_id=session_id,
            agent_id=session.agent_id,
            authorized_by=authorized_by,
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
            run,
        )
        await self._broadcast_persisted_events([event])
        await self._set_agent_status_safe(session.agent_id, AgentStatus.WORKING)
        persisted = await self.repos.executions.get(execution.execution_id)
        return persisted or execution

    async def _user_term(self, room_id: str, user_id: str) -> frozenset[str]:
        """What one human may lend an agent here, from durable membership alone."""
        member = await self.repos.room_members.get(room_id, user_id)
        granted = _policy_list(member.allowed_capabilities if member else None)
        return user_capabilities(member.role if member else None) & policy_capabilities(granted)

    async def _capability_terms(
        self,
        agent: AgentInstance,
        room_id: str,
        authorized_by: str,
        acting_as: str = "",
    ) -> CapabilityTerms:
        """The five durable terms of PRD §13, read from records alone.

        The user term is the authorizing principal's grant. A different caller
        acting on that principal's run gets the intersection of the two: nobody
        obtains through somebody else's run more than they hold themselves, and
        the authorizing principal's grant is a ceiling rather than a substitute.
        """
        user = await self._user_term(room_id, authorized_by)
        if acting_as and acting_as != authorized_by:
            user &= await self._user_term(room_id, acting_as)
        template = await self.repos.agents.get_template(agent.template_id)
        room = await self.repos.rooms.get(room_id)
        workspace = await self.repos.workspaces.get(room.workspace_id) if room is not None else None
        return CapabilityTerms(
            user=user,
            agent=frozenset(agent.capabilities),
            skill=frozenset(template.capabilities) if template else frozenset(),
            channel=policy_capabilities(_policy_list(room.allowed_capabilities if room else None)),
            workspace=policy_capabilities(
                _policy_list(workspace.allowed_capabilities if workspace else None)
            ),
        )

    @staticmethod
    def _step_schema(effective: frozenset[str]) -> dict[str, Any]:
        """Offer only the tools this run may call, so the rest are unavailable."""
        offered = allowed_tools(effective)
        properties: dict[str, Any] = {
            "action": {"type": "string", "enum": ["finish", "delegate", "wait"]},
            "output": {"type": "object"},
        }
        if offered:
            properties["action"]["enum"] = ["tool", *properties["action"]["enum"]]
            properties["tool"] = {"type": "string", "enum": offered}
            properties["input"] = {"type": "object"}
        return {"type": "object", "properties": properties, "required": ["action"]}

    async def agent_capability_terms(self, agent_id: str, requested_by: str) -> CapabilityTerms:
        """Terms as they would apply to a run this member initiated."""
        agent = await self.get_agent(agent_id)
        return await self._capability_terms(agent, agent.room_id, requested_by)

    async def _delegated_terms(self, execution: Execution, acting_as: str) -> CapabilityTerms:
        """This run's terms as they stand for a caller who is not its principal."""
        session = await self.repos.sessions.get(execution.session_id)
        if session is None:
            raise DomainError("session not found")
        agent = await self.get_agent(execution.agent_id)
        return await self._capability_terms(
            agent, session.room_id, execution.authorized_by, acting_as
        )

    async def _require_delegated_authority(
        self, execution: Execution, acting_as: str
    ) -> CapabilityTerms | None:
        """Guard every verb that advances or influences somebody else's run.

        Room MUTATE says the caller may act in this channel; it does not say what
        this run may do on their behalf. The effective set is re-derived here from
        durable records, and a caller narrower than the authorizing principal gets
        the intersection: refused when it is empty, and bounded by it when it is
        not. Returns the narrowed terms, or None when no delegation applies.
        """
        if not acting_as:
            return None
        session = await self.repos.sessions.get(execution.session_id)
        if session is None:
            raise DomainError("session not found")
        agent = await self.get_agent(execution.agent_id)
        # Interrupt, cancel and resume re-run the addressing check as well as the
        # authority one, so a caller who may not point this agent may not steer it
        # either — including the principal the run already names.
        try:
            await self._require_addressable(agent, session.room_id, acting_as)
        except AgentLaunchRefused as refusal:
            await self._record_launch_refusal(refusal)
            raise
        if acting_as == execution.authorized_by:
            return None
        terms = await self._delegated_terms(execution, acting_as)
        if not terms.effective:
            raise AuthorizationError(
                f"{acting_as} may not act on run {execution.execution_id}: no effective capability"
            )
        return terms

    async def _handle_tool_request(
        self,
        execution: Execution,
        session: Session,
        agent: AgentInstance,
        acting_as: str,
        result: dict[str, Any],
        steer_bound: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        """Permission check, policy check, approval gate, execution, audit event.

        The terms are re-derived here rather than carried in from the step that
        offered the tool: a provider call sits between the two, and a grant withdrawn
        while the model was thinking must not still be spendable when it answers.

        Re-deriving is not the same as unbinding. A steer that shaped this step
        still bounds what the step may spend, so the freshly derived terms are
        bounded again here; otherwise a narrow intervener's text could reach a
        tool through the re-derivation that her own grant never permitted.
        """
        terms = await self._capability_terms(
            agent, session.room_id, execution.authorized_by, acting_as
        )
        if steer_bound is not None:
            terms = terms.bounded_by(steer_bound)
        effective = terms.effective
        tool = str(result.get("tool", ""))
        raw_input = result.get("input")
        tool_input = raw_input if isinstance(raw_input, dict) else {}
        decision = decide(tool, effective)
        request = ToolRequest(
            request_id=new_id("toolreq"),
            room_id=session.room_id,
            execution_id=execution.execution_id,
            agent_id=agent.agent_id,
            # requested_by is the actor; authorized_by is the authority it acts under.
            requested_by=agent.agent_id,
            authorized_by=execution.authorized_by,
            tool=tool,
            input_json=json.dumps(tool_input, default=str),
            required_capability=decision.required_capability,
            effective_json=json.dumps(sorted(effective)),
            status="REJECTED" if not decision.allowed else "PENDING_APPROVAL",
            reason=decision.reason,
        )
        payload = {
            "request_id": request.request_id,
            "tool": tool,
            "agent_id": agent.agent_id,
            "execution_id": execution.execution_id,
            "required_capability": decision.required_capability,
            "effective": sorted(effective),
            "reason": decision.reason,
        }
        if not decision.allowed:
            await self.repos.tool_requests.create(request)
            await self._append_room_event(
                session.room_id,
                EventType.TOOL_CALL_REJECTED,
                payload,
                agent.agent_id,
                "agent",
            )
            return self._tool_response(request)
        if decision.requires_approval:
            approval = await self.request_approval(
                session.room_id,
                execution.execution_id,
                agent.agent_id,
                f"{tool}: {decision.required_capability}",
                authorized_by=execution.authorized_by,
            )
            request = replace(request, approval_id=approval.approval_id)
            await self.repos.tool_requests.create(request)
            # No harness work is in flight while a reviewer thinks, so the lease is a
            # long one. It is still a lease: an exemption is no deadline at all.
            await self._advance_run_for_execution(
                execution.execution_id,
                HarnessState.AWAITING_APPROVAL,
                execution.authorized_by,
                _APPROVAL_LEASE,
            )
            return self._tool_response(request)
        await self.repos.tool_requests.create(request)
        return self._tool_response(await self._execute_tool_request(request))

    async def _current_tool_decision(
        self, request: ToolRequest, acting_as: str = ""
    ) -> tuple[GatewayDecision, frozenset[str]]:
        """Decide a stored request again from the records as they stand right now."""
        agent = await self.get_agent(request.agent_id)
        execution = await self.repos.executions.get(request.execution_id)
        authorized_by = execution.authorized_by if execution is not None else ""
        terms = await self._capability_terms(agent, request.room_id, authorized_by, acting_as)
        effective = terms.effective
        return decide(request.tool, effective), effective

    async def _execute_tool_request(self, request: ToolRequest) -> ToolRequest:
        """Run an authorised tool and audit the outcome. Never raises to the caller."""
        await self._append_room_event(
            request.room_id,
            EventType.TOOL_CALL_STARTED,
            {"request_id": request.request_id, "tool": request.tool},
            request.agent_id,
            "agent",
        )
        try:
            output = await self._run_tool(request)
        except RunAuthorityRevoked as revoked:
            # The write already rolled back with the raise; the settlement is written
            # here, outside the transaction that could not have kept it.
            await self.repos.tool_requests.resolve(
                request.request_id, "REJECTED", str(revoked), "{}"
            )
            await self._append_room_event(
                request.room_id,
                EventType.AGENT_RUN_AUTHORITY_REVOKED,
                {
                    "run_id": revoked.authorization.run_id,
                    "authorized_by": revoked.authorization.authorized_by,
                    "acting_user_id": revoked.authorization.acting_user_id,
                    "stage": revoked.stage,
                    "missing_capability": revoked.authorization.required_capability,
                },
                request.agent_id,
                "agent",
            )
            await self._append_room_event(
                request.room_id,
                EventType.TOOL_CALL_REJECTED,
                {
                    "request_id": request.request_id,
                    "tool": request.tool,
                    "required_capability": request.required_capability,
                    "reason": str(revoked),
                },
                request.agent_id,
                "agent",
            )
            run = await self.repos.agent_runs.get_by_execution(request.execution_id)
            if run is not None:
                await self._settle_run(
                    run,
                    RunSettlement.AUTHORITY_REVOKED,
                    revoked.authorization.acting_user_id,
                    str(revoked),
                )
            return replace(request, status="REJECTED", reason=str(revoked))
        except DomainError as exc:
            await self.repos.tool_requests.resolve(request.request_id, "FAILED", str(exc), "{}")
            await self._append_room_event(
                request.room_id,
                EventType.TOOL_CALL_FAILED,
                {"request_id": request.request_id, "tool": request.tool, "error": str(exc)},
                request.agent_id,
                "agent",
            )
            return replace(request, status="FAILED", reason=str(exc))
        result_json = json.dumps(output, default=str)
        await self.repos.tool_requests.resolve(
            request.request_id, "EXECUTED", "executed", result_json
        )
        await self._append_room_event(
            request.room_id,
            EventType.TOOL_CALL_COMPLETED,
            {"request_id": request.request_id, "tool": request.tool},
            request.agent_id,
            "agent",
        )
        return replace(request, status="EXECUTED", reason="executed", result_json=result_json)

    async def _run_tool(self, request: ToolRequest) -> dict[str, Any]:
        """The registry's executable side. Each tool is a small, auditable action."""
        tool_input = json.loads(request.input_json)
        if request.tool == "channel.read_context":
            messages = await self.repos.messages.list_by_room(request.room_id, limit=20)
            return {
                "messages": [
                    {"message_id": m.message_id, "content": m.content, "role": m.role.value}
                    for m in messages
                ]
            }
        # The authority re-check belongs inside each writer's own transaction, not
        # wrapped around this dispatch: these calls open their own transactions, and
        # Database.transaction() refuses to nest, so a check here would sit outside
        # the write and relocate check-then-use rather than end it.
        authorization = await self._run_authorization(request)
        if request.tool == "message.react":
            # The channel the run belongs to is the boundary, checked here rather
            # than left to the agent-membership check inside the reaction: that one
            # raises AuthorizationError, which this layer does not catch, so a
            # cross-channel message id would escape the "never raises" contract.
            message = await self.get_message(str(tool_input.get("message_id", "")))
            if message.room_id != request.room_id:
                raise DomainError("message is not in this channel")
            reaction = await self.add_agent_reaction(
                message.message_id,
                request.agent_id,
                str(tool_input.get("emoji", "")),
                authorization=authorization,
            )
            return {"message_id": message.message_id, "emoji": reaction.emoji}
        if request.tool == "task.create":
            task = await self.create_task(
                request.room_id,
                str(tool_input.get("title", "")),
                str(tool_input.get("description", "")),
                created_by=request.agent_id,
                authorization=authorization,
            )
            return {"task_id": task.task_id}
        if request.tool == "artifact.write":
            artifact = await self.create_artifact(
                request.room_id,
                str(tool_input.get("name", "Untitled")),
                ArtifactType.DOCUMENT,
                str(tool_input.get("description", "")),
                created_by=request.agent_id,
                authorization=authorization,
            )
            return {"artifact_id": artifact.artifact_id}
        raise DomainError(f"tool not executable: {request.tool}")

    async def _run_authorization(self, request: ToolRequest) -> RunAuthorization:
        """What the writer re-derives its terms from, read from durable records."""
        run = await self.repos.agent_runs.get_by_execution(request.execution_id)
        execution = await self.repos.executions.get(request.execution_id)
        authorized_by = request.authorized_by or (
            execution.authorized_by if execution is not None else ""
        )
        return RunAuthorization(
            run_id=run.run_id if run is not None else request.execution_id,
            agent_id=request.agent_id,
            room_id=request.room_id,
            authorized_by=authorized_by,
            acting_user_id=run.acting_user_id if run is not None else authorized_by,
            required_capability=request.required_capability or "",
        )

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
        """
        run = await self.repos.agent_runs.get(authorization.run_id)
        if run is None or run.harness_state is HarnessState.SETTLED:
            raise RunAuthorityRevoked(authorization, stage)
        agent = await self.repos.agents.get_instance(authorization.agent_id)
        if agent is None:
            raise RunAuthorityRevoked(authorization, stage)
        terms = await self._capability_terms(
            agent,
            authorization.room_id,
            authorization.authorized_by,
            authorization.acting_user_id,
        )
        if authorization.required_capability not in terms.effective:
            raise RunAuthorityRevoked(authorization, stage)

    @staticmethod
    def _tool_response(request: ToolRequest) -> dict[str, Any]:
        return {
            "status": "ok",
            "action": "tool",
            "tool_request": {
                "request_id": request.request_id,
                "tool": request.tool,
                "status": request.status,
                "reason": request.reason,
                "required_capability": request.required_capability,
                "effective": json.loads(request.effective_json),
                "approval_id": request.approval_id,
                "result": json.loads(request.result_json),
            },
        }

    async def execute_agent_step(
        self, execution_id: str, prompt: str, acting_as: str = ""
    ) -> dict[str, Any]:
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

        # The authority the run carries, re-derived from durable records now rather
        # than trusted from the request that opened it. A principal whose grant was
        # withdrawn between that write and this dispatch can no longer make the
        # agent speak, so the run is settled instead of run.
        terms = await self._capability_terms(agent, session.room_id, execution.authorized_by)
        if not terms.effective:
            await self._settle_undispatched_run(
                execution_id,
                f"{execution.authorized_by or 'an unknown principal'} may no longer "
                f"invoke agent {execution.agent_id}: no effective capability",
            )
            raise AuthorizationError(
                f"run {execution_id} is no longer authorized by {execution.authorized_by}"
            )
        # A caller who is not that principal is bounded by their own grant too.
        delegated = await self._require_delegated_authority(execution, acting_as)
        if delegated is not None:
            terms = delegated
        # And so is every steer the run is still carrying. The intervention row says
        # who steered; what she may lend is re-derived here, from the records as they
        # stand at the moment this step spends her text. A set persisted when the text
        # was accepted would be an authorization input frozen at write time: narrowing
        # her, or removing her from the room, would leave the stale set bounding this
        # step, which is the asymmetry the run principal's own re-derivation avoids.
        steers = await self.repos.interventions.list_unconsumed(execution_id)
        steer_bound: frozenset[str] | None = None
        for steer in steers:
            authority = (await self._delegated_terms(execution, steer.intervened_by)).effective
            terms = terms.bounded_by(authority)
            steer_bound = authority if steer_bound is None else steer_bound & authority

        source_prompt = prompt
        provider_prompt = prompt
        if branch.lifecycle_managed:
            if prompt != branch.initiating_prompt:
                raise DomainError("managed branch run must use its immutable initiating prompt")
            source_prompt = branch.initiating_prompt
            provider_prompt = self._branch_execution_prompt(branch)

        if agent.harness_id not in KNOWN_HARNESS_IDS:
            raise DomainError(f"no harness is registered as {agent.harness_id!r}")
        harness = self._harness(agent.harness_id)
        agent_run = await self.repos.agent_runs.get_by_execution(execution_id)
        handle = SessionHandle(
            run_id=agent_run.run_id if agent_run is not None else execution_id,
            harness_session_id=execution_id,
        )
        # The turn is in flight from here, on a lease the sweep can expire if the
        # process driving it dies.
        await self._advance_run_for_execution(
            execution_id, HarnessState.STREAMING, acting_as, _STREAMING_LEASE
        )
        if not execution.run_id:
            if agent_run is not None:
                await harness.session_new(
                    RunContext(
                        run_id=agent_run.run_id,
                        agent_id=agent_run.agent_id,
                        identity_id=agent_run.identity_id,
                        room_id=agent_run.room_id,
                        run_credential=self._run_credentials.pop(agent_run.run_id, ""),
                        authorized_by=agent_run.authorized_by,
                        acting_user_id=acting_as or agent_run.acting_user_id,
                    )
                )
            else:
                await self.nexus.create_execution(agent, session, provider_prompt, execution)
            run_id = f"run_{execution.execution_id}"
            # replace(), not a rebuild: a rebuild silently reset triggered_by to
            # DIRECT, losing why the run was opened at the moment it starts.
            await self.repos.executions.mark_running(
                execution.execution_id, run_id, execution.status
            )
            execution = replace(execution, run_id=run_id, status=ExecutionStatus.RUNNING)

        effective = terms.effective
        try:
            turn = await harness.session_prompt(
                PromptRequest(
                    handle=handle,
                    prompt=provider_prompt,
                    response_schema=self._step_schema(effective),
                    offered_tools=tuple(allowed_tools(effective)),
                ),
                self._renew_run_lease,
            )
        except HarnessError as exc:
            # The steers stay unconsumed: a prompt that never reached the harness
            # did not spend them, and leaving them bounds the next step rather
            # than unbinding it.
            result: dict[str, Any] = {"status": "error", "error": str(exc)}
        else:
            result = dict(turn.output)
            if turn.stop_reason is StopReason.CANCELLED:
                # A cancelled turn returns before the harness drains its queue, so
                # the prompt never carried these steers. Leaving them unconsumed
                # bounds the next step rather than spending a delivery that did
                # not happen.
                result["status"] = "cancelled"
            else:
                # The prompt carried the queued steers, so they are spent here.
                await self.repos.interventions.mark_consumed(
                    [steer.intervention_id for steer in steers]
                )
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
        if result.get("action") == "tool":
            return await self._handle_tool_request(
                execution, session, agent, acting_as, result, steer_bound
            )
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
            agent_message, agent_message_event = await self._agent_message_for_mention(
                execution, session, output
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
                execution.status,
                agent_message,
                agent_message_event,
            )
            await self._broadcast_persisted_events(persisted_events)
            await self._set_agent_status_safe(execution.agent_id, AgentStatus.COMPLETED)
            await self._set_agent_status_safe(execution.agent_id, AgentStatus.IDLE)
            result["output_id"] = output.output_id
        return result

    async def execute_branch_run(
        self, branch_id: str, execution_id: str, acting_as: str = ""
    ) -> dict[str, Any]:
        branch = await self.get_branch(branch_id)
        execution = await self.repos.executions.get(execution_id)
        if execution is None or execution.branch_id != branch.branch_id:
            raise DomainError("agent run not found in branch")
        return await self.execute_agent_step(execution_id, branch.initiating_prompt, acting_as)

    async def pause_execution(self, execution_id: str, acting_as: str = "") -> bool:
        execution = await self.repos.executions.get(execution_id)
        if execution is None:
            raise DomainError("execution not found")
        await self._require_delegated_authority(execution, acting_as)
        branch = await self.get_branch(execution.branch_id)
        if not branch.lifecycle_managed:
            return await self.nexus.pause_execution(execution_id)
        _validate_transition(
            execution.status, ExecutionStatus.PAUSED, VALID_EXECUTION_TRANSITIONS, "execution"
        )
        ok = await self.nexus.pause_execution(execution_id)
        if not ok:
            return False
        await self.repos.executions.update_status(
            execution_id, ExecutionStatus.PAUSED, execution.status
        )
        return True

    async def resume_execution(self, execution_id: str, acting_as: str = "") -> bool:
        execution = await self.repos.executions.get(execution_id)
        if execution is None:
            raise DomainError("execution not found")
        await self._require_delegated_authority(execution, acting_as)
        branch = await self.get_branch(execution.branch_id)
        if not branch.lifecycle_managed:
            return await self.nexus.resume_execution(execution_id)
        _validate_transition(
            execution.status, ExecutionStatus.RUNNING, VALID_EXECUTION_TRANSITIONS, "execution"
        )
        ok = await self.nexus.resume_execution(execution_id)
        if not ok:
            return False
        await self.repos.executions.update_status(
            execution_id, ExecutionStatus.RUNNING, execution.status
        )
        return True

    async def resume_agent_run(
        self, run_id: str, resumed_by: str, *, require_member: bool = False
    ) -> Execution:
        """Continue a settled run as a new one, with the same identity and fresh authority.

        A settled run is never resumed in place: re-adopting a state nobody observed is
        exactly the ambiguity settling it removed. A parked run is not resumed at all —
        it has already used every attempt it was allowed.
        """
        previous = await self.repos.agent_runs.get(run_id)
        if previous is None:
            raise DomainError(f"agent run not found: {run_id}")
        if previous.harness_state is not HarnessState.SETTLED:
            raise DomainError(f"agent run {run_id} is still open")
        if previous.settlement is RunSettlement.PARKED:
            raise DomainError(f"agent run {run_id} is parked after {previous.attempts} attempts")
        agent = await self.get_agent(previous.agent_id)
        earlier = await self.repos.executions.get(previous.execution_id)
        try:
            await self._require_addressable(agent, previous.room_id, resumed_by)
            run = await self._prepare_agent_run(
                agent,
                previous.room_id,
                resumed_by,
                resumed_from_run_id=previous.run_id,
                attempts=previous.attempts + 1,
            )
        except AgentLaunchRefused as refusal:
            await self._record_launch_refusal(refusal)
            raise
        session = Session(
            session_id=new_id("sess"),
            room_id=previous.room_id,
            agent_id=agent.agent_id,
            status=SessionStatus.ACTIVE,
        )
        execution = Execution(
            execution_id=new_id("exec"),
            session_id=session.session_id,
            agent_id=agent.agent_id,
            authorized_by=resumed_by,
            triggered_by=earlier.triggered_by if earlier is not None else AgentTrigger.DIRECT,
            input_data=dict(earlier.input_data) if earlier is not None else {},
        )
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(previous.room_id, resumed_by)
            await self.repos.sessions.create(session)
            execution = await self.repos.executions.create(execution)
            await self.repos.agent_runs.create_in_transaction(
                replace(run, execution_id=execution.execution_id)
            )
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=previous.room_id,
                    sequence=0,
                    event_type=EventType.AGENT_RUN_STARTED,
                    payload={
                        "execution_id": execution.execution_id,
                        "session_id": session.session_id,
                        "agent_id": agent.agent_id,
                        "resumed_from_run_id": previous.run_id,
                        "attempt": run.attempts,
                    },
                    actor_id=resumed_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return execution

    async def cancel_execution(
        self, execution_id: str, cancelled_by: str, *, require_member: bool = False
    ) -> bool:
        execution = await self.repos.executions.get(execution_id)
        if execution is None:
            raise DomainError("execution not found")
        if require_member:
            await self._require_delegated_authority(execution, cancelled_by)
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
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(session.room_id, cancelled_by)
            events = await self.repos.executions.terminalize_without_output_in_transaction(
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
    def _intervention_for(
        execution: Execution, intervened_by: str, instruction: str
    ) -> ExecutionIntervention:
        """The steer to persist: who steered and what they said, never what they held.

        A capability set written here would be an authorization input frozen at the
        moment the text was accepted, and the row is immutable, so narrowing that
        person afterwards could not reach it. The step that spends this instruction
        re-derives her grant instead, which is how every other authority in this
        service is read.
        """
        return ExecutionIntervention(
            intervention_id=new_id("interv"),
            execution_id=execution.execution_id,
            intervened_by=intervened_by,
            instruction=instruction,
        )

    async def intervene_execution(
        self, execution_id: str, user_id: str, instruction: str, *, require_member: bool = False
    ) -> None:
        """Record a human redirect against a running execution. The ordered event is
        appended inside the transaction that re-checks membership, so a member demoted
        while the runtime intervention is dispatched cannot author it."""
        execution = await self.repos.executions.get(execution_id)
        if execution is None:
            raise DomainError("execution not found")
        agent = await self.get_agent(execution.agent_id)
        if require_member:
            await self._require_delegated_authority(execution, user_id)
        intervention = self._intervention_for(execution, user_id, instruction)
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(agent.room_id, user_id)
            # The bound commits with the event that records the steer, before the
            # text is queued for a prompt: nothing reaches a provider unbounded.
            await self.repos.interventions.create(intervention)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=agent.room_id,
                    sequence=0,
                    event_type=EventType.HUMAN_REDIRECTED_AGENT,
                    payload={"agent_id": execution.agent_id, "instruction": instruction},
                    actor_id=user_id,
                    actor_type="user",
                )
            )
        await self.nexus.add_execution_intervention(execution_id, instruction)
        await self._broadcast_persisted_events([event])

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
        async with self.db.transaction():
            await self._require_mutate_in_transaction(room_id, decided_by)
            event = await self.repos.output_selections.upsert_with_event_in_transaction(
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
        self,
        branch_id: str,
        title: str,
        created_by: str,
        idempotency_key: str | None = None,
    ) -> tuple[Artifact, ArtifactVersion]:
        """Compatibility route: the Decision Brief is one of three synthesis types."""
        return await self.synthesize_branch(
            branch_id,
            title,
            created_by,
            synthesis_type=SynthesisType.DECISION_BRIEF,
            idempotency_key=idempotency_key,
        )

    async def synthesize_branch(
        self,
        branch_id: str,
        title: str,
        created_by: str,
        synthesis_type: str = SynthesisType.DECISION_BRIEF,
        idempotency_key: str | None = None,
    ) -> tuple[Artifact, ArtifactVersion]:
        """Run model-backed synthesis over this Branch's explicit selected outputs."""
        spec = spec_for(synthesis_type)
        title = self._validate_non_empty(title, f"{spec.artifact_name.lower()} title")
        if idempotency_key is not None:
            idempotency_key = self._validate_idempotency_key(idempotency_key)
        operation = f"branch.synthesis.{spec.type.lower()}"
        request = {"title": title}
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
            synthesis_type=spec.type.value,
        )
        synthesis = BranchSynthesis(
            synthesis_id=new_id("syn"),
            branch_id=branch_id,
            room_id=branch.room_id,
            title=title,
            initiated_by=created_by,
            status=BranchSynthesisStatus.RUNNING,
            synthesis_type=spec.type.value,
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
            await self._require_mutate_in_transaction(branch.room_id, created_by)
            if idempotency_key is not None:
                prior = await self._claim_idempotency(
                    branch_id, created_by, idempotency_key, operation, request
                )
                if prior is not None:
                    return await self._replay_branch_synthesis(prior.result_ref)
            await self.repos.branch_syntheses.create_with_inputs(synthesis, inputs)
            if idempotency_key is not None:
                await self._record_idempotency(
                    branch_id,
                    created_by,
                    idempotency_key,
                    operation,
                    request,
                    synthesis.synthesis_id,
                )
        try:
            model_result = await self.nexus.synthesize_selected_outputs(
                title=title,
                prompt=branch.initiating_prompt,
                outputs=selected_records,
                synthesis_type=spec.type.value,
            )
            return await self._complete_branch_synthesis(
                branch, synthesis, inputs, included, title, created_by, model_result, spec
            )
        except Exception as exc:
            # The key was claimed before the model call. Any failure after that
            # point must leave a terminal FAILED row, so a replay says "retry with
            # a new key" instead of reporting the synthesis as running forever.
            await self._fail_branch_synthesis(branch, synthesis, inputs, created_by, str(exc))
            if isinstance(exc, ModelProviderError):
                raise DomainError(str(exc)) from exc
            raise

    async def _fail_branch_synthesis(
        self,
        branch: Branch,
        synthesis: BranchSynthesis,
        inputs: list[BranchSynthesisInput],
        created_by: str,
        error: str,
    ) -> None:
        async with self.db.transaction():
            current = await self.repos.branch_syntheses.get(synthesis.synthesis_id)
            if current is None or current.status is not BranchSynthesisStatus.RUNNING:
                return
            await self.repos.branch_syntheses.mark_failed(synthesis.synthesis_id, error)
            member = await self.repos.room_members.get(branch.room_id, created_by)
            if RoomCapability.MUTATE not in capabilities_for_role(member.role if member else None):
                # Initiator lost write access during the model call: the RUNNING row
                # is now terminal, but attribute no ordered event to a non-member.
                return
            started_event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=branch.room_id,
                    sequence=0,
                    event_type=EventType.BRANCH_SYNTHESIS_STARTED,
                    payload={
                        "branch_id": branch.branch_id,
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
                    payload={
                        "branch_id": branch.branch_id,
                        "synthesis_id": synthesis.synthesis_id,
                    },
                    actor_id=created_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([started_event, failed_event])

    async def _complete_branch_synthesis(
        self,
        branch: Branch,
        synthesis: BranchSynthesis,
        inputs: list[BranchSynthesisInput],
        included: list[AgentOutput],
        title: str,
        created_by: str,
        model_result: dict[str, Any],
        spec: SynthesisSpec,
    ) -> tuple[Artifact, ArtifactVersion]:
        branch_id = branch.branch_id
        document_value = model_result.get("document")
        if not isinstance(document_value, dict):
            raise DomainError("model provider returned invalid synthesis document")
        document = document_value
        content = render_synthesis(spec, title, document, bool(model_result["simulated"]))
        existing = None
        for artifact in await self.list_room_artifacts(branch.room_id):
            # Name alone is not identity: a synthesis only ever extends a lineage that a
            # synthesis published, never one someone wrote by hand under the same name.
            if artifact.name == spec.artifact_name and await self._is_published_synthesis(
                artifact.artifact_id
            ):
                existing = artifact
                break
        create_artifact = existing is None
        artifact = existing or Artifact(
            artifact_id=new_id("art"),
            room_id=branch.room_id,
            name=spec.artifact_name,
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
                    event_type=(
                        EventType.DECISION_BRIEF_SYNTHESIZED
                        if spec.type is SynthesisType.DECISION_BRIEF
                        else EventType.SYNTHESIS_PUBLISHED
                    ),
                    payload={
                        "branch_id": branch_id,
                        "synthesis_type": spec.type.value,
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
        ontology_entities: list[OntologyEntity] = []
        ontology_relationships: list[OntologyRelationship] = []
        if spec.type is SynthesisType.DECISION_BRIEF:
            # Only a Decision Brief asserts a decision; a synthesis or a progress report
            # would materialize a DECISION entity that nobody made.
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
                        "relationship_ids": [
                            item.relationship_id for item in ontology_relationships
                        ],
                    },
                    actor_id=created_by,
                    actor_type="user",
                )
            )
        aborted = False
        persisted_events: list[RoomEvent] = []
        async with self.db.transaction():
            member = await self.repos.room_members.get(branch.room_id, created_by)
            if RoomCapability.MUTATE not in capabilities_for_role(member.role if member else None):
                # Demoted during the model call: terminate the RUNNING row without
                # attributing any ordered event to a member who lost write access.
                await self.repos.branch_syntheses.mark_failed(
                    synthesis.synthesis_id, "initiator lost write access during synthesis"
                )
                aborted = True
            else:
                persisted_events = await self.repos.artifacts.create_synthesis_in_transaction(
                    artifact,
                    version,
                    claims_and_sources,
                    ontology_entities,
                    ontology_relationships,
                    event_types,
                    create_artifact=create_artifact,
                    synthesis=terminal_synthesis,
                )
        if aborted:
            raise AuthorizationError("room access forbidden")
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
            source_object: tuple[str, str],
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
                    # The durable row whose content states the relation, so a
                    # relationship-centric answer can drill down to it.
                    source_object_kind=source_object[0],
                    source_object_id=source_object[1],
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )

        published_version = (OntologyEntityKind.ARTIFACT.value, version.version_id)
        relationship(
            OntologyRelationshipKind.OWNS,
            project_id,
            artifact_entity_id,
            OntologyDerivationKind.SYSTEM_MATERIALIZED,
            (version.version_id,),
            (room_id, artifact.artifact_id, version.version_id),
            published_version,
        )
        relationship(
            OntologyRelationshipKind.OWNS,
            person_id,
            artifact_entity_id,
            OntologyDerivationKind.SYSTEM_MATERIALIZED,
            (version.version_id,),
            (created_by, artifact.artifact_id, version.version_id),
            published_version,
        )
        relationship(
            OntologyRelationshipKind.REFERENCES,
            artifact_entity_id,
            decision_id,
            OntologyDerivationKind.SYSTEM_MATERIALIZED,
            (version.version_id,),
            (artifact.artifact_id, version.version_id),
            published_version,
        )
        for claim, source in claims_and_sources:
            claim_entity_id = claim_entity_ids[claim.claim_id]
            output_entity_id = output_entity_ids[source.output_id]
            exact_evidence = (source.output_id,)
            stating_claim = (OntologyEntityKind.CLAIM.value, claim.claim_id)
            relationship(
                OntologyRelationshipKind.SUPPORTS,
                claim_entity_id,
                decision_id,
                OntologyDerivationKind.AI_DERIVED,
                exact_evidence,
                (claim.claim_id, source.output_id, version.version_id),
                stating_claim,
            )
            relationship(
                OntologyRelationshipKind.DERIVED_FROM,
                claim_entity_id,
                output_entity_id,
                OntologyDerivationKind.AI_DERIVED,
                exact_evidence,
                (claim.claim_id, source.output_id),
                stating_claim,
            )
            relationship(
                OntologyRelationshipKind.DERIVED_FROM,
                decision_id,
                output_entity_id,
                OntologyDerivationKind.AI_DERIVED,
                exact_evidence,
                (version.version_id, claim.claim_id, source.output_id),
                (OntologyEntityKind.AGENT_OUTPUT.value, source.output_id),
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
        *,
        require_member: bool = False,
        authorization: RunAuthorization | None = None,
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
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(room_id, created_by)
            if authorization is not None:
                await self._require_run_authority_in_transaction(authorization, "task.create")
            await self.repos.tasks.create(task)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.TASK_CREATED,
                    payload={"task_id": task.task_id, "title": title},
                    actor_id=created_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return task

    async def assign_task(
        self, task_id: str, agent_id: str, *, requested_by: str = "", require_member: bool = False
    ) -> Task:
        async with self.db.transaction():
            task = await self.repos.tasks.get(task_id)
            if not task:
                raise DomainError(f"task not found: {task_id}")
            if require_member:
                await self._require_mutate_in_transaction(task.room_id, requested_by)
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
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=task.room_id,
                    sequence=0,
                    event_type=EventType.TASK_ASSIGNED,
                    payload={"task_id": task_id, "agent_id": agent_id},
                    actor_id=agent_id,
                    actor_type="agent",
                )
            )
        await self._broadcast_persisted_events([event])
        return task

    async def delegate_task(
        self,
        task_id: str,
        from_agent_id: str,
        to_agent_id: str,
        description: str = "",
        *,
        requested_by: str = "",
        require_member: bool = False,
    ) -> Task:
        async with self.db.transaction():
            task = await self.repos.tasks.get(task_id)
            if not task:
                raise DomainError(f"task not found: {task_id}")
            if require_member:
                await self._require_mutate_in_transaction(task.room_id, requested_by)
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
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=task.room_id,
                    sequence=0,
                    event_type=EventType.TASK_DELEGATED,
                    payload={
                        "parent_task_id": task_id,
                        "child_task_id": child.task_id,
                        "from_agent": from_agent_id,
                        "to_agent": to_agent_id,
                    },
                    actor_id=from_agent_id,
                    actor_type="agent",
                )
            )
        await self._broadcast_persisted_events([event])
        return child

    async def complete_task(
        self, task_id: str, *, requested_by: str = "", require_member: bool = False
    ) -> Task:
        async with self.db.transaction():
            task = await self.repos.tasks.get(task_id)
            if not task:
                raise DomainError(f"task not found: {task_id}")
            if require_member:
                await self._require_mutate_in_transaction(task.room_id, requested_by)
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
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=task.room_id,
                    sequence=0,
                    event_type=EventType.TASK_COMPLETED,
                    payload={"task_id": task_id},
                    actor_id=task.assigned_agent_id or "system",
                    actor_type="agent",
                )
            )
        await self._broadcast_persisted_events([event])
        return task

    async def cancel_task(
        self, task_id: str, *, requested_by: str = "", require_member: bool = False
    ) -> Task:
        async with self.db.transaction():
            task = await self.repos.tasks.get(task_id)
            if not task:
                raise DomainError(f"task not found: {task_id}")
            if require_member:
                await self._require_mutate_in_transaction(task.room_id, requested_by)
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
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=task.room_id,
                    sequence=0,
                    event_type=EventType.TASK_CANCELLED,
                    payload={"task_id": task_id},
                    actor_id=task.created_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return task

    async def list_room_tasks(self, room_id: str) -> list[Task]:
        return await self.repos.tasks.list_by_room(room_id)

    # ── Messages ─────────────────────────────────────────────────────────────

    # ── Idempotency ──────────────────────────────────────────────────────────

    @staticmethod
    def _validate_idempotency_key(value: str) -> str:
        value = value.strip()
        if not value or len(value) > 128:
            raise DomainError("idempotency key must be 1-128 characters")
        return value

    @staticmethod
    def _request_hash(operation: str, request: dict[str, Any]) -> str:
        canonical = json.dumps(
            {"operation": operation, "request": request},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

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

    async def _replay_branch_synthesis(self, synthesis_id: str) -> tuple[Artifact, ArtifactVersion]:
        synthesis = await self.repos.branch_syntheses.get(synthesis_id)
        if synthesis is None:
            raise DomainError("idempotent synthesis replay lost its result")
        if synthesis.status is BranchSynthesisStatus.FAILED:
            raise IdempotencyConflict(
                f"synthesis {synthesis_id} failed; retry with a new idempotency key"
            )
        if synthesis.artifact_version_id is None:
            raise IdempotencyConflict(
                f"synthesis {synthesis_id} is still running; replay the key after it completes"
            )
        version = await self.repos.artifacts.get_version(synthesis.artifact_version_id)
        artifact = await self.repos.artifacts.get(version.artifact_id) if version else None
        if version is None or artifact is None:
            raise DomainError("idempotent synthesis replay lost its artifact")
        return artifact, version

    # ── Messages ─────────────────────────────────────────────────────────────

    async def _read_addressed_handles(
        self, room_id: str, content: str
    ) -> tuple[list[RoomParticipantHandle], list[str]]:
        """Split the @tokens in this text into the ones the room answers to and the rest.

        The room's issued handles are the entire vocabulary. A handle is matched
        exactly, so nothing here guesses from a prefix of somebody's display name,
        and a client cannot claim a mention the text does not contain. What the
        author typed is normalised the same way a handle is minted, which is what
        lets @Architect and @architect address the same agent and lets a mention end
        a sentence without the full stop becoming part of the address.

        The second list is the point of returning a tuple: an @handle that addresses
        nobody used to vanish silently, and the caller has to be able to say so.
        """
        typed = list(dict.fromkeys(_MENTION_PATTERN.findall(content)))
        if not typed:
            return [], []
        by_handle = {
            record.handle: record for record in await self.repos.handles.list_by_room(room_id)
        }
        resolved: list[RoomParticipantHandle] = []
        unresolved: list[str] = []
        seen: set[tuple[str, str]] = set()
        for token in typed:
            record = by_handle.get(handle_from_display_name(token))
            if record is None:
                unresolved.append(token)
                continue
            key = (record.participant_type.value, record.participant_id)
            if key in seen:
                continue
            seen.add(key)
            resolved.append(record)
        return resolved, unresolved

    async def _resolve_mentions(
        self, room_id: str, message_id: str, content: str
    ) -> tuple[list[MessageMention], list[str]]:
        """The addressed targets of one message, and the handles that addressed nobody."""
        resolved, unresolved = await self._read_addressed_handles(room_id, content)
        mentions = [
            MessageMention(
                message_id=message_id,
                room_id=room_id,
                target_type=MentionTargetType(record.participant_type.value),
                target_id=record.participant_id,
                handle=record.handle,
            )
            for record in resolved
        ]
        return mentions, unresolved

    async def unrecognized_mention_handles(self, room_id: str, content: str) -> list[str]:
        """The @handles in this text that address nobody in this room.

        Silence was the bug: a misspelled agent handle returned 200 with an empty
        mention list, so the author waited for an answer that was never coming.
        """
        _, unresolved = await self._read_addressed_handles(room_id, content)
        return unresolved

    async def uninvocable_mention_handles(self, message_id: str) -> list[str]:
        """The handles this message addressed that no agent turn can ever answer.

        Members and agents share one handle namespace, which is correct: @finance
        has to mean exactly one participant in a room. It also means a person can
        hold the handle an agent would otherwise have taken, and then a request to
        invoke the mentioned agents opens no run at all. The handle resolved, so it
        is not unrecognized; it resolved to somebody who cannot be invoked, and an
        author who asked for a turn has to be told which of the two happened.
        """
        return [
            mention.handle
            for mention in await self.repos.mentions.list_for_message(message_id)
            if mention.target_type is not MentionTargetType.AGENT
        ]

    async def _invoke_mentioned_agent_in_transaction(
        self, room_id: str, agent_id: str, requested_by: str, message_id: str
    ) -> tuple[Execution, RoomEvent]:
        """Open one agent turn that a mention explicitly asked for.

        The five-way capability intersection is the existing check for what a
        member may lend an agent. An empty effective set means this member may
        lend this agent nothing, so they may not make it speak, and raising here
        rolls the whole message write back rather than half-applying it.

        This only opens the turn. Running it is long provider I/O, so it happens
        after this transaction commits, in :meth:`_dispatch_mention_run`.
        """
        agent = await self.get_agent(agent_id)
        if agent.room_id != room_id:
            raise DomainError("mentioned agent is not in this room")
        terms = await self._capability_terms(agent, room_id, requested_by)
        if not terms.effective:
            raise AuthorizationError(
                f"{requested_by} may not invoke agent {agent_id}: no effective capability"
            )
        await self._require_addressable(agent, room_id, requested_by)
        run = await self._prepare_agent_run(agent, room_id, requested_by)
        session = Session(
            session_id=new_id("sess"),
            room_id=room_id,
            agent_id=agent_id,
            status=SessionStatus.ACTIVE,
        )
        execution = Execution(
            execution_id=new_id("exec"),
            session_id=session.session_id,
            agent_id=agent_id,
            authorized_by=requested_by,
            triggered_by=AgentTrigger.MENTION,
            input_data={"mention_message_id": message_id, "requested_by": requested_by},
        )
        await self.repos.sessions.create(session)
        execution = await self.repos.executions.create(execution)
        await self.repos.agent_runs.create_in_transaction(
            replace(run, execution_id=execution.execution_id)
        )
        event = await self.repos.events.append_with_next_sequence_in_transaction(
            RoomEvent(
                room_id=room_id,
                sequence=0,
                event_type=EventType.AGENT_RUN_STARTED,
                payload={
                    "execution_id": execution.execution_id,
                    "session_id": session.session_id,
                    "agent_id": agent_id,
                    "triggered_by": AgentTrigger.MENTION.value,
                    "mention_message_id": message_id,
                    "requested_by": requested_by,
                },
                actor_id=agent_id,
                actor_type="agent",
            )
        )
        return execution, event

    async def _settle_undispatched_run(self, execution_id: str, error: str) -> None:
        """Bring a run that will never produce a result to a described terminal state."""
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
            )
        except DomainError:
            # The run moved on between the read above and this write. Settling a run
            # somebody else is advancing is exactly the damage this guard exists to
            # prevent, so this pass loses the race and writes nothing.
            log.info("Run %s advanced while being settled; leaving it alone", execution_id)
            return
        await self._broadcast_persisted_events(events)
        await self._set_agent_status_safe(execution.agent_id, AgentStatus.FAILED)

    async def _dispatch_mention_run(self, execution_id: str, prompt: str) -> None:
        """Run a mention-invoked turn, after the write that recorded it committed.

        The invariant this holds is that no run is left in a state the system
        cannot describe. Provider failures are already terminalised by
        :meth:`execute_agent_step`; anything else that escapes it is settled here
        as FAILED with an event saying why. The one gap a running process cannot
        close is a crash between the commit and this call, and
        :meth:`_settle_orphaned_mention_runs` closes that at the next startup.

        Claiming the run first is what makes that sweep safe: from here on the run
        is visibly somebody's work, so no other process mistakes it for an orphan.
        A claim that does not take means another dispatcher already has it, or the
        run is no longer pending, and either way this process must not run it.
        """
        if not await self.repos.executions.claim_for_dispatch(execution_id, self._dispatch_claim):
            log.info("Mention-invoked run %s was already claimed; not dispatching", execution_id)
            return
        try:
            await self.execute_agent_step(execution_id, prompt)
        except Exception as exc:
            log.exception("Mention-invoked run %s did not complete", execution_id)
            try:
                await self._settle_undispatched_run(execution_id, f"dispatch failed: {exc}")
            except Exception:
                # The message itself is already committed. Failing its write here
                # would tell the author their message was lost when it was not.
                log.exception("Failed to settle mention run %s", execution_id)

    @staticmethod
    def _output_excerpt(content: str) -> tuple[str, bool]:
        """What the conversation shows of an output, and whether it is all of it."""
        text = " ".join(content.split())
        if len(text) <= _AGENT_MESSAGE_EXCERPT_CHARS:
            return text, False
        return text[:_AGENT_MESSAGE_EXCERPT_CHARS].rstrip() + "…", True

    async def _agent_message_for_mention(
        self, execution: Execution, session: Session, output: AgentOutput
    ) -> tuple[Message | None, RoomEvent | None]:
        """The conversational surface for a turn a mention asked for.

        An authenticated HTTP principal may never author an AGENT-role message, so
        the service authors it here instead, in the same transaction as the output.
        The output remains the first-class inspectable record; this message names it
        by output_id and sits at the mention's own thread coordinates, so the answer
        lands in the conversation that asked the question.

        The message carries an excerpt, not the output. Copying the content in full
        would put the same text in two places, and then the two places could be
        edited, exported or retracted apart from each other while both claimed to be
        what the agent said. The metadata says who asked for the turn, so the room
        can read why the agent spoke without opening anything.
        """
        if execution.triggered_by is not AgentTrigger.MENTION:
            return None, None
        mention_message_id = str(execution.input_data.get("mention_message_id", ""))
        mention = await self.repos.messages.get(mention_message_id) if mention_message_id else None
        if mention is None or mention.room_id != session.room_id:
            return None, None
        excerpt, excerpted = self._output_excerpt(output.content)
        message = Message(
            message_id=new_id("msg"),
            room_id=session.room_id,
            role=MessageRole.AGENT,
            sender_id=execution.agent_id,
            content=excerpt,
            metadata={
                "output_id": output.output_id,
                "execution_id": execution.execution_id,
                "triggered_by": execution.triggered_by.value,
                "requested_by": str(execution.input_data.get("requested_by", "")),
                # The reader is told when there is more in the record than here, so
                # a truncated excerpt is never mistaken for the whole answer.
                "output_excerpted": excerpted,
            },
            parent_message_id=mention.message_id,
            root_message_id=mention.root_message_id or mention.message_id,
            thread_depth=mention.thread_depth + 1,
            # An answer belongs wherever the question was asked.
            broadcast_to_room=mention.broadcast_to_room,
        )
        event = RoomEvent(
            room_id=session.room_id,
            sequence=0,
            event_type=EventType.MESSAGE_CREATED,
            payload={
                "message_id": message.message_id,
                "role": message.role.value,
                "sender_id": message.sender_id,
                "content": message.content[:500],
                "parent_message_id": message.parent_message_id,
                "root_message_id": message.root_message_id,
                "thread_depth": message.thread_depth,
                "broadcast_to_room": message.broadcast_to_room,
                "output_id": output.output_id,
                "execution_id": execution.execution_id,
                "triggered_by": execution.triggered_by.value,
                "requested_by": str(execution.input_data.get("requested_by", "")),
                "mentions": [],
            },
            actor_id=execution.agent_id,
            actor_type="agent",
        )
        return message, event

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
    ) -> Message:
        content = self._validate_non_empty(content, "message content")
        if idempotency_key is not None:
            idempotency_key = self._validate_idempotency_key(idempotency_key)
        request = {
            "role": role.value,
            "content": content,
            "metadata": metadata or {},
            "parent_message_id": parent_message_id,
            "invoke_mentioned_agents": invoke_mentioned_agents,
        }
        msg = Message(
            message_id=new_id("msg"),
            room_id=room_id,
            role=role,
            sender_id=sender_id,
            content=content,
            metadata=metadata or {},
            broadcast_to_room=broadcast_to_room,
        )
        events: list[RoomEvent] = []
        invoked: dict[str, str] = {}
        try:
            async with self.db.transaction():
                if role is MessageRole.HUMAN:
                    await self._require_mutate_in_transaction(room_id, sender_id)
                if idempotency_key is not None:
                    prior = await self._claim_idempotency(
                        room_id, sender_id, idempotency_key, "message.create", request
                    )
                    if prior is not None:
                        replay = await self.repos.messages.get(prior.result_ref)
                        if replay is None:
                            raise DomainError("idempotent message replay lost its result")
                        return replay
                if parent_message_id is not None:
                    parent = await self.repos.messages.get(parent_message_id)
                    if parent is None or parent.room_id != room_id:
                        raise DomainError(f"parent message not found in room: {parent_message_id}")
                    if parent.thread_depth + 1 > MAX_THREAD_DEPTH:
                        raise DomainError(
                            f"thread depth limit reached: a reply may not nest deeper "
                            f"than {MAX_THREAD_DEPTH}"
                        )
                    msg = replace(
                        msg,
                        parent_message_id=parent.message_id,
                        root_message_id=parent.root_message_id or parent.message_id,
                        thread_depth=parent.thread_depth + 1,
                    )
                mentions, _ = await self._resolve_mentions(room_id, msg.message_id, content)
                if invoke_mentioned_agents and msg.thread_depth >= MAX_THREAD_DEPTH:
                    # The agent's answer is a reply to this message, and it has to fit.
                    raise DomainError(
                        "thread depth limit reached: no room for an agent's answer below "
                        f"depth {MAX_THREAD_DEPTH}"
                    )
                for mention in mentions:
                    if mention.target_type is not MentionTargetType.AGENT:
                        continue
                    if not invoke_mentioned_agents:
                        continue
                    execution, run_event = await self._invoke_mentioned_agent_in_transaction(
                        room_id, mention.target_id, sender_id, msg.message_id
                    )
                    invoked[mention.target_id] = execution.execution_id
                    events.append(run_event)
                message_event = (
                    await self.repos.messages.create_with_event_and_turn_guard_in_transaction(
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
                                "parent_message_id": msg.parent_message_id,
                                "root_message_id": msg.root_message_id,
                                "thread_depth": msg.thread_depth,
                                "broadcast_to_room": msg.broadcast_to_room,
                                "mentions": [
                                    {
                                        "target_type": mention.target_type.value,
                                        "target_id": mention.target_id,
                                        "invoked_execution_id": invoked.get(mention.target_id),
                                    }
                                    for mention in mentions
                                ],
                            },
                            actor_id=sender_id,
                            actor_type=role.value.lower(),
                        ),
                    )
                )
                events.append(message_event)
                msg = replace(msg, event_sequence=message_event.sequence)
                for mention in mentions:
                    await self.repos.mentions.create(
                        replace(mention, invoked_execution_id=invoked.get(mention.target_id))
                    )
                    if mention.target_type is MentionTargetType.USER:
                        await self.repos.notifications.create(
                            Notification(
                                notification_id=new_id("notif"),
                                user_id=mention.target_id,
                                room_id=room_id,
                                title="You were mentioned",
                                body=content[:500],
                                notification_type="mention",
                            )
                        )
                if idempotency_key is not None:
                    await self._record_idempotency(
                        room_id,
                        sender_id,
                        idempotency_key,
                        "message.create",
                        request,
                        msg.message_id,
                    )
        except AgentLaunchRefused as refusal:
            # The message and the turn it asked for roll back together; the refusal is
            # appended after that rollback, or it would roll back with them.
            await self._record_launch_refusal(refusal)
            raise
        except DomainError:
            raise
        except ValueError as exc:
            raise DomainError(str(exc)) from exc
        await self._broadcast_persisted_events(events)
        # Dispatch belongs here, after the commit, beside the broadcast: a turn that
        # waited on a provider inside the write transaction would hold the room's
        # write lock for the length of the model call. The mention's own text is the
        # prompt, because that is what the author addressed to the agent.
        for execution_id in invoked.values():
            await self._dispatch_mention_run(execution_id, content)
        return msg

    async def list_room_messages(
        self, room_id: str, limit: int = 100, after_sequence: int | None = None
    ) -> list[Message]:
        return await self.repos.messages.list_by_room(
            room_id, limit=self._validate_limit(limit), after_sequence=after_sequence
        )

    async def list_message_mentions(self, message_id: str) -> list[MessageMention]:
        return await self.repos.mentions.list_for_message(message_id)

    async def get_message(self, message_id: str) -> Message:
        message = await self.repos.messages.get(message_id)
        if message is None:
            raise DomainError(f"message not found: {message_id}")
        return message

    async def list_thread(self, root_message_id: str, limit: int = 200) -> list[ThreadReply]:
        """The whole thread with counts derived from the reply rows on every read."""
        root = await self.get_message(root_message_id)
        if root.root_message_id is not None:
            root_message_id = root.root_message_id
        return await self.repos.messages.list_thread(root_message_id, limit)

    # ── Reactions ────────────────────────────────────────────────────────────

    @staticmethod
    def _validate_emoji(value: str) -> str:
        value = value.strip()
        if not value or len(value) > 16 or any(char.isspace() for char in value):
            raise DomainError("reaction emoji must be a short non-empty token")
        return value

    async def _require_reaction_actor_in_transaction(
        self, room_id: str, actor_id: str, actor_type: ParticipantType
    ) -> None:
        """Authorize the reacting principal against its own kind of room membership.

        A member is checked against room_members, an agent against the agent's own
        durable membership of this room. Neither borrows the other's: an agent id is
        not in room_members and never gains MUTATE by being mentioned there, and an
        authenticated HTTP principal never reaches the agent branch because the
        routes only ever call the member-facing methods.
        """
        if actor_type is ParticipantType.AGENT:
            if not await self.repos.agents.has_room_membership(actor_id, room_id):
                raise AuthorizationError(f"agent {actor_id} is not a member of this room")
            return
        await self._require_mutate_in_transaction(room_id, actor_id)

    async def _set_reaction(
        self,
        message_id: str,
        actor_id: str,
        emoji: str,
        *,
        removed: bool,
        actor_type: ParticipantType = ParticipantType.USER,
        authorization: RunAuthorization | None = None,
    ) -> MessageReaction:
        emoji = self._validate_emoji(emoji)
        message = await self.get_message(message_id)
        async with self.db.transaction():
            await self._require_reaction_actor_in_transaction(message.room_id, actor_id, actor_type)
            if authorization is not None:
                await self._require_run_authority_in_transaction(authorization, "message.react")
            existing = await self.repos.reactions.get(message_id, actor_id, emoji)
            if existing is not None and (existing.removed_at is not None) == removed:
                # Repeating an add or a remove is a no-op, so a retry appends no event.
                return existing
            if existing is None and removed:
                raise DomainError("no such reaction to remove")
            reaction = await self.repos.reactions.set_removed_at(
                message_id,
                message.room_id,
                actor_id,
                emoji,
                utcnow() if removed else None,
                actor_type,
            )
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=message.room_id,
                    sequence=0,
                    event_type=(
                        EventType.MESSAGE_REACTION_REMOVED
                        if removed
                        else EventType.MESSAGE_REACTION_ADDED
                    ),
                    payload={
                        "message_id": message_id,
                        "actor_id": actor_id,
                        "actor_type": actor_type.value,
                        "emoji": emoji,
                    },
                    actor_id=actor_id,
                    actor_type=actor_type.value.lower(),
                )
            )
        await self._broadcast_persisted_events([event])
        return reaction

    async def add_reaction(self, message_id: str, actor_id: str, emoji: str) -> MessageReaction:
        """A member reacts. This is the only reaction path an HTTP route may reach."""
        return await self._set_reaction(message_id, actor_id, emoji, removed=False)

    async def remove_reaction(self, message_id: str, actor_id: str, emoji: str) -> MessageReaction:
        return await self._set_reaction(message_id, actor_id, emoji, removed=True)

    async def add_agent_reaction(
        self,
        message_id: str,
        agent_id: str,
        emoji: str,
        *,
        authorization: RunAuthorization | None = None,
    ) -> MessageReaction:
        """An agent reacts as itself, on its own membership of the room.

        Reached only through the message.react tool, so the agent asks for it during
        its own run and the gateway audits the request. Deliberately not reachable
        from a route: an agent reaction is attributed to the agent, so letting a
        human ask for one would let a human sign an agent's name.
        """
        return await self._set_reaction(
            message_id,
            agent_id,
            emoji,
            removed=False,
            actor_type=ParticipantType.AGENT,
            authorization=authorization,
        )

    async def remove_agent_reaction(
        self, message_id: str, agent_id: str, emoji: str
    ) -> MessageReaction:
        return await self._set_reaction(
            message_id, agent_id, emoji, removed=True, actor_type=ParticipantType.AGENT
        )

    async def list_reactions(self, message_id: str) -> list[MessageReaction]:
        return await self.repos.reactions.list_live(message_id)

    # ── Read cursors ─────────────────────────────────────────────────────────

    async def get_read_cursor(self, room_id: str, user_id: str) -> dict[str, Any]:
        cursor = await self.repos.read_cursors.get(room_id, user_id)
        last_read = cursor.last_read_sequence if cursor else 0
        latest = await self.repos.events.get_latest_sequence(room_id)
        return {
            "room_id": room_id,
            "user_id": user_id,
            "last_read_sequence": last_read,
            "latest_sequence": latest,
            "unread_messages": await self.repos.messages.count_since_sequence(
                room_id, last_read, user_id
            ),
            "updated_at": cursor.updated_at.isoformat() if cursor else None,
        }

    async def set_read_cursor(
        self, room_id: str, user_id: str, last_read_sequence: int
    ) -> dict[str, Any]:
        if last_read_sequence < 0:
            raise DomainError("read cursor sequence must not be negative")
        async with self.db.transaction():
            await self._require_capability_in_transaction(room_id, user_id, RoomCapability.READ)
            latest = await self.repos.events.get_latest_sequence(room_id)
            if last_read_sequence > latest:
                raise DomainError("read cursor cannot pass the room's latest sequence")
            await self.repos.read_cursors.set(
                ReadCursor(room_id=room_id, user_id=user_id, last_read_sequence=last_read_sequence)
            )
        return await self.get_read_cursor(room_id, user_id)

    # ── Search ───────────────────────────────────────────────────────────────

    async def search(
        self, user_id: str, query: str, room_id: str | None = None, limit: int = 50
    ) -> list[SearchHit]:
        """Authorization is a join inside the matching query, never a later filter."""
        terms = _SEARCH_TERM_PATTERN.findall(self._validate_non_empty(query, "search query"))
        if not terms:
            raise DomainError("search query must contain a searchable term")
        match_query = " ".join(f'"{term}"' for term in terms[:16])
        return await self.repos.search.search(
            user_id, match_query, room_id, self._validate_limit(limit)
        )

    # ── Artifacts ────────────────────────────────────────────────────────────

    async def _is_published_synthesis(self, artifact_id: str) -> bool:
        """True when any version of this artifact was published by a branch synthesis."""
        versions = await self.repos.artifacts.list_versions(artifact_id)
        return any(version.branch_synthesis_id for version in versions)

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
    ) -> Artifact:
        name = self._validate_non_empty(name, "artifact name")
        if name in RESERVED_ARTIFACT_NAMES:
            raise DomainError(f"{name!r} names a published synthesis and cannot be created by hand")
        artifact = Artifact(
            artifact_id=new_id("art"),
            room_id=room_id,
            name=name,
            artifact_type=artifact_type,
            description=description,
            current_version=1 if content else 0,
            created_by=created_by,
        )
        version: ArtifactVersion | None = None
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
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(room_id, created_by)
            if authorization is not None:
                await self._require_run_authority_in_transaction(authorization, "artifact.write")
            await self.repos.artifacts.create(artifact)
            if version is not None:
                await self.repos.artifacts.create_version_in_transaction(version)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.ARTIFACT_CREATED,
                    payload={
                        "artifact_id": artifact.artifact_id,
                        "name": name,
                        "type": artifact_type.value,
                    },
                    actor_id=created_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return artifact

    async def update_artifact(
        self, artifact_id: str, content: str, updated_by: str = "", *, require_member: bool = False
    ) -> ArtifactVersion:
        artifact = await self.repos.artifacts.get(artifact_id)
        if not artifact:
            raise DomainError(f"artifact not found: {artifact_id}")
        if await self._is_published_synthesis(artifact_id):
            # Every version of a published synthesis carries the outputs it came from.
            # Appending hand-written text here would be indistinguishable from one.
            raise DomainError(
                "a published synthesis is extended by publishing a synthesis, "
                "not by writing a version"
            )
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
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(artifact.room_id, updated_by)
            await self.repos.artifacts.create_version_in_transaction(version)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=artifact.room_id,
                    sequence=0,
                    event_type=EventType.ARTIFACT_VERSION_CREATED,
                    payload={"artifact_id": artifact_id, "version": new_ver},
                    actor_id=updated_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return version

    async def list_room_artifacts(self, room_id: str) -> list[Artifact]:
        return await self.repos.artifacts.list_by_room(room_id)

    # ── Decisions ────────────────────────────────────────────────────────────

    async def create_decision(
        self,
        room_id: str,
        title: str,
        content: str,
        reason: str = "",
        created_by: str = "",
        *,
        require_member: bool = False,
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
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(room_id, created_by)
            await self.repos.decisions.create(decision)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.DECISION_CREATED,
                    payload={"decision_id": decision.decision_id, "title": title},
                    actor_id=created_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
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
        *,
        require_member: bool = False,
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
        if room_id is None:
            await self.repos.memories.create(memory)
            return memory
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(room_id, created_by)
            await self.repos.memories.create(memory)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.MEMORY_CREATED,
                    payload={"memory_id": memory.memory_id, "type": memory_type},
                    actor_id=created_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return memory

    async def list_room_memories(self, room_id: str) -> list[Memory]:
        return await self.repos.memories.list_by_room(room_id)

    # ── Approvals ────────────────────────────────────────────────────────────

    async def request_approval(
        self,
        room_id: str,
        execution_id: str,
        agent_id: str,
        action_description: str,
        *,
        requested_by: str = "",
        authorized_by: str = "",
        require_member: bool = False,
    ) -> Approval:
        approval = Approval(
            approval_id=new_id("appr"),
            room_id=room_id,
            execution_id=execution_id,
            agent_id=agent_id,
            action_description=action_description,
            authorized_by=authorized_by,
        )
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(room_id, requested_by)
            await self.repos.approvals.create(approval)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.APPROVAL_REQUESTED,
                    payload={
                        "approval_id": approval.approval_id,
                        "agent_id": agent_id,
                        "action": action_description,
                    },
                    actor_id=agent_id,
                    actor_type="agent",
                )
            )
        await self._set_agent_status_safe(agent_id, AgentStatus.WAITING_APPROVAL)
        await self._broadcast_persisted_events([event])
        return approval

    async def approve_action(
        self, approval_id: str, reviewer_id: str, comment: str = "", *, require_member: bool = False
    ) -> Approval:
        async with self.db.transaction():
            approval = await self.repos.approvals.get(approval_id)
            if not approval:
                raise DomainError(f"approval not found: {approval_id}")
            if approval.status != ApprovalStatus.PENDING:
                raise DomainError(
                    f"approval {approval_id} is not pending (current: {approval.status.value})"
                )
            if require_member:
                await self._require_capability_in_transaction(
                    approval.room_id, reviewer_id, RoomCapability.ADMINISTER
                )
            approval = Approval(
                approval_id=approval.approval_id,
                room_id=approval.room_id,
                execution_id=approval.execution_id,
                agent_id=approval.agent_id,
                action_description=approval.action_description,
                authorized_by=approval.authorized_by,
                status=ApprovalStatus.APPROVED,
                reviewer_id=reviewer_id,
                review_comment=comment,
                requested_at=approval.requested_at,
                reviewed_at=utcnow(),
            )
            await self.repos.approvals.update(approval)
            pending = await self.repos.tool_requests.get_by_approval(approval_id)
            if pending is not None and pending.status == "PENDING_APPROVAL":
                # The reviewer grants from their own capabilities, never above them:
                # an approval is not a way to lend what the reviewer does not hold.
                # Re-derived inside the transaction that grants rather than after it
                # closed; the re-stamped effective set is an audit record, never an
                # input, because the writer re-derives again inside its own.
                decision, effective = await self._current_tool_decision(pending, reviewer_id)
                run = await self.repos.agent_runs.get_by_execution(pending.execution_id)
                if run is not None and run.harness_state is HarnessState.SETTLED:
                    # The run this call belongs to ended while the reviewer was
                    # deciding. Releasing the approval now would let output arrive
                    # after the settlement, through the one door that outlives it.
                    decision = replace(
                        decision,
                        allowed=False,
                        reason=f"run {run.run_id} is settled ({run.settlement})",
                    )
                stamped = json.dumps(sorted(effective))
                await self.repos.tool_requests.set_effective(pending.request_id, stamped)
                pending = replace(pending, effective_json=stamped)
            else:
                pending = None
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=approval.room_id,
                    sequence=0,
                    event_type=EventType.APPROVAL_GRANTED,
                    payload={"approval_id": approval_id, "reviewer_id": reviewer_id},
                    actor_id=reviewer_id,
                    actor_type="user",
                )
            )
        await self._set_agent_status_safe(approval.agent_id, AgentStatus.WORKING)
        await self._broadcast_persisted_events([event])
        if pending is not None:
            if decision.allowed:
                await self._advance_run_for_execution(
                    pending.execution_id, HarnessState.STREAMING, reviewer_id, _STREAMING_LEASE
                )
                await self._execute_tool_request(pending)
            else:
                # The capability was withdrawn between the request and the grant; a
                # human's approval cannot restore what the policy no longer permits.
                await self.repos.tool_requests.resolve(
                    pending.request_id, "REJECTED", decision.reason, "{}"
                )
                await self._append_room_event(
                    pending.room_id,
                    EventType.TOOL_CALL_REJECTED,
                    {
                        "request_id": pending.request_id,
                        "tool": pending.tool,
                        "required_capability": decision.required_capability,
                        "effective": sorted(effective),
                        "reason": decision.reason,
                    },
                    pending.agent_id,
                    "agent",
                )
        return approval

    async def reject_action(
        self,
        approval_id: str,
        reviewer_id: str,
        comment: str = "",
        *,
        require_member: bool = False,
        continue_turn: bool = False,
    ) -> Approval:
        """Refuse one gated tool call, and say what becomes of the run.

        Rejection used to resolve the request and stop, leaving the run
        AWAITING_APPROVAL: not settled, not leased, and unsweepable. It now ends in one
        of two named places inside the transaction that writes it — settled
        APPROVAL_REFUSED, or returned to STREAMING on a fresh lease when the reviewer
        refuses the tool but wants the turn continued. No third path leaves the run
        where it found it.
        """
        events: list[RoomEvent] = []
        async with self.db.transaction():
            approval = await self.repos.approvals.get(approval_id)
            if not approval:
                raise DomainError(f"approval not found: {approval_id}")
            if approval.status != ApprovalStatus.PENDING:
                raise DomainError(
                    f"approval {approval_id} is not pending (current: {approval.status.value})"
                )
            if require_member:
                await self._require_capability_in_transaction(
                    approval.room_id, reviewer_id, RoomCapability.ADMINISTER
                )
            approval = Approval(
                approval_id=approval.approval_id,
                room_id=approval.room_id,
                execution_id=approval.execution_id,
                agent_id=approval.agent_id,
                action_description=approval.action_description,
                authorized_by=approval.authorized_by,
                status=ApprovalStatus.REJECTED,
                reviewer_id=reviewer_id,
                review_comment=comment,
                requested_at=approval.requested_at,
                reviewed_at=utcnow(),
            )
            await self.repos.approvals.update(approval)
            events.append(
                await self.repos.events.append_with_next_sequence_in_transaction(
                    RoomEvent(
                        room_id=approval.room_id,
                        sequence=0,
                        event_type=EventType.APPROVAL_REJECTED,
                        payload={"approval_id": approval_id, "reviewer_id": reviewer_id},
                        actor_id=reviewer_id,
                        actor_type="user",
                    )
                )
            )
            pending = await self.repos.tool_requests.get_by_approval(approval_id)
            if pending is not None and pending.status == "PENDING_APPROVAL":
                await self.repos.tool_requests.resolve(
                    pending.request_id, "REJECTED", "approval rejected", "{}"
                )
                events.append(
                    await self.repos.events.append_with_next_sequence_in_transaction(
                        RoomEvent(
                            room_id=pending.room_id,
                            sequence=0,
                            event_type=EventType.TOOL_CALL_REJECTED,
                            payload={
                                "request_id": pending.request_id,
                                "tool": pending.tool,
                                "reason": "approval rejected",
                            },
                            actor_id=pending.agent_id,
                            actor_type="agent",
                        )
                    )
                )
            events.extend(
                await self._end_refused_approval_in_transaction(
                    approval.execution_id, reviewer_id, continue_turn
                )
            )
        await self._broadcast_persisted_events(events)
        return approval

    async def _end_refused_approval_in_transaction(
        self, execution_id: str, reviewer_id: str, continue_turn: bool
    ) -> list[RoomEvent]:
        """Settle the run, or put it back on a fresh lease. Never neither."""
        run = await self.repos.agent_runs.get_by_execution(execution_id)
        if run is None or run.harness_state is HarnessState.SETTLED:
            return []
        if continue_turn:
            await self.repos.agent_runs.advance(
                run.run_id, HarnessState.STREAMING, utcnow() + _STREAMING_LEASE, reviewer_id
            )
            return []
        execution = await self.repos.executions.get(execution_id)
        if execution is not None and execution.status not in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }:
            return await self.repos.executions.terminalize_without_output_in_transaction(
                execution,
                ExecutionStatus.CANCELLED,
                "approval refused",
                [],
                RunSettlement.APPROVAL_REFUSED,
                reviewer_id,
            )
        return [
            await self.repos.events.append_with_next_sequence_in_transaction(event)
            for event in await self.repos.agent_runs.settle_in_transaction(
                execution_id, RunSettlement.APPROVAL_REFUSED, reviewer_id
            )
        ]

    async def list_pending_approvals(self, room_id: str) -> list[Approval]:
        return await self.repos.approvals.list_pending_by_room(room_id)

    # ── Human Intervention ───────────────────────────────────────────────────

    async def _agent_run_to_steer(self, agent_id: str) -> Execution | None:
        """The run an agent-scoped steer reaches: the live one, else the recorded one.

        The bridge's map of agent to run is in-memory. It is empty after a restart
        and for a run another process is dispatching, so absence there says nothing
        about whether a run exists — the records do.
        """
        execution_id = await self.nexus.get_execution_for_agent(agent_id)
        execution = await self.repos.executions.get(execution_id) if execution_id else None
        if execution is not None:
            return execution
        return await self.repos.executions.latest_open_for_agent(agent_id)

    async def _require_agent_run_authority(self, agent_id: str, acting_as: str) -> None:
        """The agent-scoped doors reach whatever run the agent is serving, so the
        run's own authorization bounds them exactly as the run-scoped doors.

        With no run to bound them, they used to check nothing at all. An absent run
        is not an absent caller: steering an agent is making it act, so the caller
        still has to hold what it takes to make this agent act here.
        """
        execution = await self._agent_run_to_steer(agent_id)
        if execution is not None:
            await self._require_delegated_authority(execution, acting_as)
            return
        agent = await self.get_agent(agent_id)
        terms = await self._capability_terms(agent, agent.room_id, acting_as)
        if not terms.effective:
            raise AuthorizationError(
                f"{acting_as} may not steer agent {agent_id}: no effective capability"
            )

    async def interrupt_agent(
        self, agent_id: str, user_id: str, reason: str = "", *, require_member: bool = False
    ) -> None:
        agent = await self.get_agent(agent_id)
        if require_member:
            await self._require_agent_run_authority(agent_id, user_id)
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(agent.room_id, user_id)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=agent.room_id,
                    sequence=0,
                    event_type=EventType.HUMAN_INTERRUPTED_AGENT,
                    payload={"agent_id": agent_id, "reason": reason},
                    actor_id=user_id,
                    actor_type="user",
                )
            )
        execution_id = await self.nexus.get_execution_for_agent(agent_id)
        if execution_id:
            await self.nexus.pause_execution(execution_id)
        await self._set_agent_status_safe(agent_id, AgentStatus.PAUSED)
        await self._broadcast_persisted_events([event])

    async def redirect_agent(
        self, agent_id: str, user_id: str, instruction: str, *, require_member: bool = False
    ) -> None:
        agent = await self.get_agent(agent_id)
        if require_member:
            await self._require_agent_run_authority(agent_id, user_id)
        # The agent-scoped door queues the same text into the same prompt as the
        # run-scoped one, so it persists the same bound.
        execution = await self._agent_run_to_steer(agent_id)
        intervention = (
            None if execution is None else self._intervention_for(execution, user_id, instruction)
        )
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(agent.room_id, user_id)
            if intervention is not None:
                await self.repos.interventions.create(intervention)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=agent.room_id,
                    sequence=0,
                    event_type=EventType.HUMAN_REDIRECTED_AGENT,
                    payload={"agent_id": agent_id, "instruction": instruction},
                    actor_id=user_id,
                    actor_type="user",
                )
            )
        if intervention is not None:
            await self.nexus.add_execution_intervention(intervention.execution_id, instruction)
        await self._broadcast_persisted_events([event])

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

    # ── Lazy ontology extraction ─────────────────────────────────────────────

    _IMMEDIATE_EVENTS: frozenset[EventType] = frozenset(
        {
            EventType.TASK_CREATED,
            EventType.TASK_ASSIGNED,
            EventType.TASK_UNASSIGNED,
            EventType.TASK_STARTED,
            EventType.TASK_COMPLETED,
            EventType.TASK_FAILED,
            EventType.TASK_CANCELLED,
            EventType.TASK_DELEGATED,
            EventType.DECISION_CREATED,
            # An artifact version and a published synthesis are projected inside
            # their own committing transaction, by create_synthesis_in_transaction.
            # They stay in this allowlist so the cursor means "every structured
            # action up to here is handled", not "every one this pass looked at".
            EventType.ARTIFACT_VERSION_CREATED,
            EventType.SYNTHESIS_PUBLISHED,
        }
    )
    _ASYNC_EVENTS: frozenset[EventType] = frozenset(
        {
            EventType.MESSAGE_CREATED,
            EventType.AGENT_OUTPUT_CREATED,
            EventType.BRANCH_SYNTHESIS_COMPLETED,
        }
    )
    _TASK_ID_KEYS = ("task_id", "child_task_id", "parent_task_id")
    _BLOCKED_BY = " is blocked by "

    async def run_ontology_extraction(
        self,
        room_id: str,
        extractor: OntologyExtractor,
        *,
        actor_id: str = "system",
        limit: int = _ASYNC_PASS_LIMIT,
    ) -> dict[str, Any]:
        """One bounded extraction pass. No read path calls this: reads never write.

        The pass snapshots head, reads only what its cursor has not seen, and writes
        the assertions, their events and the cursor advance in one transaction, so a
        crash rolls the cursor back with the work. Assertions carry deterministic IDs
        and land ON CONFLICT DO NOTHING, which makes at-least-once delivery over
        idempotent writes exactly-once in effect.
        """
        persisted: list[RoomEvent] = []
        result: dict[str, Any] = {}
        async with self.db.transaction():
            head = await self.repos.events.get_latest_sequence(room_id)
            cursor = await self.repos.ontology.get_cursor(room_id, extractor)
            last = cursor.last_sequence if cursor is not None else 0
            entities, relationships, stale_ids, to_sequence = await self._extract(
                room_id, extractor, last, head, limit
            )
            (
                entities_written,
                relationships_written,
            ) = await self.repos.ontology.materialize_in_transaction(entities, relationships)
            marked = await self.repos.ontology.mark_stale_in_transaction(
                room_id, stale_ids, to_sequence
            )
            events: list[RoomEvent] = []
            if entities_written or relationships_written:
                events.append(
                    RoomEvent(
                        room_id=room_id,
                        sequence=0,
                        event_type=EventType.ONTOLOGY_MATERIALIZED,
                        payload={
                            "extractor": extractor.value,
                            "entity_ids": [entity.entity_id for entity in entities],
                            "relationship_ids": [item.relationship_id for item in relationships],
                        },
                        actor_id=actor_id,
                        actor_type="system",
                    )
                )
            if marked:
                events.append(
                    RoomEvent(
                        room_id=room_id,
                        sequence=0,
                        event_type=EventType.ONTOLOGY_ASSERTION_SUPERSEDED,
                        payload={"assertion_ids": marked, "stale_at_sequence": to_sequence},
                        actor_id=actor_id,
                        actor_type="system",
                    )
                )
            if events:
                events.append(
                    RoomEvent(
                        room_id=room_id,
                        sequence=0,
                        event_type=EventType.ONTOLOGY_EXTRACTION_ADVANCED,
                        payload={
                            "extractor": extractor.value,
                            "from_sequence": last,
                            "to_sequence": to_sequence,
                            "entities_written": entities_written,
                            "relationships_written": relationships_written,
                        },
                        actor_id=actor_id,
                        actor_type="system",
                    )
                )
            for event in events:
                persisted.append(
                    await self.repos.events.append_with_next_sequence_in_transaction(event)
                )
            if to_sequence > last:
                await self.repos.ontology.advance_cursor_in_transaction(
                    room_id, extractor, last, to_sequence, utcnow()
                )
            result = {
                "extractor": extractor.value,
                "from_sequence": last,
                "to_sequence": to_sequence,
                "entities_written": entities_written,
                "relationships_written": relationships_written,
                "superseded": marked,
            }
        await self._broadcast_persisted_events(persisted)
        return result

    async def drain_room_ontology(self, room_id: str) -> dict[str, Any] | None:
        """The asynchronous drain, under one in-process lease per room.

        Inference is slow and fallible, so it never sits in a write path; the lease
        keeps two drains for one room from doing the same pass twice. Nothing
        schedules a call to it yet, and no read path may make one, so a room's
        asynchronous backlog is disclosed by `drain_lag_events` rather than hidden.
        """
        if room_id in self._ontology_drains:
            return None
        self._ontology_drains.add(room_id)
        try:
            return await self.run_ontology_extraction(room_id, OntologyExtractor.ASYNC)
        finally:
            self._ontology_drains.discard(room_id)

    async def _extract(
        self,
        room_id: str,
        extractor: OntologyExtractor,
        last: int,
        head: int,
        limit: int,
    ) -> tuple[list[OntologyEntity], list[OntologyRelationship], list[str], int]:
        if extractor is OntologyExtractor.SCHEDULED:
            relationships, stale_ids = await self._consolidate(room_id, head)
            return [], relationships, stale_ids, head
        allowed = (
            self._IMMEDIATE_EVENTS
            if extractor is OntologyExtractor.IMMEDIATE
            else self._ASYNC_EVENTS
        )
        read = [
            event
            for event in await self.repos.events.list_since(room_id, last, limit)
            if event.sequence <= head
        ]
        # A capped pass advances only as far as it actually read, or the next pass
        # would skip the events this one never saw.
        to_sequence = read[-1].sequence if len(read) >= limit and read else head
        relevant = [event for event in read if event.event_type in allowed]
        if extractor is OntologyExtractor.IMMEDIATE:
            entities, relationships = await self._project_structured(room_id, relevant, to_sequence)
        else:
            entities, relationships = await self._project_inferred(room_id, relevant, to_sequence)
        return entities, relationships, [], to_sequence

    async def _project_structured(
        self, room_id: str, events: list[RoomEvent], at_sequence: int
    ) -> tuple[list[OntologyEntity], list[OntologyRelationship]]:
        """Project structured records. A structured record needs no inference."""
        task_events: dict[str, list[int]] = {}
        decision_events: dict[str, list[int]] = {}
        for event in events:
            if event.event_type is EventType.DECISION_CREATED:
                decision_id = str(event.payload.get("decision_id") or "")
                if decision_id:
                    decision_events.setdefault(decision_id, []).append(event.sequence)
                continue
            for key in self._TASK_ID_KEYS:
                task_id = str(event.payload.get(key) or "")
                if task_id:
                    task_events.setdefault(task_id, []).append(event.sequence)
        timestamp = utcnow()
        entities: list[OntologyEntity] = []
        relationships: list[OntologyRelationship] = []
        owners: dict[str, str] = {}
        for task_id, sequences in sorted(task_events.items()):
            task = await self.repos.tasks.get(task_id)
            if task is None or task.room_id != room_id:
                continue
            entity_id = self._ontology_id("ont", room_id, "Task", task_id)
            entities.append(
                OntologyEntity(
                    entity_id=entity_id,
                    room_id=room_id,
                    kind=OntologyEntityKind.TASK,
                    source_object_id=task_id,
                    label=task.title,
                    properties={
                        "status": task.status.value,
                        "priority": task.priority.value,
                        "assigned_agent_id": task.assigned_agent_id or "",
                    },
                    derivation_kind=OntologyDerivationKind.SYSTEM_MATERIALIZED,
                    confidence=1.0,
                    evidence_ids=(task_id,),
                    source_ids=(task_id,),
                    extractor=OntologyExtractor.IMMEDIATE,
                    asserted_at_sequence=at_sequence,
                    evidence_event_sequences=tuple(sorted(set(sequences))),
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
            member = await self.repos.room_members.get(room_id, task.created_by)
            if member is None:
                continue
            person_id = self._ontology_id("ont", room_id, "Person", task.created_by)
            if task.created_by not in owners:
                owners[task.created_by] = person_id
                user = await self.repos.users.get(task.created_by)
                entities.append(
                    OntologyEntity(
                        entity_id=person_id,
                        room_id=room_id,
                        kind=OntologyEntityKind.PERSON,
                        source_object_id=task.created_by,
                        label=user.display_name if user is not None else task.created_by,
                        properties={"user_id": task.created_by},
                        derivation_kind=OntologyDerivationKind.SYSTEM_MATERIALIZED,
                        confidence=1.0,
                        evidence_ids=(task.created_by,),
                        source_ids=(task.created_by,),
                        extractor=OntologyExtractor.IMMEDIATE,
                        asserted_at_sequence=at_sequence,
                        evidence_event_sequences=tuple(sorted(set(sequences))),
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                )
            relationships.append(
                OntologyRelationship(
                    relationship_id=self._ontology_id("rel", room_id, "OWNS", person_id, entity_id),
                    room_id=room_id,
                    kind=OntologyRelationshipKind.OWNS,
                    from_entity_id=person_id,
                    to_entity_id=entity_id,
                    derivation_kind=OntologyDerivationKind.SYSTEM_MATERIALIZED,
                    confidence=1.0,
                    evidence_ids=(task_id,),
                    source_ids=(task.created_by, task_id),
                    source_object_kind=OntologyEntityKind.TASK.value,
                    source_object_id=task_id,
                    extractor=OntologyExtractor.IMMEDIATE,
                    asserted_at_sequence=at_sequence,
                    evidence_event_sequences=tuple(sorted(set(sequences))),
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
        for decision_id, sequences in sorted(decision_events.items()):
            decision = await self.repos.decisions.get(decision_id)
            if decision is None or decision.room_id != room_id:
                continue
            entities.append(
                OntologyEntity(
                    entity_id=self._ontology_id("ont", room_id, "Decision", decision_id),
                    room_id=room_id,
                    kind=OntologyEntityKind.DECISION,
                    source_object_id=decision_id,
                    label=decision.title,
                    properties={"status": decision.status.value, "decision_id": decision_id},
                    derivation_kind=OntologyDerivationKind.SYSTEM_MATERIALIZED,
                    confidence=1.0,
                    evidence_ids=(decision_id,),
                    source_ids=(decision_id,),
                    extractor=OntologyExtractor.IMMEDIATE,
                    asserted_at_sequence=at_sequence,
                    evidence_event_sequences=tuple(sorted(set(sequences))),
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
        return entities, relationships

    async def _project_inferred(
        self, room_id: str, events: list[RoomEvent], at_sequence: int
    ) -> tuple[list[OntologyEntity], list[OntologyRelationship]]:
        """Read the fixed allowlist and label everything it produces unconfirmed."""
        timestamp = utcnow()
        entities: list[OntologyEntity] = []
        relationships: list[OntologyRelationship] = []
        tasks_by_label = {
            entity.label.strip().lower().rstrip("."): entity
            for entity in await self.repos.ontology.list_entities(room_id)
            if entity.kind is OntologyEntityKind.TASK
        }
        for event in events:
            if event.event_type is EventType.AGENT_OUTPUT_CREATED:
                output_id = str(event.payload.get("output_id") or "")
                output = await self.repos.agent_outputs.get(output_id) if output_id else None
                if output is None or output.room_id != room_id:
                    continue
                entities.append(
                    OntologyEntity(
                        entity_id=self._ontology_id("ont", room_id, "AgentOutput", output_id),
                        room_id=room_id,
                        kind=OntologyEntityKind.AGENT_OUTPUT,
                        source_object_id=output_id,
                        label=f"Agent output {output_id}",
                        properties={
                            "agent_id": output.agent_id,
                            "execution_id": output.execution_id,
                            "provider_name": output.provider_name,
                            "provider_model": output.provider_model,
                        },
                        derivation_kind=OntologyDerivationKind.AI_DERIVED,
                        confidence=_INFERRED_CONFIDENCE,
                        evidence_ids=(output_id,),
                        source_ids=(output_id, output.execution_id),
                        review_status=OntologyReviewStatus.UNCONFIRMED,
                        extractor=OntologyExtractor.ASYNC,
                        asserted_at_sequence=at_sequence,
                        evidence_event_sequences=(event.sequence,),
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                )
                continue
            if event.event_type is not EventType.MESSAGE_CREATED:
                continue
            message_id = str(event.payload.get("message_id") or "")
            message = await self.repos.messages.get(message_id) if message_id else None
            if message is None or message.room_id != room_id:
                continue
            edge = self._blocking_edge(message.content, tasks_by_label)
            if edge is None:
                continue
            blocker, blocked = edge
            relationships.append(
                OntologyRelationship(
                    relationship_id=self._ontology_id(
                        "rel", room_id, "BLOCKS", blocker.entity_id, blocked.entity_id
                    ),
                    room_id=room_id,
                    kind=OntologyRelationshipKind.BLOCKS,
                    from_entity_id=blocker.entity_id,
                    to_entity_id=blocked.entity_id,
                    derivation_kind=OntologyDerivationKind.AI_DERIVED,
                    confidence=_INFERRED_CONFIDENCE,
                    evidence_ids=(message_id,),
                    source_ids=(message_id,),
                    review_status=OntologyReviewStatus.UNCONFIRMED,
                    # The durable row whose content states the blockage, not an
                    # endpoint: the message is what reported it.
                    source_object_kind="Message",
                    source_object_id=message_id,
                    extractor=OntologyExtractor.ASYNC,
                    asserted_at_sequence=at_sequence,
                    evidence_event_sequences=(event.sequence,),
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
        return entities, relationships

    @classmethod
    def _blocking_edge(
        cls, content: str, tasks_by_label: dict[str, OntologyEntity]
    ) -> tuple[OntologyEntity, OntologyEntity] | None:
        """One fixed form over already-materialized tasks; there is no open-ended read."""
        normalized = " ".join(content.strip().lower().split()).rstrip(".!?")
        if cls._BLOCKED_BY not in normalized:
            return None
        blocked_label, _, blocker_label = normalized.partition(cls._BLOCKED_BY)
        blocked = tasks_by_label.get(blocked_label.strip())
        blocker = tasks_by_label.get(blocker_label.strip())
        if blocked is None or blocker is None or blocked.entity_id == blocker.entity_id:
            return None
        return blocker, blocked

    async def _consolidate(
        self, room_id: str, head: int
    ) -> tuple[list[OntologyRelationship], list[str]]:
        """Relate and supersede existing assertions. It never reads raw evidence.

        Deduplication has nothing to remove: assertions carry deterministic IDs under
        a UNIQUE(room_id, kind, source_object_id) index, so a duplicate cannot be
        written in the first place. What is left is contradiction detection and the
        staleness marking that follows from it. Nothing here deletes: a removed
        assertion cannot be audited.
        """
        entities = await self.repos.ontology.list_entities(room_id)
        claims = [entity for entity in entities if entity.kind is OntologyEntityKind.CLAIM]
        by_label = {claim.label.strip().lower().rstrip("."): claim for claim in claims}
        timestamp = utcnow()
        relationships: list[OntologyRelationship] = []
        stale_ids: list[str] = []
        for claim in claims:
            label = claim.label.strip().lower().rstrip(".")
            if not label.startswith("not "):
                continue
            target = by_label.get(label[4:].strip())
            if target is None or target.entity_id == claim.entity_id:
                continue
            relationships.append(
                OntologyRelationship(
                    relationship_id=self._ontology_id(
                        "rel", room_id, "CONTRADICTS", claim.entity_id, target.entity_id
                    ),
                    room_id=room_id,
                    kind=OntologyRelationshipKind.CONTRADICTS,
                    from_entity_id=claim.entity_id,
                    to_entity_id=target.entity_id,
                    # What this pass thinks its own detection is worth. The shared
                    # repository method lowers it to the weakest of the two claims it
                    # relates, which is why a consolidation edge over two unconfirmed
                    # entities cannot reach a reader as confirmed truth.
                    derivation_kind=OntologyDerivationKind.SYSTEM_MATERIALIZED,
                    confidence=1.0,
                    evidence_ids=claim.evidence_ids,
                    source_ids=(claim.source_object_id, target.source_object_id),
                    source_object_kind=OntologyEntityKind.CLAIM.value,
                    source_object_id=claim.source_object_id,
                    extractor=OntologyExtractor.SCHEDULED,
                    asserted_at_sequence=head,
                    evidence_event_sequences=claim.evidence_event_sequences,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
            if target.asserted_at_sequence < claim.asserted_at_sequence:
                stale_ids.append(target.entity_id)
        return relationships, stale_ids

    # ── Meta ─────────────────────────────────────────────────────────────────

    _META_ENTITY_KINDS: dict[MetaQuestionKind, tuple[OntologyEntityKind, ...]] = {
        MetaQuestionKind.STATUS: (OntologyEntityKind.TASK, OntologyEntityKind.DECISION),
        # Scoped to work objects, never actors, so this query shape cannot become a
        # monitoring feed.
        MetaQuestionKind.CHANGES: (
            OntologyEntityKind.TASK,
            OntologyEntityKind.DECISION,
            OntologyEntityKind.ARTIFACT,
            OntologyEntityKind.CLAIM,
        ),
        MetaQuestionKind.DECISIONS: (OntologyEntityKind.DECISION,),
    }
    _META_RELATIONSHIP_KINDS: dict[MetaQuestionKind, tuple[OntologyRelationshipKind, ...]] = {
        MetaQuestionKind.STATUS: (OntologyRelationshipKind.OWNS,),
        MetaQuestionKind.BLOCKERS: (OntologyRelationshipKind.BLOCKS,),
        MetaQuestionKind.DECISIONS: (OntologyRelationshipKind.SUPPORTS,),
        MetaQuestionKind.DISAGREEMENT: (OntologyRelationshipKind.CONTRADICTS,),
    }
    _DISAGREEMENT_ENDPOINTS = frozenset({OntologyEntityKind.CLAIM, OntologyEntityKind.AGENT_OUTPUT})

    @staticmethod
    def _meta_question_kind(question: str) -> MetaQuestionKind:
        """Refuse first, match exactly second, refuse again otherwise."""
        return classify_meta_question(question)

    @staticmethod
    def _meta_assurance(
        derivation_kind: OntologyDerivationKind, review_status: OntologyReviewStatus
    ) -> OntologyAssurance:
        """What a reader is entitled to treat this assertion as."""
        if review_status is not OntologyReviewStatus.UNCONFIRMED:
            return OntologyAssurance.CONFIRMED
        if derivation_kind is OntologyDerivationKind.SYSTEM_MATERIALIZED:
            return OntologyAssurance.SYSTEM_MATERIALIZED
        return OntologyAssurance.UNCONFIRMED_AI

    def _meta_claim_record(
        self,
        *,
        assertion_id: str,
        assertion_type: str,
        kind: str,
        label: str,
        properties: dict[str, Any],
        derivation_kind: OntologyDerivationKind,
        confidence: float,
        review_status: OntologyReviewStatus,
        evidence_ids: tuple[str, ...],
        source_object_kind: str,
        source_object_id: str,
        asserted_at_sequence: int,
        evidence_event_sequences: tuple[int, ...],
        stale_at_sequence: int | None,
        currency: tuple[bool, int],
        review: OntologyReview | None,
    ) -> dict[str, Any]:
        assurance = self._meta_assurance(derivation_kind, review_status)
        current, invalidating = currency
        record: dict[str, Any] = {
            "assertion_id": assertion_id,
            "assertion_type": assertion_type,
            "kind": kind,
            "label": label,
            # An unreviewed extraction is never rendered as a plain statement.
            "text": f"{_UNCONFIRMED_TEMPLATE}: {label}"
            if assurance is OntologyAssurance.UNCONFIRMED_AI
            else label,
            "properties": properties,
            "assurance": assurance.value,
            "derivation_kind": derivation_kind.value,
            "confidence": confidence,
            "review_status": review_status.value,
            "evidence_ids": list(evidence_ids),
            "source_object_kind": source_object_kind,
            "source_object_id": source_object_id,
            "asserted_at_sequence": asserted_at_sequence,
            "evidence_event_sequences": list(evidence_event_sequences),
            "stale_at_sequence": stale_at_sequence,
            "current": current,
            "invalidating_events": invalidating,
        }
        if assurance is OntologyAssurance.CONFIRMED and review is not None:
            record["review_id"] = review.review_id
            record["reviewed_by"] = review.reviewed_by
        return record

    async def _meta_currency(
        self,
        room_id: str,
        user_id: str,
        head: int,
        positions: list[tuple[str, int, tuple[str, ...]]],
    ) -> dict[str, tuple[bool, int]]:
        """Derive currency per assertion, one grouped read per class, never per claim."""
        grouped: dict[tuple[str, ...], list[tuple[str, int]]] = {}
        for assertion_id, sequence, event_class in positions:
            grouped.setdefault(event_class, []).append((assertion_id, sequence))
        currency: dict[str, tuple[bool, int]] = {}
        for event_class, members in grouped.items():
            floor = min(sequence for _assertion_id, sequence in members)
            sequences = await self.repos.meta.invalidating_sequences(
                room_id, user_id, event_class, floor, head
            )
            for assertion_id, sequence in members:
                invalidating = sum(1 for item in sequences if item > sequence)
                currency[assertion_id] = (invalidating == 0, invalidating)
        return currency

    async def _meta_freshness(
        self,
        room_id: str,
        user_id: str,
        head: int,
        claims: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Freshness, computed inside the authorized scope like every other aggregate."""
        cursors = await self.repos.meta.extraction_cursors(room_id, user_id)
        # An extractor with no cursor row has drained nothing, so it is the furthest
        # behind, not absent. Reading only the rows that exist made a room whose
        # asynchronous drain had never run report that everything was current — and
        # nothing wakes that drain today, so it is the ordinary case, not an edge.
        drained_to = min(cursors.get(extractor.value, 0) for extractor in OntologyExtractor)
        positions = [
            int(claim["asserted_at_sequence"]) for claim in claims if not claim.get("hidden")
        ]
        return {
            "authorized_head": head,
            # Pending work a reader can see; it decides nothing they are shown.
            "drain_lag_events": max(0, head - drained_to),
            "claims_as_of": min(positions) if positions else None,
        }

    @staticmethod
    def _meta_summary(
        kind: MetaQuestionKind, claims: list[dict[str, Any]], distinct_sources: int
    ) -> str:
        """Prose over an already-authorized claim set; unconfirmed labels never enter it."""
        if not claims:
            return "no confirmed assertions in this room answer that question"
        labels = "; ".join(str(claim["label"]) for claim in claims)
        if kind is MetaQuestionKind.STATUS:
            counts: dict[str, int] = {}
            for claim in claims:
                if claim["assertion_type"] != "ENTITY":
                    continue
                status = str(claim["properties"].get("status", "UNKNOWN"))
                counts[status] = counts.get(status, 0) + 1
            grouped = ", ".join(f"{status} {count}" for status, count in sorted(counts.items()))
            return f"{len(claims)} governed assertions describe where things stand ({grouped})"
        if kind is MetaQuestionKind.BLOCKERS:
            return f"{len(claims)} blocking relationships: {labels}"
        if kind is MetaQuestionKind.CHANGES:
            latest = max(int(claim["asserted_at_sequence"]) for claim in claims)
            return f"{len(claims)} work objects changed, latest at sequence {latest}: {labels}"
        if kind is MetaQuestionKind.DECISIONS:
            return f"{len(claims)} decisions carry governed support: {labels}"
        return f"{len(claims)} contradictions from {distinct_sources} distinct sources: {labels}"

    def _meta_envelope(
        self,
        *,
        question: str,
        kind: MetaQuestionKind,
        room_id: str,
        limit: int,
        claims: list[dict[str, Any]],
        unconfirmed: list[dict[str, Any]],
        freshness: dict[str, Any],
        summary: str,
        refusal_reason: MetaRefusalReason,
    ) -> dict[str, Any]:
        """The shared answer envelope. "We do not know" is a real answer at HTTP 200."""
        if claims:
            status = MetaAnswerStatus.ANSWERED
        elif unconfirmed:
            status = MetaAnswerStatus.ANSWERED_UNCONFIRMED_ONLY
        else:
            status = MetaAnswerStatus.REFUSED
        return {
            "query": {
                "question": question,
                "kind": kind.value,
                "supported_kinds": [member.value for member in MetaQuestionKind],
            },
            "status": status.value,
            "refusal_reason": (
                refusal_reason.value if status is MetaAnswerStatus.REFUSED else None
            ),  # named only when the answer is a refusal
            "summary": summary,
            # Two result sets, never merged: merging them would require code that
            # does not exist, which is a stronger guarantee than a naming convention.
            "claims": claims,
            "unconfirmed": unconfirmed,
            "counts": {
                # Unconfirmed extractions are excluded from every figure presented
                # as fact, and counted separately.
                "claims": len(claims),
                "unconfirmed": len(unconfirmed),
                "current_claims": sum(1 for claim in claims if claim["current"]),
                "max_claims": limit,
            },
            "freshness": freshness,
            "scope": {"room_id": room_id, "max_claims": limit},
        }

    async def answer_decision_meta(
        self,
        room_id: str,
        question: str,
        *,
        user_id: str,
        version_id: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Answer one bounded Meta question from current governed assertions."""
        question_kind = self._meta_question_kind(question)
        if not 1 <= limit <= 10:
            raise DomainError("Meta evidence limit must be between 1 and 10")
        await self.get_room(room_id)
        if question_kind in DECISION_KINDS:
            return await self._answer_decision_meta(
                room_id, user_id, question, question_kind, version_id, limit
            )
        return await self._answer_assertion_meta(room_id, user_id, question, question_kind, limit)

    async def _answer_assertion_meta(
        self,
        room_id: str,
        user_id: str,
        question: str,
        kind: MetaQuestionKind,
        limit: int,
    ) -> dict[str, Any]:
        head = await self.repos.meta.head(room_id, user_id)
        if head is None:
            # Nothing this reader may see, so no head, no counts and no other
            # aggregate — a consequence of the query, not a special case.
            return self._meta_envelope(
                question=question,
                kind=kind,
                room_id=room_id,
                limit=limit,
                claims=[],
                unconfirmed=[],
                freshness={},
                summary="no authorized evidence in this room answers that question",
                refusal_reason=MetaRefusalReason.NO_AUTHORIZED_EVIDENCE,
            )
        entities = await self.repos.meta.entities(
            room_id,
            user_id,
            self._META_ENTITY_KINDS.get(kind, ()),
            since_sequence=0 if kind is MetaQuestionKind.CHANGES else None,
            limit=limit,
        )
        relationships = await self.repos.meta.relationships(
            room_id, user_id, self._META_RELATIONSHIP_KINDS.get(kind, ()), limit=limit
        )
        endpoint_ids = sorted(
            {
                entity_id
                for item in relationships
                for entity_id in (item.from_entity_id, item.to_entity_id)
            }
        )
        endpoints = {
            entity.entity_id: entity
            for entity in await self.repos.meta.entities_by_ids(room_id, user_id, endpoint_ids)
        }
        entity_ids = {entity.entity_id for entity in entities}
        relationships = [
            item
            for item in relationships
            if self._meta_edge_in_scope(kind, item, entity_ids, endpoints)
        ]
        positions: list[tuple[str, int, tuple[str, ...]]] = [
            (entity.entity_id, entity.asserted_at_sequence, invalidation_class(entity.kind))
            for entity in entities
        ]
        positions.extend(
            (
                item.relationship_id,
                item.asserted_at_sequence,
                invalidation_class(
                    endpoints[item.from_entity_id].kind, endpoints[item.to_entity_id].kind
                ),
            )
            for item in relationships
        )
        currency = await self._meta_currency(room_id, user_id, head, positions)

        records: list[dict[str, Any]] = []
        for entity in entities:
            records.append(
                self._meta_claim_record(
                    assertion_id=entity.entity_id,
                    assertion_type="ENTITY",
                    kind=entity.kind.value,
                    label=entity.label,
                    properties=entity.properties,
                    derivation_kind=entity.derivation_kind,
                    confidence=entity.confidence,
                    review_status=entity.review_status,
                    evidence_ids=entity.evidence_ids,
                    source_object_kind=entity.kind.value,
                    source_object_id=entity.source_object_id,
                    asserted_at_sequence=entity.asserted_at_sequence,
                    evidence_event_sequences=entity.evidence_event_sequences,
                    stale_at_sequence=entity.stale_at_sequence,
                    currency=currency[entity.entity_id],
                    review=await self.repos.meta.latest_review(room_id, user_id, entity.entity_id),
                )
            )
        for item in relationships:
            source = endpoints[item.from_entity_id]
            target = endpoints[item.to_entity_id]
            records.append(
                self._meta_claim_record(
                    assertion_id=item.relationship_id,
                    assertion_type="RELATIONSHIP",
                    kind=item.kind.value,
                    label=f"{source.label} {item.kind.value} {target.label}",
                    properties={},
                    derivation_kind=item.derivation_kind,
                    confidence=item.confidence,
                    review_status=item.review_status,
                    evidence_ids=item.evidence_ids,
                    source_object_kind=item.source_object_kind,
                    source_object_id=item.source_object_id,
                    asserted_at_sequence=item.asserted_at_sequence,
                    evidence_event_sequences=item.evidence_event_sequences,
                    stale_at_sequence=item.stale_at_sequence,
                    currency=currency[item.relationship_id],
                    review=await self.repos.meta.latest_review(
                        room_id, user_id, item.relationship_id
                    ),
                )
            )
        claims = [
            record
            for record in records
            if record["assurance"] != OntologyAssurance.UNCONFIRMED_AI.value
        ]
        unconfirmed = [
            record
            for record in records
            if record["assurance"] == OntologyAssurance.UNCONFIRMED_AI.value
        ]
        distinct_sources = len(
            {
                str(endpoints[item.from_entity_id].properties.get("agent_id", ""))
                for item in relationships
            }
            - {""}
        )
        return self._meta_envelope(
            question=question,
            kind=kind,
            room_id=room_id,
            limit=limit,
            claims=claims,
            unconfirmed=unconfirmed,
            freshness=await self._meta_freshness(room_id, user_id, head, records),
            summary=self._meta_summary(kind, claims, distinct_sources),
            refusal_reason=MetaRefusalReason.NO_ASSERTIONS_IN_SCOPE,
        )

    @staticmethod
    def _meta_edge_in_scope(
        kind: MetaQuestionKind,
        relationship: OntologyRelationship,
        entity_ids: set[str],
        endpoints: dict[str, OntologyEntity],
    ) -> bool:
        """An edge whose endpoints this reader may not see is not part of the answer."""
        if (
            relationship.from_entity_id not in endpoints
            or relationship.to_entity_id not in endpoints
        ):
            return False
        if kind in {MetaQuestionKind.STATUS, MetaQuestionKind.DECISIONS}:
            return relationship.to_entity_id in entity_ids
        if kind is MetaQuestionKind.DISAGREEMENT:
            return (
                endpoints[relationship.from_entity_id].kind
                in MultiplayerService._DISAGREEMENT_ENDPOINTS
                and endpoints[relationship.to_entity_id].kind
                in MultiplayerService._DISAGREEMENT_ENDPOINTS
            )
        return True

    async def _answer_decision_meta(
        self,
        room_id: str,
        user_id: str,
        question: str,
        question_kind: MetaQuestionKind,
        version_id: str | None,
        limit: int,
    ) -> dict[str, Any]:
        """The frozen-provenance chain, unchanged, inside the authorized scope."""
        head = await self.repos.meta.head(room_id, user_id)
        if head is None:
            return self._meta_envelope(
                question=question,
                kind=question_kind,
                room_id=room_id,
                limit=limit,
                claims=[],
                unconfirmed=[],
                freshness={},
                summary="no authorized evidence in this room answers that question",
                refusal_reason=MetaRefusalReason.NO_AUTHORIZED_EVIDENCE,
            )
        resolved = await self.repos.artifacts.resolve_decision_version(room_id, version_id)
        if resolved is None:
            raise DomainError("decision artifact version not found in room")
        artifact, version = resolved
        provenance, available_claims = await self.repos.artifacts.get_version_provenance_bounded(
            version.version_id, limit
        )
        decision = await self.repos.meta.entity_by_source(
            room_id, user_id, OntologyEntityKind.DECISION, version.version_id
        )
        if decision is None:
            raise DomainError("decision ontology is not available for artifact version")
        decision_review = await self.repos.meta.latest_review(room_id, user_id, decision.entity_id)

        chains: list[dict[str, Any]] = []
        for source in provenance:
            claim = await self.repos.meta.entity_by_source(
                room_id, user_id, OntologyEntityKind.CLAIM, str(source["claim_id"])
            )
            output = await self.repos.meta.entity_by_source(
                room_id, user_id, OntologyEntityKind.AGENT_OUTPUT, str(source["output_id"])
            )
            if claim is None or output is None:
                raise DomainError("decision evidence chain is incomplete")
            claim_to_decision = await self.repos.meta.relationship_between(
                room_id, user_id, claim.entity_id, decision.entity_id
            )
            claim_to_output = await self.repos.meta.relationship_between(
                room_id, user_id, claim.entity_id, output.entity_id
            )
            if claim_to_decision is None or claim_to_output is None:
                raise DomainError("decision evidence relationship is incomplete")
            claim_review = await self.repos.meta.latest_review(room_id, user_id, claim.entity_id)
            output_review = await self.repos.meta.latest_review(room_id, user_id, output.entity_id)
            decision_link_review = await self.repos.meta.latest_review(
                room_id, user_id, claim_to_decision.relationship_id
            )
            output_link_review = await self.repos.meta.latest_review(
                room_id, user_id, claim_to_output.relationship_id
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
                    "_assertions": (claim, claim_to_decision),
                    "_reviews": (claim_review, decision_link_review),
                }
            )

        # Only reviewed claims are named as fact; an unreviewed extraction reaches the
        # reader through unconfirmed[] and its hedged template, never through prose.
        current_claims = [
            str(chain["claim"]["label"])
            for chain in chains
            if self._meta_assurance(
                chain["_assertions"][0].derivation_kind,
                chain["_assertions"][0].review_status,
            )
            is not OntologyAssurance.UNCONFIRMED_AI
        ]
        relationship_counts: dict[str, int] = {}
        for chain in chains:
            kind = str(chain["relationships"]["claim_to_decision"]["kind"])
            relationship_counts[kind] = relationship_counts.get(kind, 0) + 1
        relationship_summary = ", ".join(
            f"{kind} {count}" for kind, count in sorted(relationship_counts.items())
        )
        if question_kind is MetaQuestionKind.WHY_DECISION:
            summary = (
                f"{decision.label} has {len(chains)} deliberately selected "
                f"claim{'s' if len(chains) != 1 else ''} ({relationship_summary}): "
                + ("; ".join(current_claims) or "none of them reviewed yet")
            )
        else:
            summary = (
                f"{len(chains)} selected AgentOutput"
                f"{'s' if len(chains) != 1 else ''} are linked to {decision.label} "
                f"through governed claims ({relationship_summary})."
            )

        positions: list[tuple[str, int, tuple[str, ...]]] = [
            (
                decision.entity_id,
                decision.asserted_at_sequence,
                invalidation_class(decision.kind),
            )
        ]
        for chain in chains:
            claim_entity, link = chain["_assertions"]
            positions.append(
                (
                    claim_entity.entity_id,
                    claim_entity.asserted_at_sequence,
                    invalidation_class(claim_entity.kind),
                )
            )
            positions.append(
                (
                    link.relationship_id,
                    link.asserted_at_sequence,
                    invalidation_class(claim_entity.kind, decision.kind),
                )
            )
        currency = await self._meta_currency(room_id, user_id, head, positions)
        # Retrieval is bounded, so the answer names only the evidence it retrieved.
        bounded_evidence = tuple(
            str(chain["exact_source_evidence"]["output_id"]) for chain in chains
        )
        records = [
            self._meta_claim_record(
                assertion_id=decision.entity_id,
                assertion_type="ENTITY",
                kind=decision.kind.value,
                label=decision.label,
                properties=decision.properties,
                derivation_kind=decision.derivation_kind,
                confidence=decision.confidence,
                review_status=decision.review_status,
                evidence_ids=bounded_evidence,
                source_object_kind=decision.kind.value,
                source_object_id=decision.source_object_id,
                asserted_at_sequence=decision.asserted_at_sequence,
                evidence_event_sequences=decision.evidence_event_sequences,
                stale_at_sequence=decision.stale_at_sequence,
                currency=currency[decision.entity_id],
                review=decision_review,
            )
        ]
        for chain in chains:
            claim_entity, link = chain["_assertions"]
            claim_review, link_review = chain.pop("_reviews")
            del chain["_assertions"]
            records.append(
                self._meta_claim_record(
                    assertion_id=claim_entity.entity_id,
                    assertion_type="ENTITY",
                    kind=claim_entity.kind.value,
                    label=claim_entity.label,
                    properties=claim_entity.properties,
                    derivation_kind=claim_entity.derivation_kind,
                    confidence=claim_entity.confidence,
                    review_status=claim_entity.review_status,
                    evidence_ids=claim_entity.evidence_ids,
                    source_object_kind=claim_entity.kind.value,
                    source_object_id=claim_entity.source_object_id,
                    asserted_at_sequence=claim_entity.asserted_at_sequence,
                    evidence_event_sequences=claim_entity.evidence_event_sequences,
                    stale_at_sequence=claim_entity.stale_at_sequence,
                    currency=currency[claim_entity.entity_id],
                    review=claim_review,
                )
            )
            records.append(
                self._meta_claim_record(
                    assertion_id=link.relationship_id,
                    assertion_type="RELATIONSHIP",
                    kind=link.kind.value,
                    label=f"{claim_entity.label} {link.kind.value} {decision.label}",
                    properties={},
                    derivation_kind=link.derivation_kind,
                    confidence=link.confidence,
                    review_status=link.review_status,
                    evidence_ids=link.evidence_ids,
                    source_object_kind=link.source_object_kind,
                    source_object_id=link.source_object_id,
                    asserted_at_sequence=link.asserted_at_sequence,
                    evidence_event_sequences=link.evidence_event_sequences,
                    stale_at_sequence=link.stale_at_sequence,
                    currency=currency[link.relationship_id],
                    review=link_review,
                )
            )
        claims = [
            record
            for record in records
            if record["assurance"] != OntologyAssurance.UNCONFIRMED_AI.value
        ]
        unconfirmed = [
            record
            for record in records
            if record["assurance"] == OntologyAssurance.UNCONFIRMED_AI.value
        ]
        envelope = self._meta_envelope(
            question=question,
            kind=question_kind,
            room_id=room_id,
            limit=limit,
            claims=claims,
            unconfirmed=unconfirmed,
            freshness=await self._meta_freshness(room_id, user_id, head, records),
            summary=summary,
            refusal_reason=MetaRefusalReason.NO_ASSERTIONS_IN_SCOPE,
        )
        envelope["scope"] = {
            "room_id": room_id,
            "artifact_id": artifact.artifact_id,
            "version_id": version.version_id,
            "version_number": version.version_number,
            "max_claims": limit,
        }
        envelope["decision"] = {
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
        }
        envelope["evidence_chains"] = chains
        envelope["freshness"] = {
            **envelope["freshness"],
            "artifact_created_at": version.created_at.isoformat(),
            "decision_updated_at": decision.updated_at.isoformat(),
        }
        envelope["retrieval_counts"] = {
            "available_claims": available_claims,
            "returned_claims": len(chains),
            "returned_outputs": len(chains),
            "truncated": available_claims > len(chains),
        }
        envelope["provenance"] = {
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
        }
        return envelope

    async def review_ontology_entity(
        self,
        room_id: str,
        entity_id: str,
        action: OntologyReviewAction,
        reviewed_by: str,
        reason: str,
        *,
        require_member: bool = False,
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
            if require_member:
                await self._require_mutate_in_transaction(room_id, reviewed_by)
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
        require_member: bool = False,
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
            if require_member:
                await self._require_mutate_in_transaction(room_id, reviewed_by)
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

    @staticmethod
    def _thread_state(message: Message, summaries: dict[str, ThreadSummary]) -> dict[str, Any]:
        """How a channel describes one message's thread, every field counted on read.

        The whole thread, not just the direct answers: a message with no thread has
        no replies, no later reply time, and one participant — its own author.

        A reply broadcast to the channel is summarised by the thread it belongs to,
        not by itself. Summaries are keyed on roots, so looking one up by the reply's
        own id found nothing and the channel drew it as a message with no thread at
        all — offering "Reply" on something that was already an answer. It is told
        here that it is a reply, and which conversation it came out of.
        """
        root_id = message.root_message_id or message.message_id
        summary = summaries.get(root_id)
        return {
            "reply_count": summary.descendant_count if summary else 0,
            "participant_count": summary.participant_count if summary else 1,
            "last_reply_at": (
                summary.last_reply_at.isoformat()
                if summary is not None and summary.last_reply_at is not None
                else None
            ),
            "is_thread_reply": message.root_message_id is not None,
            "thread_root_id": root_id,
        }

    async def get_room_state(
        self, room_id: str, last_sequence: int = 0, user_id: str = ""
    ) -> dict[str, Any]:
        room = await self.get_room(room_id)
        events = await self.get_room_events(room_id, last_sequence)
        members = await self.get_room_members(room_id)
        agents = await self.list_room_agents(room_id)
        # The roster is where a reader learns what to type: a participant whose
        # handle the client cannot see is a participant nobody can address.
        handles = {
            (record.participant_type.value, record.participant_id): record.handle
            for record in await self.repos.handles.list_by_room(room_id)
        }
        tasks = await self.list_room_tasks(room_id)
        messages = await self.list_room_messages(room_id, limit=50)
        thread_summaries = await self.repos.messages.thread_summaries_by_room(room_id)
        reactions: dict[str, list[dict[str, str]]] = {}
        for reaction in await self.repos.reactions.list_live_by_room(room_id):
            reactions.setdefault(reaction.message_id, []).append(
                {
                    "emoji": reaction.emoji,
                    "actor_id": reaction.actor_id,
                    "actor_type": reaction.actor_type.value,
                }
            )
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
            "members": [
                {
                    "user_id": m.user_id,
                    "role": m.role,
                    "handle": handles.get((ParticipantType.USER.value, m.user_id), ""),
                }
                for m in members
            ],
            "agents": [
                {
                    "agent_id": a.agent_id,
                    "name": a.name,
                    "handle": handles.get((ParticipantType.AGENT.value, a.agent_id), ""),
                    "role": a.role,
                    "status": a.status.value,
                }
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
                    # Half of "why did this agent speak"; the other half is the event.
                    "triggered_by": run.triggered_by.value,
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
                    "metadata": m.metadata,
                    "sequence": m.event_sequence,
                    "parent_message_id": m.parent_message_id,
                    "root_message_id": m.root_message_id,
                    "thread_depth": m.thread_depth,
                    "broadcast_to_room": m.broadcast_to_room,
                    # Derived here too: the snapshot never carries a stored counter.
                    **self._thread_state(m, thread_summaries),
                    "reactions": reactions.get(m.message_id, []),
                    "created_at": m.created_at.isoformat(),
                }
                for m in messages
            ],
            "read_cursor": (await self.get_read_cursor(room_id, user_id) if user_id else None),
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
