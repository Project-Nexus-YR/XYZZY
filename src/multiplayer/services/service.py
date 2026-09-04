"""Core service layer: orchestrates domain operations across repos, events, and NEXUS."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..db.connection import Database
from ..db.repositories import Repos
from ..domain.agent_card import DEFAULT_OUTPUT_MODES
from ..domain.agent_tasks import (
    AgentTask,
    AgentTaskMessage,
    AgentTaskState,
    Part,
    PartKind,
    TaskMessageRole,
    TaskNotCancelableError,
    TaskNotFoundError,
    negotiate_output_modes,
    new_context_id,
    require_delegable,
    require_transition,
)
from ..domain.events import EventType, RoomEvent
from ..domain.meta import (
    DECISION_KINDS,
    REFUSAL_PREFIX,
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
    ArtifactShare,
    ArtifactType,
    ArtifactVersion,
    Attachment,
    BootstrapContext,
    Branch,
    BranchMode,
    BranchStatus,
    BranchSynthesis,
    BranchSynthesisInput,
    BranchSynthesisStatus,
    ClaimSource,
    Decision,
    DecisionStatus,
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
    RoomStatus,
    RoomTemplate,
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
    User,
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
from ..metrics import Metrics
from ..model_providers import ModelProviderError
from ..nexus_bridge.agent_bridge import NexusAgentBridge
from ..realtime.hub import RealtimeHub
from ..security.audit import GENESIS_HASH, event_chain_hash, verify_event_chain
from ..security.authorization import (
    AuthorizationError,
    RoomCapability,
    RoomPolicy,
    capabilities_for_role,
)
from ..security.boundary import agent_turn, require_human_boundary
from ..security.capabilities import (
    AGENT_PRINCIPAL_PREFIX,
    CAPABILITIES,
    BoundingPrincipals,
    CapabilityTerms,
    GatewayDecision,
    Posture,
    RunAuthorization,
    UnboundedTerms,
    agent_principal,
    allowed_tools,
    decide,
    delegating_agent_id,
    may_address,
    policy_capabilities,
    under_posture,
    user_capabilities,
)
from ..security.identity import (
    credential_hash,
    credential_matches,
    new_launch_challenge,
    new_run_credential,
    verify_challenge_answer,
)
from ..security.screening import fenced, screen
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


class MultiplayerService:
    def __init__(
        self,
        db: Database,
        hub: RealtimeHub,
        known_users: frozenset[str] | None = None,
        presence_redis: Any | None = None,
        metrics: Metrics | None = None,
        nexus: NexusAgentBridge | None = None,
    ) -> None:
        self.db = db
        self.repos = Repos(db)
        self.hub = hub
        self.presence = PresenceService(redis_client=presence_redis)
        self.metrics = metrics
        self.nexus = nexus if nexus is not None else NexusAgentBridge(db_path=":memory:")
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
        # Holds a strong reference to every background dispatch this process has
        # scheduled, so the event loop cannot garbage-collect a task nobody is
        # awaiting out from under it mid-flight; the done callback below is what
        # lets each one go once it finishes.
        self._background_tasks: set[asyncio.Task[None]] = set()
        # Set for real by _apply_migrations, before _backfill_event_chain ever
        # reads it. False here is the safe default: a backfill that has not
        # been told this is the migration's first boot must not touch anything.
        self._event_chain_migration_is_new = False

    def _record_model_tokens(self, payload: dict[str, Any]) -> None:
        """Count what a provider said it spent; a missing or odd value counts nothing."""
        if self.metrics is None:
            return
        tokens = payload.get("token_usage", 0)
        if isinstance(tokens, int):
            self.metrics.record_model_tokens(tokens)

    async def initialize(self) -> None:
        await self._apply_migrations(Path(__file__).parent.parent / "migrations")
        await self._backfill_event_chain()
        await self._backfill_legacy_artifact_provenance_hashes()
        await self._backfill_participant_handles()
        # Objects written before their kind joined the search allowlist.
        await self.repos.search.backfill()
        await self._seed_default_templates()
        await self._settle_orphaned_mention_runs()
        await self.sweep_expired_run_leases()
        # Constant-work recovery, same as the run-lease sweep above: a crash
        # between an A2A accept and the background dispatch it schedules is
        # the only way a task sits SUBMITTED past the staleness threshold, so
        # a restart heals it here rather than leaving it stranded forever.
        await self.sweep_stale_submitted_agent_tasks()
        # The other half of that recovery: a task a harder kill left WORKING
        # behind a run that sweep_expired_run_leases just settled (or that
        # settled some other way) is failed here too, so a restart is enough
        # even when nothing this process runs afterward will ever revisit it.
        await self.sweep_stranded_working_agent_tasks()

    # The migration that added prev_hash/event_hash. Rows written before it ran
    # are the only ones a startup backfill has any business filling in; whether
    # this boot is the one that just applied it (read from schema_migrations
    # before this boot touched that table) is what tells a legacy row, never
    # hashed, from a tampered one, hashed once and since cleared.
    _EVENT_CHAIN_MIGRATION_NAME = "033_the_log_commits_to_its_past.sql"

    async def _backfill_event_chain(self) -> None:
        """Hash events written before the chain existed, room by room, in order.

        Only rows whose event_hash is NULL are touched, so a tampered stored
        hash is never papered over by a fresh recomputation. Runs only on the
        boot that applies the migration adding these columns: on every later
        boot, that migration is already on record, so a NULL event_hash found
        then is not a legacy row waiting on this method, it is tampering, and
        this method is not the one that gets to decide that quietly.
        """
        if not self._event_chain_migration_is_new:
            return
        rooms = await self.db.fetch_all(
            "SELECT DISTINCT room_id FROM room_events WHERE event_hash IS NULL"
        )
        for room_row in rooms:
            room_id = str(room_row["room_id"])
            async with self.db.transaction():
                rows = await self.db.fetch_all(
                    "SELECT event_id, sequence, event_type, payload, actor_id, actor_type, "
                    "timestamp, schema_version, event_hash "
                    "FROM room_events WHERE room_id = ? ORDER BY sequence",
                    (room_id,),
                )
                prev_hash = GENESIS_HASH
                for row in rows:
                    if row["event_hash"] is not None:
                        prev_hash = str(row["event_hash"])
                        continue
                    event_hash = event_chain_hash(
                        prev_hash,
                        str(row["event_id"]),
                        room_id,
                        int(row["sequence"]),
                        str(row["event_type"]),
                        str(row["payload"]),
                        str(row["actor_id"]),
                        str(row["actor_type"]),
                        str(row["timestamp"]),
                        int(row["schema_version"]),
                    )
                    await self.db.execute(
                        "UPDATE room_events SET prev_hash = ?, event_hash = ? WHERE event_id = ?",
                        (prev_hash, event_hash, str(row["event_id"])),
                    )
                    prev_hash = event_hash

    async def _apply_migrations(self, migrations_dir: Path) -> None:
        """Apply each pending migration and the row recording it as one commit.

        A crash mid-migration leaves the database exactly at the previous
        migration: the script's statements and its schema_migrations row are one
        transaction, so nothing half-applied is ever marked done, and nothing
        applied is ever left unmarked to fail on replay. A migration that uses
        the sanctioned rebuild recipe declares PRAGMA foreign_keys=OFF, which a
        transaction would silently ignore, so that toggle is hoisted onto the
        connection around the transaction.
        """
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied_rows = await self.db.fetch_all("SELECT name FROM schema_migrations")
        applied = {str(row["name"]) for row in applied_rows}
        # Read before this boot applies anything: true only for the one boot
        # that is about to apply the event chain migration for the first time.
        self._event_chain_migration_is_new = self._EVENT_CHAIN_MIGRATION_NAME not in applied
        for migration_file in sorted(migrations_dir.glob("*.sql")):
            if migration_file.name in applied:
                continue
            body = migration_file.read_text()
            # A body that commits inside the wrapper would leave its own DDL
            # committed but unrecorded on a later failure - wedged forever.
            # (A stray BEGIN needs no guard: it fails as a nested transaction
            # and rolls back cleanly.)
            if re.search(r"(?im)(?:^|;)\s*(COMMIT|ROLLBACK)\b", body):
                raise RuntimeError(f"migration {migration_file.name} manages its own transaction")
            # Case and whitespace insensitive, and blind to a comment mentioning the
            # pragma rather than issuing it: a substring match on the raw body would
            # miss a respelled pragma (extra spaces, lower case) and would also fire
            # on a comment that merely names the literal, disabling FK enforcement
            # for a migration that never asked for that.
            body_without_comments = re.sub(r"--[^\n]*", "", body)
            wants_foreign_keys_off = bool(
                re.search(
                    r"(?i)\bPRAGMA\s+foreign_keys\s*=\s*(OFF|0|false)\b", body_without_comments
                )
            )
            record = (
                "INSERT INTO schema_migrations(name, applied_at) VALUES "
                f"('{migration_file.name.replace(chr(39), chr(39) * 2)}', "
                f"'{utcnow().isoformat()}');"
            )
            if wants_foreign_keys_off:
                await self.db.execute("PRAGMA foreign_keys=OFF")
            try:
                await self.db.execute_script(f"BEGIN IMMEDIATE;\n{body}\n{record}\nCOMMIT;")
            except Exception as exc:
                with suppress(Exception):
                    await self.db.execute("ROLLBACK")
                raise RuntimeError(f"migration {migration_file.name} failed") from exc
            finally:
                if wants_foreign_keys_off:
                    await self.db.execute("PRAGMA foreign_keys=ON")

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
                orphan.execution_id,
                "dispatcher stopped before the run started",
                RunSettlement.ORPHANED,
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

    async def seed_demo_workspace(self) -> None:
        """Populate an empty demo deployment with one realistic, offline scene.

        Guarded on organizations existing at all, not on a flag row: a database
        that already has a workspace was seeded by an earlier startup of this
        same demo, or holds a real one, and either way there is nothing left
        for this call to add. That makes the guard the idempotence itself —
        a second startup finds a non-empty table and returns immediately.
        Every write below goes through the same service methods an HTTP
        caller would use, so it picks up every invariant those methods
        enforce for free, and needs no API key: leaving model_provider and
        model_name unset resolves to the SIMULATED provider, same as any
        other room with no provider configured.
        """
        if await self.db.fetch_one("SELECT 1 FROM organizations LIMIT 1") is not None:
            return
        _org, _workspace, room = await self.bootstrap_user_workspace(
            DEMO_USER_ID, "Yasser", "General"
        )
        room_id = room.room_id
        if await self.repos.users.get(DEMO_SECOND_USER_ID) is None:
            await self.repos.users.create(
                User(
                    user_id=DEMO_SECOND_USER_ID,
                    display_name="Amira",
                    email=f"{DEMO_SECOND_USER_ID}@demo.local",
                )
            )
        await self.invite_room_member(room_id, DEMO_SECOND_USER_ID, "editor", DEMO_USER_ID)
        demo_third_user_id = "user_demo_third"
        if await self.repos.users.get(demo_third_user_id) is None:
            await self.repos.users.create(
                User(
                    user_id=demo_third_user_id,
                    display_name="Karim",
                    email=f"{demo_third_user_id}@demo.local",
                )
            )
        await self.invite_room_member(room_id, demo_third_user_id, "editor", DEMO_USER_ID)

        async def say(sender: str, content: str, parent_message_id: str | None = None) -> str:
            message = await self.send_message(
                room_id,
                MessageRole.HUMAN,
                sender,
                content,
                parent_message_id=parent_message_id,
                invoke_mentioned_agents=False,
            )
            return message.message_id

        m1 = await say(
            DEMO_USER_ID,
            "Morning - picking up the payments-provider decision. Stripe vs Adyen vs "
            "building on our bank's raw API.",
        )
        await say(
            DEMO_SECOND_USER_ID,
            "Finance wants an answer by Thursday. The contract renewal is the forcing function.",
        )
        m3 = await say(
            DEMO_USER_ID,
            "Main unknowns for me: EU settlement times, and what the migration costs us "
            "in engineering weeks.",
        )
        await say(
            DEMO_SECOND_USER_ID,
            "I'll pull our current chargeback numbers so the branches have real inputs.",
        )
        await say(
            DEMO_USER_ID,
            "Adyen quotes T+1 for EU settlement on their site - worth verifying in the branch run.",
            parent_message_id=m3,
        )
        await say(
            DEMO_SECOND_USER_ID,
            "Our bank's API settles T+2 at best, and that's before reconciliation.",
            parent_message_id=m3,
        )
        await say(
            demo_third_user_id,
            "Watching from the finance side. Ping me once the branch has numbers, "
            "I'll sanity-check them against last quarter's chargeback report.",
        )
        await self.add_reaction(m1, DEMO_SECOND_USER_ID, "\U0001f44d")

        templates = (await self.list_agent_templates())[:2]
        agent_ids = []
        for template in templates:
            agent = await self.spawn_agent(
                room_id,
                template.template_id,
                template.name,
                requested_by=DEMO_USER_ID,
                require_member=True,
            )
            agent_ids.append(agent.agent_id)
        branch, runs = await self.start_branch(
            room_id,
            BranchMode.PARALLEL,
            "Compare Stripe, Adyen, and our bank's raw API for EU card payments: "
            "settlement time, fees at our volume, and migration effort.",
            DEMO_USER_ID,
            agent_ids,
        )
        for run in runs:
            await self.execute_branch_run(branch.branch_id, run.execution_id, DEMO_USER_ID)
        # Every output must be decided before a synthesis can read the branch, and
        # each one is included here so the seeded brief has both perspectives in it.
        for output in await self.list_room_outputs(room_id):
            await self.select_output(
                room_id, output.output_id, OutputDisposition.INCLUDED, DEMO_USER_ID
            )
        await self.synthesize_branch_decision_brief(
            branch.branch_id, "Decision Brief", DEMO_USER_ID
        )
        await say(
            DEMO_SECOND_USER_ID,
            "Reading the brief now. The settlement-time claim needs a source before we commit.",
        )
        await say(DEMO_USER_ID, "Agreed - flagged it in Evidence. Let's decide Thursday morning.")

        # Seeding runs in one instant, which stamps every message with the same
        # minute and makes the scene read as the fixture it is. Spread the
        # message rows back across a plausible stretch of morning instead. Only
        # messages.created_at moves: room_events keep their true times, so the
        # hash chain over the event log is untouched and still verifies.
        rows = await self.db.fetch_all(
            "SELECT message_id FROM messages WHERE room_id = ? ORDER BY created_at, message_id",
            (room_id,),
        )
        gaps_minutes = [0, 4, 9, 2, 7, 3, 12, 5, 8, 6, 4, 10]
        start = utcnow() - timedelta(minutes=sum(gaps_minutes[: len(rows)]) + 3)
        elapsed = start
        for i, row in enumerate(rows):
            elapsed += timedelta(minutes=gaps_minutes[i % len(gaps_minutes)])
            await self.db.execute(
                "UPDATE messages SET created_at = ? WHERE message_id = ?",
                (elapsed.isoformat(), row["message_id"]),
            )

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
        # One transaction, like create_room: a failure between the two writes
        # would otherwise leave a memberless org, invisible to list_for_user,
        # unadministrable, and holding its globally unique slug forever.
        async with self.db.transaction():
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
            # The typed display name is only ever known here; a session-authenticated
            # principal has no other path that records it. Heal it in on every
            # bootstrap call, fresh or idempotent, without touching an existing row.
            if await self.repos.users.get(user_id) is None:
                await self.repos.users.create(
                    User(
                        user_id=user_id,
                        display_name=display_name,
                        # No email is known at bootstrap; users.email is UNIQUE, so a
                        # per-user placeholder keeps two bootstraps from colliding.
                        email=f"{user_id}@bootstrap.local",
                    )
                )

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
        # Same guarantee as create_organization, for the same reason: a memberless
        # workspace is invisible, unadministrable, and undeletable through the API.
        async with self.db.transaction():
            await self.repos.workspaces.create(ws)
            await self.repos.workspaces.add_member(
                WorkspaceMember(workspace_id=ws.workspace_id, user_id=creator_id, role="admin")
            )
        return ws

    async def list_workspaces(self, org_id: str) -> list[Workspace]:
        return await self.repos.workspaces.list_by_org(org_id)

    # ── Room ─────────────────────────────────────────────────────────────────

    async def create_room(
        self,
        workspace_id: str,
        name: str,
        creator_id: str,
        description: str = "",
        room_template_id: str | None = None,
    ) -> Room:
        name = self._validate_non_empty(name, "room name")
        room = Room(
            room_id=new_id("room"),
            workspace_id=workspace_id,
            name=name,
            description=description,
            created_by=creator_id,
        )
        room_template: RoomTemplate | None = None
        if room_template_id is not None:
            room_template = await self.repos.room_templates.get(room_template_id)
            if room_template is None or room_template.deleted_at is not None:
                raise DomainError(f"room template not found: {room_template_id}")
            if room_template.workspace_id != workspace_id:
                raise DomainError(f"room template not found in workspace: {room_template_id}")
        async with self.db.transaction():
            # Serializing the duplicate check and the insert turns a concurrent
            # duplicate create into a clean rejection rather than two identical
            # sidebar entries.
            existing = await self.repos.rooms.list_by_workspace(workspace_id)
            if any(
                r.status != RoomStatus.ARCHIVED and r.name.casefold() == name.casefold()
                for r in existing
            ):
                raise DomainError("a channel with that name already exists")
            # A recipe is read once, at save time, and again here, fresh: a
            # specialist it named can have been deleted or unshared since. This
            # room must not exist half-populated, so the whole create is refused
            # before a single row is written.
            spawn_templates: list[AgentTemplate] = []
            if room_template is not None:
                for agent_template_id in room_template.agent_template_ids:
                    agent_template = await self.repos.agents.get_template(agent_template_id)
                    if agent_template is None or not await self._agent_template_usable_in_workspace(
                        agent_template, workspace_id
                    ):
                        raise DomainError(
                            "room template names a specialist no longer available: "
                            f"{agent_template_id}"
                        )
                    spawn_templates.append(agent_template)
            await self.repos.rooms.create(room)
            await self.repos.room_members.add(
                RoomMember(room_id=room.room_id, user_id=creator_id, role="admin")
            )
            await self._issue_handle(room.room_id, ParticipantType.USER, creator_id, creator_id)
            payload: dict[str, Any] = {"name": name, "description": description}
            if room_template_id is not None:
                payload["room_template_id"] = room_template_id
            events = [
                await self.repos.events.append_with_next_sequence_in_transaction(
                    RoomEvent(
                        room_id=room.room_id,
                        sequence=0,
                        event_type=EventType.ROOM_CREATED,
                        payload=payload,
                        actor_id=creator_id,
                        actor_type="user",
                    )
                )
            ]
            # The room row and every preselected specialist commit or roll back
            # together: writing the spawns here, inside this same transaction,
            # rather than as separate spawn_agent calls after commit, closes the
            # 19th appearance of the check-then-use class — a template deleted or
            # unshared in the gap between commit and a later spawn call would
            # otherwise leave a committed room half-populated.
            for agent_template in spawn_templates:
                if agent_template.workspace_id is not None:
                    agent_template_prompt = fenced(
                        screen(agent_template.system_prompt, "agent template")
                    )
                else:
                    agent_template_prompt = agent_template.system_prompt
                # Template spawns carry no caller-declared model identity, so
                # resolution can never refuse here; it still runs so the row
                # stores the configured identity, same as any direct spawn.
                resolved_provider, resolved_model = self._resolve_model_identity("", "")
                _agent, agent_events = await self._spawn_agent_writes_in_transaction(
                    room.room_id,
                    agent_template,
                    agent_template_prompt,
                    None,
                    None,
                    resolved_provider,
                    resolved_model,
                    creator_id,
                    NEXUS_HARNESS_ID,
                    AddressingMode.ANYONE,
                    room,
                )
                events.extend(agent_events)
        await self._broadcast_persisted_events(events)
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
        require_human_boundary("member.invite")
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
            room_for_membership = await self.repos.rooms.get(room_id)
            await self.repos.room_members.add(member)
            if room_for_membership is not None:
                # Mirror bootstrap: a room member without workspace membership gets
                # 403 "workspace access forbidden" on every workspace-scoped route.
                # Never overwrite an existing row/role - this only fills a gap.
                await self.repos.workspaces.add_member_if_absent(
                    WorkspaceMember(
                        workspace_id=room_for_membership.workspace_id,
                        user_id=invited_user_id,
                        role="member",
                    )
                )
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
            # The durable half of telling them: a live socket message reaches
            # only whoever is connected this instant, and an invitation is
            # exactly the message someone offline must still find later.
            room_name = room_for_membership.name if room_for_membership else room_id
            inviter_names = await self.repos.room_members.display_names(room_id)
            await self.repos.notifications.create(
                Notification(
                    notification_id=new_id("notif"),
                    user_id=invited_user_id,
                    room_id=room_id,
                    title=f"You were invited to #{room_name}",
                    body=f"Invited as {role} by {inviter_names.get(invited_by, invited_by)}",
                    notification_type="invitation",
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
        require_human_boundary("member.leave")
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
        """Change another member's access, including promoting to or demoting from admin.

        Demoting the room's last admin is refused - the channel must always keep one,
        the same invariant leave_room enforces. Changing your own membership is still
        leave_room's job, not this route's.
        """
        require_human_boundary("member.role")
        if role not in {"viewer", "editor", "admin"}:
            raise DomainError("member role must be viewer, editor, or admin")
        if user_id == changed_by:
            raise DomainError("use leave to change your own membership")
        async with self.db.transaction():
            # Re-read the changer's authority inside BEGIN IMMEDIATE, like every
            # other ADMINISTER write here: an admin removed after the route
            # authorized them must not still hand out admin.
            await self._require_capability_in_transaction(
                room_id, changed_by, RoomCapability.ADMINISTER
            )
            member = await self.repos.room_members.get(room_id, user_id)
            if member is None:
                raise DomainError("user is not a channel member")
            if member.role == role:
                return member
            if member.role == "admin" and role != "admin":
                others = await self.repos.room_members.list(room_id)
                if not any(o.user_id != user_id and o.role == "admin" for o in others):
                    raise DomainError("cannot demote the last admin of the room")
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
        require_human_boundary("room.policy")
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

    async def declare_room_posture(self, room_id: str, posture: Posture, declared_by: str) -> str:
        """Say how much of this channel's work stops at a human. Never what it may do.

        Administering the channel, because raising the bar and lowering it are the
        same act seen from two sides and both are governance: the check is on the
        write, so a posture cannot be reached through any door that is not this one.
        require_human_boundary is that sentence for the agent surface.

        Loosening is permitted, and the reason is that a posture which only rises
        makes one mistaken STRICT permanent and the channel disposable; the harm a
        one-way rule would prevent does not exist here, because the posture is read
        once, when a call is decided, so loosening cannot reach a call already parked
        at a reviewer — that call is released by the reviewer or by nobody.

        Nothing is overwritten. The declaration is a row, so what governed an action
        stays answerable from records that could not have changed since.
        """
        require_human_boundary("room.posture")
        async with self.db.transaction():
            await self._require_capability_in_transaction(
                room_id, declared_by, RoomCapability.ADMINISTER
            )
            declaration_id = await self.repos.room_postures.declare(room_id, posture, declared_by)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.ROOM_POSTURE_DECLARED,
                    payload={
                        "declaration_id": declaration_id,
                        "posture": posture.value,
                        "declared_by": declared_by,
                    },
                    actor_id=declared_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return declaration_id

    async def set_member_capabilities(
        self, room_id: str, user_id: str, allowed: list[str] | None, changed_by: str
    ) -> None:
        """Bound what one member may lend to the agents they run. None restores the role default."""
        require_human_boundary("member.capabilities")
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
        require_human_boundary("workspace.policy")
        stored = _policy_json(allowed)
        events: list[RoomEvent] = []
        async with self.db.transaction():
            # A workspace-wide bound outranks any single room's, so it demands
            # the workspace admin role, re-read inside the transaction that
            # writes - the same fence every room-tier governance write has.
            member = await self.repos.workspaces.get_member(workspace_id, changed_by)
            if member is None or member.role != "admin":
                raise AuthorizationError("workspace access forbidden")
            await self.repos.workspaces.set_allowed_capabilities(workspace_id, stored)
            for room in await self.repos.rooms.list_by_workspace(workspace_id):
                events.append(
                    await self.repos.events.append_with_next_sequence_in_transaction(
                        RoomEvent(
                            room_id=room.room_id,
                            sequence=0,
                            event_type=EventType.WORKSPACE_POLICY_UPDATED,
                            payload={
                                "workspace_id": workspace_id,
                                "allowed_capabilities": allowed,
                            },
                            actor_id=changed_by,
                            actor_type="user",
                        )
                    )
                )
        await self._broadcast_persisted_events(events)

    async def remove_room_member(self, room_id: str, user_id: str, removed_by: str) -> None:
        """Revoke a non-admin member's access, including any live realtime subscription."""
        require_human_boundary("member.remove")
        if user_id == removed_by:
            raise DomainError("use leave to remove yourself")
        async with self.db.transaction():
            # The route authorized ADMINISTER; a demotion committing in between
            # must not let a former admin's removal land. Same fence as invite.
            await self._require_capability_in_transaction(
                room_id, removed_by, RoomCapability.ADMINISTER
            )
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

    async def list_workspace_agent_templates(self, workspace_id: str) -> list[AgentTemplate]:
        """Built-ins plus this workspace's own live templates."""
        return await self.repos.agents.list_visible_to_workspace(workspace_id)

    async def _is_shared_into(self, template: AgentTemplate, target_workspace_id: str) -> bool:
        """Whether a shared template is currently spawnable from another workspace.

        Re-read fresh at every call site, never cached: unsetting shared_at must
        revoke spawnability from outside the origin workspace immediately.
        """
        if template.shared_at is None or template.workspace_id is None:
            return False
        target = await self.repos.workspaces.get(target_workspace_id)
        origin = await self.repos.workspaces.get(template.workspace_id)
        return target is not None and origin is not None and target.org_id == origin.org_id

    async def _agent_template_usable_in_workspace(
        self, template: AgentTemplate, workspace_id: str
    ) -> bool:
        """A built-in, this workspace's own live template, or one shared into it."""
        if template.deleted_at is not None:
            return False
        if template.workspace_id is None or template.workspace_id == workspace_id:
            return True
        return await self._is_shared_into(template, workspace_id)

    async def create_agent_template(
        self, workspace_id: str, name: str, role: str, system_prompt: str, created_by: str
    ) -> AgentTemplate:
        """A workspace-authored specialist. Its prompt is member text, not developer text."""
        require_human_boundary("agent_template.create")
        name = self._validate_non_empty(name, "template name")
        role = self._validate_non_empty(role, "template role")
        system_prompt = self._validate_non_empty(system_prompt, "template system_prompt")
        template = AgentTemplate(
            template_id=new_id("tmpl"),
            name=name,
            description="",
            role=role,
            system_prompt=system_prompt,
            # The creation body names no capabilities (spec: {name, role, system_prompt}),
            # and the five-way intersection in _lendable_terms bounds a run by the
            # narrowest of user/agent/skill/channel/workspace — an empty skill term
            # here would make every agent spawned from this template unusable by
            # anyone, forever. The built-ins each carry a real, non-empty subset for
            # the same reason; this grants the full set and lets the other four terms
            # do the actual narrowing, same as an "admin"/"editor" room role does.
            capabilities=CAPABILITIES,
            workspace_id=workspace_id,
            created_by=created_by,
        )
        async with self.db.transaction():
            # The route already confirmed membership; re-read it here so a removal
            # committing in between cannot let a former member's write land.
            await self.authorization.require_workspace_member(workspace_id, created_by)
            existing = await self.repos.agents.list_visible_to_workspace(workspace_id)
            if any(t.name.casefold() == name.casefold() for t in existing):
                raise DomainError(f"a template named {name!r} already exists in this workspace")
            await self.repos.agents.create_template(template)
        return template

    async def delete_agent_template(
        self, workspace_id: str, template_id: str, requested_by: str
    ) -> None:
        require_human_boundary("agent_template.delete")
        async with self.db.transaction():
            template = await self.repos.agents.get_template(template_id)
            if template is None:
                raise DomainError(f"agent template not found: {template_id}")
            if template.workspace_id is None:
                raise DomainError("built-in agent templates cannot be deleted")
            if template.workspace_id != workspace_id:
                raise DomainError(f"agent template not found in workspace: {template_id}")
            member = await self.repos.workspaces.get_member(workspace_id, requested_by)
            is_admin = member is not None and member.role == "admin"
            if not is_admin and requested_by != template.created_by:
                raise AuthorizationError("workspace access forbidden")
            # Agents already spawned from this template copied its fields onto
            # themselves at spawn time, so marking it deleted rather than removing
            # the row breaks nothing they still read, and keeps the FK
            # agent_instances.template_id holds against this row intact.
            await self.repos.agents.soft_delete_template(template_id, utcnow())

    async def list_org_shared_agent_templates(self, workspace_id: str) -> list[AgentTemplate]:
        """Live templates other workspaces in this workspace's organization shared."""
        workspace = await self.repos.workspaces.get(workspace_id)
        if workspace is None:
            raise DomainError(f"workspace not found: {workspace_id}")
        return await self.repos.agents.list_shared_for_org(workspace.org_id, workspace_id)

    async def share_agent_template(
        self, workspace_id: str, template_id: str, requested_by: str
    ) -> AgentTemplate:
        """Distribution/trust machinery beyond the organization stays parked (spec §G):
        this only flips org-wide visibility on, owned and retractable by this workspace.
        """
        require_human_boundary("agent_template.share")
        async with self.db.transaction():
            template = await self.repos.agents.get_template(template_id)
            if template is None:
                raise DomainError(f"agent template not found: {template_id}")
            if template.workspace_id is None:
                raise DomainError("built-in agent templates are already global")
            if template.workspace_id != workspace_id:
                raise DomainError(f"agent template not found in workspace: {template_id}")
            if template.deleted_at is not None:
                raise DomainError(f"agent template was deleted: {template_id}")
            member = await self.repos.workspaces.get_member(workspace_id, requested_by)
            is_admin = member is not None and member.role == "admin"
            if not is_admin and requested_by != template.created_by:
                raise AuthorizationError("workspace access forbidden")
            shared_at = utcnow()
            await self.repos.agents.share_template(template_id, shared_at)
        return replace(template, shared_at=shared_at)

    async def unshare_agent_template(
        self, workspace_id: str, template_id: str, requested_by: str
    ) -> AgentTemplate:
        require_human_boundary("agent_template.unshare")
        async with self.db.transaction():
            template = await self.repos.agents.get_template(template_id)
            if template is None:
                raise DomainError(f"agent template not found: {template_id}")
            if template.workspace_id != workspace_id:
                raise DomainError(f"agent template not found in workspace: {template_id}")
            member = await self.repos.workspaces.get_member(workspace_id, requested_by)
            is_admin = member is not None and member.role == "admin"
            if not is_admin and requested_by != template.created_by:
                raise AuthorizationError("workspace access forbidden")
            await self.repos.agents.unshare_template(template_id)
        return replace(template, shared_at=None)

    # ── Room templates ───────────────────────────────────────────────────────

    async def create_room_template(
        self,
        workspace_id: str,
        name: str,
        description: str,
        agent_template_ids: list[str],
        created_by: str,
    ) -> RoomTemplate:
        """A workspace's saved room recipe. Every specialist it names must be one
        this workspace could spawn right now, or the recipe would make a promise
        room creation could not keep."""
        require_human_boundary("room_template.create")
        name = self._validate_non_empty(name, "room template name")
        template = RoomTemplate(
            template_id=new_id("rtmpl"),
            workspace_id=workspace_id,
            name=name,
            description=description,
            agent_template_ids=tuple(agent_template_ids),
            created_by=created_by,
        )
        async with self.db.transaction():
            await self.authorization.require_workspace_member(workspace_id, created_by)
            existing = await self.repos.room_templates.list_live_by_workspace(workspace_id)
            if any(t.name.casefold() == name.casefold() for t in existing):
                raise DomainError(
                    f"a room template named {name!r} already exists in this workspace"
                )
            for agent_template_id in agent_template_ids:
                agent_template = await self.repos.agents.get_template(agent_template_id)
                if agent_template is None or not await self._agent_template_usable_in_workspace(
                    agent_template, workspace_id
                ):
                    raise DomainError(
                        f"agent template not spawnable in this workspace: {agent_template_id}"
                    )
            await self.repos.room_templates.create(template)
        return template

    async def list_room_templates(self, workspace_id: str) -> list[RoomTemplate]:
        return await self.repos.room_templates.list_live_by_workspace(workspace_id)

    async def delete_room_template(
        self, workspace_id: str, template_id: str, requested_by: str
    ) -> None:
        require_human_boundary("room_template.delete")
        async with self.db.transaction():
            template = await self.repos.room_templates.get(template_id)
            if template is None or template.deleted_at is not None:
                raise DomainError(f"room template not found: {template_id}")
            if template.workspace_id != workspace_id:
                raise DomainError(f"room template not found in workspace: {template_id}")
            member = await self.repos.workspaces.get_member(workspace_id, requested_by)
            is_admin = member is not None and member.role == "admin"
            if not is_admin and requested_by != template.created_by:
                raise AuthorizationError("workspace access forbidden")
            await self.repos.room_templates.soft_delete(template_id, utcnow())

    def _resolve_model_identity(self, model_provider: str, model_name: str) -> tuple[str, str]:
        """Refuse configuration a spawn cannot honor; fill in what it means when unset.

        A non-empty ``model_provider``/``model_name`` that disagrees with the
        provider this process actually runs would let the API accept
        configuration it silently ignores, and the mismatch would then read
        back from the agent row as if it had been honored. Empty stays
        allowed and means "the configured provider" - which is what actually
        runs - and is stored as such so the row describes itself.
        """
        configured_provider, configured_model = self.nexus.provider_identity
        if model_provider and model_provider != configured_provider:
            raise DomainError(
                f"model provider {model_provider!r} was requested but this deployment "
                f"runs {configured_provider!r}"
            )
        if model_name and model_name != configured_model:
            raise DomainError(
                f"model {model_name!r} was requested but this deployment runs {configured_model!r}"
            )
        return model_provider or configured_provider, model_name or configured_model

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
    ) -> tuple[AgentInstance, list[RoomEvent]]:
        """The write phase of a spawn, assuming the caller already holds an open
        transaction and has already validated the template. Shared by spawn_agent's
        own transaction and by create_room's room-plus-recipe transaction, so a
        room created from a template either commits with every specialist or not
        at all — never half-populated.
        """
        agent = AgentInstance(
            agent_id=new_id("agent"),
            template_id=template.template_id,
            room_id=room_id,
            name=name or template.name,
            role=template.role,
            system_prompt=system_prompt or template_system_prompt,
            capabilities=template.capabilities,
            model_provider=model_provider,
            model_name=model_name,
            harness_id=harness_id,
        )
        identity = AgentIdentity(
            identity_id=new_id("ident"),
            agent_id=agent.agent_id,
            proof_mode=ProofMode.IN_PROCESS,
        )
        addressing = AgentAddressing(
            agent_id=agent.agent_id,
            room_id=room_id,
            mode=addressing_mode,
            owner_user_id=requested_by or (room.created_by if room is not None else ""),
            updated_by=requested_by or "system",
        )
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
        return agent, events

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
        require_human_boundary("agent.spawn")
        model_provider, model_name = self._resolve_model_identity(model_provider, model_name)
        template = await self.repos.agents.get_template(template_id)
        if not template:
            raise DomainError(f"agent template not found: {template_id}")
        if template.deleted_at is not None:
            raise DomainError(f"agent template was deleted: {template_id}")
        if harness_id not in KNOWN_HARNESS_IDS:
            raise DomainError(f"no harness is registered as {harness_id!r}")
        room = await self.repos.rooms.get(room_id)
        cross_workspace = False
        if template.workspace_id is not None:
            if room is None:
                raise DomainError(f"agent template {template_id} belongs to a different workspace")
            cross_workspace = template.workspace_id != room.workspace_id
            if cross_workspace and not await self._is_shared_into(template, room.workspace_id):
                raise DomainError(f"agent template {template_id} belongs to a different workspace")
            # A workspace member wrote this prompt, not this deployment's developer.
            # It reaches the model exactly like any other member-authored text does,
            # whether the spawning room belongs to the authoring workspace or to
            # another workspace this template was shared into.
            template_system_prompt = fenced(screen(template.system_prompt, "agent template"))
        else:
            template_system_prompt = template.system_prompt
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(room_id, requested_by)
            if cross_workspace:
                # The check-then-use class this schema has relocated eighteen
                # times (033-040): re-read shared_at fresh, inside the
                # transaction that spawns, so an unshare committing in between
                # the check above and this write revokes spawnability in time.
                assert room is not None
                fresh_template = await self.repos.agents.get_template(template_id)
                if fresh_template is None or not await self._is_shared_into(
                    fresh_template, room.workspace_id
                ):
                    raise DomainError(
                        f"agent template {template_id} belongs to a different workspace"
                    )
            agent, events = await self._spawn_agent_writes_in_transaction(
                room_id,
                template,
                template_system_prompt,
                name,
                system_prompt,
                model_provider,
                model_name,
                requested_by,
                harness_id,
                addressing_mode,
                room,
            )
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
        require_human_boundary("agent.addressing")
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
        require_human_boundary("agent.identity.revoke")
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
        self,
        execution_id: str,
        state: HarnessState,
        acting_user_id: str,
        lease: timedelta,
        expected: HarnessState | None = None,
    ) -> bool:
        """Move the envelope and renew its lease. A settled run never moves.

        ``expected``, when given, refuses the move unless the run is still in
        that state, so a caller can tell a genuine advance from a race that
        already moved the run somewhere else.
        """
        run = await self.repos.agent_runs.get_by_execution(execution_id)
        if run is None or run.harness_state is HarnessState.SETTLED:
            return False
        if expected is not None and run.harness_state is not expected:
            return False
        return await self.repos.agent_runs.advance(
            run.run_id,
            state,
            utcnow() + lease,
            acting_user_id or run.acting_user_id,
            expected=expected,
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
        # Nothing will prompt a settled run again, so the turn held for a reviewer is
        # not waiting any more either — and neither is the approval it was held for.
        await self.repos.suspended_turns.discard(run.execution_id)
        await self._expire_undecided_approvals(run.execution_id, error)
        await self._broadcast_persisted_events(events)
        return True

    async def sweep_expired_run_leases(self) -> int:
        """Settle every run whose lease ran out, so none sits unclaimed by anything.

        A run picked up its full allowance of attempts that died every time is PARKED
        rather than ORPHANED. Both are terminal; the difference is what a reader is
        told about why nothing is coming, which is the whole point of settling it.

        A run holding at a reviewer is a third thing, and it used to be told the
        second one: nothing was orphaned, nothing was dispatched and lost, and no
        attempt was spent — a person simply never answered. Naming that outcome is
        only half of it, because the approval row it belongs to sat PENDING for ever
        against a run that had ended. It is closed with the run, in
        :meth:`_settle_run`, rather than only here.
        """
        settled = 0
        for run in await self.repos.agent_runs.list_expired(utcnow()):
            if run.harness_state is HarnessState.AWAITING_APPROVAL:
                settlement = RunSettlement.APPROVAL_EXPIRED
                error = "no reviewer decided the approval this run was waiting on"
            elif run.attempts >= run.max_attempts:
                settlement = RunSettlement.PARKED
                error = f"lease expired after {run.attempts} attempt(s)"
            else:
                settlement = RunSettlement.ORPHANED
                error = f"lease expired after {run.attempts} attempt(s)"
            if await self._settle_run(run, settlement, "system", error):
                settled += 1
        # The periodic caller of this method (server.py's lease-sweep loop) is
        # the only thing that revisits a long-lived process's runs at all, so
        # it is also the thing that has to notice a task stranded WORKING
        # behind one of the runs just settled above, or by anything else.
        await self.sweep_stranded_working_agent_tasks()
        return settled

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

    async def remove_agent_from_room(
        self, agent_id: str, room_id: str, removed_by: str, *, require_member: bool = False
    ) -> None:
        """Take an agent out of a room and settle everything it had in flight.

        Settlement is decided here and telling the harness is best-effort, so an
        in-flight turn can still land. What stops it writing is the settled-run refusal
        inside complete_execution, not the credential.
        """
        require_human_boundary("agent.remove")
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
            # These runs settle here rather than through _settle_run, so the turn any
            # of them was holding at a reviewer is released here too, in the same
            # transaction. Nothing prompts a settled run again.
            for run in settled:
                await self.repos.suspended_turns.discard(run.execution_id)
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
        # The approvals those runs were holding at end with them. It happens outside
        # the transaction above because closing one is a transaction of its own, and
        # the alternative — leaving it — is the row that outlives what it gated.
        for run in settled:
            await self._expire_undecided_approvals(run.execution_id, "agent removed from room")
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

    async def rejoin_agent_to_room(
        self, agent_id: str, room_id: str, rejoined_by: str, *, require_member: bool = False
    ) -> AgentRoomMembership:
        """Put a removed agent back in a room, as a new membership beside the old one.

        Rejoining had no path at all: ``add_room_membership`` is INSERT OR IGNORE, so
        it silently no-opped against the removed row, and no verb reached it. The
        only thing that did work was reversing the removal in the database, which
        erased the departure — which is why the schema now refuses that and this
        writes a new row naming the departure it follows instead. The record shows
        the agent joined, left, and came back; nothing in it is overwritten.

        ADMINISTER, the same grant removal takes: putting an agent back in a channel
        is a membership change, and the removal it reverses was one.
        """
        require_human_boundary("agent.rejoin")
        agent = await self.get_agent(agent_id)
        if agent.room_id != room_id:
            raise DomainError("agent is not in this room")
        async with self.db.transaction():
            if require_member:
                await self._require_capability_in_transaction(
                    room_id, rejoined_by, RoomCapability.ADMINISTER
                )
            previous = await self.repos.agents.latest_membership(agent_id, room_id)
            if previous is None:
                raise DomainError(f"agent {agent_id} has never been a member of room {room_id}")
            if previous.removed_at is None:
                raise DomainError(f"agent {agent_id} is already in room {room_id}")
            membership = AgentRoomMembership(
                agent_id=agent_id,
                room_id=room_id,
                rejoined_from_membership_id=previous.membership_id,
            )
            await self.repos.agents.rejoin_room_membership_in_transaction(membership)
            # The handle went back to the room with the membership, so the returning
            # agent is addressed again rather than staying unmentionable.
            handle = await self._issue_handle(room_id, ParticipantType.AGENT, agent_id, agent.name)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.AGENT_REJOINED_ROOM,
                    payload={
                        "agent_id": agent_id,
                        "handle": handle,
                        "rejoined_by": rejoined_by,
                        "membership_id": membership.membership_id,
                        "rejoined_from_membership_id": previous.membership_id,
                        "left_at": previous.removed_at.isoformat(),
                    },
                    actor_id=rejoined_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return membership

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
                "schema": "xyzzy.branch-context.v1",
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
        # The snapshot is member-authored room history - the widest untrusted
        # surface any prompt carries - so it enters screened and fenced.
        return (
            f"Branch prompt:\n{branch.initiating_prompt}\n\n"
            f"Immutable bounded channel context (hash {branch.context_hash}):\n"
            f"{fenced(screen(snapshot, 'channel context'))}"
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

    async def _principal_term(self, room_id: str, principal: str) -> frozenset[str]:
        """What one principal may lend an agent here, whichever kind it is.

        A human lends from durable room membership. A delegating agent lends from
        its own capability row, and never more than it holds — which is the whole
        reason one agent asking another cannot become a way to obtain something
        the asker was itself refused.

        Both are read here rather than at the call sites. A call site that has to
        know which kind of principal it is holding is a call site that will get it
        wrong for the third kind, and being one participant short is how the same
        defect was relocated thirteen times.

        A delegator that has left the room lends nothing. That is re-read at every
        spend, so removing an agent mid-delegation stops the delegate too, rather
        than leaving it running on authority its asker no longer has.
        """
        delegator_id = delegating_agent_id(principal)
        if delegator_id is None:
            return await self._user_term(room_id, principal)
        delegator = await self.repos.agents.get_instance(delegator_id)
        if delegator is None or not await self.repos.agents.has_room_membership(
            delegator_id, room_id
        ):
            return frozenset()
        return frozenset(delegator.capabilities)

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

    async def _authorized_terms(self, authorization: RunAuthorization) -> CapabilityTerms:
        """The one derivation a spend-point spends. Nothing else produces spendable terms.

        Thirteen rounds relocated one defect because the bound was applied by
        remembering which identities to apply, and the list was one short every time:
        a spend-point re-derived the five terms from durable records — correctly, in
        itself — and did not know it also owed a steerer, or a caller who was not the
        run's own principal.

        Nothing is enumerated here now. The authorization carries every bounding
        principal as one set, this reads what each of them may lend right now, and
        :meth:`UnboundedTerms.spend_under` refuses to produce a spendable set for any
        run but the one they were read for. A spend-point written next year gets all
        of it by consuming the object it already had to consume.
        """
        agent = await self.get_agent(authorization.agent_id)
        unbounded = await self._lendable_terms(agent, authorization.room_id, authorization.bounding)
        return unbounded.spend_under(authorization)

    async def _authorization_for(
        self,
        execution_id: str,
        agent_id: str,
        room_id: str,
        required_capability: str = "",
    ) -> RunAuthorization:
        """Build the authority object every spend-point re-derives its terms from.

        The single place a ``RunAuthorization`` is made, and it takes no principal:
        there is no argument through which a caller could hand it a set that is
        missing one. Who bounds this run is a question the durable rows answer, in one
        read, and that read is the only thing that fills the set.
        """
        run = await self.repos.agent_runs.get_by_execution(execution_id)
        return RunAuthorization(
            run_id=run.run_id if run is not None else execution_id,
            agent_id=agent_id,
            room_id=room_id,
            bounding=BoundingPrincipals.read_from(
                await self.repos.executions.bounding_principals(execution_id)
            ),
            required_capability=required_capability,
        )

    @staticmethod
    def _step_schema(effective: frozenset[str]) -> dict[str, Any]:
        """Offer only the tools this run may call, so the rest are unavailable.

        And only the actions the server acts on. "delegate" and "wait" were offered
        and no branch handled either: a model that picked one ended its step having
        neither answered nor called a tool, and the run was left STREAMING for the
        lease sweep to mislabel a quarter of an hour later. Offering an action nobody
        implements is the same defect as leaving a tool unguarded, pointed the other
        way. A harness that answers outside this schema is still settled, below.
        """
        offered = allowed_tools(effective)
        properties: dict[str, Any] = {
            "action": {"type": "string", "enum": ["finish"]},
            "output": {"type": "object"},
        }
        if offered:
            properties["action"]["enum"] = ["tool", *properties["action"]["enum"]]
            properties["tool"] = {"type": "string", "enum": offered}
            properties["input"] = {"type": "object"}
        return {"type": "object", "properties": properties, "required": ["action"]}

    async def agent_capability_terms(
        self, room_id: str, agent_id: str, requested_by: str
    ) -> UnboundedTerms:
        """What this member could lend this agent, for a run they have not opened yet.

        A preview, not a spend: there is no run, so this member is the whole of it.

        The room is required and checked. Resolving the terms from the agent's own
        room while the caller was authorized against a different one let anyone who
        could read any room read any agent's channel and workspace policy anywhere -
        the caller passed a room they owned and named an agent belonging to someone
        else's workspace.
        """
        agent = await self.get_agent(agent_id)
        if agent.room_id != room_id:
            raise DomainError("agent is not in this room")
        return await self._lendable_terms(
            agent, room_id, BoundingPrincipals(frozenset({requested_by}))
        )

    async def _require_delegated_authority(self, execution: Execution, acting_as: str) -> None:
        """Guard every verb that advances or influences somebody else's run.

        Room MUTATE says the caller may act in this channel; it does not say what
        this run may do on their behalf. What the caller may lend is re-derived here
        from durable records, and a caller narrower than the authorizing principal is
        refused when the intersection is empty.

        What the run may then spend is not decided here. That is
        :meth:`_authorized_terms`, which narrows by every principal bounding the run
        at once; asking it here instead would let a steerer who has since been
        narrowed to nothing block the cancel that ends her own turn.

        A caller this gate admits is written down as one of the run's callers, because
        from here on their grant bounds it. A caller it refuses is not: a refusal is
        not participation, and recording one would let anybody narrow a run they were
        never allowed to touch.
        """
        if not acting_as:
            return
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
            return
        lendable = await self._lendable_terms(
            agent,
            session.room_id,
            BoundingPrincipals(frozenset({execution.authorized_by, acting_as})),
        )
        if not lendable.lendable():
            raise AuthorizationError(
                f"{acting_as} may not act on run {execution.execution_id}: no effective capability"
            )
        await self.repos.executions.record_caller(execution.execution_id, acting_as)

    async def _handle_tool_request(
        self,
        execution: Execution,
        session: Session,
        agent: AgentInstance,
        result: dict[str, Any],
        continuation: _TurnContinuation,
    ) -> dict[str, Any]:
        """Permission check, policy check, approval gate, execution, audit event.

        The terms are re-derived here rather than carried in from the step that
        offered the tool: a provider call sits between the two, and a grant withdrawn
        while the model was thinking must not still be spendable when it answers.

        Re-deriving is not the same as unbinding. The caller who drove this step and
        the steers that shaped it still bound what it may spend, and the authorization
        carries every one of them, so the derivation applies them without this door
        having to know any of them exist — or being able to name one if it wanted to.

        The channel's posture is read here too, and here only, because this is the one
        moment a call becomes a pause or an execution. It is read, never carried: the
        declaration rows are consulted beside the terms rather than a value resolved
        somewhere earlier being spent. What it may do to the decision is bounded by
        :func:`under_posture` rather than by this door's discipline — it raises the
        pause and cannot reach ``allowed``, so a channel's posture never changes what
        that channel permits.
        """
        authorization = await self._authorization_for(
            execution.execution_id, agent.agent_id, session.room_id
        )
        effective = (await self._authorized_terms(authorization)).effective
        tool = str(result.get("tool", ""))
        raw_input = result.get("input")
        tool_input = raw_input if isinstance(raw_input, dict) else {}
        decision = under_posture(
            decide(tool, effective), await self.repos.room_postures.current(session.room_id)
        )
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
            # Deciding this approval puts the run back on a STREAMING lease, and that
            # lease is only honest if the rest of the turn is there to be prompted.
            # The two used to be separate writes — the approval committed here, the
            # continuation was saved by the turn loop afterwards — so a crash or a
            # race in between left an approval whose grant stranded the run: STREAMING,
            # NULL settlement, and a lease held by nobody. They are one transaction
            # now. Either the reviewer has a question and the turn is parked behind
            # it, or neither exists.
            async with self.db.transaction():
                approval, approval_event = await self._request_approval_in_transaction(
                    session.room_id,
                    execution.execution_id,
                    agent.agent_id,
                    f"{tool}: {decision.required_capability}",
                    execution.authorized_by,
                )
                request = replace(request, approval_id=approval.approval_id)
                await self.repos.tool_requests.create(request)
                # No harness work is in flight while a reviewer thinks, so the lease is
                # a long one. It is still a lease: an exemption is no deadline at all.
                await self._advance_run_for_execution(
                    execution.execution_id,
                    HarnessState.AWAITING_APPROVAL,
                    execution.authorized_by,
                    _APPROVAL_LEASE,
                )
                # Durably rather than in this process's memory: the decision that
                # releases it can be made on any process.
                await self.repos.suspended_turns.save(
                    execution.execution_id,
                    continuation.prompt,
                    continuation.acting_as,
                    continuation.observations,
                )
            await self._set_agent_status_safe(agent.agent_id, AgentStatus.WAITING_APPROVAL)
            await self._broadcast_persisted_events([approval_event])
            return self._tool_response(request)
        await self.repos.tool_requests.create(request)
        return self._tool_response(await self._execute_tool_request(request))

    async def _current_tool_decision(
        self, request: ToolRequest
    ) -> tuple[GatewayDecision, frozenset[str]]:
        """Decide a stored request again from the records as they stand right now.

        A twelve-hour approval window sits in front of this, and everything that can
        narrow a run can happen inside it — a steer reduced, the caller who asked for
        this tool narrowed, either of them taken out of the room. The authorization
        carries all of them, so the door a reviewer opens is bounded exactly as the
        gateway was, and takes no principal from its caller to be bounded by.

        The reviewer about to release it bounds it here as well, through the same
        helper the writer's own derivation uses, so this door refuses a call she may
        not answer for cleanly instead of leaving it to be revoked inside the write.

        The channel's posture is deliberately not applied. A posture decides whether a
        call pauses, and this call has already paused and been answered; re-pausing it
        would make an approval something a rule change could quietly revoke.
        """
        authorization = await self._bounded_by_this_calls_reviewers(
            request,
            await self._authorization_for(
                request.execution_id,
                request.agent_id,
                request.room_id,
                request.required_capability or "",
            ),
        )
        effective = (await self._authorized_terms(authorization)).effective
        return decide(request.tool, effective), effective

    async def _execute_tool_request(self, request: ToolRequest) -> ToolRequest:
        """Everything below runs inside the agent-turn boundary."""
        with agent_turn(request.execution_id):
            return await self._execute_tool_request_inner(request)

    async def _execute_tool_request_inner(self, request: ToolRequest) -> ToolRequest:
        """Run an authorised tool and audit the outcome. Never raises to the caller.

        The contract was not true. Only RunAuthorityRevoked and DomainError were
        caught, so add_agent_reaction's membership check — a bare AuthorizationError,
        which is a PermissionError and not a DomainError — escaped, leaving the
        request PENDING_APPROVAL under a tool.call_started event that never got a
        completion or a rejection: a call that started and, in the log, never ended.
        Every exit below resolves the row and appends a terminal event, and the last
        clause is a catch-all so an exception nobody anticipated cannot reopen the
        same hole.
        """
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
            await self._append_room_event(
                request.room_id,
                EventType.AGENT_RUN_AUTHORITY_REVOKED,
                {
                    "run_id": revoked.authorization.run_id,
                    "bounded_by": sorted(revoked.authorization.bounding),
                    "stage": revoked.stage,
                    "missing_capability": revoked.authorization.required_capability,
                },
                request.agent_id,
                "agent",
            )
            await self._resolve_tool_request_terminal(
                request,
                "REJECTED",
                str(revoked),
                "{}",
                EventType.TOOL_CALL_REJECTED,
                {
                    "request_id": request.request_id,
                    "tool": request.tool,
                    "required_capability": request.required_capability,
                    "reason": str(revoked),
                },
            )
            run = await self.repos.agent_runs.get_by_execution(request.execution_id)
            if run is not None:
                await self._settle_run(
                    run,
                    RunSettlement.AUTHORITY_REVOKED,
                    run.acting_user_id,
                    str(revoked),
                )
            return replace(request, status="REJECTED", reason=str(revoked))
        except AuthorizationError as denied:
            # A refusal, not a failure: the tool was not permitted to this agent at
            # the moment it ran, which is what tool.call_rejected records.
            await self._resolve_tool_request_terminal(
                request,
                "REJECTED",
                str(denied),
                "{}",
                EventType.TOOL_CALL_REJECTED,
                {
                    "request_id": request.request_id,
                    "tool": request.tool,
                    "required_capability": request.required_capability,
                    "reason": str(denied),
                },
            )
            return replace(request, status="REJECTED", reason=str(denied))
        except DomainError as exc:
            await self._resolve_tool_request_terminal(
                request,
                "FAILED",
                str(exc),
                "{}",
                EventType.TOOL_CALL_FAILED,
                {"request_id": request.request_id, "tool": request.tool, "error": str(exc)},
            )
            return replace(request, status="FAILED", reason=str(exc))
        except Exception as exc:
            # Nothing gets to end a started tool call by escaping. An error nobody
            # named is still a failure, and it is recorded as one rather than
            # unwinding past the audit trail.
            log.exception(
                "Tool %s failed unexpectedly for request %s", request.tool, request.request_id
            )
            error = f"{type(exc).__name__}: {exc}"
            await self._resolve_tool_request_terminal(
                request,
                "FAILED",
                error,
                "{}",
                EventType.TOOL_CALL_FAILED,
                {"request_id": request.request_id, "tool": request.tool, "error": error},
            )
            return replace(request, status="FAILED", reason=error)
        result_json = json.dumps(output, default=str)
        await self._resolve_tool_request_terminal(
            request,
            "EXECUTED",
            "executed",
            result_json,
            EventType.TOOL_CALL_COMPLETED,
            {"request_id": request.request_id, "tool": request.tool},
        )
        return replace(request, status="EXECUTED", reason="executed", result_json=result_json)

    async def _run_tool(self, request: ToolRequest) -> dict[str, Any]:
        """The registry's executable side. Each tool is a small, auditable action."""
        tool_input = json.loads(request.input_json)
        # Authority is established before any branch, reads included. This used to be
        # derived after the read returned, so channel.read_context reached no re-check
        # at all: an agent removed from the room mid-turn still read the room's
        # messages back, including ones posted before it was ever mentioned. The
        # continuation loop turned that one-shot window into a per-prompt one.
        authorization = await self._run_authorization(request)
        if request.tool == "channel.read_context":
            # A read has no writer of its own to check inside, so the check and the
            # read are made one transaction here. Disclosure is the mutation a read
            # performs, and it is gated in the same place a write's would be.
            async with self.db.transaction():
                await self._require_run_authority_in_transaction(
                    authorization, "channel.read_context"
                )
                messages = await self.repos.messages.list_by_room(request.room_id, limit=20)
            return {
                "messages": [
                    {"message_id": m.message_id, "content": m.content, "role": m.role.value}
                    for m in messages
                ]
            }
        # Each writer below re-checks inside its own transaction rather than here:
        # those calls open their own, and Database.transaction() refuses to nest, so
        # a second check here would sit outside the write and relocate
        # check-then-use rather than end it.
        if request.tool == "message.react":
            # The channel the run belongs to is the boundary, checked here so a
            # cross-channel message id is refused as a domain error rather than as
            # an authorization one: the reaction's own membership check is about who
            # may react, not about which channel this run belongs to.
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
        """What a stored call is decided and written against, read from durable records.

        It used to read the acting caller off ``agent_runs.acting_user_id``, which is
        the last human to have moved the run rather than the set of humans whose grant
        bounds it. By the time a reviewer released a parked call that column had been
        overwritten with the run's own principal, and the delegate who asked for the
        call was bounding nothing.

        The run's own principals are read whole, by the one factory that reads them,
        and this call's reviewers are added to that set beside them.
        """
        return await self._bounded_by_this_calls_reviewers(
            request,
            await self._authorization_for(
                request.execution_id,
                request.agent_id,
                request.room_id,
                request.required_capability or "",
            ),
        )

    async def _bounded_by_this_calls_reviewers(
        self, request: ToolRequest, authorization: RunAuthorization
    ) -> RunAuthorization:
        """Put the humans who released this one call over it, and over nothing else.

        A reviewer answers for the call she released and for no other, so her grant
        belongs to that request's rows rather than to the run's. Recording her as a
        caller of the run instead — which is what releasing a call used to do — made
        an administrator scoped to ``retrieval`` strip ``writing`` from every later
        call of a run she had touched once, and made answering an approval something
        to avoid.

        Adding can only narrow: the terms are an intersection over the set, so a wider
        set is a smaller grant. There is no expression here that removes a principal
        the durable rows named, which is what keeps a per-call bound from becoming a
        way to spend more than the run's own principals hold. Both doors that decide a
        stored call — the reviewer's and the writer's — reach the reviewers through
        here, so neither can be the one that forgot them.
        """
        return replace(
            authorization,
            bounding=authorization.bounding.also_bounded_by(
                await self.repos.tool_requests.reviewers(request.request_id)
            ),
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
    def _prompt_with_tool_results(provider_prompt: str, observations: list[str]) -> str:
        """The same turn, continued: what the tools this turn already called returned.

        A tool result can carry member-authored text - a channel read returns
        whatever was said in the room - so the block is screened and fenced.
        """
        results = "\n".join(f"- {observation}" for observation in observations)
        block = fenced(screen(results, "tool results"))
        return (
            f"{provider_prompt}\n\nTool results from this turn, in order:\n{block}\n\n"
            'Answer with action "finish" unless another tool call is genuinely required.'
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

    async def _settle_turn_without_answer(
        self,
        execution: Execution,
        acting_as: str,
        result: dict[str, Any],
        settlement: RunSettlement,
        status: AgentStatus,
        error: str,
    ) -> dict[str, Any]:
        """End a step that produced no answer, now, in a state a reader can name.

        Two things reach here, and neither used to end anything. A turn cancelled
        mid-continuation came back with a cancelled stop reason and no tool request,
        so the loop returned and the run sat STREAMING with a NULL settlement until
        the lease sweep called it PARKED — "turn stopped without an answer", which is
        untrue of a run somebody cancelled, and non-resumable besides. And an action
        the server does not continue fell through the dispatch below to the same
        silence. Settling here is what makes the loop's promise true: nothing leaves
        this method with the run RUNNING and nobody about to prompt it.
        """
        run = await self.repos.agent_runs.get_by_execution(execution.execution_id)
        if run is not None and run.harness_state is not HarnessState.SETTLED:
            await self._settle_run(run, settlement, acting_as or "system", error)
            await self._set_agent_status_safe(execution.agent_id, status)
        return {**result, "error": error, "settlement": settlement.value}

    async def _execute_one_agent_step(
        self, execution_id: str, continuation: _TurnContinuation, *, require_idle: bool = False
    ) -> dict[str, Any]:
        """Everything below runs inside the agent-turn boundary.

        ``require_idle`` rides a contextvar rather than a parameter to the inner
        step, because several tests substitute that inner step outright and are
        entitled to keep its original two argument shape; a caller replacing it
        never sees this claim, and does not need to.
        """
        token = _require_idle_entrance.set(require_idle)
        try:
            with agent_turn(execution_id):
                return await self._execute_one_agent_step_inner(execution_id, continuation)
        finally:
            _require_idle_entrance.reset(token)

    async def _execute_one_agent_step_inner(
        self, execution_id: str, continuation: _TurnContinuation
    ) -> dict[str, Any]:
        """One prompt of a turn: authority, harness, and whatever the model chose.

        Every authority this spends is re-derived here rather than carried in from
        the prompt before it, so a grant withdrawn between two tool calls stops the
        second one.
        """
        prompt = continuation.prompt
        acting_as = continuation.acting_as
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
        principal = await self._lendable_terms(
            agent, session.room_id, BoundingPrincipals(frozenset({execution.authorized_by}))
        )
        if not principal.lendable():
            await self._settle_undispatched_run(
                execution_id,
                f"{execution.authorized_by or 'an unknown principal'} may no longer "
                f"invoke agent {execution.agent_id}: no effective capability",
                RunSettlement.AUTHORITY_REVOKED,
            )
            raise AuthorizationError(
                f"run {execution_id} is no longer authorized by {execution.authorized_by}"
            )
        # A caller who is not that principal is bounded by their own grant too, and so
        # is every steer the run is carrying. The gate above writes this caller into
        # the run's own records, and the authorization below reads every principal
        # back out of them. The steers still queued are read separately, because those
        # are what this prompt delivers rather than what bounds it.
        await self._require_delegated_authority(execution, acting_as)
        steers = await self.repos.interventions.list_unconsumed(execution_id)
        authorization = await self._authorization_for(execution_id, agent.agent_id, session.room_id)
        terms = await self._authorized_terms(authorization)
        if not terms.effective and execution.agent_task_id:
            # The gate above is a liveness check on the run's own authorizer, and it
            # passes while a *delegator* is revoked — leaving a run that dispatches,
            # derives an empty schema and sits there tooled with nothing.
            #
            # Only for a run answering a task. A run with an empty set is not always
            # finished: a mention run whose steerer was narrowed still answers in
            # words, and test_intervention_authority pins that an empty set de-tools
            # the step and audits a REJECTED request rather than killing the turn. A
            # delegated run is different because somebody is waiting on its answer —
            # a delegator blocked on a turn that can no longer do anything is the
            # state a task's terminal states exist to spare it.
            await self._settle_undispatched_run(
                execution_id,
                f"no principal bounding run {execution_id} can still lend "
                f"agent {execution.agent_id} anything",
                RunSettlement.AUTHORITY_REVOKED,
            )
            raise AuthorizationError(f"run {execution_id} is no longer authorized")

        source_prompt = prompt
        provider_prompt = prompt
        if branch.lifecycle_managed:
            if prompt != branch.initiating_prompt:
                raise DomainError("managed branch run must use its immutable initiating prompt")
            source_prompt = branch.initiating_prompt
            provider_prompt = self._branch_execution_prompt(branch)
        if continuation.observations:
            provider_prompt = self._prompt_with_tool_results(
                provider_prompt, continuation.observations
            )

        if agent.harness_id not in KNOWN_HARNESS_IDS:
            raise DomainError(f"no harness is registered as {agent.harness_id!r}")
        harness = self._harness(agent.harness_id)
        agent_run = await self.repos.agent_runs.get_by_execution(execution_id)
        handle = SessionHandle(
            run_id=agent_run.run_id if agent_run is not None else execution_id,
            harness_session_id=execution_id,
        )
        # The turn is in flight from here, on a lease the sweep can expire if the
        # process driving it dies. The entrance prompt of a call from outside makes
        # this a claim rather than an unconditional advance: a run already
        # streaming or already parked at a reviewer refuses instead of being
        # prompted again on top of a turn already in flight.
        require_idle = _require_idle_entrance.get()
        claimed = await self._advance_run_for_execution(
            execution_id,
            HarnessState.STREAMING,
            acting_as,
            _STREAMING_LEASE,
            expected=HarnessState.STARTING if require_idle else None,
        )
        if require_idle and not claimed:
            raise DomainError(
                f"execution {execution_id} is not awaiting a fresh turn, so this step is refused"
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
            self._record_model_tokens(result)
            # The NEXUS harness carries provenance inside the turn output; the model
            # provider harness returns it in the TurnResult's own field, and the reader
            # below looks only in the output. Without this an agent on that harness
            # records no provider input, model or evidence at all - and provenance is
            # the whole reason a synthesis claim can be drilled back to its source.
            if turn.provenance and not result.get("provenance"):
                result["provenance"] = dict(turn.provenance)
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
        if result.get("status") == "cancelled":
            return await self._settle_turn_without_answer(
                execution,
                acting_as,
                result,
                RunSettlement.CANCELLED,
                AgentStatus.IDLE,
                "cancelled while the turn was in flight",
            )
        if result.get("action") == "tool":
            return await self._handle_tool_request(execution, session, agent, result, continuation)
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
        return await self._settle_turn_without_answer(
            execution,
            acting_as,
            result,
            RunSettlement.FAILED,
            AgentStatus.FAILED,
            f"step returned {str(result.get('action', '')) or 'no action'}, "
            "which is not an answer and not a tool call",
        )

    async def execute_branch_run(
        self, branch_id: str, execution_id: str, acting_as: str = ""
    ) -> dict[str, Any]:
        branch = await self.get_branch(branch_id)
        execution = await self.repos.executions.get(execution_id)
        if execution is None or execution.branch_id != branch.branch_id:
            raise DomainError("agent run not found in branch")
        return await self.execute_agent_step(execution_id, branch.initiating_prompt, acting_as)

    async def pause_execution(self, execution_id: str, acting_as: str = "") -> bool:
        require_human_boundary("run.pause")
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
        require_human_boundary("run.resume")
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
        require_human_boundary("run.reopen")
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
            # A resume does not re-root the run. On a delegated turn the chain's
            # root human is who the whole chain is authorized by, and writing the
            # resumer here replaced them — widening the bound to whoever pressed
            # resume. The resumer is a caller, which is a row, and it is written
            # below once the run exists to hang it on.
            authorized_by=(
                earlier.authorized_by
                if earlier is not None and earlier.agent_task_id
                else resumed_by
            ),
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
        await self.repos.executions.record_caller(execution.execution_id, resumed_by)
        await self._broadcast_persisted_events([event])
        return execution

    async def cancel_execution(
        self, execution_id: str, cancelled_by: str, *, require_member: bool = False
    ) -> bool:
        """Cancel a run durably, wherever the process driving it happens to be.

        The bridge's map of run to execution is one process's memory. It used to be
        the whole cancel on a branch that is not lifecycle-managed — which is every
        room's default branch — and a veto on one that is: a second process, or the
        same process after a restart, found nothing in the map, returned False and
        wrote nothing, so the run went on until the lease sweep named it something
        else. Telling the bridge is a best-effort stop signal to a turn that may be
        in flight here; the durable settlement below is the cancellation, and it is
        the same on any process.
        """
        require_human_boundary("run.cancel")
        execution = await self.repos.executions.get(execution_id)
        if execution is None:
            raise DomainError("execution not found")
        if require_member:
            await self._require_delegated_authority(execution, cancelled_by)
        if execution.status in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }:
            raise DomainError("execution is already terminal")
        await self.nexus.cancel_execution(execution_id)
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
                RunSettlement.CANCELLED,
                cancelled_by,
            )
        # Nothing prompts a cancelled run again, so a turn held at a reviewer is not
        # waiting on one either — nor is the approval that turn stopped at.
        await self.repos.suspended_turns.discard(execution_id)
        await self._expire_undecided_approvals(execution_id, "cancelled by user")
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
        require_human_boundary("run.intervene")
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
        self, room_id: str, title: str | None, created_by: str
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
        title: str | None,
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
        title: str | None,
        created_by: str,
        synthesis_type: str = SynthesisType.DECISION_BRIEF,
        idempotency_key: str | None = None,
    ) -> tuple[Artifact, ArtifactVersion]:
        """Run model-backed synthesis over this Branch's explicit selected outputs."""
        spec = spec_for(synthesis_type)
        if idempotency_key is not None:
            idempotency_key = self._validate_idempotency_key(idempotency_key)
        branch = await self.get_branch(branch_id)
        if title is None or not title.strip():
            # No caller-supplied title: derive one from what this branch is actually
            # about, so every untitled brief is not stamped with the same stale
            # placeholder decision.
            prompt = branch.initiating_prompt.strip()
            title = prompt[:80] if prompt else "Decision"
        title = self._validate_non_empty(title, f"{spec.artifact_name.lower()} title")
        operation = f"branch.synthesis.{spec.type.lower()}"
        request = {"title": title}
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
        self._record_model_tokens(model_result)
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
                    # A published Decision Brief is a decision taken, and it says so
                    # here: every Decision entity carries its status, so the question
                    # "what has been decided" is a query rather than an inference.
                    "status": DecisionStatus.ACTIVE.value,
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
        mentioner = BoundingPrincipals(frozenset({requested_by}))
        if not (await self._lendable_terms(agent, room_id, mentioner)).lendable():
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
        attachment_ids: list[str] | None = None,
    ) -> Message:
        content = self._validate_non_empty(content, "message content")
        if idempotency_key is not None:
            idempotency_key = self._validate_idempotency_key(idempotency_key)
        request: dict[str, Any] = {
            "role": role.value,
            "content": content,
            "metadata": metadata or {},
            "parent_message_id": parent_message_id,
            "invoke_mentioned_agents": invoke_mentioned_agents,
        }
        # Folded in only when present, so every hash already stored for an
        # attachment-free send still matches and an old client retrying one
        # keeps working. A retry with the same key and different attachments
        # is a different request, not a replay of the first.
        if attachment_ids:
            request["attachment_ids"] = sorted(attachment_ids)
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
                                # Filenames and sizes only, never bytes — the message
                                # event is what a model path and an export both read.
                                "attachment_ids": list(attachment_ids or []),
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
                for attachment_id in attachment_ids or []:
                    # Same room, same uploader, still unbound — checked and claimed
                    # in one statement, inside the transaction that writes the
                    # message. The message row must exist first: the FK this binds
                    # against is on the message this attachment is claimed for.
                    bound = await self.repos.attachments.bind_to_message_in_transaction(
                        attachment_id, room_id, sender_id, msg.message_id
                    )
                    if not bound:
                        raise DomainError(f"attachment not available to bind: {attachment_id}")
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
        # prompt, because that is what the author addressed to the agent - screened
        # and fenced, because any member can author it.
        for execution_id in invoked.values():
            await self._dispatch_mention_run(execution_id, fenced(screen(content, "room message")))
        return msg

    async def list_room_messages(
        self, room_id: str, limit: int = 100, after_sequence: int | None = None
    ) -> list[Message]:
        return await self.repos.messages.list_by_room(
            room_id, limit=self._validate_limit(limit), after_sequence=after_sequence
        )

    async def list_message_mentions(self, message_id: str) -> list[MessageMention]:
        return await self.repos.mentions.list_for_message(message_id)

    async def list_message_attachments(self, message_id: str) -> list[Attachment]:
        return await self.repos.attachments.list_for_message(message_id)

    async def upload_attachment(
        self,
        room_id: str,
        uploader_id: str,
        filename: str,
        content_type: str,
        data: bytes,
        max_bytes: int,
    ) -> Attachment:
        """Store a file a member uploaded, unbound until a message claims it.

        The bytes never leave this method except into the row: nothing here
        builds a model prompt, and nothing downstream of this call is handed
        the blob — only filename/content_type/size_bytes ever ride a message.
        """
        filename = self._validate_non_empty(filename, "filename")
        if len(data) > max_bytes:
            raise DomainError(f"attachment exceeds the {max_bytes}-byte limit")
        attachment = Attachment(
            attachment_id=new_id("att"),
            room_id=room_id,
            uploader_id=uploader_id,
            filename=filename,
            content_type=content_type,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            data=data,
        )
        async with self.db.transaction():
            await self._require_mutate_in_transaction(room_id, uploader_id)
            await self.repos.attachments.create(attachment)
        return attachment

    async def get_attachment(self, attachment_id: str) -> Attachment:
        attachment = await self.repos.attachments.get(attachment_id)
        if attachment is None:
            raise DomainError(f"attachment not found: {attachment_id}")
        return attachment

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

    # ── Artifact shares — the room's one door to the outside ───────────────────

    async def create_artifact_share(
        self, artifact_id: str, created_by: str
    ) -> tuple[ArtifactShare, str]:
        """Mint a public read-only link for an artifact's latest published content.

        Sharing outward is a governance act, not authorship, so it is gated on
        room ADMINISTER rather than the MUTATE that writing a version needs. The
        bearer token is returned here and nowhere else; only its hash is stored.
        """
        artifact = await self.repos.artifacts.get(artifact_id)
        if artifact is None:
            raise DomainError(f"artifact not found: {artifact_id}")
        token = secrets.token_urlsafe(32)
        share = ArtifactShare(
            share_id=new_id("share"),
            artifact_id=artifact_id,
            room_id=artifact.room_id,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            created_by=created_by,
        )
        async with self.db.transaction():
            # Re-check ADMINISTER inside the write's own transaction: an admin
            # demoted after the route authorized them must not still be able to
            # open a door out of the room.
            await self._require_capability_in_transaction(
                artifact.room_id, created_by, RoomCapability.ADMINISTER
            )
            await self.repos.artifact_shares.create_in_transaction(share)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=artifact.room_id,
                    sequence=0,
                    event_type=EventType.ARTIFACT_SHARE_CREATED,
                    payload={"artifact_id": artifact_id, "share_id": share.share_id},
                    actor_id=created_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return share, token

    async def list_artifact_shares(self, artifact_id: str) -> list[ArtifactShare]:
        return await self.repos.artifact_shares.list_by_artifact(artifact_id)

    async def revoke_artifact_share(self, artifact_id: str, share_id: str, revoked_by: str) -> None:
        share = await self.repos.artifact_shares.get(share_id)
        if share is None or share.artifact_id != artifact_id:
            raise DomainError(f"artifact share not found: {share_id}")
        async with self.db.transaction():
            await self._require_capability_in_transaction(
                share.room_id, revoked_by, RoomCapability.ADMINISTER
            )
            revoked = await self.repos.artifact_shares.revoke_in_transaction(share_id)
            if revoked is None:
                raise DomainError(f"artifact share already revoked: {share_id}")
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=share.room_id,
                    sequence=0,
                    event_type=EventType.ARTIFACT_SHARE_REVOKED,
                    payload={"artifact_id": artifact_id, "share_id": share_id},
                    actor_id=revoked_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])

    async def resolve_public_share(self, token: str) -> tuple[Artifact, ArtifactVersion] | None:
        """The `/share/{token}` route's only lookup — unauthenticated, so this never
        raises: an unknown, malformed, or revoked token is the same None to the
        caller, which is what keeps the public 404 from becoming an oracle."""
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        share = await self.repos.artifact_shares.get_live_by_token_hash(token_hash)
        if share is None:
            return None
        artifact = await self.repos.artifacts.get(share.artifact_id)
        if artifact is None:
            return None
        versions = await self.repos.artifacts.list_versions(share.artifact_id)
        if not versions:
            return None
        return artifact, versions[0]

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

    async def update_decision_status(
        self,
        decision_id: str,
        status: DecisionStatus,
        *,
        reviewed_by: str = "",
        require_member: bool = False,
    ) -> Decision:
        """Move a decision between states, and say so in the room's order.

        Without this a decision could only ever be proposed, so the open list had
        nothing that could drain it and the made list could never match a row. The
        emitted event is the one the Decision invalidation class already listens
        for, so the assertion over this row stops reading as current the moment the
        row moves.
        """
        async with self.db.transaction():
            decision = await self.repos.decisions.get(decision_id)
            if decision is None:
                raise DomainError(f"decision not found: {decision_id}")
            if require_member:
                await self._require_mutate_in_transaction(decision.room_id, reviewed_by)
            _validate_transition(decision.status, status, VALID_DECISION_TRANSITIONS, "decision")
            await self.repos.decisions.update_status(decision_id, status, reviewed_by)
            decision = replace(decision, status=status, reviewed_by=reviewed_by)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=decision.room_id,
                    sequence=0,
                    event_type=(
                        EventType.DECISION_SUPERSEDED
                        if status is DecisionStatus.SUPERSEDED
                        else EventType.DECISION_UPDATED
                    ),
                    payload={"decision_id": decision_id, "status": status.value},
                    actor_id=reviewed_by,
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
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(room_id, requested_by)
            approval, event = await self._request_approval_in_transaction(
                room_id, execution_id, agent_id, action_description, authorized_by
            )
        await self._set_agent_status_safe(agent_id, AgentStatus.WAITING_APPROVAL)
        await self._broadcast_persisted_events([event])
        return approval

    async def _request_approval_in_transaction(
        self,
        room_id: str,
        execution_id: str,
        agent_id: str,
        action_description: str,
        authorized_by: str,
    ) -> tuple[Approval, RoomEvent]:
        """Open one approval for a caller that already owns the write transaction.

        The gateway needs it, because the approval and the rest of the turn it holds
        up have to commit together or not at all.
        """
        approval = Approval(
            approval_id=new_id("appr"),
            room_id=room_id,
            execution_id=execution_id,
            agent_id=agent_id,
            action_description=action_description,
            authorized_by=authorized_by,
        )
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
        return approval, event

    async def approve_action(
        self, approval_id: str, reviewer_id: str, comment: str = "", *, require_member: bool = False
    ) -> Approval:
        require_human_boundary("approval.approve")
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
            pending = self._request_this_approval_gated(
                approval, await self.repos.tool_requests.get_by_approval(approval_id)
            )
            if pending is not None:
                # The reviewer grants from their own capabilities, never above them:
                # an approval is not a way to lend what the reviewer does not hold.
                # She is written down first and the derivation below reads her back
                # out with everybody else — rather than being handed to it as the one
                # identity this door happens to know about.
                #
                # Against this call, not against the run. Releasing one call is not
                # taking the run over: recording her as a caller of it put her grant
                # over every call it made afterwards, so an administrator scoped to
                # `retrieval` who approved a single read turned the run's later writes
                # from paused into refused. It failed closed, so nobody obtained
                # anything — but they approved one call and bounded a hundred, which
                # is a reach, and one that teaches people not to answer approvals.
                await self.repos.tool_requests.record_reviewer(pending.request_id, reviewer_id)
                # Re-derived inside the transaction that grants rather than after it
                # closed; the re-stamped effective set is an audit record, never an
                # input, because the writer re-derives again inside its own.
                decision, effective = await self._current_tool_decision(pending)
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
                # Under the principal the turn was parked on, not under the reviewer.
                # `agent_runs.advance` writes its acting human into the run's callers,
                # so naming her here would put back — through the database, where it
                # is harder to see — exactly the run-wide bound the line above stopped
                # taking. It is the same principal the park named, which is the same
                # one `_resume_suspended_turn` carries the rest of the turn under.
                await self._advance_run_for_execution(
                    pending.execution_id,
                    HarnessState.STREAMING,
                    pending.authorized_by,
                    _STREAMING_LEASE,
                )
                resolved = await self._execute_tool_request(pending)
            else:
                # The capability was withdrawn between the request and the grant; a
                # human's approval cannot restore what the policy no longer permits.
                await self._resolve_tool_request_terminal(
                    pending,
                    "REJECTED",
                    decision.reason,
                    "{}",
                    EventType.TOOL_CALL_REJECTED,
                    {
                        "request_id": pending.request_id,
                        "tool": pending.tool,
                        "required_capability": decision.required_capability,
                        "effective": sorted(effective),
                        "reason": decision.reason,
                    },
                )
                resolved = replace(pending, status="REJECTED", reason=decision.reason)
            # The turn stopped at this reviewer. Running the tool is not what the
            # room was waiting for; the answer is, so the rest of the turn runs now.
            await self._resume_suspended_turn(pending.execution_id, resolved)
        return approval

    async def _resume_suspended_turn(self, execution_id: str, request: ToolRequest | None) -> None:
        """Carry a turn that stopped at a reviewer through to its answer.

        It resumes under the principals it suspended under, not under the reviewer:
        she decided one tool call, and lending her grant to the rest of the turn
        would be a wider authority than anyone asked her for. Every prompt re-derives
        from durable records regardless, so a grant withdrawn while she deliberated
        still stops the next call.

        The approval is committed, and failing it here would tell the reviewer her
        decision was lost when it was not. Swallowing the refusal is not the same as
        absorbing it, though: ``claim`` has already deleted the continuation, so a
        refusal that goes nowhere leaves the run STREAMING with a NULL settlement,
        nobody about to prompt it and no record of why — the one state
        ``_continue_agent_turn`` promises cannot happen. A step that refuses itself
        settles the run on the way out; a refusal reaching here settled nothing, so
        it is settled here instead of vanishing.

        The same is true of finding no continuation at all. The gate that opened the
        approval writes both in one transaction, so absence here means the row was
        lost after that commit rather than never written — and the decision above has
        already issued the STREAMING lease it was meant to spend. Returning would leave
        that lease held by nobody, which is the fourth route into the state this
        docstring rules out. Nothing is carrying the run and nothing will, so it is
        settled ORPHANED now rather than by a sweep a quarter of an hour later.
        """
        parked = await self.repos.suspended_turns.claim(execution_id)
        if parked is None:
            await self._settle_unresumable_turn(
                execution_id,
                "",
                RunSettlement.ORPHANED,
                "the rest of this turn was not there to resume after its approval decision",
            )
            return
        turn = _TurnContinuation(
            prompt=str(parked["prompt"]),
            acting_as=str(parked["acting_as"]),
            observations=list(parked["observations"]),
        )
        if request is not None:
            response = self._tool_response(request)
            turn.observations.append(self._tool_observation(response["tool_request"]))
        if await self._park_if_attempts_spent(execution_id, turn.acting_as) is not None:
            return
        try:
            await self._continue_agent_turn(execution_id, turn)
        except AuthorizationError as refusal:
            log.info("Turn for %s was refused after its approval decision", execution_id)
            await self._settle_unresumable_turn(
                execution_id, turn.acting_as, RunSettlement.AUTHORITY_REVOKED, str(refusal)
            )
        except DomainError as failure:
            log.info("Turn for %s could not resume after its approval decision", execution_id)
            await self._settle_unresumable_turn(
                execution_id, turn.acting_as, RunSettlement.FAILED, str(failure)
            )

    async def _settle_unresumable_turn(
        self, execution_id: str, acting_as: str, settlement: RunSettlement, error: str
    ) -> None:
        """Say what became of a run whose continuation refused to run.

        A no-op when the step that raised already settled it, so the first true
        account of the run is the one that stands.
        """
        run = await self.repos.agent_runs.get_by_execution(execution_id)
        if run is None or run.harness_state is HarnessState.SETTLED:
            return
        await self._settle_run(run, settlement, acting_as or "system", error)
        await self._set_agent_status_safe(run.agent_id, AgentStatus.FAILED)

    async def reject_action(
        self,
        approval_id: str,
        reviewer_id: str,
        comment: str = "",
        *,
        require_member: bool = False,
        continue_turn: bool = False,
    ) -> Approval:
        require_human_boundary("approval.reject")
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
            gated = self._request_this_approval_gated(
                approval, await self.repos.tool_requests.get_by_approval(approval_id)
            )
            pending = gated
            if pending is not None:
                await self.repos.tool_requests.resolve_in_transaction(
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
                pending = replace(pending, status="REJECTED", reason="approval rejected")
            events.extend(
                await self._end_refused_approval_in_transaction(
                    approval.execution_id, gated, reviewer_id, continue_turn
                )
            )
        await self._broadcast_persisted_events(events)
        if gated is not None:
            if continue_turn:
                # The fresh lease above is only honest if something is about to prompt
                # this run again. That is here.
                await self._resume_suspended_turn(approval.execution_id, pending)
            else:
                await self.repos.suspended_turns.discard(approval.execution_id)
        return approval

    @staticmethod
    def _request_this_approval_gated(
        approval: Approval, request: ToolRequest | None
    ) -> ToolRequest | None:
        """The undecided tool call this approval is actually holding, if it is holding one.

        An approval that gated nothing is a record of a question, and deciding it is a
        record of an answer. It is not an account of why a run ended, and it used to be
        allowed to write one: any member could open an approval against a live run
        through the approvals route, reject it, and settle that run APPROVAL_REFUSED —
        an untrue account of a run nobody had refused anything to. With
        ``continue_turn`` it put the run back on a fresh STREAMING lease with nothing
        suspended to prompt it, which is the state the turn loop promises cannot exist.
        """
        if request is None or request.status != "PENDING_APPROVAL":
            return None
        if request.execution_id != approval.execution_id:
            return None
        return request

    async def _end_refused_approval_in_transaction(
        self, execution_id: str, gated: ToolRequest | None, reviewer_id: str, continue_turn: bool
    ) -> list[RoomEvent]:
        """Settle the run this approval was holding, or put it back on a fresh lease.

        Never neither — and never a run this approval was not holding. ``gated`` is
        the undecided tool call the approval gated, and without one there is nothing
        here to end: refusing a question nobody's turn was waiting on leaves the run
        exactly where it was found.
        """
        if gated is None:
            return []
        run = await self.repos.agent_runs.get_by_execution(execution_id)
        if run is None or run.harness_state is HarnessState.SETTLED:
            return []
        if continue_turn:
            # The same reach as the approve path, and refused for a stronger reason:
            # this reviewer released nothing at all, so putting her name on the advance
            # would bound every remaining call of the run by somebody who said no to
            # one of them. The turn continues under the principal it was parked on.
            await self.repos.agent_runs.advance(
                run.run_id,
                HarnessState.STREAMING,
                utcnow() + _STREAMING_LEASE,
                gated.authorized_by or run.acting_user_id,
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
        bounding = BoundingPrincipals(frozenset({acting_as}))
        if not (await self._lendable_terms(agent, agent.room_id, bounding)).lendable():
            raise AuthorizationError(
                f"{acting_as} may not steer agent {agent_id}: no effective capability"
            )

    async def interrupt_agent(
        self, agent_id: str, user_id: str, reason: str = "", *, require_member: bool = False
    ) -> None:
        require_human_boundary("agent.interrupt")
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
        require_human_boundary("agent.redirect")
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
        """This room's assertions, each told with the currency the Meta path derives.

        Without it a superseded assertion left here byte-identical to a live one,
        and this is the account embedded in room state — the one a reconnecting
        client believes.
        """
        await self.get_room(room_id)
        entities = await self.repos.ontology.list_entities(room_id)
        relationships = await self.repos.ontology.list_relationships(room_id)
        reviews = await self.repos.ontology.list_reviews(room_id)
        currency = await self._ontology_currency(room_id, entities, relationships)
        return {
            "entities": [
                self._with_currency(
                    await self._ontology_entity_record(entity), currency[entity.entity_id]
                )
                for entity in entities
            ],
            "relationships": [
                self._with_currency(
                    self._ontology_relationship_record(relationship),
                    currency[relationship.relationship_id],
                )
                for relationship in relationships
            ],
            "reviews": [self._ontology_review_record(review) for review in reviews],
        }

    async def _ontology_currency(
        self,
        room_id: str,
        entities: list[OntologyEntity],
        relationships: list[OntologyRelationship],
    ) -> dict[str, tuple[bool, int]]:
        """Currency for a whole room, on the rule and the read shape Meta already uses.

        Both surfaces now ask the log for the events that can invalidate an
        assertion. Counting a fetched page of the room's own ordered events instead
        made every assertion past that page report itself current for ever, and a
        page is wrong again at whatever the next limit turns out to be.
        """
        kinds = {entity.entity_id: entity.kind for entity in entities}
        positions: list[tuple[str, int, tuple[str, ...]]] = [
            (entity.entity_id, entity.asserted_at_sequence, invalidation_class(entity.kind))
            for entity in entities
        ]
        positions.extend(
            (
                relationship.relationship_id,
                relationship.asserted_at_sequence,
                invalidation_class(
                    kinds[relationship.from_entity_id], kinds[relationship.to_entity_id]
                ),
            )
            for relationship in relationships
        )
        return await self._currency(
            positions,
            lambda event_class, floor: self.repos.ontology.invalidating_sequences(
                room_id, event_class, floor
            ),
        )

    @staticmethod
    async def _currency(
        positions: list[tuple[str, int, tuple[str, ...]]],
        invalidating: Callable[[tuple[str, ...], int], Awaitable[list[int]]],
    ) -> dict[str, tuple[bool, int]]:
        """Group by invalidation class, one read per class, then count per assertion.

        The one derivation every surface goes through, because two of them written
        separately is how the ontology route and the Meta path came to disagree.
        """
        grouped: dict[tuple[str, ...], list[tuple[str, int]]] = {}
        for assertion_id, sequence, event_class in positions:
            grouped.setdefault(event_class, []).append((assertion_id, sequence))
        currency: dict[str, tuple[bool, int]] = {}
        for event_class, members in grouped.items():
            floor = min(sequence for _assertion_id, sequence in members)
            sequences = await invalidating(event_class, floor)
            for assertion_id, sequence in members:
                count = sum(1 for item in sequences if item > sequence)
                currency[assertion_id] = (count == 0, count)
        return currency

    @staticmethod
    def _with_currency(record: dict[str, Any], currency: tuple[bool, int]) -> dict[str, Any]:
        """The two derived fields the Meta path reports, named the same way."""
        current, invalidating = currency
        return {**record, "current": current, "invalidating_events": invalidating}

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
            # A decision that moves state changes the row the assertion describes,
            # so the pass that would otherwise never look again re-reads it here.
            EventType.DECISION_UPDATED,
            EventType.DECISION_SUPERSEDED,
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
    _DECISION_EVENTS: frozenset[EventType] = frozenset(
        {
            EventType.DECISION_CREATED,
            EventType.DECISION_UPDATED,
            EventType.DECISION_SUPERSEDED,
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
                reconciled,
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
            if reconciled:
                # A reviewed assertion the pass may not rewrite is still an
                # assertion whose row moved, so the log says so rather than the
                # pass passing over it in silence.
                events.append(
                    RoomEvent(
                        room_id=room_id,
                        sequence=0,
                        event_type=EventType.ONTOLOGY_ASSERTION_RECONCILED,
                        payload={"assertion_ids": reconciled, "at_sequence": to_sequence},
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
                "reconciled": reconciled,
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

    @staticmethod
    def _task_account(task: Task) -> dict[str, Any]:
        """What a task row says about itself. One definition, projected and compared."""
        return {
            "label": task.title,
            "properties": {
                "status": task.status.value,
                "priority": task.priority.value,
                "assigned_agent_id": task.assigned_agent_id or "",
            },
        }

    @staticmethod
    def _decision_account(decision: Decision) -> dict[str, Any]:
        """What a decision row says about itself."""
        return {
            "label": decision.title,
            "properties": {
                "status": decision.status.value,
                "decision_id": decision.decision_id,
            },
        }

    async def _source_account(self, entity: OntologyEntity) -> dict[str, Any] | None:
        """The source row's own account of itself, read now; None when no row states it.

        A pass projects this into an assertion. A read compares against it, so the two
        are the same function: a shape that drifted between them would invent a
        disagreement out of its own formatting. An assertion whose source is frozen —
        a published version, an agent output — has no row that can move, and gets None.
        """
        if entity.kind is OntologyEntityKind.TASK:
            task = await self.repos.tasks.get(entity.source_object_id)
            if task is None or task.room_id != entity.room_id:
                return None
            return self._task_account(task)
        if entity.kind is OntologyEntityKind.DECISION:
            decision = await self.repos.decisions.get(entity.source_object_id)
            if decision is None or decision.room_id != entity.room_id:
                return None
            return self._decision_account(decision)
        return None

    async def _project_structured(
        self, room_id: str, events: list[RoomEvent], at_sequence: int
    ) -> tuple[list[OntologyEntity], list[OntologyRelationship]]:
        """Project structured records. A structured record needs no inference."""
        task_events: dict[str, list[int]] = {}
        decision_events: dict[str, list[int]] = {}
        for event in events:
            if event.event_type in self._DECISION_EVENTS:
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
            account = self._task_account(task)
            entities.append(
                OntologyEntity(
                    entity_id=entity_id,
                    room_id=room_id,
                    kind=OntologyEntityKind.TASK,
                    source_object_id=task_id,
                    label=account["label"],
                    properties=account["properties"],
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
            # A re-assertion replaces the row's account of itself, not its history:
            # the events that produced the earlier assertion still evidence this one.
            asserted = await self.repos.ontology.get_entity_by_source(
                room_id, OntologyEntityKind.DECISION, decision_id
            )
            if asserted is not None:
                sequences = [*sequences, *asserted.evidence_event_sequences]
            account = self._decision_account(decision)
            entities.append(
                OntologyEntity(
                    entity_id=self._ontology_id("ont", room_id, "Decision", decision_id),
                    room_id=room_id,
                    kind=OntologyEntityKind.DECISION,
                    source_object_id=decision_id,
                    label=account["label"],
                    properties=account["properties"],
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
        MetaQuestionKind.DECISIONS_OPEN: (OntologyEntityKind.DECISION,),
        MetaQuestionKind.DECISIONS_MADE: (OntologyEntityKind.DECISION,),
    }
    # The two decision kinds ask the same entity kind opposite questions, so the
    # query, not the prose, is what separates them: a decision is open while it is
    # still proposed and made once it has been taken, superseded or rejected.
    _META_ENTITY_STATUSES: dict[MetaQuestionKind, tuple[str, ...]] = {
        MetaQuestionKind.DECISIONS_OPEN: (DecisionStatus.PROPOSED.value,),
        MetaQuestionKind.DECISIONS_MADE: (
            DecisionStatus.ACTIVE.value,
            DecisionStatus.SUPERSEDED.value,
            DecisionStatus.REJECTED.value,
        ),
    }
    # STATUS asked for OWNS and got `owner OWNS <task>` for every task in the answer,
    # which is one person's work list — the shape the free-text pass refuses in
    # aggregate. A kind may not reach what a phrasing cannot, so it is not asked for
    # here and `_meta_edge_in_scope` refuses it however it is asked for.
    _META_RELATIONSHIP_KINDS: dict[MetaQuestionKind, tuple[OntologyRelationshipKind, ...]] = {
        MetaQuestionKind.BLOCKERS: (OntologyRelationshipKind.BLOCKS,),
        MetaQuestionKind.DECISIONS_OPEN: (OntologyRelationshipKind.SUPPORTS,),
        MetaQuestionKind.DECISIONS_MADE: (OntologyRelationshipKind.SUPPORTS,),
        MetaQuestionKind.DISAGREEMENT: (OntologyRelationshipKind.CONTRADICTS,),
    }
    _DECISION_SCOPED_KINDS = frozenset(
        {
            MetaQuestionKind.STATUS,
            MetaQuestionKind.DECISIONS_OPEN,
            MetaQuestionKind.DECISIONS_MADE,
        }
    )
    _DISAGREEMENT_ENDPOINTS = frozenset({OntologyEntityKind.CLAIM, OntologyEntityKind.AGENT_OUTPUT})

    @staticmethod
    def _meta_question_kind(question: str) -> MetaQuestionKind:
        """Refuse first, match exactly second, refuse again otherwise."""
        return classify_meta_question(question)

    @staticmethod
    def _resolve_meta_kind(question: str | None, kind: MetaQuestionKind | None) -> MetaQuestionKind:
        """A named kind is taken as given; free text is matched exactly or refused.

        The enum is the closed set of things this workspace answers, so naming a
        kind cannot reach an activity, ranking or productivity figure — there is no
        such kind to name. Free text supplied alongside a kind is recorded, never
        parsed: it decides nothing, so it cannot decide wrongly.
        """
        if kind is not None:
            return kind
        if question is None:
            raise DomainError(
                f"{REFUSAL_PREFIX}; name a question kind or ask a question, and this asked neither"
            )
        return classify_meta_question(question)

    @staticmethod
    def _audit_question(question: str | None) -> str | None:
        """The copy of the free text that lands in the durable audit record.

        It decides nothing, but it is attacker-chosen and it is kept, so it is
        bounded to what the route already accepts and carries no character that
        could rewrite a line of whatever reads the record back.
        """
        if question is None:
            return None
        return "".join(character for character in question if character.isprintable())[
            :_MAX_AUDITED_QUESTION
        ]

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

    @staticmethod
    def _source_disagreement(
        label: str,
        properties: dict[str, Any],
        review_status: OntologyReviewStatus,
        source_account: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """The row's account, disclosed only while it currently contradicts a reviewed one.

        A reviewed assertion is a person's account and no later pass rewrites it, so
        the two can come apart. Whether they are apart *now* is a question about the
        row as it stands, asked here, when the answer is built. Recorded instead, it
        outlived what it described: the pass in which the row converged back onto the
        person's account changed nothing, so it wrote nothing, and the marker stood.
        """
        if source_account is None or review_status is OntologyReviewStatus.UNCONFIRMED:
            return None
        if source_account["label"] == label and source_account["properties"] == properties:
            return None
        return source_account

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
        source_account: dict[str, Any] | None,
        currency: tuple[bool, int],
        review: OntologyReview | None,
    ) -> dict[str, Any]:
        assurance = self._meta_assurance(derivation_kind, review_status)
        current, invalidating = currency
        source_disagreement = self._source_disagreement(
            label, properties, review_status, source_account
        )
        # The status a reader is shown is one some source actually holds: the row's
        # while a row still states it, the assertion's when none does — named either
        # way, and never a third value assembled from a marker. Resolved once here,
        # so the prose, the counts and the payload cannot answer differently.
        held = properties if source_account is None else source_account["properties"]
        status = held.get("status")
        record: dict[str, Any] = {
            "assertion_id": assertion_id,
            "assertion_type": assertion_type,
            "kind": kind,
            "label": label,
            # An unreviewed extraction is never rendered as a plain statement, and
            # neither is a reviewed one the source row has since contradicted.
            "text": f"{_UNCONFIRMED_TEMPLATE}: {label}"
            if assurance is OntologyAssurance.UNCONFIRMED_AI
            else f"{label} ({_DISAGREEMENT_TEMPLATE})"
            if source_disagreement is not None
            else label,
            "properties": properties,
            # Compared as this answer was built: null while the assertion and its row
            # agree, otherwise the row's own account beside the person's.
            "source_disagreement": source_disagreement,
            "status": None if status is None else str(status),
            "status_source": "ASSERTION" if source_account is None else "SOURCE_ROW",
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
        return await self._currency(
            positions,
            lambda event_class, floor: self.repos.meta.invalidating_sequences(
                room_id, user_id, event_class, floor, head
            ),
        )

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
        positions = [int(claim["asserted_at_sequence"]) for claim in claims]
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
        # A confirmed assertion whose row has moved is counted by the row's account,
        # and the prose says so, because a caveat only the payload carries is a
        # caveat a reader of the sentence never gets.
        disputed = sum(1 for claim in claims if claim["source_disagreement"] is not None)
        caveat = (
            f" ({disputed} confirmed by a person and since contradicted by the source record)"
            if disputed
            else ""
        )
        if kind is MetaQuestionKind.STATUS:
            counts: dict[str, int] = {}
            for claim in claims:
                if claim["assertion_type"] != "ENTITY":
                    continue
                status = MultiplayerService._claim_status(claim)
                counts[status] = counts.get(status, 0) + 1
            grouped = ", ".join(f"{status} {count}" for status, count in sorted(counts.items()))
            return (
                f"{len(claims)} governed assertions describe where things stand ({grouped}){caveat}"
            )
        if kind is MetaQuestionKind.BLOCKERS:
            return f"{len(claims)} blocking relationships: {labels}"
        if kind is MetaQuestionKind.CHANGES:
            latest = max(int(claim["asserted_at_sequence"]) for claim in claims)
            return (
                f"{len(claims)} work objects changed, latest at sequence {latest}{caveat}: {labels}"
            )
        if kind is MetaQuestionKind.DECISIONS_OPEN:
            return f"{len(claims)} decisions are still open{caveat}: {labels}"
        if kind is MetaQuestionKind.DECISIONS_MADE:
            return f"{len(claims)} decisions have been made{caveat}: {labels}"
        return f"{len(claims)} contradictions from {distinct_sources} distinct sources: {labels}"

    @staticmethod
    def _claim_status(claim: dict[str, Any]) -> str:
        """The status a reader is entitled to, resolved from a source when the record was built."""
        return str(claim["status"] or "UNKNOWN")

    def _meta_envelope(
        self,
        *,
        question: str | None,
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
        question: str | None = None,
        *,
        kind: MetaQuestionKind | None = None,
        user_id: str,
        version_id: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Answer one bounded Meta question from current governed assertions.

        The kind is the parameter; the question is free text, kept in the answer for
        audit. A caller that names its kind reaches every supported question, so no
        capability depends on a phrasing this workspace happens to recognize.
        """
        question_kind = self._resolve_meta_kind(question, kind)
        # Classify the question as asked, record a bounded copy: shortening it first
        # would let padding push a surveillance clause past the cut and match a form.
        question = self._audit_question(question)
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
        question: str | None,
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
            statuses=self._META_ENTITY_STATUSES.get(kind, ()),
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
                    source_account=await self._source_account(entity),
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
                    source_account=None,
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
        """An edge whose endpoints this reader may not see is not part of the answer.

        Nor is an edge that names a person and the work attributed to them: a page
        of those is a per-person work list whatever kind asked for it, and the
        refusal pass already declines that shape in free text. Enforced over what
        an answer may carry rather than over one table entry, so no kind can reach
        it by being pointed at another relationship.
        """
        if (
            relationship.from_entity_id not in endpoints
            or relationship.to_entity_id not in endpoints
        ):
            return False
        if OntologyEntityKind.PERSON in (
            endpoints[relationship.from_entity_id].kind,
            endpoints[relationship.to_entity_id].kind,
        ):
            return False
        if kind in MultiplayerService._DECISION_SCOPED_KINDS:
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
        question: str | None,
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
                        **await self._ontology_entity_record(claim),
                        "published_text": source["text"],
                        "latest_review": (
                            self._ontology_review_record(claim_review)
                            if claim_review is not None
                            else None
                        ),
                    },
                    "agent_output": {
                        **await self._ontology_entity_record(output),
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
                    "_assertions": (claim, output, claim_to_decision, claim_to_output),
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
            claim_entity, output_entity, link, output_link = chain["_assertions"]
            positions.append(
                (
                    claim_entity.entity_id,
                    claim_entity.asserted_at_sequence,
                    invalidation_class(claim_entity.kind),
                )
            )
            positions.append(
                (
                    output_entity.entity_id,
                    output_entity.asserted_at_sequence,
                    invalidation_class(output_entity.kind),
                )
            )
            positions.append(
                (
                    link.relationship_id,
                    link.asserted_at_sequence,
                    invalidation_class(claim_entity.kind, decision.kind),
                )
            )
            positions.append(
                (
                    output_link.relationship_id,
                    output_link.asserted_at_sequence,
                    invalidation_class(claim_entity.kind, output_entity.kind),
                )
            )
        currency = await self._meta_currency(room_id, user_id, head, positions)
        # Currency is derived once and every record describing an assertion carries
        # that one answer. A chain record left without it reported the same assertion
        # as still current inside the same response that called it stale.
        for chain in chains:
            claim_entity, output_entity, link, output_link = chain["_assertions"]
            links = chain["relationships"]
            chain["claim"] = self._with_currency(chain["claim"], currency[claim_entity.entity_id])
            chain["agent_output"] = self._with_currency(
                chain["agent_output"], currency[output_entity.entity_id]
            )
            links["claim_to_decision"] = self._with_currency(
                links["claim_to_decision"], currency[link.relationship_id]
            )
            links["claim_to_agent_output"] = self._with_currency(
                links["claim_to_agent_output"], currency[output_link.relationship_id]
            )
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
                source_account=await self._source_account(decision),
                currency=currency[decision.entity_id],
                review=decision_review,
            )
        ]
        for chain in chains:
            claim_entity, _output_entity, link, _output_link = chain["_assertions"]
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
                    source_account=await self._source_account(claim_entity),
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
                    source_account=None,
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
            **self._with_currency(
                await self._ontology_entity_record(decision), currency[decision.entity_id]
            ),
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

    async def _ontology_entity_record(self, entity: OntologyEntity) -> dict[str, Any]:
        """One assertion, including where in the room's order it stands.

        The sequence fields are not decoration: dropping `stale_at_sequence` was
        what let a superseded assertion read exactly like a live one.

        It reads the source row rather than taking a record of it, so this surface
        cannot report a disagreement the other two have stopped reporting.
        """
        source_account = await self._source_account(entity)
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
            "asserted_at_sequence": entity.asserted_at_sequence,
            "evidence_event_sequences": list(entity.evidence_event_sequences),
            "stale_at_sequence": entity.stale_at_sequence,
            # Compared as this record was built: null while the assertion and its row
            # agree, otherwise the row's own account beside the human's.
            "source_disagreement": self._source_disagreement(
                entity.label, entity.properties, entity.review_status, source_account
            ),
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
            "asserted_at_sequence": relationship.asserted_at_sequence,
            "evidence_event_sequences": list(relationship.evidence_event_sequences),
            "stale_at_sequence": relationship.stale_at_sequence,
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

    async def get_room_events(
        self, room_id: str, after_sequence: int = 0, limit: int = _ROOM_EVENTS_DEFAULT_LIMIT
    ) -> list[RoomEvent]:
        """Up to ``limit`` events past after_sequence, paging list_since itself.

        A reconnecting client asked for everything it missed, and a single
        list_since call silently truncates at its own page cap, the same defect
        class already fixed once for the audit export - but "everything" is not
        unbounded either: a room's history is bounded by practice, not by
        anything this method enforces, so a caller past the cap gets the cap,
        never the whole table built in memory first and trimmed after.
        """
        capped_limit = max(1, min(limit, _ROOM_EVENTS_MAX_LIMIT))
        after = max(0, after_sequence)
        events: list[RoomEvent] = []
        while len(events) < capped_limit:
            page_size = min(500, capped_limit - len(events))
            page = await self.repos.events.list_since(room_id, after, limit=page_size)
            if not page:
                break
            events.extend(page)
            after = page[-1].sequence
            if len(page) < page_size:
                break
        return events[:capped_limit]

    async def export_room_audit(self, room_id: str) -> AsyncIterator[str]:
        """Every event this room ever recorded, one JSON line each, then a summary.

        Pages on after_sequence rather than trusting one read: list_since's own
        page is capped at 500, and a room with more events than that would have
        its export silently stop there — the same shape of bug 030's migration
        already named once in this codebase, reborn in a new reader.
        """
        room = await self.repos.rooms.get(room_id)
        if room is None:
            raise DomainError(f"room not found: {room_id}")
        after_sequence = 0
        exported = 0
        while True:
            page = await self.repos.events.list_since_with_chain(room_id, after_sequence)
            if not page:
                break
            for row in page:
                exported += 1
                yield (
                    json.dumps(
                        {
                            "sequence": row["sequence"],
                            "event_type": row["event_type"],
                            "actor": {"actor_id": row["actor_id"], "actor_type": row["actor_type"]},
                            "created_at": row["timestamp"],
                            "payload": json.loads(row["payload"]),
                            "event_hash": row["event_hash"],
                            "prev_hash": row["prev_hash"],
                        }
                    )
                    + "\n"
                )
            after_sequence = int(page[-1]["sequence"])
        sequence_counter = await self.repos.events.get_sequence_counter(room_id)
        _, breaks = await verify_event_chain(self.db, room_id=room_id)
        # A break already covers a divergent hash or a missing sequence; it does not
        # cover this reader stopping early. verify_event_chain makes exactly this
        # comparison for its own break detection (log end vs. room counter) — the
        # same fact, read here instead of recomputed, because a reader whose page
        # count silently fell short of the counter is unverified for the same
        # reason a broken hash is: what it exported is not what the room holds.
        chain_verified = (
            not any(b.room_id == room_id for b in breaks) and exported == sequence_counter
        )
        yield (
            json.dumps(
                {
                    "export_summary": {
                        "room_id": room_id,
                        "events": exported,
                        "sequence_counter": sequence_counter,
                        "chain_verified": chain_verified,
                        "verified_at": utcnow().isoformat(),
                    }
                }
            )
            + "\n"
        )

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
        self,
        room_id: str,
        last_sequence: int = 0,
        user_id: str = "",
        event_limit: int = _ROOM_EVENTS_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        room = await self.get_room(room_id)
        events = await self.get_room_events(room_id, last_sequence, limit=event_limit)
        members = await self.get_room_members(room_id)
        member_display_names = await self.repos.room_members.display_names(room_id)
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
        # Why each parked call is parked, from the call's own row. A reader answering
        # an approval can see whether the channel's posture stopped it or the tool's
        # own floor did, which is the difference between "this room pauses everything"
        # and "this action always pauses".
        approval_reasons = {
            approval.approval_id: gated.reason
            for approval in pending_approvals
            if (gated := await self.repos.tool_requests.get_by_approval(approval.approval_id))
        }
        posture = await self.repos.room_postures.current(room_id)
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
                # Derived from the declaration rows on this read, like every other
                # reader of a posture. Nothing here is a value spent later.
                "posture": posture.value,
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
                    "display_name": member_display_names.get(m.user_id, m.user_id),
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
                    "branch_id": run.branch_id,
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
                    # Metadata only, never the blob.
                    "attachments": [
                        {
                            "attachment_id": a.attachment_id,
                            "filename": a.filename,
                            "content_type": a.content_type,
                            "size_bytes": a.size_bytes,
                        }
                        for a in await self.repos.attachments.list_for_message(m.message_id)
                    ],
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
                    "reason": approval_reasons.get(a.approval_id, ""),
                }
                for a in pending_approvals
            ],
            "presence": [{"user_id": p.user_id, "status": p.status.value} for p in presence],
            "ontology": ontology,
        }

    # ── Agent-to-agent tasks ─────────────────────────────────────────────────

    async def _require_agent_task(self, task_id: str) -> AgentTask:
        """The row, or the specification's name for its absence."""
        task = await self.repos.agent_tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"no agent task {task_id}")
        return task

    async def _append_agent_task_event(
        self, task: AgentTask, actor_id: str, actor_type: str, **extra: Any
    ) -> RoomEvent:
        """One event type for the whole lifecycle, with the state in the payload.

        A reader that has to match one event type per state is a reader that will
        miss the ninth one the day somebody adds it. TASK_DELEGATED is the room
        log's existing name for "an agent was asked to do something", and every
        move of that task is the same fact changing, not a different kind of fact.
        """
        return await self._append_room_event(
            task.room_id,
            EventType.TASK_DELEGATED,
            {
                "task_id": task.task_id,
                "context_id": task.context_id,
                "state": task.state.value,
                "target_agent_id": task.target_agent_id,
                "delegating_agent_id": task.delegating_agent_id,
                "updated_at": task.updated_at.isoformat(),
                **extra,
            },
            actor_id,
            actor_type,
        )

    @staticmethod
    def _require_asker(task: AgentTask, requested_by: str) -> None:
        """Only the party that asked may say more about the task or take it back.

        The asker is a human on a task somebody opened by hand and an agent on a
        delegated one, so this compares against both rather than reaching for the
        room's membership table — an agent holds no membership there, and gating
        on one would have made every delegated task uncontinuable by the only
        party with anything to add.

        The refusal is the one a task nobody has ever heard of gets. Anything else
        would answer a question the caller was not entitled to ask: a stranger who
        can tell "that is not yours" from "there is no such thing" can enumerate
        every task id in the deployment one guess at a time.
        """
        if requested_by not in {task.authorized_by, task.requested_by, task.delegating_agent_id}:
            raise TaskNotFoundError(_NO_SUCH_AGENT_TASK)

    async def _asker_task(self, task_id: str, requested_by: str) -> AgentTask:
        """The task, if this caller is the one that asked for it. One answer if not.

        Asking is authorized once, at task creation, and never rechecked here for
        the agent case, because an agent holds no room membership to recheck. A
        human asker does hold membership, and it can change after the task opened
        (removal, demotion), so a human's continued standing is reread against the
        room every time, not trusted from the original ask.
        """
        task = await self.repos.agent_tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(_NO_SUCH_AGENT_TASK)
        self._require_asker(task, requested_by)
        if requested_by != task.delegating_agent_id:
            try:
                await self.authorization.require(task.room_id, requested_by, RoomCapability.MUTATE)
            except AuthorizationError as exc:
                raise TaskNotFoundError(_NO_SUCH_AGENT_TASK) from exc
        return task

    async def _visible_agent_task(
        self, task_id: str, user_id: str, capability: RoomCapability
    ) -> AgentTask:
        """The task, if this person may act on the room holding it. One answer if not.

        Resolution has to come first here, because the row is what says which room
        to ask about — so instead of authorizing first, the two failures are made
        one. A task id that was never minted and a task in a room this person
        cannot see are the same refusal, byte for byte and with no id echoed back.
        Asserting that each of them refuses is what let the difference between them
        survive: both were refusals, and the pair was an index of what exists.
        """
        task = await self.repos.agent_tasks.get(task_id)
        if task is not None:
            try:
                await self.authorization.require(task.room_id, user_id, capability)
            except AuthorizationError:
                task = None
        if task is None:
            raise TaskNotFoundError(_NO_SUCH_AGENT_TASK)
        return task

    async def _require_may_ask_here(
        self, room_id: str, requested_by: str, delegating_agent_id: str | None
    ) -> None:
        """May this caller ask anything in this room, decided before anything is looked up.

        Authorize first, resolve second. Resolution refusals describe what exists —
        an agent that is real but filed under another room, an id that was never
        minted — and a caller who may not act here is entitled to neither. Three
        distinguishable answers let somebody who is a member of nothing confirm
        that an agent id is real and then find the room it lives in, by walking ids
        against the differences.

        So the only thing consulted before the gate is the caller and the room, and
        the caller is a person every time — including on a delegated ask, where the
        route holds the delegating run and can name the human it is authorized by.
        An ``agent:`` principal has no membership row and so is refused here like
        any other stranger, which is the same wall that stops one from becoming a
        chain's root authority further down.

        The delegating agent has to be in the room it is asking in. That lookup
        answers False for an agent filed elsewhere and for an agent that was never
        spawned alike, and its refusal is worded as the gate's, so getting past the
        gate is the only thing any of these answers can be read for.
        """
        await self.authorization.require(room_id, requested_by, RoomCapability.MUTATE)
        if delegating_agent_id is not None and not await self.repos.agents.has_room_membership(
            delegating_agent_id, room_id
        ):
            raise AuthorizationError(_ROOM_ACCESS_FORBIDDEN)

    @staticmethod
    def _require_human_asker(requested_by: str) -> None:
        """A chain's root authority is a person, and the prefix is what proves it.

        An ``agent:`` principal arriving here would be written into ``authorized_by``
        and from there into ``executions.authorized_by``, which is one arm of the
        union every spend-point bounds by. The chain would then be authorized by an
        agent: removing the human from the room would change nothing about what it
        could spend, because the human was never in the record to begin with.
        """
        if requested_by.startswith(AGENT_PRINCIPAL_PREFIX):
            raise AuthorizationError(
                f"{requested_by} is an agent and cannot be the human a task is authorized by"
            )

    async def _delegating_task(
        self,
        room_id: str,
        delegating_agent_id: str,
        delegating_run_id: str | None,
        parent_task_id: str | None,
    ) -> AgentTask:
        """The task the delegating agent is itself running under, read from its run.

        Derived, never accepted. A parent the asker may decline to mention is a chain
        the asker may decline to have: A asking B asking A asking B arrived as twelve
        separate roots, every one of them depth zero with no chain rows at all, and
        ``require_delegable`` was handed an empty ancestry to look for a cycle in. The
        cycle was real; the evidence of it was simply never written down.

        So the delegator's own open run is what says which task this delegation
        descends from, and a delegation whose parent cannot be established is refused
        rather than rooted afresh. An agent that is not running under a task has
        nothing to delegate from — a person can open it one.
        """
        running = await self.repos.executions.latest_open_for_agent(delegating_agent_id)
        if running is None:
            raise TaskNotFoundError(
                f"agent {delegating_agent_id} has no open run and may not delegate"
            )
        if delegating_run_id is not None and delegating_run_id != running.execution_id:
            raise AuthorizationError(
                f"agent {delegating_agent_id} is running under {running.execution_id}, "
                f"not {delegating_run_id}"
            )
        parent = next(
            (
                task
                for task in await self.repos.agent_tasks.list_open_for_agent(delegating_agent_id)
                if task.execution_id == running.execution_id
            ),
            None,
        )
        if parent is None:
            raise TaskNotFoundError(
                f"the run agent {delegating_agent_id} is serving answers no task, "
                "so a delegation from it descends from nothing"
            )
        # And that task has to be in the room this delegation is being made in. An
        # agent placed in two rooms is running under one task at a time, so without
        # this an agent working in R1 could delegate in R2 and carry R1's context,
        # authority, chain and depth across a boundary the workspace draws to keep
        # one room's authority out of another's.
        if parent.room_id != room_id:
            raise AuthorizationError(
                f"agent {delegating_agent_id} is running under a task in another room"
            )
        if parent_task_id is not None and parent_task_id != parent.task_id:
            raise AuthorizationError(
                f"agent {delegating_agent_id} is running under task {parent.task_id}, "
                f"not {parent_task_id}"
            )
        return parent

    async def open_agent_task(
        self,
        room_id: str,
        target_agent_id: str,
        parts: tuple[Part, ...],
        *,
        requested_by: str,
        delegating_agent_id: str | None = None,
        delegating_run_id: str | None = None,
        parent_task_id: str | None = None,
        accepted_output_modes: tuple[str, ...] = (),
    ) -> AgentTask:
        """Ask an agent to do something, whether the asker is a person or an agent.

        The gates are the mention path's gates in the mention path's order, and
        delegation adds nothing to them but one more principal in the bounding
        set. That is the whole design: an agent asking is a principal like any
        other, so a delegate is ceilinged by every spend-point that already
        ceilings a mentioned agent, without any of them having learned a new name.
        """
        await self._require_may_ask_here(room_id, requested_by, delegating_agent_id)

        if delegating_agent_id is None:
            self._require_human_asker(requested_by)
            if parent_task_id is not None:
                raise AuthorizationError(
                    "a task a person opens is the root of its own chain; inheriting "
                    "another chain's authority is not something an asker may ask for"
                )
            parent = None
            ancestry: tuple[str, ...] = ()
            context_id = new_context_id()
            authorized_by = requested_by
        else:
            parent = await self._delegating_task(
                room_id, delegating_agent_id, delegating_run_id, parent_task_id
            )
            ancestry = (
                *await self.repos.agent_tasks.ancestry(parent.task_id),
                parent.target_agent_id,
            )
            context_id = parent.context_id
            # The human at the root of the chain, always, and read off the parent
            # rather than off the caller. A delegated task that re-rooted authority
            # on the delegating agent would be a way to launder a grant the human
            # never made: each hop would be authorized by the previous hop, and the
            # person who started it would drop out of the record on the first one.
            authorized_by = parent.authorized_by

        # Resolved after the gate, never before it. Which room an agent is filed
        # under, and whether the id names one at all, are answers this method used
        # to give away forty lines before it asked whether the caller could act here.
        agent = await self.get_agent(target_agent_id)
        if agent.room_id != room_id:
            raise DomainError("the agent being asked is not in this room")

        # Built whole rather than added to. ``also_bounded_by`` is reached by one
        # function in this class and takes no principal from its caller, which is
        # what keeps it from being the widening door fourteen rounds looked for;
        # this method does take one from its caller, so it names its set outright
        # and stays a construction site like the mention gate beside it.
        #
        # Both humans, because both of their ceilings apply. The root is who the
        # chain is authorized by and the caller is who asked for this hop, and on a
        # delegated task they are different people. Naming only the root was
        # relocation fifteen: an editor narrowed to one capability asked through a
        # delegation and the delegate spent three, because the narrowed member was
        # swapped out of this set rather than added to it. A principal whose ceiling
        # applies is added; one that is not durable is a row to write, which is why
        # ``requested_by`` is stored below and read back at every spend.
        asking = {authorized_by, requested_by}
        if delegating_agent_id is not None:
            asking.add(agent_principal(delegating_agent_id))
        bounding = BoundingPrincipals(frozenset(asking))
        if not (await self._lendable_terms(agent, room_id, bounding)).lendable():
            # No principal and no target in the wording: the caller knows who they
            # are, and this string ends up in logs and in an error body that crosses
            # an organisational boundary.
            raise AuthorizationError("no effective capability to open a task for this agent")
        await self._require_addressable(agent, room_id, authorized_by)

        depth = require_delegable(ancestry, target_agent_id)
        agreed = negotiate_output_modes(accepted_output_modes, DEFAULT_OUTPUT_MODES)
        task = AgentTask(
            task_id=new_id("a2atask"),
            context_id=context_id,
            room_id=room_id,
            target_agent_id=target_agent_id,
            authorized_by=authorized_by,
            requested_by=requested_by,
            delegating_agent_id=delegating_agent_id,
            # The run the delegation was made under, read off the parent task rather
            # than believed from the argument, so the row says which turn actually
            # asked instead of which turn the asker claimed.
            delegating_run_id=parent.execution_id if parent is not None else None,
            accepted_output_modes=agreed,
            depth=depth,
        )
        actor_type = "agent" if delegating_agent_id is not None else "user"
        async with self.db.transaction():
            created = await self.repos.agent_tasks.create_in_transaction(task, ancestry)
            await self.repos.agent_tasks.append_message_with_next_sequence_in_transaction(
                created.task_id, TaskMessageRole.ASKER, parts
            )
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.TASK_DELEGATED,
                    payload={
                        "task_id": created.task_id,
                        "context_id": created.context_id,
                        "state": created.state.value,
                        "target_agent_id": created.target_agent_id,
                        "delegating_agent_id": created.delegating_agent_id,
                        "updated_at": created.updated_at.isoformat(),
                        "authorized_by": created.authorized_by,
                        "depth": created.depth,
                        "requested_by": requested_by,
                    },
                    actor_id=delegating_agent_id or requested_by,
                    actor_type=actor_type,
                )
            )
        await self._broadcast_persisted_events([event])
        # NOT dispatched here yet. :meth:`_dispatch_agent_task_run` below is the
        # entry point the feature lacked, and calling it from here is what turns a
        # submitted task into a running one — but a turn dispatched from this line
        # runs to a terminal state before this method returns, which means a
        # delegating agent's run is closed by the time anything could delegate from
        # it. Delegation happens mid-turn, from inside the harness, so the trigger
        # belongs where the delegate's turn can hold itself open. Wiring it here
        # without that ends every chain at depth one. Named in the report, not
        # papered over.
        return created

    # A task older than this and still SUBMITTED did not merely lose a race with
    # the background dispatch that accepting it schedules — it lost the dispatch
    # itself, most likely to a crash between the accept commit and the
    # create_task call. The sweep below is what heals that on the next restart,
    # or on the next pass of whatever process calls it periodically.
    _STALE_SUBMITTED_TASK_SECONDS = 30

    def dispatch_agent_task_in_background(self, task: AgentTask) -> None:
        """Schedule a submitted task's turn off the request path, supervised.

        A2A's message/send is non-blocking by contract (see `_accept_message`
        in a2a.py): the caller is owed a SUBMITTED task back immediately, not
        the wall time of a provider call. `_dispatch_agent_task_run` never
        raises anything but its own cancellation, propagated rather than
        swallowed, and every other exit resolves the task or logs, so this
        only needs to keep the asyncio.Task alive until it finishes; without
        that reference the loop is free to garbage-collect it mid-flight.
        """
        running = asyncio.create_task(self._dispatch_agent_task_run(task))
        self._background_tasks.add(running)
        running.add_done_callback(self._background_tasks.discard)

    async def sweep_stale_submitted_agent_tasks(self) -> int:
        """Drain every task stuck SUBMITTED past the point that can only mean
        a lost handoff, so a crash between accept and dispatch heals on its own.

        One task at a time, one batch per query: the drain is a marathon with
        a bounded stride, never a thundering herd of concurrent provider
        calls. `_dispatch_agent_task_run` never raises and resolves losing
        races through its compare-and-swap, so a task the post-accept path
        already grabbed is skipped here without ceremony.
        """
        drained = 0
        attempted: set[str] = set()
        while True:
            threshold = utcnow() - timedelta(seconds=self._STALE_SUBMITTED_TASK_SECONDS)
            stale = await self.repos.agent_tasks.list_stale_submitted(threshold)
            fresh = [task for task in stale if task.task_id not in attempted]
            if not fresh:
                # Anything still listed was already attempted this drain: a
                # task that will not leave SUBMITTED is a row to investigate,
                # not a reason to spin on it.
                return drained
            for task in fresh:
                attempted.add(task.task_id)
                await self._dispatch_agent_task_run(task)
                drained += 1

    async def sweep_stranded_working_agent_tasks(self) -> int:
        """Fail every task WORKING behind a run that has already settled.

        ``_dispatch_agent_task_run`` fails a task itself when its own turn
        ends badly or is cancelled, but a harder kill (SIGKILL, an OOM, the
        process dying) leaves no handler running to catch anything: the run
        it was driving is later settled by ``sweep_expired_run_leases``,
        ORPHANED or otherwise, and nothing else ever revisits the task,
        because ``sweep_stale_submitted_agent_tasks`` only looks at
        SUBMITTED. A task WORKING behind a settled run is a delegator waiting
        on an answer that will never come, which is exactly the failure mode
        the docstring on ``_dispatch_agent_task_run`` says a state machine
        exists to prevent; this is what makes that true after a restart too,
        not only while the same process is still running.
        """
        failed = 0
        for task in await self.repos.agent_tasks.list_working_with_settled_run():
            run = await self.repos.agent_runs.get_by_execution(task.execution_id or "")
            settlement = run.settlement.value if run is not None and run.settlement else "unknown"
            try:
                await self.fail_agent_task(
                    task.task_id,
                    f"the run driving this task settled ({settlement}) with nothing "
                    "left to carry it further",
                    by_agent_id=task.target_agent_id,
                )
                failed += 1
            except DomainError:
                # Something else moved the task on since the list was read; the
                # sweep's own compare-and-swap (inside transition()) is what
                # makes that race land on whoever actually won it, not on this.
                log.info("Agent task %s moved before the stranded sweep reached it", task.task_id)
        return failed

    async def _dispatch_agent_task_run(self, task: AgentTask) -> None:
        """Drive a submitted task to a terminal state, or say on the row why not.

        The invariant is the mention path's: no task is left in a state the system
        cannot describe. Starting it is the compare-and-swap that makes it this
        process's work; the claim on the execution then keeps the startup sweep from
        mistaking a live run for an orphan. Anything that escapes the turn fails the
        task rather than leaving it WORKING forever, because a delegator waiting on
        an answer that will never come is the failure mode a state machine exists to
        prevent.
        """
        asked = " ".join(part.content for part in await self._asker_parts(task.task_id))
        try:
            started = await self.start_agent_task(task.task_id)
        except DomainError:
            log.exception("Agent task %s could not be started", task.task_id)
            return
        execution_id = started.execution_id or ""
        if not await self.repos.executions.claim_for_dispatch(execution_id, self._dispatch_claim):
            log.info("Agent task run %s was already claimed; not dispatching", execution_id)
            return
        try:
            result = await self.execute_agent_step(
                execution_id, fenced(screen(asked, "agent task"))
            )
            output_id = str(result.get("output_id", ""))
            output = await self.repos.agent_outputs.get(output_id) if output_id else None
            if output is None:
                # A turn that ended without an answer is not a completed task. The
                # run's own settlement already says what happened to it.
                await self.fail_agent_task(
                    task.task_id,
                    "the turn ended without an answer",
                    by_agent_id=task.target_agent_id,
                )
                return
            await self.complete_agent_task(
                task.task_id,
                (Part(kind=PartKind.TEXT, content=output.content),),
                by_agent_id=task.target_agent_id,
            )
        except asyncio.CancelledError:
            # A shutdown cancels every fire-and-forget dispatch (server.py), and
            # CancelledError derives from BaseException, so it would otherwise
            # escape the Exception handler below and leave this task claiming to
            # be working forever, with nothing left running that could ever move
            # it. Failed here instead, then re-raised, so the cancellation still
            # propagates the way the rest of this process's shutdown expects.
            log.info("Agent task %s dispatch was cancelled", task.task_id)
            try:
                await self.fail_agent_task(
                    task.task_id, "dispatch was cancelled", by_agent_id=task.target_agent_id
                )
            except Exception:
                log.exception("Failed to fail agent task %s after cancellation", task.task_id)
            raise
        except Exception as exc:
            log.exception("Agent task %s did not complete", task.task_id)
            try:
                await self.fail_agent_task(
                    task.task_id, f"dispatch failed: {exc}", by_agent_id=task.target_agent_id
                )
            except Exception:
                # The task and its ask are already committed. Failing this write
                # would leave the row claiming to be working; the sweep is what
                # catches that, and it is told by logs rather than by a raise here.
                log.exception("Failed to fail agent task %s", task.task_id)

    async def _asker_parts(self, task_id: str) -> tuple[Part, ...]:
        """What the asker last said, which is the prompt the delegate answers."""
        messages = await self.repos.agent_tasks.list_messages(task_id)
        asks = [m for m in messages if m.role is TaskMessageRole.ASKER]
        return asks[-1].parts if asks else ()

    async def start_agent_task(self, task_id: str) -> AgentTask:
        """Open the turn that answers a submitted task.

        The execution is triggered DIRECT rather than by a fourth trigger value of
        its own. ``executions.triggered_by`` carries a CHECK constraint admitting
        MENTION, DIRECT and SCHEDULE, so a fourth would mean rebuilding the table —
        and it would restate there what one join already answers, because the fact
        that distinguishes a delegated turn, who asked and under which run, is a
        column on ``agent_tasks`` and not a shade of the reason a turn opened.

        The task moves before the turn is opened, because the move is the only
        mutual exclusion there is. Opening first meant two concurrent starts both
        built a session, an execution and a run envelope, and only the winner's got
        attached — leaving the loser's holding a live credential and a lease that
        nothing would ever come back to close. Losing the compare-and-swap now costs
        a refusal instead of an orphan.
        """
        task = await self._require_agent_task(task_id)
        agent = await self.get_agent(task.target_agent_id)
        run = await self._prepare_agent_run(agent, task.room_id, task.authorized_by)
        session = Session(
            session_id=new_id("sess"),
            room_id=task.room_id,
            agent_id=task.target_agent_id,
            status=SessionStatus.ACTIVE,
        )
        execution = Execution(
            execution_id=new_id("exec"),
            session_id=session.session_id,
            agent_id=task.target_agent_id,
            authorized_by=task.authorized_by,
            # The link the bound is derived through. A task opens a fresh run every
            # time it resumes, and each of them has to keep pointing at the task.
            agent_task_id=task.task_id,
            triggered_by=AgentTrigger.DIRECT,
            input_data={"agent_task_id": task.task_id, "context_id": task.context_id},
        )
        # The WORKING transition, the session/execution/run rows and the
        # attach that binds them back onto the task are one fact — a task
        # left WORKING with no run is an orphan the settler cannot see,
        # because it only scans MENTION triggers. All or nothing here.
        async with self.db.transaction():
            await self.repos.agent_tasks.transition_in_transaction(
                task_id, task.state, AgentTaskState.WORKING
            )
            await self.repos.sessions.create(session)
            execution = await self.repos.executions.create(execution)
            await self.repos.agent_runs.create_in_transaction(
                replace(run, execution_id=execution.execution_id)
            )
            await self.repos.agent_tasks.attach_execution_in_transaction(
                task_id, execution.execution_id
            )
        # The asker is a participant of this run, so the run carries a row saying so
        # and every spend reads it. ``authorized_by`` is already an arm of the bound;
        # on a delegated task the caller is somebody else, and without this row their
        # ceiling would apply at the door and nowhere afterwards.
        await self.repos.executions.record_caller(execution.execution_id, task.requested_by)
        started = await self._require_agent_task(task_id)
        await self._append_agent_task_event(
            started, started.target_agent_id, "agent", execution_id=execution.execution_id
        )
        return started

    async def continue_agent_task(
        self, task_id: str, parts: tuple[Part, ...], *, requested_by: str
    ) -> AgentTask:
        """The asker answers what the delegate asked for, and the task resumes."""
        task = await self._asker_task(task_id, requested_by)
        # The message is what moves the task, so it is written first — and the move
        # is checked before it, or a refused transition would leave its message
        # standing in the task's log as a turn that never happened. Every method
        # here that writes before it transitions asks in this order for that reason.
        # Both writes are one transaction, so the loser of a race against another
        # caller of this same method never leaves its message standing without the
        # move it was written for.
        require_transition(task.state, AgentTaskState.WORKING)
        async with self.db.transaction():
            await self.repos.agent_tasks.append_message_with_next_sequence_in_transaction(
                task_id, TaskMessageRole.ASKER, parts
            )
            moved = await self.repos.agent_tasks.transition_in_transaction(
                task_id, task.state, AgentTaskState.WORKING
            )
        await self._append_agent_task_event(
            moved,
            requested_by,
            "agent" if requested_by == task.delegating_agent_id else "user",
        )
        return moved

    async def _delegate_task(self, task_id: str, by_agent_id: str) -> AgentTask:
        """The task, if this agent is the one being asked. One answer if not.

        Five verbs move a task from the delegate's side — it needs more, it needs a
        person, it answers, it fails, it declines — and all five took a task id and
        nothing else. Any principal that reached one could finish or fail somebody
        else's task, and the refusal is the unknown-task one for the same reason the
        asker's is: a caller who can tell "not yours" from "no such thing" can
        enumerate the deployment's task ids one guess at a time.
        """
        task = await self.repos.agent_tasks.get(task_id)
        if task is None or task.target_agent_id != by_agent_id:
            raise TaskNotFoundError(_NO_SUCH_AGENT_TASK)
        return task

    async def require_agent_task_input(
        self, task_id: str, parts: tuple[Part, ...], *, by_agent_id: str
    ) -> AgentTask:
        """The delegate needs more from whoever asked, and stops until it arrives."""
        task = await self._delegate_task(task_id, by_agent_id)
        require_transition(task.state, AgentTaskState.INPUT_REQUIRED)
        async with self.db.transaction():
            await self.repos.agent_tasks.append_message_with_next_sequence_in_transaction(
                task_id, TaskMessageRole.DELEGATE, parts
            )
            moved = await self.repos.agent_tasks.transition_in_transaction(
                task_id, task.state, AgentTaskState.INPUT_REQUIRED
            )
        await self._append_agent_task_event(moved, moved.target_agent_id, "agent")
        return moved

    async def escalate_agent_task(
        self, task_id: str, *, reason: str, by_agent_id: str
    ) -> AgentTask:
        """Nobody in the chain can lend what this task needs, so a person is asked.

        The reason rides on the event rather than on ``refusal_reason``: nothing has
        been refused yet, and a column that means "why it ended" would then be
        holding "what somebody is being asked for" on a task still very much alive.
        """
        task = await self._delegate_task(task_id, by_agent_id)
        moved = await self.repos.agent_tasks.transition(
            task_id, task.state, AgentTaskState.AUTH_REQUIRED
        )
        await self._append_agent_task_event(
            moved, moved.target_agent_id, "agent", escalation_reason=reason
        )
        return moved

    async def resolve_agent_task_escalation(
        self, task_id: str, *, granted: bool, by_user_id: str
    ) -> AgentTask:
        """A named person answers the one escalation, either way.

        "No" is a rejection of the task and not a failure of the agent that asked,
        which is why AUTH_REQUIRED has an edge to REJECTED at all.
        """
        task = await self._visible_agent_task(task_id, by_user_id, RoomCapability.MUTATE)
        target = AgentTaskState.WORKING if granted else AgentTaskState.REJECTED
        moved = await self.repos.agent_tasks.transition(
            task_id,
            task.state,
            target,
            refusal_reason="" if granted else f"{by_user_id} declined the escalation",
        )
        await self._append_agent_task_event(moved, by_user_id, "user", granted=granted)
        return moved

    async def complete_agent_task(
        self, task_id: str, parts: tuple[Part, ...], *, by_agent_id: str
    ) -> AgentTask:
        """The delegate answers, and the task ends where it was asked to end."""
        task = await self._delegate_task(task_id, by_agent_id)
        require_transition(task.state, AgentTaskState.COMPLETED)
        async with self.db.transaction():
            await self.repos.agent_tasks.append_message_with_next_sequence_in_transaction(
                task_id, TaskMessageRole.DELEGATE, parts
            )
            moved = await self.repos.agent_tasks.transition_in_transaction(
                task_id, task.state, AgentTaskState.COMPLETED
            )
        await self._append_agent_task_event(moved, moved.target_agent_id, "agent")
        return moved

    async def fail_agent_task(self, task_id: str, reason: str, *, by_agent_id: str) -> AgentTask:
        task = await self._delegate_task(task_id, by_agent_id)
        moved = await self.repos.agent_tasks.transition(
            task_id, task.state, AgentTaskState.FAILED, refusal_reason=reason
        )
        await self._append_agent_task_event(moved, moved.target_agent_id, "agent")
        return moved

    async def reject_agent_task(self, task_id: str, reason: str, *, by_agent_id: str) -> AgentTask:
        """The delegate declines before doing anything, which is not a failure."""
        task = await self._delegate_task(task_id, by_agent_id)
        moved = await self.repos.agent_tasks.transition(
            task_id, task.state, AgentTaskState.REJECTED, refusal_reason=reason
        )
        await self._append_agent_task_event(moved, moved.target_agent_id, "agent")
        return moved

    async def cancel_agent_task(self, task_id: str, *, requested_by: str) -> AgentTask:
        """The asker takes it back, unless it has already ended.

        A finished task that can be cancelled is not a task with a lifecycle, so the
        refusal is named rather than left to the transition table: a caller cancelling
        something that completed a second earlier is owed the difference between "too
        late" and "that was never allowed".
        """
        task = await self._asker_task(task_id, requested_by)
        if task.is_terminal:
            raise TaskNotCancelableError(
                f"task {task_id} is already {task.state.value} and cannot be canceled"
            )
        try:
            moved = await self.repos.agent_tasks.transition(
                task_id,
                task.state,
                AgentTaskState.CANCELED,
                refusal_reason=f"canceled by {requested_by}",
            )
        except DomainError:
            # It ended between the read above and the write. The caller is owed the
            # same name they would have got a moment earlier, not a description of
            # the compare-and-swap that lost.
            settled = await self.repos.agent_tasks.get(task_id)
            if settled is not None and settled.is_terminal:
                raise TaskNotCancelableError(
                    f"task {task_id} is already {settled.state.value} and cannot be canceled"
                ) from None
            raise
        await self._append_agent_task_event(
            moved,
            requested_by,
            "agent" if requested_by == task.delegating_agent_id else "user",
        )
        return moved

    async def get_agent_task(self, task_id: str, *, viewer_id: str) -> AgentTask:
        return await self._visible_agent_task(task_id, viewer_id, RoomCapability.READ)

    async def list_agent_task_messages(
        self, task_id: str, *, viewer_id: str
    ) -> list[AgentTaskMessage]:
        await self._visible_agent_task(task_id, viewer_id, RoomCapability.READ)
        return await self.repos.agent_tasks.list_messages(task_id)


# Needed for hashlib import in create_artifact
