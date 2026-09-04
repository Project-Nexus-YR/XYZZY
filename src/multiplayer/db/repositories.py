"""Repository layer: typed data access over the multiplayer database."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import Any

from ..domain.agent_tasks import (
    TERMINAL_STATES,
    AgentTask,
    AgentTaskMessage,
    AgentTaskState,
    DelegationCycleError,
    Part,
    TaskMessageRole,
    require_transition,
)
from ..domain.events import EventType, RoomEvent
from ..domain.models import (
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
    IdempotencyRecord,
    Memory,
    MemoryScope,
    MentionTargetType,
    Message,
    MessageMention,
    MessageReaction,
    MessageRole,
    Notification,
    NotificationStatus,
    OidcAuthorization,
    OntologyDerivationKind,
    OntologyEntity,
    OntologyEntityKind,
    OntologyExtractionCursor,
    OntologyExtractor,
    OntologyRelationship,
    OntologyRelationshipKind,
    OntologyReview,
    OntologyReviewAction,
    OntologyReviewStatus,
    OntologyReviewTarget,
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
    SearchObjectKind,
    Session,
    SessionRefreshToken,
    SessionStatus,
    Task,
    TaskDependency,
    TaskPriority,
    TaskStatus,
    ThreadReply,
    ThreadSummary,
    ToolPermission,
    ToolRequest,
    TurnLock,
    TurnLockScopeType,
    TurnLockStatus,
    User,
    UserSession,
    UserStatus,
    Workspace,
    WorkspaceMember,
    new_id,
    utcnow,
    weakest_derivation_kind,
    weakest_review_status,
)
from ..security.audit import GENESIS_HASH, event_chain_hash
from ..security.authorization import RoomCapability, roles_with_capability
from ..security.capabilities import Posture
from .connection import Database, deserialize_datetime, serialize_datetime

log = logging.getLogger(__name__)


class Repos:
    """Access point for all repository operations."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.users = UserRepo(db)
        self.user_sessions = UserSessionRepo(db)
        self.orgs = OrgRepo(db)
        self.workspaces = WorkspaceRepo(db)
        self.bootstrap_contexts = BootstrapContextRepo(db)
        self.rooms = RoomRepo(db)
        self.room_postures = RoomPostureRepo(db)
        self.room_members = RoomMemberRepo(db)
        self.agents = AgentRepo(db)
        self.room_templates = RoomTemplateRepo(db)
        self.agent_identities = AgentIdentityRepo(db)
        self.agent_addressing = AgentAddressingRepo(db)
        self.agent_runs = AgentRunRepo(db)
        self.branches = BranchRepo(db)
        self.sessions = SessionRepo(db)
        self.executions = ExecutionRepo(db)
        self.interventions = ExecutionInterventionRepo(db)
        self.suspended_turns = SuspendedTurnRepo(db)
        self.agent_outputs = AgentOutputRepo(db)
        self.output_selections = OutputSelectionRepo(db)
        self.branch_syntheses = BranchSynthesisRepo(db)
        self.turn_locks = TurnLockRepo(db)
        self.tasks = TaskRepo(db)
        self.agent_tasks = AgentTaskRepo(db)
        self.messages = MessageRepo(db)
        self.mentions = MessageMentionRepo(db)
        self.attachments = AttachmentRepo(db)
        self.handles = RoomParticipantHandleRepo(db)
        self.reactions = MessageReactionRepo(db)
        self.read_cursors = ReadCursorRepo(db)
        self.search = SearchRepo(db)
        self.events = EventRepo(db)
        self.artifacts = ArtifactRepo(db)
        self.artifact_shares = ArtifactShareRepo(db)
        self.decisions = DecisionRepo(db)
        self.memories = MemoryRepo(db)
        self.approvals = ApprovalRepo(db)
        self.notifications = NotificationRepo(db)
        self.tool_permissions = ToolPermissionRepo(db)
        self.tool_requests = ToolRequestRepo(db)
        self.idempotency = IdempotencyRepo(db)
        self.ontology = OntologyRepo(db)
        self.meta = MetaRepo(db)


def _require_execution_transition(
    cursor: Any,
    execution_id: str,
    expected: ExecutionStatus,
    target: ExecutionStatus,
) -> None:
    """Every execution status write is conditional on the status the caller read.

    A write that no longer matches touches zero rows, which means another process
    moved the run on. Raising here surfaces that instead of letting the later
    writer silently win, and rolls back anything written beside it.
    """
    if cursor.rowcount != 1:
        raise DomainError(
            f"execution {execution_id} is no longer {expected.value}: "
            f"the transition to {target.value} was not applied"
        )


async def _require_open_agent_run(db: Database, execution_id: str) -> None:
    """A settled run may not write. This refusal, not the credential, is what stops it.

    Settlement is decided by the database and telling the harness is best-effort, so an
    in-flight turn can still land after its run was settled. The write path it lands on
    is complete_execution, which consulted neither agent_runs nor any credential.
    """
    row = await db.fetch_one(
        "SELECT harness_state, settlement FROM agent_runs WHERE execution_id = ?", (execution_id,)
    )
    if row is not None and row["harness_state"] == HarnessState.SETTLED.value:
        raise DomainError(
            f"run for execution {execution_id} is settled ({row['settlement']}) "
            "and may not write an output"
        )


async def _settle_agent_run_in_transaction(
    db: Database,
    execution_id: str,
    settlement: RunSettlement,
    decided_by: str,
) -> list[RoomEvent]:
    """Settle the envelope around one execution, if it has one and is still open."""
    row = await db.fetch_one(
        "SELECT run_id, room_id, harness_state FROM agent_runs WHERE execution_id = ?",
        (execution_id,),
    )
    if row is None or row["harness_state"] == HarnessState.SETTLED.value:
        return []
    settled_at = serialize_datetime(utcnow())
    await db.execute(
        "UPDATE agent_runs SET harness_state = ?, settlement = ?, settled_at = ? "
        "WHERE run_id = ? AND harness_state <> ?",
        (
            HarnessState.SETTLED.value,
            settlement.value,
            settled_at,
            row["run_id"],
            HarnessState.SETTLED.value,
        ),
    )
    events = [
        RoomEvent(
            room_id=str(row["room_id"]),
            sequence=0,
            event_type=EventType.AGENT_RUN_SETTLED,
            payload={
                "run_id": str(row["run_id"]),
                "execution_id": execution_id,
                "settlement": settlement.value,
                "decided_by": decided_by,
            },
            actor_id=decided_by or "system",
            actor_type="system",
        )
    ]
    if settlement is RunSettlement.ORPHANED:
        events.append(
            RoomEvent(
                room_id=str(row["room_id"]),
                sequence=0,
                event_type=EventType.AGENT_RUN_ORPHANED,
                payload={"run_id": str(row["run_id"]), "execution_id": execution_id},
                actor_id=decided_by or "system",
                actor_type="system",
            )
        )
    return events


async def _finish_managed_branch_if_terminal(
    db: Database,
    branch_id: str,
    room_id: str,
    actor_id: str,
) -> list[RoomEvent]:
    """Derive a branch terminal status from every owned AgentRun."""
    branch_row = await db.fetch_one(
        "SELECT lifecycle_managed, status FROM branches WHERE branch_id = ?", (branch_id,)
    )
    if branch_row is None or not bool(branch_row["lifecycle_managed"]):
        return []
    if branch_row["status"] in {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED"}:
        return []
    rows = await db.fetch_all(
        "SELECT status FROM executions WHERE branch_id = ? ORDER BY execution_id", (branch_id,)
    )
    if not rows:
        return []
    terminal_values = {"COMPLETED", "FAILED", "CANCELLED"}
    statuses = [str(row["status"]) for row in rows]
    if any(status not in terminal_values for status in statuses):
        return []
    distinct = set(statuses)
    if len(distinct) > 1:
        branch_status = BranchStatus.PARTIAL
        event_type = EventType.BRANCH_PARTIAL
    elif statuses[0] == "COMPLETED":
        branch_status = BranchStatus.COMPLETED
        event_type = EventType.BRANCH_COMPLETED
    elif statuses[0] == "FAILED":
        branch_status = BranchStatus.FAILED
        event_type = EventType.BRANCH_FAILED
    else:
        branch_status = BranchStatus.CANCELLED
        event_type = EventType.BRANCH_CANCELLED
    completed_at = serialize_datetime(utcnow())
    await db.execute(
        "UPDATE branches SET status = ?, updated_at = ?, completed_at = ? WHERE branch_id = ?",
        (branch_status.value, completed_at, completed_at, branch_id),
    )
    events = [
        RoomEvent(
            room_id=room_id,
            sequence=0,
            event_type=event_type,
            payload={"branch_id": branch_id, "status": branch_status.value},
            actor_id=actor_id,
            actor_type="agent",
        )
    ]
    lock_row = await db.fetch_one(
        "SELECT lock_id FROM turn_locks WHERE branch_id = ? AND status = 'ACTIVE'", (branch_id,)
    )
    if lock_row is not None:
        await db.execute(
            "UPDATE turn_locks SET status = 'RELEASED', released_at = ?, release_reason = ? "
            "WHERE lock_id = ?",
            (completed_at, branch_status.value, lock_row["lock_id"]),
        )
        events.append(
            RoomEvent(
                room_id=room_id,
                sequence=0,
                event_type=EventType.TURN_LOCK_RELEASED,
                payload={
                    "lock_id": lock_row["lock_id"],
                    "branch_id": branch_id,
                    "reason": branch_status.value,
                },
                actor_id=actor_id,
                actor_type="agent",
            )
        )
    return events


class UserRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, user: User) -> User:
        await self.db.execute(
            "INSERT INTO users(user_id, display_name, email, avatar_url, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                user.user_id,
                user.display_name,
                user.email,
                user.avatar_url,
                user.status.value,
                serialize_datetime(user.created_at),
            ),
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


class UserSessionRepo:
    """Sessions, their refresh tokens, and the pending half of a login.

    A session and the access credential that spends it are written together, and
    revoked together, because a credential that outlives its session is exactly
    the hole a revocation is supposed to close.

    Nothing here decides policy. Whether a session is still alive is
    ``UserSession.alive_at``, asked once by the authenticator; whether a refresh
    may be spent is decided by the atomic claim in :meth:`claim_refresh`, which
    is a write rather than a read for the same reason every other check-then-use
    defect in this codebase was a read.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _row_to_session(row: dict[str, Any]) -> UserSession:
        return UserSession(
            session_id=row["session_id"],
            user_id=row["user_id"],
            issuer=row["issuer"],
            subject=row["subject"],
            idp_session_id=row["idp_session_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            idle_expires_at=datetime.fromisoformat(row["idle_expires_at"]),
            absolute_expires_at=datetime.fromisoformat(row["absolute_expires_at"]),
            revoked_at=(datetime.fromisoformat(row["revoked_at"]) if row["revoked_at"] else None),
            revoked_reason=row["revoked_reason"] or "",
            idp_id_token=row["idp_id_token"] or "",
            idp_refresh_token=row["idp_refresh_token"] or "",
        )

    async def create_in_transaction(
        self,
        session: UserSession,
        access_token_hash: str,
        refresh: SessionRefreshToken,
        access_expires_at: datetime,
    ) -> UserSession:
        """Session, refresh credential and access credential, or none of them."""
        await self.db.execute(
            "INSERT INTO user_sessions(session_id, user_id, issuer, subject, idp_session_id, "
            "created_at, idle_expires_at, absolute_expires_at, revoked_at, revoked_reason, "
            "idp_id_token, idp_refresh_token) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, '', ?, ?)",
            (
                session.session_id,
                session.user_id,
                session.issuer,
                session.subject,
                session.idp_session_id,
                serialize_datetime(session.created_at),
                serialize_datetime(session.idle_expires_at),
                serialize_datetime(session.absolute_expires_at),
                session.idp_id_token,
                session.idp_refresh_token,
            ),
        )
        await self.db.execute(
            "INSERT INTO user_tokens(token_hash, user_id, label, created_at, session_id, "
            "expires_at) VALUES (?, ?, 'sso', ?, ?, ?)",
            (
                access_token_hash,
                session.user_id,
                serialize_datetime(session.created_at),
                session.session_id,
                serialize_datetime(access_expires_at),
            ),
        )
        await self.issue_refresh_in_transaction(refresh)
        return session

    async def issue_refresh_in_transaction(self, refresh: SessionRefreshToken) -> None:
        await self.db.execute(
            "INSERT INTO session_refresh_tokens(token_hash, session_id, issued_at, expires_at, "
            "consumed_at, replaced_by_hash) VALUES (?, ?, ?, ?, NULL, NULL)",
            (
                refresh.token_hash,
                refresh.session_id,
                serialize_datetime(refresh.issued_at),
                serialize_datetime(refresh.expires_at),
            ),
        )

    async def get(self, session_id: str) -> UserSession | None:
        row = await self.db.fetch_one(
            "SELECT * FROM user_sessions WHERE session_id = ?", (session_id,)
        )
        return None if row is None else self._row_to_session(row)

    async def touch_idle(self, session_id: str, idle_expires_at: datetime) -> None:
        """Push the idle clock forward, never backward.

        The guard matters because requests race: two in the same instant would
        otherwise let the one that computed its deadline first overwrite the
        later one, quietly shortening a session that is being actively used.
        """
        await self.db.execute(
            "UPDATE user_sessions SET idle_expires_at = ? "
            "WHERE session_id = ? AND idle_expires_at < ? AND revoked_at IS NULL",
            (serialize_datetime(idle_expires_at), session_id, serialize_datetime(idle_expires_at)),
        )
        await self.db.commit()

    async def get_refresh(self, token_hash: str) -> SessionRefreshToken | None:
        row = await self.db.fetch_one(
            "SELECT * FROM session_refresh_tokens WHERE token_hash = ?", (token_hash,)
        )
        if row is None:
            return None
        return SessionRefreshToken(
            token_hash=row["token_hash"],
            session_id=row["session_id"],
            issued_at=datetime.fromisoformat(row["issued_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
            consumed_at=(
                datetime.fromisoformat(row["consumed_at"]) if row["consumed_at"] else None
            ),
            replaced_by_hash=row["replaced_by_hash"],
        )

    async def claim_refresh_in_transaction(
        self, token_hash: str, replaced_by_hash: str, moment: datetime
    ) -> bool:
        """Spend a refresh token, once. True only for the caller that won it.

        The claim is the UPDATE, not a preceding SELECT. Two clients presenting
        the same token concurrently both read it unconsumed; only one can write
        the row that says so, and the loser is told it replayed — which is the
        correct answer, because one of them is holding a copy it should not have.
        """
        cursor = await self.db.execute(
            "UPDATE session_refresh_tokens SET consumed_at = ?, replaced_by_hash = ? "
            "WHERE token_hash = ? AND consumed_at IS NULL",
            (serialize_datetime(moment), replaced_by_hash, token_hash),
        )
        return bool(cursor.rowcount)

    async def revoke_in_transaction(self, session_id: str, reason: str, moment: datetime) -> bool:
        """Kill a session and every credential minted for it, in one write."""
        cursor = await self.db.execute(
            "UPDATE user_sessions SET revoked_at = ?, revoked_reason = ? "
            "WHERE session_id = ? AND revoked_at IS NULL",
            (serialize_datetime(moment), reason, session_id),
        )
        revoked = bool(cursor.rowcount)
        await self.db.execute(
            "UPDATE user_tokens SET revoked_at = ? WHERE session_id = ? AND revoked_at IS NULL",
            (serialize_datetime(moment), session_id),
        )
        await self.db.execute(
            "UPDATE session_refresh_tokens SET consumed_at = ? "
            "WHERE session_id = ? AND consumed_at IS NULL",
            (serialize_datetime(moment), session_id),
        )
        return revoked

    async def live_session_ids(
        self,
        *,
        user_id: str | None = None,
        issuer: str | None = None,
        subject: str | None = None,
        idp_session_id: str | None = None,
    ) -> list[str]:
        """Every unrevoked session matching whichever identifiers were given.

        Back-channel logout names a session by `sid`, or a person by `sub`, and
        the caller does not get to guess which: an unnamed field is not a filter,
        so nothing widens to "all sessions" because an argument was omitted.
        """
        clauses = ["revoked_at IS NULL"]
        params: list[Any] = []
        for column, value in (
            ("user_id", user_id),
            ("issuer", issuer),
            ("subject", subject),
            ("idp_session_id", idp_session_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if len(clauses) == 1:
            raise ValueError("live_session_ids needs at least one identifier to match on")
        rows = await self.db.fetch_all(
            f"SELECT session_id FROM user_sessions WHERE {' AND '.join(clauses)}",
            tuple(params),
        )
        return [str(row["session_id"]) for row in rows]

    async def start_authorization(self, authorization: OidcAuthorization) -> None:
        await self.db.execute(
            "INSERT INTO oidc_authorizations(state, nonce, code_verifier, "
            "browser_binding_hash, created_at, expires_at, consumed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (
                authorization.state,
                authorization.nonce,
                authorization.code_verifier,
                authorization.browser_binding_hash,
                serialize_datetime(authorization.created_at),
                serialize_datetime(authorization.expires_at),
            ),
        )
        await self.db.commit()

    async def claim_authorization(
        self, state: str, moment: datetime, binding_hash: str = ""
    ) -> OidcAuthorization | None:
        """Consume a pending login once, and only for the browser that opened it.

        The binding is part of the claim rather than a check after it. Consuming
        first and comparing afterwards lets anyone holding a state value burn a
        victim's pending login: the attacker fails, but so does the victim, who
        now has to start again for reasons nothing explains.

        The comparison is exact: an empty stored binding is not a wildcard that
        matches any caller. Only ``begin_login`` writes this column today and it
        always hashes a real binding, so an empty stored value never happens in
        practice, but a comparison that treats one as "anybody" would silently
        disable the protection the moment a second writer, a migration default,
        or a fixture ever left one empty.
        """
        cursor = await self.db.execute(
            "UPDATE oidc_authorizations SET consumed_at = ? "
            "WHERE state = ? AND consumed_at IS NULL AND browser_binding_hash = ?",
            (serialize_datetime(moment), state, binding_hash),
        )
        if not cursor.rowcount:
            await self.db.commit()
            return None
        row = await self.db.fetch_one("SELECT * FROM oidc_authorizations WHERE state = ?", (state,))
        await self.db.commit()
        if row is None:
            return None
        return OidcAuthorization(
            state=row["state"],
            nonce=row["nonce"],
            code_verifier=row["code_verifier"],
            browser_binding_hash=row["browser_binding_hash"] or "",
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
            consumed_at=datetime.fromisoformat(row["consumed_at"]),
        )

    async def remember_logout_token_in_transaction(
        self, jti: str, issuer: str, moment: datetime
    ) -> bool:
        """True the first time this logout token is seen, False for a replay.

        Deliberately has no commit of its own. Burning the jti in one transaction
        and revoking in another means a failure between them loses the revocation
        permanently: the retry is refused as a replay while the session it named
        stays alive. A critic proved that by making the revocation raise, so the
        two now succeed or fail together.
        """
        cursor = await self.db.execute(
            "INSERT INTO oidc_logout_tokens(jti, issuer, seen_at) VALUES (?, ?, ?) "
            "ON CONFLICT(jti, issuer) DO NOTHING",
            (jti, issuer, serialize_datetime(moment)),
        )
        return bool(cursor.rowcount)

    async def supersede_access_tokens_in_transaction(
        self, session_id: str, moment: datetime, keep_hash: str
    ) -> None:
        """Retire every access credential of this session but the one just minted.

        Rotation that mints without retiring is accumulation: a critic refreshed
        twice and held three live access tokens, the oldest still authenticating
        for the session's whole absolute lifetime. Rotating a refresh token while
        leaving the access token it replaced alive rotates nothing.
        """
        await self.db.execute(
            "UPDATE user_tokens SET revoked_at = ? "
            "WHERE session_id = ? AND token_hash <> ? AND revoked_at IS NULL",
            (serialize_datetime(moment), session_id, keep_hash),
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
        return (
            None
            if row is None
            else Organization(
                org_id=row["org_id"],
                name=row["name"],
                slug=row["slug"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
        )

    async def add_member(self, member: OrgMember) -> None:
        await self.db.execute(
            "INSERT INTO organization_members(org_id, user_id, role, created_at) "
            "VALUES (?, ?, ?, ?)",
            (member.org_id, member.user_id, member.role, serialize_datetime(member.created_at)),
        )
        await self.db.commit()

    async def get_member(self, org_id: str, user_id: str) -> OrgMember | None:
        row = await self.db.fetch_one(
            "SELECT * FROM organization_members WHERE org_id = ? AND user_id = ?",
            (org_id, user_id),
        )
        return (
            None
            if row is None
            else OrgMember(
                org_id=row["org_id"],
                user_id=row["user_id"],
                role=row["role"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
        )

    async def list_members(self, org_id: str) -> list[OrgMember]:
        rows = await self.db.fetch_all(
            "SELECT * FROM organization_members WHERE org_id = ?", (org_id,)
        )
        return [
            OrgMember(
                org_id=r["org_id"],
                user_id=r["user_id"],
                role=r["role"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    async def list_for_user(self, user_id: str) -> list[Organization]:
        """Return only organizations the user can access through durable membership."""
        rows = await self.db.fetch_all(
            "SELECT o.* FROM organizations o "
            "JOIN organization_members m ON m.org_id = o.org_id "
            "WHERE m.user_id = ? ORDER BY o.created_at",
            (user_id,),
        )
        return [
            Organization(
                org_id=row["org_id"],
                name=row["name"],
                slug=row["slug"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]


class WorkspaceRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, ws: Workspace) -> Workspace:
        await self.db.execute(
            "INSERT INTO workspaces(workspace_id, org_id, name, slug, created_at, "
            "allowed_capabilities) VALUES (?, ?, ?, ?, ?, ?)",
            (
                ws.workspace_id,
                ws.org_id,
                ws.name,
                ws.slug,
                serialize_datetime(ws.created_at),
                ws.allowed_capabilities,
            ),
        )
        await self.db.commit()
        return ws

    async def get(self, workspace_id: str) -> Workspace | None:
        row = await self.db.fetch_one(
            "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
        )
        return (
            None
            if row is None
            else Workspace(
                workspace_id=row["workspace_id"],
                org_id=row["org_id"],
                name=row["name"],
                slug=row["slug"],
                created_at=datetime.fromisoformat(row["created_at"]),
                allowed_capabilities=row["allowed_capabilities"],
            )
        )

    async def list_by_org(self, org_id: str) -> list[Workspace]:
        rows = await self.db.fetch_all(
            "SELECT * FROM workspaces WHERE org_id = ? ORDER BY created_at", (org_id,)
        )
        return [
            Workspace(
                workspace_id=r["workspace_id"],
                org_id=r["org_id"],
                name=r["name"],
                slug=r["slug"],
                created_at=datetime.fromisoformat(r["created_at"]),
                allowed_capabilities=r["allowed_capabilities"],
            )
            for r in rows
        ]

    async def set_allowed_capabilities(self, workspace_id: str, allowed: str | None) -> None:
        await self.db.execute(
            "UPDATE workspaces SET allowed_capabilities = ? WHERE workspace_id = ?",
            (allowed, workspace_id),
        )
        await self.db.commit()

    async def add_member(self, member: WorkspaceMember) -> None:
        await self.db.execute(
            "INSERT INTO workspace_members(workspace_id, user_id, role, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                member.workspace_id,
                member.user_id,
                member.role,
                serialize_datetime(member.created_at),
            ),
        )
        await self.db.commit()

    async def add_member_if_absent(self, member: WorkspaceMember) -> None:
        """Grant workspace membership without ever overwriting an existing row/role."""
        await self.db.execute(
            "INSERT OR IGNORE INTO workspace_members(workspace_id, user_id, role, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                member.workspace_id,
                member.user_id,
                member.role,
                serialize_datetime(member.created_at),
            ),
        )
        await self.db.commit()

    async def get_member(self, workspace_id: str, user_id: str) -> WorkspaceMember | None:
        row = await self.db.fetch_one(
            "SELECT * FROM workspace_members WHERE workspace_id = ? AND user_id = ?",
            (workspace_id, user_id),
        )
        return (
            None
            if row is None
            else WorkspaceMember(
                workspace_id=row["workspace_id"],
                user_id=row["user_id"],
                role=row["role"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
        )

    async def list_for_user(self, user_id: str) -> list[Workspace]:
        """Return only workspaces the user can access through durable membership."""
        rows = await self.db.fetch_all(
            "SELECT w.* FROM workspaces w "
            "JOIN workspace_members m ON m.workspace_id = w.workspace_id "
            "WHERE m.user_id = ? ORDER BY w.created_at",
            (user_id,),
        )
        return [
            Workspace(
                workspace_id=row["workspace_id"],
                org_id=row["org_id"],
                name=row["name"],
                slug=row["slug"],
                created_at=datetime.fromisoformat(row["created_at"]),
                allowed_capabilities=row["allowed_capabilities"],
            )
            for row in rows
        ]

    async def list_members(self, workspace_id: str) -> list[WorkspaceMember]:
        rows = await self.db.fetch_all(
            "SELECT * FROM workspace_members WHERE workspace_id = ? ORDER BY created_at",
            (workspace_id,),
        )
        return [
            WorkspaceMember(
                workspace_id=r["workspace_id"],
                user_id=r["user_id"],
                role=r["role"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    async def member_display_names(self, workspace_id: str) -> dict[str, str]:
        """Member user_id -> users.display_name, falling back to the user_id itself."""
        rows = await self.db.fetch_all(
            "SELECT wm.user_id AS user_id, u.display_name AS display_name "
            "FROM workspace_members wm LEFT JOIN users u ON u.user_id = wm.user_id "
            "WHERE wm.workspace_id = ?",
            (workspace_id,),
        )
        return {r["user_id"]: (r["display_name"] or r["user_id"]) for r in rows}


class BootstrapContextRepo:
    """Durable principal key for concurrency-safe first-time setup."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, user_id: str) -> BootstrapContext | None:
        row = await self.db.fetch_one(
            "SELECT * FROM user_bootstrap_contexts WHERE user_id = ?", (user_id,)
        )
        return (
            None
            if row is None
            else BootstrapContext(
                user_id=row["user_id"],
                org_id=row["org_id"],
                workspace_id=row["workspace_id"],
                room_id=row["room_id"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
        )

    async def create(self, context: BootstrapContext) -> BootstrapContext:
        await self.db.execute(
            "INSERT INTO user_bootstrap_contexts("
            "user_id, org_id, workspace_id, room_id, created_at"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                context.user_id,
                context.org_id,
                context.workspace_id,
                context.room_id,
                serialize_datetime(context.created_at),
            ),
        )
        await self.db.commit()
        return context


class ToolRequestRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, request: ToolRequest) -> ToolRequest:
        await self.db.execute(
            "INSERT INTO tool_requests(request_id, room_id, execution_id, agent_id, "
            "requested_by, authorized_by, tool, input_json, required_capability, effective_json, "
            "status, reason, approval_id, result_json, created_at, resolved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request.request_id,
                request.room_id,
                request.execution_id,
                request.agent_id,
                request.requested_by,
                request.authorized_by,
                request.tool,
                request.input_json,
                request.required_capability,
                request.effective_json,
                request.status,
                request.reason,
                request.approval_id,
                request.result_json,
                serialize_datetime(request.created_at),
                serialize_datetime(request.resolved_at) if request.resolved_at else None,
            ),
        )
        await self.db.commit()
        return request

    async def get(self, request_id: str) -> ToolRequest | None:
        row = await self.db.fetch_one(
            "SELECT * FROM tool_requests WHERE request_id = ?", (request_id,)
        )
        return None if row is None else self._from_row(row)

    async def get_by_approval(self, approval_id: str) -> ToolRequest | None:
        row = await self.db.fetch_one(
            "SELECT * FROM tool_requests WHERE approval_id = ?", (approval_id,)
        )
        return None if row is None else self._from_row(row)

    async def set_effective(self, request_id: str, effective_json: str) -> None:
        await self.db.execute(
            "UPDATE tool_requests SET effective_json = ? WHERE request_id = ?",
            (effective_json, request_id),
        )
        await self.db.commit()

    async def resolve(self, request_id: str, status: str, reason: str, result_json: str) -> None:
        async with self.db.transaction():
            await self.resolve_in_transaction(request_id, status, reason, result_json)

    async def resolve_in_transaction(
        self, request_id: str, status: str, reason: str, result_json: str
    ) -> None:
        """Move a request into a terminal state, for a caller that appends the
        event recording it in the same transaction — the two are one fact.
        """
        if not self.db.owns_current_transaction:
            raise RuntimeError("tool request resolve requires transaction ownership")
        await self.db.execute(
            "UPDATE tool_requests SET status = ?, reason = ?, result_json = ?, "
            "resolved_at = ? WHERE request_id = ?",
            (status, reason, result_json, serialize_datetime(utcnow()), request_id),
        )

    async def record_reviewer(self, request_id: str, reviewer_id: str) -> None:
        """Write down the human releasing this one call, so its bound can read them.

        Against the call, never against the run. What she may lend is not stored —
        that is read from her room membership again every time this call is decided.
        """
        if not reviewer_id:
            return
        await self.db.execute(
            "INSERT OR IGNORE INTO tool_request_reviewers(request_id, reviewer_id, reviewed_at) "
            "VALUES (?, ?, ?)",
            (request_id, reviewer_id, utcnow().isoformat()),
        )
        await self.db.commit()

    async def reviewers(self, request_id: str) -> frozenset[str]:
        """Every human who released this call, and nobody else's calls."""
        rows = await self.db.fetch_all(
            "SELECT reviewer_id FROM tool_request_reviewers WHERE request_id = ?",
            (request_id,),
        )
        return frozenset(str(row["reviewer_id"]) for row in rows)

    def _from_row(self, row: dict[str, Any]) -> ToolRequest:
        resolved = row["resolved_at"]
        return ToolRequest(
            request_id=row["request_id"],
            room_id=row["room_id"],
            execution_id=row["execution_id"],
            agent_id=row["agent_id"],
            requested_by=row["requested_by"],
            authorized_by=row.get("authorized_by") or "",
            tool=row["tool"],
            input_json=row["input_json"],
            required_capability=row["required_capability"],
            effective_json=row["effective_json"],
            status=row["status"],
            reason=row["reason"],
            approval_id=row["approval_id"],
            result_json=row["result_json"],
            created_at=datetime.fromisoformat(row["created_at"]),
            resolved_at=datetime.fromisoformat(resolved) if resolved else None,
        )


class RoomPostureRepo:
    """Append-only declarations of how much of a channel's work stops at a human."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def declare(self, room_id: str, posture: Posture, declared_by: str) -> str:
        """Say what pauses here from now on. A new row every time; never an edit."""
        declaration_id = new_id("posture")
        await self.db.execute(
            "INSERT INTO room_postures(declaration_id, room_id, posture, declared_by, declared_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (declaration_id, room_id, posture.value, declared_by, utcnow().isoformat()),
        )
        await self.db.commit()
        return declaration_id

    async def current(self, room_id: str) -> Posture:
        """The latest declaration at or before now. A channel that never spoke is GUARDED.

        Derived at every read rather than kept anywhere. A posture resolved once and
        spent later is the defect this repository has now lost fifteen rounds to, and
        a stored one would be that defect with a new name on it.
        """
        row = await self.db.fetch_one(
            "SELECT posture FROM room_postures WHERE room_id = ? "
            "ORDER BY declared_at DESC, rowid DESC LIMIT 1",
            (room_id,),
        )
        return Posture.GUARDED if row is None else Posture(str(row["posture"]))


class RoomRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, room: Room) -> Room:
        await self.db.execute(
            "INSERT INTO rooms(room_id, workspace_id, name, description, status, "
            "created_by, created_at, allowed_capabilities) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                room.room_id,
                room.workspace_id,
                room.name,
                room.description,
                room.status.value,
                room.created_by,
                serialize_datetime(room.created_at),
                room.allowed_capabilities,
            ),
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

    async def list_for_user(self, user_id: str) -> list[Room]:
        """Return only rooms the user can access through durable membership."""
        rows = await self.db.fetch_all(
            "SELECT r.* FROM rooms r "
            "JOIN room_members m ON m.room_id = r.room_id "
            "WHERE m.user_id = ? ORDER BY r.created_at",
            (user_id,),
        )
        return [self._from_row(row) for row in rows]

    async def update_status(self, room_id: str, status: RoomStatus) -> None:
        await self.db.execute(
            "UPDATE rooms SET status = ? WHERE room_id = ?", (status.value, room_id)
        )
        await self.db.commit()

    def _from_row(self, row: dict[str, Any]) -> Room:
        return Room(
            room_id=row["room_id"],
            workspace_id=row["workspace_id"],
            name=row["name"],
            description=row["description"],
            status=RoomStatus(row["status"]),
            created_by=row["created_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
            allowed_capabilities=row["allowed_capabilities"],
        )

    async def set_allowed_capabilities(self, room_id: str, allowed: str | None) -> None:
        await self.db.execute(
            "UPDATE rooms SET allowed_capabilities = ? WHERE room_id = ?",
            (allowed, room_id),
        )
        await self.db.commit()


class RoomMemberRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(self, member: RoomMember) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO room_members(room_id, user_id, role, joined_at, "
            "allowed_capabilities) VALUES (?, ?, ?, ?, ?)",
            (
                member.room_id,
                member.user_id,
                member.role,
                serialize_datetime(member.joined_at),
                member.allowed_capabilities,
            ),
        )
        await self.db.commit()

    async def get(self, room_id: str, user_id: str) -> RoomMember | None:
        row = await self.db.fetch_one(
            "SELECT * FROM room_members WHERE room_id = ? AND user_id = ?",
            (room_id, user_id),
        )
        return (
            None
            if row is None
            else RoomMember(
                room_id=row["room_id"],
                user_id=row["user_id"],
                role=row["role"],
                joined_at=datetime.fromisoformat(row["joined_at"]),
                allowed_capabilities=row["allowed_capabilities"],
            )
        )

    async def set_allowed_capabilities(
        self, room_id: str, user_id: str, allowed: str | None
    ) -> None:
        await self.db.execute(
            "UPDATE room_members SET allowed_capabilities = ? WHERE room_id = ? AND user_id = ?",
            (allowed, room_id, user_id),
        )
        await self.db.commit()

    async def update_role(self, room_id: str, user_id: str, role: str) -> None:
        await self.db.execute(
            "UPDATE room_members SET role = ? WHERE room_id = ? AND user_id = ?",
            (role, room_id, user_id),
        )
        await self.db.commit()

    async def remove(self, room_id: str, user_id: str) -> None:
        await self.db.execute(
            "DELETE FROM room_members WHERE room_id = ? AND user_id = ?", (room_id, user_id)
        )
        await self.db.commit()

    async def list(self, room_id: str) -> list[RoomMember]:
        rows = await self.db.fetch_all("SELECT * FROM room_members WHERE room_id = ?", (room_id,))
        return [
            RoomMember(
                room_id=r["room_id"],
                user_id=r["user_id"],
                role=r["role"],
                joined_at=datetime.fromisoformat(r["joined_at"]),
                allowed_capabilities=r["allowed_capabilities"],
            )
            for r in rows
        ]

    async def is_member(self, room_id: str, user_id: str) -> bool:
        row = await self.db.fetch_one(
            "SELECT 1 FROM room_members WHERE room_id = ? AND user_id = ?",
            (room_id, user_id),
        )
        return row is not None

    async def display_names(self, room_id: str) -> dict[str, str]:
        """Member user_id -> users.display_name, falling back to the user_id itself."""
        rows = await self.db.fetch_all(
            "SELECT rm.user_id AS user_id, u.display_name AS display_name "
            "FROM room_members rm LEFT JOIN users u ON u.user_id = rm.user_id "
            "WHERE rm.room_id = ?",
            (room_id,),
        )
        return {r["user_id"]: (r["display_name"] or r["user_id"]) for r in rows}


class AgentRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create_template(self, template: AgentTemplate) -> AgentTemplate:
        await self.db.execute(
            "INSERT INTO agent_templates(template_id, name, description, role, system_prompt, "
            "capabilities, preferred_tools, avatar_url, created_at, workspace_id, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                template.template_id,
                template.name,
                template.description,
                template.role,
                template.system_prompt,
                json.dumps(sorted(template.capabilities)),
                json.dumps(list(template.preferred_tools)),
                template.avatar_url,
                serialize_datetime(template.created_at),
                template.workspace_id,
                template.created_by,
            ),
        )
        await self.db.commit()
        return template

    async def get_template(self, template_id: str) -> AgentTemplate | None:
        row = await self.db.fetch_one(
            "SELECT * FROM agent_templates WHERE template_id = ?", (template_id,)
        )
        return None if row is None else self._template_from_row(row)

    async def list_templates(self) -> list[AgentTemplate]:
        """Built-ins only: workspace_id IS NULL. Kept for the global (unscoped) route."""
        rows = await self.db.fetch_all(
            "SELECT * FROM agent_templates WHERE workspace_id IS NULL ORDER BY created_at"
        )
        return [self._template_from_row(r) for r in rows]

    async def list_visible_to_workspace(self, workspace_id: str) -> list[AgentTemplate]:
        """Every built-in plus this workspace's own, live templates only."""
        rows = await self.db.fetch_all(
            "SELECT * FROM agent_templates WHERE deleted_at IS NULL "
            "AND (workspace_id IS NULL OR workspace_id = ?) ORDER BY created_at",
            (workspace_id,),
        )
        return [self._template_from_row(r) for r in rows]

    async def soft_delete_template(self, template_id: str, deleted_at: datetime) -> None:
        await self.db.execute(
            "UPDATE agent_templates SET deleted_at = ? WHERE template_id = ?",
            (serialize_datetime(deleted_at), template_id),
        )
        await self.db.commit()

    async def share_template(self, template_id: str, shared_at: datetime) -> None:
        await self.db.execute(
            "UPDATE agent_templates SET shared_at = ? WHERE template_id = ?",
            (serialize_datetime(shared_at), template_id),
        )
        await self.db.commit()

    async def unshare_template(self, template_id: str) -> None:
        await self.db.execute(
            "UPDATE agent_templates SET shared_at = NULL WHERE template_id = ?",
            (template_id,),
        )
        await self.db.commit()

    async def list_shared_for_org(
        self, org_id: str, exclude_workspace_id: str
    ) -> list[AgentTemplate]:
        """Live templates other workspaces in this organization opted to share.

        Joins to workspaces on org_id: idx_agent_templates_workspace (038)
        answers "this workspace's own rows," not "every workspace in this org."
        """
        rows = await self.db.fetch_all(
            "SELECT t.* FROM agent_templates t "
            "JOIN workspaces w ON w.workspace_id = t.workspace_id "
            "WHERE w.org_id = ? AND t.workspace_id != ? "
            "AND t.shared_at IS NOT NULL AND t.deleted_at IS NULL "
            "ORDER BY t.created_at",
            (org_id, exclude_workspace_id),
        )
        return [self._template_from_row(r) for r in rows]

    async def create_instance(self, agent: AgentInstance) -> AgentInstance:
        await self.db.execute(
            "INSERT INTO agent_instances(agent_id, template_id, room_id, name, role, status, "
            "system_prompt, capabilities, model_provider, model_name, harness_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                agent.agent_id,
                agent.template_id,
                agent.room_id,
                agent.name,
                agent.role,
                agent.status.value,
                agent.system_prompt,
                json.dumps(sorted(agent.capabilities)),
                agent.model_provider,
                agent.model_name,
                agent.harness_id,
                serialize_datetime(agent.created_at),
            ),
        )
        await self.db.commit()
        return agent

    async def get_instance(self, agent_id: str) -> AgentInstance | None:
        row = await self.db.fetch_one(
            "SELECT * FROM agent_instances WHERE agent_id = ?", (agent_id,)
        )
        return None if row is None else self._instance_from_row(row)

    async def list_instances_by_room(self, room_id: str) -> list[AgentInstance]:
        """The roster: the agents whose membership of this room is still live.

        A removed agent keeps its instance row, because its runs, outputs and events
        still name it. It leaves this list, because a roster is who is in the room —
        and reading agent_instances alone is what let a removed agent stay on it.
        """
        rows = await self.db.fetch_all(
            "SELECT a.* FROM agent_instances a "
            "JOIN agent_room_memberships m ON m.agent_id = a.agent_id AND m.room_id = a.room_id "
            "WHERE a.room_id = ? AND m.removed_at IS NULL ORDER BY a.created_at",
            (room_id,),
        )
        return [self._instance_from_row(r) for r in rows]

    async def has_room_membership(self, agent_id: str, room_id: str) -> bool:
        """The agent's own durable membership, joined to the room it was spawned in.

        Both halves are required. The membership row says the agent belongs to this
        room; agent_instances.room_id says the room still owns the agent, so a stale
        membership left behind by a move cannot carry an agent across the isolation
        boundary.
        """
        row = await self.db.fetch_one(
            "SELECT 1 AS present FROM agent_room_memberships m "
            "JOIN agent_instances a ON a.agent_id = m.agent_id AND a.room_id = m.room_id "
            "WHERE m.agent_id = ? AND m.room_id = ? AND m.removed_at IS NULL",
            (agent_id, room_id),
        )
        return row is not None

    async def update_status(self, agent_id: str, status: AgentStatus) -> None:
        await self.db.execute(
            "UPDATE agent_instances SET status = ? WHERE agent_id = ?",
            (status.value, agent_id),
        )
        await self.db.commit()

    async def add_room_membership(self, membership: AgentRoomMembership) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO agent_room_memberships("
            "membership_id, agent_id, room_id, joined_at, rejoined_from_membership_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                membership.membership_id,
                membership.agent_id,
                membership.room_id,
                serialize_datetime(membership.joined_at),
                membership.rejoined_from_membership_id,
            ),
        )
        await self.db.commit()

    async def latest_membership(self, agent_id: str, room_id: str) -> AgentRoomMembership | None:
        """This agent's most recent spell in this room, live or ended."""
        row = await self.db.fetch_one(
            "SELECT * FROM agent_room_memberships WHERE agent_id = ? AND room_id = ? "
            "ORDER BY joined_at DESC, rowid DESC LIMIT 1",
            (agent_id, room_id),
        )
        if row is None:
            return None
        return AgentRoomMembership(
            agent_id=row["agent_id"],
            room_id=row["room_id"],
            membership_id=row["membership_id"],
            joined_at=datetime.fromisoformat(row["joined_at"]),
            removed_at=deserialize_datetime(row["removed_at"]),
            rejoined_from_membership_id=row["rejoined_from_membership_id"],
        )

    async def rejoin_room_membership_in_transaction(self, membership: AgentRoomMembership) -> None:
        """Write the returning agent a new membership beside the departure it names.

        A plain INSERT, not OR IGNORE: the guards on this table decide whether a
        rejoin is legitimate, and a silence here would hide their answer, which is
        exactly how the old INSERT OR IGNORE managed to no-op against a removed row.
        """
        await self.db.execute(
            "INSERT INTO agent_room_memberships("
            "membership_id, agent_id, room_id, joined_at, rejoined_from_membership_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                membership.membership_id,
                membership.agent_id,
                membership.room_id,
                serialize_datetime(membership.joined_at),
                membership.rejoined_from_membership_id,
            ),
        )

    async def remove_room_membership_in_transaction(
        self, agent_id: str, room_id: str, removed_at: datetime
    ) -> bool:
        """Stamp the membership removed. Removing twice is not an error, but only the
        first removal reports that it removed anything."""
        cursor = await self.db.execute(
            "UPDATE agent_room_memberships SET removed_at = ? "
            "WHERE agent_id = ? AND room_id = ? AND removed_at IS NULL",
            (serialize_datetime(removed_at), agent_id, room_id),
        )
        return cursor.rowcount == 1

    def _template_from_row(self, row: dict[str, Any]) -> AgentTemplate:
        return AgentTemplate(
            template_id=row["template_id"],
            name=row["name"],
            description=row["description"],
            role=row["role"],
            system_prompt=row["system_prompt"],
            capabilities=frozenset(json.loads(row["capabilities"])),
            preferred_tools=tuple(json.loads(row["preferred_tools"])),
            avatar_url=row["avatar_url"],
            created_at=datetime.fromisoformat(row["created_at"]),
            workspace_id=row.get("workspace_id"),
            created_by=row.get("created_by"),
            deleted_at=(
                datetime.fromisoformat(row["deleted_at"]) if row.get("deleted_at") else None
            ),
            shared_at=(datetime.fromisoformat(row["shared_at"]) if row.get("shared_at") else None),
        )

    def _instance_from_row(self, row: dict[str, Any]) -> AgentInstance:
        return AgentInstance(
            agent_id=row["agent_id"],
            template_id=row["template_id"],
            room_id=row["room_id"],
            name=row["name"],
            role=row["role"],
            status=AgentStatus(row["status"]),
            system_prompt=row["system_prompt"],
            capabilities=frozenset(json.loads(row["capabilities"])),
            model_provider=row["model_provider"],
            model_name=row["model_name"],
            harness_id=row.get("harness_id") or "nexus",
            created_at=datetime.fromisoformat(row["created_at"]),
        )


class RoomTemplateRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, template: RoomTemplate) -> RoomTemplate:
        await self.db.execute(
            "INSERT INTO room_templates(template_id, workspace_id, name, description, "
            "agent_template_ids, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                template.template_id,
                template.workspace_id,
                template.name,
                template.description,
                json.dumps(list(template.agent_template_ids)),
                template.created_by,
                serialize_datetime(template.created_at),
            ),
        )
        await self.db.commit()
        return template

    async def get(self, template_id: str) -> RoomTemplate | None:
        row = await self.db.fetch_one(
            "SELECT * FROM room_templates WHERE template_id = ?", (template_id,)
        )
        return None if row is None else self._from_row(row)

    async def list_live_by_workspace(self, workspace_id: str) -> list[RoomTemplate]:
        rows = await self.db.fetch_all(
            "SELECT * FROM room_templates WHERE workspace_id = ? AND deleted_at IS NULL "
            "ORDER BY created_at",
            (workspace_id,),
        )
        return [self._from_row(r) for r in rows]

    async def soft_delete(self, template_id: str, deleted_at: datetime) -> None:
        await self.db.execute(
            "UPDATE room_templates SET deleted_at = ? WHERE template_id = ?",
            (serialize_datetime(deleted_at), template_id),
        )
        await self.db.commit()

    def _from_row(self, row: dict[str, Any]) -> RoomTemplate:
        return RoomTemplate(
            template_id=row["template_id"],
            workspace_id=row["workspace_id"],
            name=row["name"],
            description=row["description"],
            agent_template_ids=tuple(json.loads(row["agent_template_ids"])),
            created_by=row["created_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
            deleted_at=(
                datetime.fromisoformat(row["deleted_at"]) if row.get("deleted_at") else None
            ),
        )


class AgentIdentityRepo:
    """One immutable identity row per agent instance."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def create_in_transaction(self, identity: AgentIdentity) -> AgentIdentity:
        await self.db.execute(
            "INSERT INTO agent_identities(identity_id, created_at, revoked_at, proof_mode, "
            "public_key, key_fingerprint, agent_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                identity.identity_id,
                serialize_datetime(identity.created_at),
                serialize_datetime(identity.revoked_at),
                identity.proof_mode.value,
                identity.public_key,
                identity.key_fingerprint,
                identity.agent_id,
            ),
        )
        return identity

    async def get_for_agent(self, agent_id: str) -> AgentIdentity | None:
        row = await self.db.fetch_one(
            "SELECT * FROM agent_identities WHERE agent_id = ?", (agent_id,)
        )
        return None if row is None else self._from_row(row)

    async def revoke(self, agent_id: str, revoked_at: datetime) -> bool:
        """Revoking is idempotent, and the first revocation is the one that stands."""
        cursor = await self.db.execute(
            "UPDATE agent_identities SET revoked_at = ? WHERE agent_id = ? AND revoked_at IS NULL",
            (serialize_datetime(revoked_at), agent_id),
        )
        await self.db.commit()
        return cursor.rowcount == 1

    @staticmethod
    def _from_row(row: dict[str, Any]) -> AgentIdentity:
        revoked = row.get("revoked_at")
        return AgentIdentity(
            identity_id=row["identity_id"],
            agent_id=row["agent_id"],
            proof_mode=ProofMode(row["proof_mode"]),
            public_key=row.get("public_key"),
            key_fingerprint=row.get("key_fingerprint"),
            created_at=datetime.fromisoformat(row["created_at"]),
            revoked_at=datetime.fromisoformat(revoked) if revoked else None,
        )


class AgentAddressingRepo:
    """Who may point an agent, stored here rather than in harness configuration."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def upsert_in_transaction(self, addressing: AgentAddressing) -> None:
        await self.db.execute(
            "INSERT INTO agent_addressing(agent_id, room_id, mode, owner_user_id, updated_at, "
            "updated_by) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(agent_id) DO UPDATE SET "
            "mode = excluded.mode, owner_user_id = excluded.owner_user_id, "
            "updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (
                addressing.agent_id,
                addressing.room_id,
                addressing.mode.value,
                addressing.owner_user_id,
                serialize_datetime(addressing.updated_at),
                addressing.updated_by,
            ),
        )
        await self.db.execute(
            "DELETE FROM agent_address_allowlist WHERE agent_id = ?", (addressing.agent_id,)
        )
        for user_id in sorted(addressing.allowlist):
            await self.db.execute(
                "INSERT INTO agent_address_allowlist(agent_id, user_id, added_by, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    addressing.agent_id,
                    user_id,
                    addressing.updated_by,
                    serialize_datetime(addressing.updated_at),
                ),
            )

    async def get(self, agent_id: str) -> AgentAddressing | None:
        row = await self.db.fetch_one(
            "SELECT * FROM agent_addressing WHERE agent_id = ?", (agent_id,)
        )
        if row is None:
            return None
        allowed = await self.db.fetch_all(
            "SELECT user_id FROM agent_address_allowlist WHERE agent_id = ?", (agent_id,)
        )
        return AgentAddressing(
            agent_id=row["agent_id"],
            room_id=row["room_id"],
            mode=AddressingMode(row["mode"]),
            owner_user_id=row["owner_user_id"],
            allowlist=frozenset(str(item["user_id"]) for item in allowed),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            updated_by=row["updated_by"],
        )


class AgentRunRepo:
    """The identity-and-authority envelope around one executions row."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def create_in_transaction(self, run: AgentRun) -> AgentRun:
        """The launch guards live in the database, so a refusal here is an
        sqlite3.IntegrityError from a trigger rather than a service-level check."""
        await self.db.execute(
            "INSERT INTO agent_runs(run_id, execution_id, agent_id, identity_id, room_id, "
            "authorized_by, acting_user_id, harness_id, credential_hash, challenge_verified_at, "
            "harness_state, settlement, resumed_from_run_id, lease_expires_at, created_at, "
            "settled_at, attempts, max_attempts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run.run_id,
                run.execution_id,
                run.agent_id,
                run.identity_id,
                run.room_id,
                run.authorized_by,
                run.acting_user_id,
                run.harness_id,
                run.credential_hash,
                serialize_datetime(run.challenge_verified_at),
                run.harness_state.value,
                run.settlement.value if run.settlement else None,
                run.resumed_from_run_id,
                serialize_datetime(run.lease_expires_at),
                serialize_datetime(run.created_at),
                serialize_datetime(run.settled_at),
                run.attempts,
                run.max_attempts,
            ),
        )
        return run

    async def get(self, run_id: str) -> AgentRun | None:
        row = await self.db.fetch_one("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,))
        return None if row is None else self._from_row(row)

    async def get_by_execution(self, execution_id: str) -> AgentRun | None:
        row = await self.db.fetch_one(
            "SELECT * FROM agent_runs WHERE execution_id = ?", (execution_id,)
        )
        return None if row is None else self._from_row(row)

    async def list_open_by_agent_room(self, agent_id: str, room_id: str) -> list[AgentRun]:
        rows = await self.db.fetch_all(
            "SELECT * FROM agent_runs WHERE agent_id = ? AND room_id = ? AND harness_state <> ? "
            "ORDER BY created_at, run_id",
            (agent_id, room_id, HarnessState.SETTLED.value),
        )
        return [self._from_row(row) for row in rows]

    async def list_expired(self, now: datetime) -> list[AgentRun]:
        """Every non-settled run whose lease has run out. No state is exempt: an
        exemption is not a longer deadline but no deadline."""
        rows = await self.db.fetch_all(
            "SELECT * FROM agent_runs WHERE harness_state <> ? AND lease_expires_at <= ? "
            "ORDER BY lease_expires_at, run_id",
            (HarnessState.SETTLED.value, serialize_datetime(now)),
        )
        return [self._from_row(row) for row in rows]

    async def settle_in_transaction(
        self, execution_id: str, settlement: RunSettlement, decided_by: str
    ) -> list[RoomEvent]:
        """Settle the envelope around one execution and return its unsequenced events."""
        return await _settle_agent_run_in_transaction(self.db, execution_id, settlement, decided_by)

    async def advance(
        self,
        run_id: str,
        state: HarnessState,
        lease_expires_at: datetime,
        acting_user_id: str,
        expected: HarnessState | None = None,
    ) -> bool:
        """Move an open run and renew its lease. A settled run never moves.

        ``expected``, when given, makes the move a compare and swap: it only
        lands from that exact state, so a caller entering a turn from a stale
        read cannot advance a run somebody else already moved on.
        """
        if expected is not None:
            cursor = await self.db.execute(
                "UPDATE agent_runs SET harness_state = ?, lease_expires_at = ?, "
                "acting_user_id = ? WHERE run_id = ? AND harness_state = ?",
                (
                    state.value,
                    serialize_datetime(lease_expires_at),
                    acting_user_id,
                    run_id,
                    expected.value,
                ),
            )
        else:
            cursor = await self.db.execute(
                "UPDATE agent_runs SET harness_state = ?, lease_expires_at = ?, "
                "acting_user_id = ? WHERE run_id = ? AND harness_state <> ?",
                (
                    state.value,
                    serialize_datetime(lease_expires_at),
                    acting_user_id,
                    run_id,
                    HarnessState.SETTLED.value,
                ),
            )
        await self.db.commit()
        return cursor.rowcount == 1

    async def spend_attempt(self, run_id: str, expected: int) -> bool:
        """Charge one attempt to an open run, conditional on the count its caller read.

        Every prompt of a turn spends one, so the bound a continuation runs under is
        the same ``max_attempts`` the lease sweep parks a run on rather than a second
        limit beside it. A write that no longer matches touches zero rows, and the
        caller parks the run instead of prompting it again.
        """
        cursor = await self.db.execute(
            "UPDATE agent_runs SET attempts = attempts + 1 "
            "WHERE run_id = ? AND attempts = ? AND attempts < max_attempts "
            "AND harness_state <> ?",
            (run_id, expected, HarnessState.SETTLED.value),
        )
        await self.db.commit()
        return cursor.rowcount == 1

    @staticmethod
    def _from_row(row: dict[str, Any]) -> AgentRun:
        settlement = row.get("settlement")
        verified = row.get("challenge_verified_at")
        settled = row.get("settled_at")
        return AgentRun(
            run_id=row["run_id"],
            execution_id=row["execution_id"],
            agent_id=row["agent_id"],
            identity_id=row["identity_id"],
            room_id=row["room_id"],
            authorized_by=row["authorized_by"],
            acting_user_id=row["acting_user_id"],
            harness_id=row["harness_id"],
            credential_hash=row["credential_hash"],
            lease_expires_at=datetime.fromisoformat(row["lease_expires_at"]),
            harness_state=HarnessState(row["harness_state"]),
            settlement=RunSettlement(settlement) if settlement else None,
            resumed_from_run_id=row.get("resumed_from_run_id"),
            challenge_verified_at=datetime.fromisoformat(verified) if verified else None,
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            settled_at=datetime.fromisoformat(settled) if settled else None,
        )


class BranchRepo:
    """Durable branch context and lifecycle state."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, branch: Branch) -> Branch:
        await self.db.execute(
            "INSERT INTO branches(branch_id, room_id, mode, status, initiated_by, "
            "initiating_prompt, context_event_sequence, context_message_ids, context_snapshot, "
            "context_hash, lifecycle_managed, created_at, updated_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                branch.branch_id,
                branch.room_id,
                branch.mode.value,
                branch.status.value,
                branch.initiated_by,
                branch.initiating_prompt,
                branch.context_event_sequence,
                json.dumps(branch.context_message_ids),
                json.dumps(branch.context_snapshot, sort_keys=True, separators=(",", ":")),
                branch.context_hash,
                int(branch.lifecycle_managed),
                serialize_datetime(branch.created_at),
                serialize_datetime(branch.updated_at),
                serialize_datetime(branch.completed_at),
            ),
        )
        await self.db.commit()
        return branch

    async def get(self, branch_id: str) -> Branch | None:
        row = await self.db.fetch_one("SELECT * FROM branches WHERE branch_id = ?", (branch_id,))
        return None if row is None else self._from_row(row)

    async def list_by_room(self, room_id: str) -> list[Branch]:
        rows = await self.db.fetch_all(
            "SELECT * FROM branches WHERE room_id = ? ORDER BY created_at, branch_id",
            (room_id,),
        )
        return [self._from_row(row) for row in rows]

    async def get_or_create_legacy(self, room_id: str, initiated_by: str) -> Branch:
        row = await self.db.fetch_one(
            "SELECT * FROM branches WHERE room_id = ? AND lifecycle_managed = 0",
            (room_id,),
        )
        if row is not None:
            return self._from_row(row)
        seq_row = await self.db.fetch_one(
            "SELECT seq FROM room_sequences WHERE room_id = ?", (room_id,)
        )
        sequence = int(seq_row["seq"]) if seq_row else 0
        snapshot = {"boundary": "LEGACY_LOW_LEVEL_API", "messages": []}
        encoded = json.dumps(
            {
                "context_event_sequence": sequence,
                "context_message_ids": [],
                "context_snapshot": snapshot,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        branch = Branch(
            branch_id=new_id("branch"),
            room_id=room_id,
            mode=BranchMode.PARALLEL,
            status=BranchStatus.RUNNING,
            initiated_by=initiated_by,
            initiating_prompt="LEGACY_LOW_LEVEL_WORKFLOW",
            context_event_sequence=sequence,
            context_message_ids=(),
            context_snapshot=snapshot,
            context_hash=hashlib.sha256(encoded).hexdigest(),
            lifecycle_managed=False,
        )
        return await self.create(branch)

    async def update_status(self, branch_id: str, status: BranchStatus) -> None:
        completed_at = (
            serialize_datetime(utcnow())
            if status
            in {
                BranchStatus.COMPLETED,
                BranchStatus.PARTIAL,
                BranchStatus.FAILED,
                BranchStatus.CANCELLED,
            }
            else None
        )
        await self.db.execute(
            "UPDATE branches SET status = ?, updated_at = ?, completed_at = ? WHERE branch_id = ?",
            (status.value, serialize_datetime(utcnow()), completed_at, branch_id),
        )
        await self.db.commit()

    @staticmethod
    def _from_row(row: dict[str, Any]) -> Branch:
        try:
            message_ids = json.loads(row["context_message_ids"])
        except (json.JSONDecodeError, TypeError):
            message_ids = []
        try:
            snapshot = json.loads(row["context_snapshot"])
        except (json.JSONDecodeError, TypeError):
            snapshot = {}
        return Branch(
            branch_id=row["branch_id"],
            room_id=row["room_id"],
            mode=BranchMode(row["mode"]),
            status=BranchStatus(row["status"]),
            initiated_by=row["initiated_by"],
            initiating_prompt=row["initiating_prompt"],
            context_event_sequence=int(row["context_event_sequence"]),
            context_message_ids=tuple(str(item) for item in message_ids),
            context_snapshot=dict(snapshot) if isinstance(snapshot, dict) else {},
            context_hash=row["context_hash"],
            lifecycle_managed=bool(row["lifecycle_managed"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            completed_at=(
                datetime.fromisoformat(row["completed_at"]) if row.get("completed_at") else None
            ),
        )


class SessionRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, session: Session) -> Session:
        await self.db.execute(
            "INSERT INTO sessions(session_id, room_id, agent_id, task_id, status, "
            "started_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session.session_id,
                session.room_id,
                session.agent_id,
                session.task_id,
                session.status.value,
                serialize_datetime(session.started_at),
                serialize_datetime(session.ended_at),
            ),
        )
        await self.db.commit()
        return session

    async def get(self, session_id: str) -> Session | None:
        row = await self.db.fetch_one("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
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
            session_id=row["session_id"],
            room_id=row["room_id"],
            agent_id=row["agent_id"],
            task_id=row.get("task_id"),
            status=SessionStatus(row["status"]),
            started_at=datetime.fromisoformat(row["started_at"]),
            ended_at=datetime.fromisoformat(row["ended_at"]) if row.get("ended_at") else None,
        )


class ExecutionRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, execution: Execution) -> Execution:
        if not execution.branch_id:
            session = await SessionRepo(self.db).get(execution.session_id)
            if session is None:
                raise ValueError("execution session not found")
            branch = await BranchRepo(self.db).get_or_create_legacy(
                session.room_id, execution.agent_id
            )
            execution = replace(execution, branch_id=branch.branch_id)
        await self.db.execute(
            "INSERT INTO executions(execution_id, session_id, agent_id, authorized_by, "
            "agent_task_id, branch_id, run_id, triggered_by, status, input_data, "
            "output_data, error, started_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                execution.execution_id,
                execution.session_id,
                execution.agent_id,
                execution.authorized_by,
                execution.agent_task_id,
                execution.branch_id,
                execution.run_id,
                execution.triggered_by.value,
                execution.status.value,
                json.dumps(execution.input_data),
                json.dumps(execution.output_data),
                execution.error,
                serialize_datetime(execution.started_at),
                serialize_datetime(execution.completed_at),
            ),
        )
        await self.db.commit()
        return execution

    async def start_with_event(
        self,
        execution: Execution,
        event: RoomEvent,
        agent_run: AgentRun | None = None,
    ) -> RoomEvent:
        """Atomically activate a session, create its execution, and append the run event.

        The run envelope is written in the same transaction, so the fail-closed launch
        triggers refuse the whole start rather than leaving an execution with no run.
        """
        async with self.db.transaction():
            session = await SessionRepo(self.db).get(execution.session_id)
            if session is None:
                raise DomainError("execution session not found")
            active_lock = await TurnLockRepo(self.db).get_active(
                TurnLockScopeType.ROOM, session.room_id
            )
            if active_lock is not None and execution.branch_id != active_lock.branch_id:
                raise DomainError(f"room turn is locked by branch {active_lock.branch_id}")
            if not execution.branch_id:
                branch = await BranchRepo(self.db).get_or_create_legacy(
                    session.room_id, execution.agent_id
                )
                execution = replace(execution, branch_id=branch.branch_id)
            await self.db.execute(
                "UPDATE sessions SET status = ? WHERE session_id = ?",
                (SessionStatus.ACTIVE.value, execution.session_id),
            )
            await self.db.execute(
                "INSERT INTO executions(execution_id, session_id, agent_id, authorized_by, "
                "agent_task_id, branch_id, run_id, triggered_by, status, input_data, "
                "output_data, error, started_at, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    execution.execution_id,
                    execution.session_id,
                    execution.agent_id,
                    execution.authorized_by,
                    execution.agent_task_id,
                    execution.branch_id,
                    execution.run_id,
                    execution.triggered_by.value,
                    execution.status.value,
                    json.dumps(execution.input_data),
                    json.dumps(execution.output_data),
                    execution.error,
                    serialize_datetime(execution.started_at),
                    serialize_datetime(execution.completed_at),
                ),
            )
            if agent_run is not None:
                await AgentRunRepo(self.db).create_in_transaction(
                    replace(agent_run, execution_id=execution.execution_id)
                )
            persisted = await EventRepo(self.db).append_with_next_sequence_in_transaction(event)
        return persisted

    async def get(self, execution_id: str) -> Execution | None:
        row = await self.db.fetch_one(
            "SELECT * FROM executions WHERE execution_id = ?", (execution_id,)
        )
        return None if row is None else self._from_row(row)

    async def update_status(
        self,
        execution_id: str,
        status: ExecutionStatus,
        expected: ExecutionStatus,
        output_data: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        updates: dict[str, Any] = {"status": status.value}
        if output_data is not None:
            updates["output_data"] = json.dumps(output_data)
        if error:
            updates["error"] = error
        if status in (ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED):
            updates["completed_at"] = utcnow().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        cursor = await self.db.execute(
            f"UPDATE executions SET {set_clause} WHERE execution_id = ? AND status = ?",
            (*updates.values(), execution_id, expected.value),
        )
        _require_execution_transition(cursor, execution_id, expected, status)
        await self.db.commit()

    async def mark_running(self, execution_id: str, run_id: str, expected: ExecutionStatus) -> None:
        cursor = await self.db.execute(
            "UPDATE executions SET status = ?, run_id = ? WHERE execution_id = ? AND status = ?",
            (ExecutionStatus.RUNNING.value, run_id, execution_id, expected.value),
        )
        _require_execution_transition(cursor, execution_id, expected, ExecutionStatus.RUNNING)
        await self.db.commit()

    async def claim_for_dispatch(self, execution_id: str, claim: str) -> bool:
        """Take responsibility for dispatching one PENDING run, or report that
        somebody else already has. An unclaimed run is the only kind the startup
        sweep may settle, so this is what separates an orphan from live work."""
        cursor = await self.db.execute(
            "UPDATE executions SET dispatch_claim = ? "
            "WHERE execution_id = ? AND status = ? AND dispatch_claim IS NULL",
            (claim, execution_id, ExecutionStatus.PENDING.value),
        )
        await self.db.commit()
        return cursor.rowcount == 1

    async def terminalize_without_output(
        self,
        execution: Execution,
        status: ExecutionStatus,
        error: str,
        events: list[RoomEvent],
        settlement: RunSettlement | None = None,
        decided_by: str = "",
    ) -> list[RoomEvent]:
        async with self.db.transaction():
            return await self.terminalize_without_output_in_transaction(
                execution, status, error, events, settlement, decided_by
            )

    async def terminalize_without_output_in_transaction(
        self,
        execution: Execution,
        status: ExecutionStatus,
        error: str,
        events: list[RoomEvent],
        settlement: RunSettlement | None = None,
        decided_by: str = "",
    ) -> list[RoomEvent]:
        """Body of :meth:`terminalize_without_output` for a caller that already owns the
        write transaction, so a membership re-check can share that same transaction.

        The run envelope settles in the same transaction as the domain status, so a run
        is never terminal in one of the two records and open in the other.
        """
        if status not in {ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}:
            raise ValueError("terminal execution status must be FAILED or CANCELLED")
        if not self.db.owns_current_transaction:
            raise RuntimeError("terminalize_without_output_in_transaction requires ownership")
        if settlement is None:
            settlement = (
                RunSettlement.CANCELLED
                if status is ExecutionStatus.CANCELLED
                else RunSettlement.FAILED
            )
        persisted_events: list[RoomEvent] = []
        completed_at = serialize_datetime(utcnow())
        cursor = await self.db.execute(
            "UPDATE executions SET status = ?, error = ?, completed_at = ? "
            "WHERE execution_id = ? AND status = ?",
            (status.value, error, completed_at, execution.execution_id, execution.status.value),
        )
        _require_execution_transition(cursor, execution.execution_id, execution.status, status)
        await self.db.execute(
            "UPDATE sessions SET status = ?, ended_at = ? WHERE session_id = ?",
            (SessionStatus.FAILED.value, completed_at, execution.session_id),
        )
        session = await SessionRepo(self.db).get(execution.session_id)
        if session is None:
            raise ValueError("execution session not found")
        settle_events = await _settle_agent_run_in_transaction(
            self.db, execution.execution_id, settlement, decided_by or execution.agent_id
        )
        branch_events = await _finish_managed_branch_if_terminal(
            self.db, execution.branch_id, session.room_id, execution.agent_id
        )
        for event in [*events, *settle_events, *branch_events]:
            persisted_events.append(
                await EventRepo(self.db).append_with_next_sequence_in_transaction(event)
            )
        return persisted_events

    async def list_by_session(self, session_id: str) -> list[Execution]:
        rows = await self.db.fetch_all(
            "SELECT * FROM executions WHERE session_id = ? ORDER BY started_at", (session_id,)
        )
        return [self._from_row(r) for r in rows]

    async def list_by_room(self, room_id: str) -> list[Execution]:
        rows = await self.db.fetch_all(
            "SELECT e.* FROM executions e "
            "JOIN sessions s ON s.session_id = e.session_id "
            "WHERE s.room_id = ? ORDER BY e.started_at",
            (room_id,),
        )
        return [self._from_row(r) for r in rows]

    async def list_unclaimed_pending_by_trigger(self, trigger: AgentTrigger) -> list[Execution]:
        """Runs waiting to start that no dispatcher ever claimed, for the startup
        sweep. A claimed run belongs to a dispatcher that is running it; the sweep
        cannot tell that dispatcher's health from here, so it leaves it alone."""
        rows = await self.db.fetch_all(
            "SELECT * FROM executions WHERE status = ? AND triggered_by = ? "
            "AND dispatch_claim IS NULL ORDER BY started_at",
            (ExecutionStatus.PENDING.value, trigger.value),
        )
        return [self._from_row(r) for r in rows]

    async def latest_open_for_agent(self, agent_id: str) -> Execution | None:
        """The newest unfinished run this agent is serving, from the records alone.

        The bridge's live map is in-memory, so it is empty after a restart and for
        a run another process dispatched. A steer addressed to the agent still
        reaches that run, so the run that bounds the steer is read from here.
        """
        row = await self.db.fetch_one(
            "SELECT * FROM executions WHERE agent_id = ? AND status IN (?, ?, ?) "
            "ORDER BY started_at DESC, execution_id DESC LIMIT 1",
            (
                agent_id,
                ExecutionStatus.PENDING.value,
                ExecutionStatus.RUNNING.value,
                ExecutionStatus.PAUSED.value,
            ),
        )
        return None if row is None else self._from_row(row)

    async def list_by_branch(self, branch_id: str) -> list[Execution]:
        rows = await self.db.fetch_all(
            "SELECT * FROM executions WHERE branch_id = ? ORDER BY started_at, execution_id",
            (branch_id,),
        )
        return [self._from_row(row) for row in rows]

    async def record_caller(self, execution_id: str, caller_id: str) -> None:
        """Write down a human who is acting on this run, so its bound can read them.

        Idempotent and append-only: the row says that this person directed this run,
        which stops being true never. What they may lend is not stored — that is read
        from their room membership every time it is spent.
        """
        if not caller_id:
            return
        await self.db.execute(
            "INSERT OR IGNORE INTO execution_callers(execution_id, caller_id, first_acted_at) "
            "VALUES (?, ?, ?)",
            (execution_id, caller_id, utcnow().isoformat()),
        )
        await self.db.commit()

    async def bounding_principals(self, execution_id: str) -> frozenset[str]:
        """Every principal whose grant bounds this run, as one set, in one read.

        Four kinds of participant leave a durable row against a run, and this is the
        only place they are enumerated: the human who authorized it, every human who
        has acted on it, every human who has steered it, and every agent in the
        delegation chain the run is answering. A fifth kind is a fifth arm of this
        union and nothing else — no spend-point learns a new name, because no
        spend-point ever knew any of these.

        The fourth arm was declared before it was written. A delegating agent was put
        into the bounding set at the door that opens a task and nowhere near the till
        that spends it, so a delegate ran on the whole of its own capability set:
        ceilinged at the gate, unbounded at every tool call after it.

        It then joined through ``agent_tasks.execution_id``, and that is a one-slot
        pointer to the newest turn: a task that stopped for input and started again
        overwrote it, and the earlier run — still PENDING, still dispatchable —
        stopped being bounded by its own chain. The join is ``executions.agent_task_id``
        instead, which is the direction the relationship actually runs, so every turn
        a task ever opened keeps its link.

        There is no separate arm for ``agent_tasks.delegating_agent_id``. It would be
        an alias of a row this one already returns: ``_delegating_task`` derives the
        parent from the delegator's own open run, so the parent's ``target_agent_id``
        *is* the delegating agent, and the chain the child is written with has it at
        the last position. A second arm returning the same principal would be an
        untested duplicate of a load-bearing one, which is how an arm gets deleted by
        somebody who checks only that the tests still pass.
        """
        # Read here rather than at module scope because this is the only query in
        # this module that writes a principal rather than reading one back, and
        # spelling the prefix into the SQL would put a second definition of it
        # somewhere nothing would ever come back to update.
        from ..security.capabilities import AGENT_PRINCIPAL_PREFIX

        rows = await self.db.fetch_all(
            "SELECT authorized_by AS principal FROM executions WHERE execution_id = ?"
            " UNION "
            "SELECT caller_id FROM execution_callers WHERE execution_id = ?"
            " UNION "
            "SELECT intervened_by FROM execution_interventions WHERE execution_id = ?"
            " UNION "
            "SELECT ? || chain.agent_id FROM agent_task_chain chain "
            "JOIN executions ex ON ex.agent_task_id = chain.task_id "
            "WHERE ex.execution_id = ?",
            (execution_id, execution_id, execution_id, AGENT_PRINCIPAL_PREFIX, execution_id),
        )
        return frozenset(str(row["principal"]) for row in rows)

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
            execution_id=row["execution_id"],
            session_id=row["session_id"],
            agent_id=row["agent_id"],
            authorized_by=row.get("authorized_by") or "",
            agent_task_id=row.get("agent_task_id"),
            branch_id=row.get("branch_id") or "",
            run_id=row.get("run_id"),
            triggered_by=AgentTrigger(row["triggered_by"]),
            status=ExecutionStatus(row["status"]),
            input_data=input_data,
            output_data=output_data,
            error=row["error"],
            started_at=datetime.fromisoformat(row["started_at"]),
            completed_at=datetime.fromisoformat(row["completed_at"])
            if row.get("completed_at")
            else None,
        )


class ExecutionInterventionRepo:
    """Human steers, each stored with the identity of whoever produced it."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, intervention: ExecutionIntervention) -> ExecutionIntervention:
        await self.db.execute(
            "INSERT INTO execution_interventions(intervention_id, execution_id, intervened_by, "
            "instruction, created_at, consumed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                intervention.intervention_id,
                intervention.execution_id,
                intervention.intervened_by,
                intervention.instruction,
                serialize_datetime(intervention.created_at),
                serialize_datetime(intervention.consumed_at),
            ),
        )
        await self.db.commit()
        return intervention

    async def list_unconsumed(self, execution_id: str) -> list[ExecutionIntervention]:
        """Every steer this run is still carrying, oldest first."""
        rows = await self.db.fetch_all(
            "SELECT * FROM execution_interventions WHERE execution_id = ? AND consumed_at IS NULL "
            "ORDER BY created_at, intervention_id",
            (execution_id,),
        )
        return [self._from_row(row) for row in rows]

    async def mark_consumed(self, intervention_ids: list[str]) -> None:
        """Retire the steers a step has just taken into its prompt."""
        if not intervention_ids:
            return
        placeholders = ", ".join("?" for _ in intervention_ids)
        await self.db.execute(
            "UPDATE execution_interventions SET consumed_at = ? WHERE consumed_at IS NULL "
            f"AND intervention_id IN ({placeholders})",
            (utcnow().isoformat(), *intervention_ids),
        )
        await self.db.commit()

    def _from_row(self, row: dict[str, Any]) -> ExecutionIntervention:
        return ExecutionIntervention(
            intervention_id=row["intervention_id"],
            execution_id=row["execution_id"],
            intervened_by=row["intervened_by"],
            instruction=row["instruction"],
            consumed_at=datetime.fromisoformat(row["consumed_at"])
            if row.get("consumed_at")
            else None,
            created_at=datetime.fromisoformat(row["created_at"]),
        )


class SuspendedTurnRepo:
    """The rest of a turn that stopped at a reviewer, held where any process finds it.

    Records only, never authority: the prompt and what this turn's tools already
    returned. The step that resumes re-derives every capability from durable rows, as
    every other step does — including who has steered the turn, which is read from
    ``execution_interventions`` rather than copied here.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    async def save(
        self,
        execution_id: str,
        prompt: str,
        acting_as: str,
        observations: Sequence[str],
    ) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO suspended_turns("
            "execution_id, prompt, acting_as, observations, suspended_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                execution_id,
                prompt,
                acting_as,
                json.dumps(list(observations)),
                utcnow().isoformat(),
            ),
        )
        await self.db.commit()

    async def claim(self, execution_id: str) -> dict[str, Any] | None:
        """Take the continuation, so exactly one caller carries the turn on.

        The delete is what claims it. A second process reaching here after the first
        deletes nothing and is told there is no turn to resume, rather than both
        prompting the same run.
        """
        row = await self.db.fetch_one(
            "SELECT * FROM suspended_turns WHERE execution_id = ?", (execution_id,)
        )
        if row is None:
            return None
        cursor = await self.db.execute(
            "DELETE FROM suspended_turns WHERE execution_id = ?", (execution_id,)
        )
        await self.db.commit()
        if cursor.rowcount != 1:
            return None
        return {
            "prompt": row["prompt"],
            "acting_as": row["acting_as"],
            "observations": [str(item) for item in json.loads(row["observations"])],
        }

    async def discard(self, execution_id: str) -> None:
        """Nothing will prompt this run again, so nothing is waiting to resume it."""
        await self.db.execute("DELETE FROM suspended_turns WHERE execution_id = ?", (execution_id,))
        await self.db.commit()


class AgentOutputRepo:
    """Durable access for immutable outputs and atomic terminal run commits."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, output_id: str) -> AgentOutput | None:
        row = await self.db.fetch_one(
            "SELECT o.*, e.branch_id AS branch_id FROM agent_outputs o "
            "LEFT JOIN executions e ON e.execution_id = o.execution_id WHERE o.output_id = ?",
            (output_id,),
        )
        return None if row is None else self._from_row(row)

    async def get_by_execution(self, execution_id: str) -> AgentOutput | None:
        row = await self.db.fetch_one(
            "SELECT o.*, e.branch_id AS branch_id FROM agent_outputs o "
            "LEFT JOIN executions e ON e.execution_id = o.execution_id WHERE o.execution_id = ?",
            (execution_id,),
        )
        return None if row is None else self._from_row(row)

    async def list_by_room(self, room_id: str) -> list[AgentOutput]:
        rows = await self.db.fetch_all(
            "SELECT o.*, e.branch_id AS branch_id FROM agent_outputs o "
            "LEFT JOIN executions e ON e.execution_id = o.execution_id "
            "WHERE o.room_id = ? ORDER BY o.created_at",
            (room_id,),
        )
        return [self._from_row(row) for row in rows]

    async def list_by_branch(self, branch_id: str) -> list[AgentOutput]:
        rows = await self.db.fetch_all(
            "SELECT o.*, e.branch_id AS branch_id FROM agent_outputs o "
            "JOIN executions e ON e.execution_id = o.execution_id "
            "WHERE e.branch_id = ? ORDER BY o.created_at, o.output_id",
            (branch_id,),
        )
        return [self._from_row(row) for row in rows]

    async def complete_execution(
        self,
        output: AgentOutput,
        events: list[RoomEvent],
        expected: ExecutionStatus,
        message: Message | None = None,
        message_event: RoomEvent | None = None,
    ) -> list[RoomEvent]:
        """Persist output, terminal state, and canonical events in one transaction.

        When the turn was addressed in the conversation, the agent's message lands
        in the same transaction as the output it points at: either the room gets
        both or it gets neither, so a reader never sees an answer with no record or
        a record the conversation never mentioned.
        """
        persisted_events: list[RoomEvent] = []
        async with self.db.transaction():
            # Re-read the run inside the transaction that writes: a run settled while
            # this turn was in flight gets no output, no terminal status, and keeps the
            # settlement it already has.
            await _require_open_agent_run(self.db, output.execution_id)
            await self.db.execute(
                "INSERT INTO agent_outputs(output_id, room_id, session_id, execution_id, "
                "agent_id, content, output_data, source_prompt, provider_input, provider_name, "
                "provider_model, provider_response_id, provider_interventions, "
                "provider_evidence, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    output.output_id,
                    output.room_id,
                    output.session_id,
                    output.execution_id,
                    output.agent_id,
                    output.content,
                    json.dumps(output.output_data, sort_keys=True, default=str),
                    output.source_prompt,
                    output.provider_input,
                    output.provider_name,
                    output.provider_model,
                    output.provider_response_id,
                    json.dumps(output.provider_interventions),
                    output.provider_evidence,
                    serialize_datetime(output.created_at),
                ),
            )
            # The answer the agent produced, and nothing else about the turn.
            # provider_input, provider_evidence, source_prompt and output_data are
            # deliberately excluded: the provider request is assembled context —
            # system prompt, retrieved memories, other people's messages — so a
            # snippet of it would present text borrowed from elsewhere as if this
            # output had said it, and some of that context is drawn from scopes
            # wider than the room the hit is authorized against.
            await SearchRepo(self.db).index(
                SearchObjectKind.AGENT_OUTPUT,
                output.output_id,
                output.room_id,
                output.agent_id,
                output.content,
                output.created_at,
            )
            completed_at = serialize_datetime(utcnow())
            cursor = await self.db.execute(
                "UPDATE executions SET status = ?, output_data = ?, completed_at = ? "
                "WHERE execution_id = ? AND status = ?",
                (
                    ExecutionStatus.COMPLETED.value,
                    json.dumps(output.output_data, sort_keys=True, default=str),
                    completed_at,
                    output.execution_id,
                    expected.value,
                ),
            )
            # A run somebody else already settled must not complete, and must not
            # speak: raising here rolls back the output, the events and the message.
            _require_execution_transition(
                cursor, output.execution_id, expected, ExecutionStatus.COMPLETED
            )
            await self.db.execute(
                "UPDATE sessions SET status = ?, ended_at = ? WHERE session_id = ?",
                (SessionStatus.COMPLETED.value, completed_at, output.session_id),
            )
            settle_events = await _settle_agent_run_in_transaction(
                self.db, output.execution_id, RunSettlement.END_TURN, output.agent_id
            )
            branch_events = await _finish_managed_branch_if_terminal(
                self.db, output.branch_id, output.room_id, output.agent_id
            )
            for event in [*events, *settle_events, *branch_events]:
                persisted = await EventRepo(self.db).append_with_next_sequence_in_transaction(event)
                persisted_events.append(persisted)
            if message is not None and message_event is not None:
                # Last, so the log reads: output recorded, run completed, agent spoke.
                persisted_events.append(
                    await MessageRepo(self.db).create_with_event_and_turn_guard_in_transaction(
                        message, message_event
                    )
                )
        return persisted_events

    @staticmethod
    def _from_row(row: dict[str, Any]) -> AgentOutput:
        try:
            output_data = json.loads(row["output_data"])
        except (json.JSONDecodeError, TypeError):
            output_data = {}
        try:
            raw_interventions = json.loads(row.get("provider_interventions", "[]"))
            provider_interventions = (
                tuple(str(item) for item in raw_interventions)
                if isinstance(raw_interventions, list)
                else ()
            )
        except (json.JSONDecodeError, TypeError):
            provider_interventions = ()
        return AgentOutput(
            output_id=row["output_id"],
            room_id=row["room_id"],
            session_id=row["session_id"],
            execution_id=row["execution_id"],
            agent_id=row["agent_id"],
            content=row["content"],
            branch_id=row.get("branch_id") or "",
            output_data=output_data,
            source_prompt=row["source_prompt"],
            provider_input=row.get("provider_input", ""),
            provider_name=row.get("provider_name", ""),
            provider_model=row.get("provider_model", ""),
            provider_response_id=row.get("provider_response_id", ""),
            provider_interventions=provider_interventions,
            provider_evidence=row.get("provider_evidence", ""),
            created_at=datetime.fromisoformat(row["created_at"]),
        )


class OutputSelectionRepo:
    """Shared output review decisions, committed with their canonical event."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def upsert_with_event(self, selection: OutputSelection, event: RoomEvent) -> RoomEvent:
        async with self.db.transaction():
            return await self.upsert_with_event_in_transaction(selection, event)

    async def upsert_with_event_in_transaction(
        self, selection: OutputSelection, event: RoomEvent
    ) -> RoomEvent:
        """The body of upsert_with_event for a caller that owns the transaction."""
        await self.db.execute(
            "INSERT INTO output_selections(room_id, output_id, disposition, decided_by, "
            "updated_at, branch_id) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(room_id, output_id) DO UPDATE SET "
            "disposition = excluded.disposition, decided_by = excluded.decided_by, "
            "updated_at = excluded.updated_at",
            (
                selection.room_id,
                selection.output_id,
                selection.disposition.value,
                selection.decided_by,
                serialize_datetime(selection.updated_at),
                selection.branch_id,
            ),
        )
        return await EventRepo(self.db).append_with_next_sequence_in_transaction(event)

    async def list_by_room(self, room_id: str) -> list[OutputSelection]:
        rows = await self.db.fetch_all(
            "SELECT * FROM output_selections WHERE room_id = ? ORDER BY updated_at, output_id",
            (room_id,),
        )
        return [
            OutputSelection(
                room_id=row["room_id"],
                output_id=row["output_id"],
                disposition=OutputDisposition(row["disposition"]),
                decided_by=row["decided_by"],
                branch_id=row.get("branch_id") or "",
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    async def list_by_branch(self, branch_id: str) -> list[OutputSelection]:
        rows = await self.db.fetch_all(
            "SELECT * FROM output_selections WHERE branch_id = ? ORDER BY updated_at, output_id",
            (branch_id,),
        )
        return [
            OutputSelection(
                room_id=row["room_id"],
                output_id=row["output_id"],
                disposition=OutputDisposition(row["disposition"]),
                decided_by=row["decided_by"],
                branch_id=row["branch_id"],
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]


class BranchSynthesisRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create_with_inputs(
        self,
        synthesis: BranchSynthesis,
        inputs: list[BranchSynthesisInput],
    ) -> BranchSynthesis:
        await self.db.execute(
            "INSERT INTO branch_syntheses(synthesis_id, branch_id, room_id, synthesis_type, "
            "status, title, initiated_by, provider_input, provider_name, provider_model, "
            "provider_response_id, provider_evidence, simulated, content, error, "
            "artifact_version_id, created_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                synthesis.synthesis_id,
                synthesis.branch_id,
                synthesis.room_id,
                synthesis.synthesis_type,
                synthesis.status.value,
                synthesis.title,
                synthesis.initiated_by,
                synthesis.provider_input,
                synthesis.provider_name,
                synthesis.provider_model,
                synthesis.provider_response_id,
                synthesis.provider_evidence,
                int(synthesis.simulated),
                synthesis.content,
                synthesis.error,
                synthesis.artifact_version_id,
                serialize_datetime(synthesis.created_at),
                serialize_datetime(synthesis.completed_at),
            ),
        )
        for item in inputs:
            await self.db.execute(
                "INSERT INTO branch_synthesis_inputs(synthesis_id, output_id, ordinal) "
                "VALUES (?, ?, ?)",
                (item.synthesis_id, item.output_id, item.ordinal),
            )
        await self.db.commit()
        return synthesis

    async def get(self, synthesis_id: str) -> BranchSynthesis | None:
        row = await self.db.fetch_one(
            "SELECT * FROM branch_syntheses WHERE synthesis_id = ?", (synthesis_id,)
        )
        return None if row is None else self._from_row(row)

    async def list_by_branch(self, branch_id: str) -> list[BranchSynthesis]:
        rows = await self.db.fetch_all(
            "SELECT * FROM branch_syntheses WHERE branch_id = ? ORDER BY created_at, synthesis_id",
            (branch_id,),
        )
        return [self._from_row(row) for row in rows]

    async def list_inputs(self, synthesis_id: str) -> list[BranchSynthesisInput]:
        rows = await self.db.fetch_all(
            "SELECT * FROM branch_synthesis_inputs WHERE synthesis_id = ? ORDER BY ordinal",
            (synthesis_id,),
        )
        return [
            BranchSynthesisInput(
                synthesis_id=row["synthesis_id"],
                output_id=row["output_id"],
                ordinal=int(row["ordinal"]),
            )
            for row in rows
        ]

    async def mark_running(self, synthesis_id: str, provider_input: str) -> None:
        await self.db.execute(
            "UPDATE branch_syntheses SET status = ?, provider_input = ? WHERE synthesis_id = ?",
            (BranchSynthesisStatus.RUNNING.value, provider_input, synthesis_id),
        )
        await self.db.commit()

    async def mark_failed(self, synthesis_id: str, error: str) -> None:
        await self.db.execute(
            "UPDATE branch_syntheses SET status = ?, error = ?, completed_at = ? "
            "WHERE synthesis_id = ?",
            (
                BranchSynthesisStatus.FAILED.value,
                error,
                serialize_datetime(utcnow()),
                synthesis_id,
            ),
        )
        await self.db.commit()

    @staticmethod
    def _from_row(row: dict[str, Any]) -> BranchSynthesis:
        return BranchSynthesis(
            synthesis_id=row["synthesis_id"],
            branch_id=row["branch_id"],
            room_id=row["room_id"],
            synthesis_type=row["synthesis_type"],
            status=BranchSynthesisStatus(row["status"]),
            title=row["title"],
            initiated_by=row["initiated_by"],
            provider_input=row["provider_input"],
            provider_name=row["provider_name"],
            provider_model=row["provider_model"],
            provider_response_id=row["provider_response_id"],
            provider_evidence=row["provider_evidence"],
            simulated=bool(row["simulated"]),
            content=row["content"],
            error=row["error"],
            artifact_version_id=row.get("artifact_version_id"),
            created_at=datetime.fromisoformat(row["created_at"]),
            completed_at=(
                datetime.fromisoformat(row["completed_at"]) if row.get("completed_at") else None
            ),
        )


class TurnLockRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, lock: TurnLock) -> TurnLock:
        await self.db.execute(
            "INSERT INTO turn_locks(lock_id, scope_type, scope_id, branch_id, status, "
            "acquired_by, acquired_at, released_at, release_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                lock.lock_id,
                lock.scope_type.value,
                lock.scope_id,
                lock.branch_id,
                lock.status.value,
                lock.acquired_by,
                serialize_datetime(lock.acquired_at),
                serialize_datetime(lock.released_at),
                lock.release_reason,
            ),
        )
        await self.db.commit()
        return lock

    async def get_active(self, scope_type: TurnLockScopeType, scope_id: str) -> TurnLock | None:
        row = await self.db.fetch_one(
            "SELECT * FROM turn_locks WHERE scope_type = ? AND scope_id = ? AND status = 'ACTIVE'",
            (scope_type.value, scope_id),
        )
        return None if row is None else self._from_row(row)

    async def get_by_branch(self, branch_id: str) -> TurnLock | None:
        row = await self.db.fetch_one(
            "SELECT * FROM turn_locks WHERE branch_id = ? ORDER BY acquired_at DESC LIMIT 1",
            (branch_id,),
        )
        return None if row is None else self._from_row(row)

    async def release(self, branch_id: str, reason: str) -> None:
        await self.db.execute(
            "UPDATE turn_locks SET status = ?, released_at = ?, release_reason = ? "
            "WHERE branch_id = ? AND status = ?",
            (
                TurnLockStatus.RELEASED.value,
                serialize_datetime(utcnow()),
                reason,
                branch_id,
                TurnLockStatus.ACTIVE.value,
            ),
        )
        await self.db.commit()

    @staticmethod
    def _from_row(row: dict[str, Any]) -> TurnLock:
        return TurnLock(
            lock_id=row["lock_id"],
            scope_type=TurnLockScopeType(row["scope_type"]),
            scope_id=row["scope_id"],
            branch_id=row["branch_id"],
            status=TurnLockStatus(row["status"]),
            acquired_by=row["acquired_by"],
            acquired_at=datetime.fromisoformat(row["acquired_at"]),
            released_at=(
                datetime.fromisoformat(row["released_at"]) if row.get("released_at") else None
            ),
            release_reason=row["release_reason"],
        )


class TaskRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, task: Task) -> Task:
        await self.db.execute(
            "INSERT INTO tasks(task_id, room_id, title, description, status, priority, "
            "assigned_agent_id, created_by, parent_task_id, delegation_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task.task_id,
                task.room_id,
                task.title,
                task.description,
                task.status.value,
                task.priority.value,
                task.assigned_agent_id,
                task.created_by,
                task.parent_task_id,
                task.delegation_id,
                serialize_datetime(task.created_at),
                serialize_datetime(task.updated_at),
            ),
        )
        await self._index(task)
        await self.db.commit()
        return task

    async def _index(self, task: Task) -> None:
        """Index the title only.

        description is deliberately excluded: no read path returns it. Both the
        task list and the room state carry title, status, priority and assignee and
        nothing else, so indexing the description would make text findable that a
        member of the room has no endpoint to read.
        """
        await SearchRepo(self.db).index(
            SearchObjectKind.TASK,
            task.task_id,
            task.room_id,
            task.created_by,
            task.title,
            task.created_at,
        )

    async def get(self, task_id: str) -> Task | None:
        row = await self.db.fetch_one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
        return None if row is None else self._from_row(row)

    async def update(self, task: Task) -> Task:
        await self.db.execute(
            "UPDATE tasks SET title = ?, description = ?, status = ?, priority = ?, "
            "assigned_agent_id = ?, updated_at = ? WHERE task_id = ?",
            (
                task.title,
                task.description,
                task.status.value,
                task.priority.value,
                task.assigned_agent_id,
                serialize_datetime(utcnow()),
                task.task_id,
            ),
        )
        await self._index(task)
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
            "INSERT INTO task_dependencies(task_id, depends_on_task_id, created_at) "
            "VALUES (?, ?, ?)",
            (dep.task_id, dep.depends_on_task_id, serialize_datetime(dep.created_at)),
        )
        await self.db.commit()

    def _from_row(self, row: dict[str, Any]) -> Task:
        return Task(
            task_id=row["task_id"],
            room_id=row["room_id"],
            title=row["title"],
            description=row["description"],
            status=TaskStatus(row["status"]),
            priority=TaskPriority(row["priority"]),
            assigned_agent_id=row.get("assigned_agent_id"),
            created_by=row["created_by"],
            parent_task_id=row.get("parent_task_id"),
            delegation_id=row.get("delegation_id"),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


class AgentTaskRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create_in_transaction(self, task: AgentTask, ancestry: tuple[str, ...]) -> AgentTask:
        """Write the task and the chain it descends from as one unit.

        A task with no chain rows reads as a task a human opened, which is
        precisely the claim a delegated task must never be able to make about
        itself. So the two writes are not allowed to come apart.

        Depth is written as the length of that chain rather than as whatever the
        caller believed it to be. Migration 035 stores the ancestry one row per
        ancestor so that depth is a count; a caller-supplied number beside it
        would be a second, quieter answer to the same question, and the two
        would disagree the first time a service computed one of them wrong.

        An agent appearing twice is refused here rather than left to the UNIQUE
        index, so the caller is told which agent repeated instead of being told
        that some constraint failed. The index is still what makes the row
        unwritable; this is what makes the refusal readable.
        """
        if not self.db.owns_current_transaction:
            raise RuntimeError("agent task creation requires transaction ownership")
        seen: set[str] = set()
        for agent_id in ancestry:
            if agent_id in seen:
                raise DelegationCycleError(
                    f"{agent_id} appears twice in this chain: {' -> '.join(ancestry)}"
                )
            seen.add(agent_id)
        stored = replace(task, depth=len(ancestry))
        await self.db.execute(
            "INSERT INTO agent_tasks(task_id, context_id, room_id, target_agent_id, "
            "delegating_agent_id, delegating_run_id, execution_id, state, "
            "accepted_output_modes, depth, authorized_by, requested_by, created_at, "
            "updated_at, terminal_at, refusal_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stored.task_id,
                stored.context_id,
                stored.room_id,
                stored.target_agent_id,
                stored.delegating_agent_id,
                stored.delegating_run_id,
                stored.execution_id,
                stored.state.value,
                json.dumps(list(stored.accepted_output_modes)),
                stored.depth,
                stored.authorized_by,
                stored.requested_by,
                serialize_datetime(stored.created_at),
                serialize_datetime(stored.updated_at),
                serialize_datetime(stored.terminal_at),
                stored.refusal_reason,
            ),
        )
        for position, agent_id in enumerate(ancestry):
            await self.db.execute(
                "INSERT INTO agent_task_chain(task_id, position, agent_id) VALUES (?, ?, ?)",
                (stored.task_id, position, agent_id),
            )
        return stored

    async def create(self, task: AgentTask, ancestry: tuple[str, ...]) -> AgentTask:
        async with self.db.transaction():
            return await self.create_in_transaction(task, ancestry)

    async def get(self, task_id: str) -> AgentTask | None:
        row = await self.db.fetch_one("SELECT * FROM agent_tasks WHERE task_id = ?", (task_id,))
        return None if row is None else self._from_row(row)

    async def ancestry(self, task_id: str) -> tuple[str, ...]:
        """Every agent already in this task's chain, root first.

        Empty means a human asked, and that is why the whole chain is returned
        rather than the delegator alone: A asks B asks A is the loop people
        picture, A asks B asks C asks A is the loop they hit, and only the
        first of the two is visible one step back.
        """
        rows = await self.db.fetch_all(
            "SELECT agent_id FROM agent_task_chain WHERE task_id = ? ORDER BY position",
            (task_id,),
        )
        return tuple(str(row["agent_id"]) for row in rows)

    async def list_by_context(self, context_id: str) -> list[AgentTask]:
        # task_id breaks the tie. Tasks a fan-out opened together share a
        # timestamp to the microsecond, and SQLite promises nothing about the
        # order of rows an ORDER BY cannot separate.
        rows = await self.db.fetch_all(
            "SELECT * FROM agent_tasks WHERE context_id = ? ORDER BY created_at, task_id",
            (context_id,),
        )
        return [self._from_row(r) for r in rows]

    async def list_stale_submitted(self, before: datetime, limit: int = 25) -> list[AgentTask]:
        """Oldest tasks still SUBMITTED whose accept committed before ``before``.

        A fresh accept and a lost dispatch look identical for a moment; the
        cutoff is what tells them apart, so a task the caller only just
        opened is never swept out from under the background task it is
        already waiting on. The limit bounds one sweep pass: a large backlog
        drains a batch at a time across passes instead of stampeding a
        restart with every stranded task at once.
        """
        rows = await self.db.fetch_all(
            "SELECT * FROM agent_tasks WHERE state = ? AND created_at < ? "
            "ORDER BY created_at, task_id LIMIT ?",
            (AgentTaskState.SUBMITTED.value, serialize_datetime(before), limit),
        )
        return [self._from_row(r) for r in rows]

    async def list_working_with_settled_run(self, limit: int = 25) -> list[AgentTask]:
        """Tasks WORKING behind a run that has already settled, oldest first.

        ``execution_id`` always names the run currently driving a task; a task
        stays WORKING only for as long as something is still driving it. A run
        reaching SETTLED (however it got there: a natural end, an expired
        lease turned ORPHANED or PARKED, authority revoked) with the task
        still WORKING is the same defect a hard kill produces: whatever was
        going to move this task on already ran its course, or never will,
        and the task's own row is the only place left where that is not
        written down yet.
        """
        rows = await self.db.fetch_all(
            "SELECT t.* FROM agent_tasks t "
            "JOIN agent_runs r ON r.execution_id = t.execution_id "
            "WHERE t.state = ? AND r.harness_state = ? "
            "ORDER BY t.updated_at, t.task_id LIMIT ?",
            (AgentTaskState.WORKING.value, HarnessState.SETTLED.value, limit),
        )
        return [self._from_row(r) for r in rows]

    async def list_open_for_agent(self, agent_id: str) -> list[AgentTask]:
        placeholders = ", ".join("?" for _ in TERMINAL_STATES)
        rows = await self.db.fetch_all(
            "SELECT * FROM agent_tasks WHERE target_agent_id = ? "
            f"AND state NOT IN ({placeholders}) ORDER BY created_at, task_id",
            (agent_id, *(state.value for state in TERMINAL_STATES)),
        )
        return [self._from_row(r) for r in rows]

    async def transition(
        self,
        task_id: str,
        expected: AgentTaskState,
        target: AgentTaskState,
        *,
        refusal_reason: str = "",
    ) -> AgentTask:
        async with self.db.transaction():
            return await self.transition_in_transaction(
                task_id, expected, target, refusal_reason=refusal_reason
            )

    async def transition_in_transaction(
        self,
        task_id: str,
        expected: AgentTaskState,
        target: AgentTaskState,
        *,
        refusal_reason: str = "",
    ) -> AgentTask:
        """Move the task, conditional on the state the caller read.

        The state machine is asked first, so a move it does not have is refused
        before the database is touched. The WHERE clause is the second refusal
        and the one that matters under concurrency: a caller acting on a state
        that has since moved writes nothing and is told so, rather than
        overwriting whoever got there first.

        RETURNING is what makes the returned task the task this call wrote.
        Reading the row back afterwards would hand the caller whichever
        transition happened to land last, which under exactly the contention
        the WHERE clause exists to catch is somebody else's.
        """
        if not self.db.owns_current_transaction:
            raise RuntimeError("agent task transition requires transaction ownership")
        require_transition(expected, target)
        now = utcnow()
        updates: dict[str, Any] = {"state": target.value, "updated_at": now.isoformat()}
        if refusal_reason:
            updates["refusal_reason"] = refusal_reason
        if target in TERMINAL_STATES:
            updates["terminal_at"] = now.isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        cursor = await self.db.execute(
            f"UPDATE agent_tasks SET {set_clause} WHERE task_id = ? AND state = ? RETURNING *",
            (*updates.values(), task_id, expected.value),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            raise DomainError(
                f"agent task {task_id} is no longer {expected.value}: "
                f"the transition to {target.value} was not applied"
            )
        return self._from_row(dict(row))

    async def attach_execution(self, task_id: str, execution_id: str) -> None:
        async with self.db.transaction():
            await self.attach_execution_in_transaction(task_id, execution_id)

    async def attach_execution_in_transaction(self, task_id: str, execution_id: str) -> None:
        """Record the run this task is being served by, while it can still be served.

        Guarded like a transition, and for the same reason: an unguarded UPDATE
        reports success against a task_id nobody ever wrote, and hangs a live
        execution off a task that finished while the caller was starting it.
        """
        if not self.db.owns_current_transaction:
            raise RuntimeError("agent task execution attach requires transaction ownership")
        placeholders = ", ".join("?" for _ in TERMINAL_STATES)
        cursor = await self.db.execute(
            "UPDATE agent_tasks SET execution_id = ?, updated_at = ? "
            f"WHERE task_id = ? AND state NOT IN ({placeholders})",
            (
                execution_id,
                serialize_datetime(utcnow()),
                task_id,
                *(state.value for state in TERMINAL_STATES),
            ),
        )
        if cursor.rowcount != 1:
            raise DomainError(
                f"agent task {task_id} is unknown or already terminal: "
                f"execution {execution_id} was not attached"
            )

    async def append_message_with_next_sequence(
        self, task_id: str, role: TaskMessageRole, parts: tuple[Part, ...]
    ) -> AgentTaskMessage:
        """Atomically allocate this task's next sequence and insert its message."""
        async with self.db.transaction():
            return await self.append_message_with_next_sequence_in_transaction(task_id, role, parts)

    async def append_message_with_next_sequence_in_transaction(
        self, task_id: str, role: TaskMessageRole, parts: tuple[Part, ...]
    ) -> AgentTaskMessage:
        """Append while the caller owns a wider atomic state transition.

        The sequence is taken by the statement that writes it. Read separately,
        two turns landing together would both see the same maximum, and
        UNIQUE(task_id, sequence) would turn the loser's message into an error
        instead of into the next message.
        """
        if not self.db.owns_current_transaction:
            raise RuntimeError("agent task message append requires transaction ownership")
        message_id = new_id("a2amsg")
        created_at = utcnow()
        cursor = await self.db.execute(
            "INSERT INTO agent_task_messages(message_id, task_id, sequence, role, parts, "
            "created_at) "
            "SELECT ?, ?, COALESCE(MAX(sequence), 0) + 1, ?, ?, ? "
            "FROM agent_task_messages WHERE task_id = ? "
            "RETURNING sequence",
            (
                message_id,
                task_id,
                role.value,
                json.dumps([part.as_dict() for part in parts]),
                serialize_datetime(created_at),
                task_id,
            ),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return AgentTaskMessage(
            message_id=message_id,
            task_id=task_id,
            sequence=int(row["sequence"]) if row else 1,
            role=role,
            parts=parts,
            created_at=created_at,
        )

    async def list_messages(self, task_id: str) -> list[AgentTaskMessage]:
        rows = await self.db.fetch_all(
            "SELECT * FROM agent_task_messages WHERE task_id = ? ORDER BY sequence", (task_id,)
        )
        return [self._message_from_row(r) for r in rows]

    def _from_row(self, row: dict[str, Any]) -> AgentTask:
        return AgentTask(
            task_id=row["task_id"],
            context_id=row["context_id"],
            room_id=row["room_id"],
            target_agent_id=row["target_agent_id"],
            authorized_by=row["authorized_by"],
            requested_by=row["requested_by"],
            delegating_agent_id=row["delegating_agent_id"],
            delegating_run_id=row["delegating_run_id"],
            execution_id=row["execution_id"],
            state=AgentTaskState(row["state"]),
            accepted_output_modes=tuple(json.loads(row["accepted_output_modes"])),
            depth=int(row["depth"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            terminal_at=deserialize_datetime(row["terminal_at"]),
            refusal_reason=row["refusal_reason"],
        )

    def _message_from_row(self, row: dict[str, Any]) -> AgentTaskMessage:
        return AgentTaskMessage(
            message_id=row["message_id"],
            task_id=row["task_id"],
            sequence=int(row["sequence"]),
            role=TaskMessageRole(row["role"]),
            parts=tuple(Part.from_dict(part) for part in json.loads(row["parts"])),
            created_at=datetime.fromisoformat(row["created_at"]),
        )


class MessageRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, message: Message) -> Message:
        await self.db.execute(
            "INSERT INTO messages(message_id, room_id, role, sender_id, content, "
            "metadata, event_sequence, parent_message_id, root_message_id, thread_depth, "
            "broadcast_to_room, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message.message_id,
                message.room_id,
                message.role.value,
                message.sender_id,
                message.content,
                json.dumps(message.metadata),
                message.event_sequence,
                message.parent_message_id,
                message.root_message_id,
                message.thread_depth,
                int(message.broadcast_to_room),
                serialize_datetime(message.created_at),
            ),
        )
        await SearchRepo(self.db).index(
            SearchObjectKind.MESSAGE,
            message.message_id,
            message.room_id,
            message.sender_id,
            message.content,
            message.created_at,
        )
        await self.db.commit()
        return message

    async def create_with_event_and_turn_guard(
        self, message: Message, event: RoomEvent
    ) -> RoomEvent:
        """Serialize human messages against room turn-lock acquisition."""
        async with self.db.transaction():
            return await self.create_with_event_and_turn_guard_in_transaction(message, event)

    async def create_with_event_and_turn_guard_in_transaction(
        self, message: Message, event: RoomEvent
    ) -> RoomEvent:
        """Append while the caller owns a wider atomic state transition."""
        if not self.db.owns_current_transaction:
            raise RuntimeError("message append requires transaction ownership")
        if message.role == MessageRole.HUMAN:
            lock = await TurnLockRepo(self.db).get_active(TurnLockScopeType.ROOM, message.room_id)
            if lock is not None:
                raise ValueError(
                    f"room turn is locked by branch {lock.branch_id} until it is terminal"
                )
        await self.create(message)
        persisted = await EventRepo(self.db).append_with_next_sequence_in_transaction(event)
        # The message and the event that created it are one atomic unit, so the
        # message can carry the sequence rather than a reader having to join for it.
        await self.db.execute(
            "UPDATE messages SET event_sequence = ? WHERE message_id = ?",
            (persisted.sequence, message.message_id),
        )
        return persisted

    async def get(self, message_id: str) -> Message | None:
        row = await self.db.fetch_one("SELECT * FROM messages WHERE message_id = ?", (message_id,))
        return self._from_row(row) if row else None

    async def list_by_room(
        self,
        room_id: str,
        limit: int = 100,
        offset: int = 0,
        after_sequence: int | None = None,
    ) -> list[Message]:
        """The flat channel log: top-level messages, plus replies that were broadcast.

        broadcast_to_room is enforced here rather than by whichever client happens
        to filter, so every caller of this listing — browser or not — sees the same
        channel.
        """
        limit = min(limit, 500)
        broadcast = "(parent_message_id IS NULL OR broadcast_to_room = 1)"
        if after_sequence is not None:
            rows = await self.db.fetch_all(
                f"SELECT * FROM messages WHERE room_id = ? AND event_sequence > ? AND {broadcast} "
                "ORDER BY event_sequence, message_id LIMIT ?",
                (room_id, after_sequence, limit),
            )
            return [self._from_row(r) for r in rows]
        rows = await self.db.fetch_all(
            f"SELECT * FROM messages WHERE room_id = ? AND {broadcast} "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (room_id, limit, offset),
        )
        return [self._from_row(r) for r in reversed(rows)]

    async def count_since_sequence(self, room_id: str, sequence: int, viewer_id: str) -> int:
        """How many messages this reader has not seen, in the channel they are shown.

        The broadcast rule is the same one list_by_room applies, because an unread
        pill that counts thread replies the channel never displays sends the reader
        hunting for messages that are not there. Their own messages are excluded for
        the same reason: nobody arrives at their own sentence unread.
        """
        row = await self.db.fetch_one(
            "SELECT COUNT(*) AS unread FROM messages WHERE room_id = ? AND event_sequence > ? "
            "AND (parent_message_id IS NULL OR broadcast_to_room = 1) AND sender_id <> ?",
            (room_id, sequence, viewer_id),
        )
        return int(row["unread"]) if row else 0

    async def count_replies(self, message_id: str) -> int:
        """Derived from the durable reply rows; this layer stores no reply counter."""
        row = await self.db.fetch_one(
            "SELECT COUNT(*) AS replies FROM messages WHERE parent_message_id = ?", (message_id,)
        )
        return int(row["replies"]) if row else 0

    async def thread_summaries_by_room(self, room_id: str) -> dict[str, ThreadSummary]:
        """Every thread in one room, summarised from its own rows on this read.

        Grouped on root_message_id, not on parent_message_id: a channel shows one
        entry per thread, so the number beside it must be the whole thread. Grouping
        on the parent would tell a twelve-message thread it has two replies because
        only two of them answered the root directly.
        """
        rows = await self.db.fetch_all(
            "SELECT COALESCE(m.root_message_id, m.message_id) AS root_id, "
            "SUM(CASE WHEN m.root_message_id IS NULL THEN 0 ELSE 1 END) AS descendants, "
            "COUNT(DISTINCT m.sender_id) AS participants, "
            "MAX(CASE WHEN m.root_message_id IS NULL THEN NULL ELSE m.created_at END) "
            "AS last_reply_at "
            "FROM messages m WHERE m.room_id = ? GROUP BY root_id HAVING descendants > 0",
            (room_id,),
        )
        return {
            str(row["root_id"]): ThreadSummary(
                root_message_id=str(row["root_id"]),
                descendant_count=int(row["descendants"]),
                participant_count=int(row["participants"]),
                last_reply_at=(
                    datetime.fromisoformat(row["last_reply_at"])
                    if row.get("last_reply_at")
                    else None
                ),
            )
            for row in rows
        }

    async def list_thread(self, root_message_id: str, limit: int = 200) -> list[ThreadReply]:
        """The root and every descendant, each with its own count computed on read."""
        rows = await self.db.fetch_all(
            "SELECT m.*, ("
            "  SELECT COUNT(*) FROM messages c WHERE c.parent_message_id = m.message_id"
            ") AS reply_count "
            "FROM messages m "
            "WHERE m.message_id = ? OR m.root_message_id = ? "
            "ORDER BY m.event_sequence, m.message_id LIMIT ?",
            (root_message_id, root_message_id, min(limit, 500)),
        )
        return [ThreadReply(self._from_row(r), int(r["reply_count"])) for r in rows]

    @staticmethod
    def _from_row(r: dict[str, Any]) -> Message:
        return Message(
            message_id=r["message_id"],
            room_id=r["room_id"],
            role=MessageRole(r["role"]),
            sender_id=r["sender_id"],
            content=r["content"],
            metadata=json.loads(r["metadata"]),
            event_sequence=int(r["event_sequence"]),
            parent_message_id=r.get("parent_message_id"),
            root_message_id=r.get("root_message_id"),
            thread_depth=int(r["thread_depth"]),
            broadcast_to_room=bool(r["broadcast_to_room"]),
            created_at=datetime.fromisoformat(r["created_at"]),
        )


class MessageMentionRepo:
    """Mentions are derived from message text, so this repo only stores and reads."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, mention: MessageMention) -> None:
        await self.db.execute(
            "INSERT INTO message_mentions(message_id, room_id, target_type, target_id, handle, "
            "invoked_execution_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                mention.message_id,
                mention.room_id,
                mention.target_type.value,
                mention.target_id,
                mention.handle,
                mention.invoked_execution_id,
                serialize_datetime(mention.created_at),
            ),
        )
        await self.db.commit()

    async def list_for_message(self, message_id: str) -> list[MessageMention]:
        rows = await self.db.fetch_all(
            "SELECT * FROM message_mentions WHERE message_id = ? ORDER BY target_type, target_id",
            (message_id,),
        )
        return [self._from_row(r) for r in rows]

    @staticmethod
    def _from_row(r: dict[str, Any]) -> MessageMention:
        return MessageMention(
            message_id=r["message_id"],
            room_id=r["room_id"],
            target_type=MentionTargetType(r["target_type"]),
            target_id=r["target_id"],
            handle=r["handle"],
            invoked_execution_id=r.get("invoked_execution_id"),
            created_at=datetime.fromisoformat(r["created_at"]),
        )


class AttachmentRepo:
    """Uploaded files: unbound until a message claims them, always scoped to a room."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, attachment: Attachment) -> Attachment:
        await self.db.execute(
            "INSERT INTO attachments(attachment_id, room_id, uploader_id, filename, "
            "content_type, size_bytes, sha256, created_at, message_id, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attachment.attachment_id,
                attachment.room_id,
                attachment.uploader_id,
                attachment.filename,
                attachment.content_type,
                attachment.size_bytes,
                attachment.sha256,
                serialize_datetime(attachment.created_at),
                attachment.message_id,
                attachment.data,
            ),
        )
        await self.db.commit()
        return attachment

    async def get(self, attachment_id: str) -> Attachment | None:
        row = await self.db.fetch_one(
            "SELECT * FROM attachments WHERE attachment_id = ?", (attachment_id,)
        )
        return None if row is None else self._from_row(row)

    async def get_metadata_only(self, attachment_id: str) -> Attachment | None:
        """Every column but the blob, for a caller that only ever needs metadata."""
        row = await self.db.fetch_one(
            "SELECT attachment_id, room_id, uploader_id, filename, content_type, "
            "size_bytes, sha256, created_at, message_id FROM attachments "
            "WHERE attachment_id = ?",
            (attachment_id,),
        )
        return None if row is None else self._from_row(row, with_data=False)

    async def bind_to_message_in_transaction(
        self, attachment_id: str, room_id: str, uploader_id: str, message_id: str
    ) -> bool:
        """Claim an unbound upload for a message. False if it is not eligible.

        Eligible means: exists, belongs to this room, was uploaded by this sender,
        and nothing has bound it yet — all three checked in the same statement so
        a concurrent second bind can claim at most one of two identical uploads.
        """
        if not self.db.owns_current_transaction:
            raise RuntimeError("attachment bind requires transaction ownership")
        cursor = await self.db.execute(
            "UPDATE attachments SET message_id = ? WHERE attachment_id = ? AND room_id = ? "
            "AND uploader_id = ? AND message_id IS NULL",
            (message_id, attachment_id, room_id, uploader_id),
        )
        bound = cursor.rowcount > 0
        await cursor.close()
        return bound

    async def list_for_message(self, message_id: str) -> list[Attachment]:
        rows = await self.db.fetch_all(
            "SELECT attachment_id, room_id, uploader_id, filename, content_type, "
            "size_bytes, sha256, created_at, message_id FROM attachments "
            "WHERE message_id = ? ORDER BY created_at",
            (message_id,),
        )
        return [self._from_row(r, with_data=False) for r in rows]

    @staticmethod
    def _from_row(r: dict[str, Any], *, with_data: bool = True) -> Attachment:
        return Attachment(
            attachment_id=r["attachment_id"],
            room_id=r["room_id"],
            uploader_id=r["uploader_id"],
            filename=r["filename"],
            content_type=r["content_type"],
            size_bytes=int(r["size_bytes"]),
            sha256=r["sha256"],
            data=r["data"] if with_data else b"",
            message_id=r.get("message_id"),
            created_at=datetime.fromisoformat(r["created_at"]),
        )


class RoomParticipantHandleRepo:
    """The room's address book: one handle per participant, unique in the room."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def claim(self, handle: RoomParticipantHandle) -> bool:
        """Take the handle if the room still has it free.

        The unique index decides, not a prior SELECT: two participants joining at
        once would both read the same handle as free. False means somebody else
        holds it and the caller should try the next suffix.
        """
        try:
            await self.db.execute(
                "INSERT INTO room_participant_handles(room_id, participant_type, "
                "participant_id, handle, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    handle.room_id,
                    handle.participant_type.value,
                    handle.participant_id,
                    handle.handle,
                    serialize_datetime(handle.created_at),
                ),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    async def get_for_participant(
        self, room_id: str, participant_type: ParticipantType, participant_id: str
    ) -> RoomParticipantHandle | None:
        row = await self.db.fetch_one(
            "SELECT * FROM room_participant_handles WHERE room_id = ? AND participant_type = ? "
            "AND participant_id = ?",
            (room_id, participant_type.value, participant_id),
        )
        return self._from_row(row) if row else None

    async def list_participants_without_handles(self) -> list[dict[str, Any]]:
        """Members and agents that predate handles, in a fixed order.

        The order is what makes the upgrade reproducible: when two participants in a
        room normalise to the same handle, it decides which one keeps the bare form.
        """
        return await self.db.fetch_all(
            "SELECT rm.room_id AS room_id, 'USER' AS participant_type, "
            "rm.user_id AS participant_id, rm.user_id AS display_name "
            "FROM room_members rm LEFT JOIN room_participant_handles h "
            "  ON h.room_id = rm.room_id AND h.participant_type = 'USER' "
            "  AND h.participant_id = rm.user_id "
            "WHERE h.handle IS NULL "
            "UNION ALL "
            "SELECT a.room_id, 'AGENT', a.agent_id, a.name "
            "FROM agent_instances a LEFT JOIN room_participant_handles h "
            "  ON h.room_id = a.room_id AND h.participant_type = 'AGENT' "
            "  AND h.participant_id = a.agent_id "
            "WHERE h.handle IS NULL "
            # A removed agent has no handle by design. Without this it would look
            # like one that predates handles, and the backfill would re-address it.
            "  AND EXISTS (SELECT 1 FROM agent_room_memberships m "
            "    WHERE m.agent_id = a.agent_id AND m.room_id = a.room_id "
            "      AND m.removed_at IS NULL) "
            "ORDER BY room_id, participant_type, participant_id"
        )

    async def release_in_transaction(
        self, room_id: str, participant_type: ParticipantType, participant_id: str
    ) -> None:
        """Give the handle back to the room when its holder leaves.

        A handle is durable while it is held, which is why the table refuses to
        repoint one. Releasing it is the other half of that: a participant who is no
        longer in the room holds no address in it, and past mentions still name the
        participant by id rather than by the handle they typed.
        """
        await self.db.execute(
            "DELETE FROM room_participant_handles WHERE room_id = ? AND participant_type = ? "
            "AND participant_id = ?",
            (room_id, participant_type.value, participant_id),
        )

    async def list_by_room(self, room_id: str) -> list[RoomParticipantHandle]:
        rows = await self.db.fetch_all(
            "SELECT * FROM room_participant_handles WHERE room_id = ? ORDER BY handle",
            (room_id,),
        )
        return [self._from_row(r) for r in rows]

    @staticmethod
    def _from_row(r: dict[str, Any]) -> RoomParticipantHandle:
        return RoomParticipantHandle(
            room_id=r["room_id"],
            participant_type=ParticipantType(r["participant_type"]),
            participant_id=r["participant_id"],
            handle=r["handle"],
            created_at=datetime.fromisoformat(r["created_at"]),
        )


class MessageReactionRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def set_removed_at(
        self,
        message_id: str,
        room_id: str,
        actor_id: str,
        emoji: str,
        removed_at: datetime | None,
        actor_type: ParticipantType = ParticipantType.USER,
    ) -> MessageReaction:
        """Add or restore when removed_at is None, soft-remove when it is a time."""
        now = utcnow()
        await self.db.execute(
            "INSERT INTO message_reactions(message_id, room_id, actor_id, emoji, actor_type, "
            "created_at, updated_at, removed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(message_id, actor_id, emoji) DO UPDATE SET "
            "updated_at = excluded.updated_at, removed_at = excluded.removed_at",
            (
                message_id,
                room_id,
                actor_id,
                emoji,
                actor_type.value,
                serialize_datetime(now),
                serialize_datetime(now),
                serialize_datetime(removed_at),
            ),
        )
        reaction = await self.get(message_id, actor_id, emoji)
        if reaction is None:
            raise DomainError("reaction was not persisted")
        return reaction

    async def get(self, message_id: str, actor_id: str, emoji: str) -> MessageReaction | None:
        row = await self.db.fetch_one(
            "SELECT * FROM message_reactions WHERE message_id = ? AND actor_id = ? AND emoji = ?",
            (message_id, actor_id, emoji),
        )
        return self._from_row(row) if row else None

    async def list_live(self, message_id: str) -> list[MessageReaction]:
        rows = await self.db.fetch_all(
            "SELECT * FROM message_reactions WHERE message_id = ? AND removed_at IS NULL "
            "ORDER BY emoji, actor_id",
            (message_id,),
        )
        return [self._from_row(r) for r in rows]

    async def list_live_by_room(self, room_id: str) -> list[MessageReaction]:
        rows = await self.db.fetch_all(
            "SELECT * FROM message_reactions WHERE room_id = ? AND removed_at IS NULL "
            "ORDER BY message_id, emoji, actor_id",
            (room_id,),
        )
        return [self._from_row(r) for r in rows]

    @staticmethod
    def _from_row(r: dict[str, Any]) -> MessageReaction:
        return MessageReaction(
            message_id=r["message_id"],
            room_id=r["room_id"],
            actor_id=r["actor_id"],
            emoji=r["emoji"],
            actor_type=ParticipantType(r["actor_type"]),
            created_at=datetime.fromisoformat(r["created_at"]),
            updated_at=datetime.fromisoformat(r["updated_at"]),
            removed_at=(datetime.fromisoformat(r["removed_at"]) if r.get("removed_at") else None),
        )


class ReadCursorRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, room_id: str, user_id: str) -> ReadCursor | None:
        row = await self.db.fetch_one(
            "SELECT * FROM room_read_cursors WHERE room_id = ? AND user_id = ?",
            (room_id, user_id),
        )
        if row is None:
            return None
        return ReadCursor(
            room_id=row["room_id"],
            user_id=row["user_id"],
            last_read_sequence=int(row["last_read_sequence"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    async def set(self, cursor: ReadCursor) -> None:
        await self.db.execute(
            "INSERT INTO room_read_cursors(room_id, user_id, last_read_sequence, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(room_id, user_id) DO UPDATE SET "
            "last_read_sequence = excluded.last_read_sequence, updated_at = excluded.updated_at",
            (
                cursor.room_id,
                cursor.user_id,
                cursor.last_read_sequence,
                serialize_datetime(cursor.updated_at),
            ),
        )
        await self.db.commit()


class SearchRepo:
    """Full-text search over an opt-in allowlist of object kinds.

    Nothing reaches the index unless its kind has a row in search_indexed_kinds:
    that is a foreign key, so an unlisted kind cannot be written at all. And the
    asker's room membership is a join inside the matching query itself, so a
    non-member's search produces zero rows in SQLite rather than rows that some
    later Python filter is trusted to drop.
    """

    # The roles that carry RoomCapability.READ, derived from the authorization
    # policy at import time rather than restated here. The join is still
    # deny-by-default — it is a role predicate, not "any row in room_members" —
    # but its predicate now cannot outlive a change to the policy table.
    _READING_ROLES = roles_with_capability(RoomCapability.READ)

    def __init__(self, db: Database) -> None:
        self.db = db

    async def index(
        self,
        kind: SearchObjectKind,
        object_id: str,
        room_id: str,
        author_id: str,
        content: str,
        created_at: datetime,
        container_id: str = "",
    ) -> None:
        await self.db.execute(
            "INSERT INTO search_documents(object_kind, object_id, room_id, container_id, "
            "author_id, content, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(object_kind, object_id) DO UPDATE SET content = excluded.content, "
            "container_id = excluded.container_id",
            (
                kind.value,
                object_id,
                room_id,
                container_id,
                author_id,
                content,
                serialize_datetime(created_at),
            ),
        )

    async def backfill(self) -> None:
        """Index the rows that already existed when their kind joined the allowlist.

        Every statement is INSERT OR IGNORE against UNIQUE(object_kind, object_id),
        so a second run inserts nothing and overwrites nothing a write path has
        indexed since. Each selects exactly the fields its write path indexes, so a
        backfilled row and a freshly written one are the same row.

        Each also joins rooms. A row whose room no longer exists has no membership
        to authorize a reader against, so it is skipped rather than indexed — and
        joining says so, where relying on the foreign key would abort the whole
        backfill on the first orphan a legacy database happens to hold.
        """
        await self.db.execute(
            "INSERT OR IGNORE INTO search_documents(object_kind, object_id, room_id, "
            "container_id, author_id, content, created_at) "
            "SELECT 'ARTIFACT_VERSION', v.version_id, a.room_id, a.artifact_id, v.created_by, "
            "v.content, v.created_at FROM artifact_versions v "
            "JOIN artifacts a ON a.artifact_id = v.artifact_id "
            "JOIN rooms r ON r.room_id = a.room_id "
            "WHERE v.version_number = ("
            "SELECT MAX(n.version_number) FROM artifact_versions n "
            "WHERE n.artifact_id = v.artifact_id)"
        )
        await self.db.execute(
            "INSERT OR IGNORE INTO search_documents(object_kind, object_id, room_id, "
            "container_id, author_id, content, created_at) "
            "SELECT 'TASK', t.task_id, t.room_id, '', t.created_by, t.title, t.created_at "
            "FROM tasks t JOIN rooms r ON r.room_id = t.room_id"
        )
        await self.db.execute(
            "INSERT OR IGNORE INTO search_documents(object_kind, object_id, room_id, "
            "container_id, author_id, content, created_at) "
            "SELECT 'AGENT_OUTPUT', o.output_id, o.room_id, '', o.agent_id, o.content, "
            "o.created_at FROM agent_outputs o JOIN rooms r ON r.room_id = o.room_id"
        )
        await self.db.execute(
            "INSERT OR IGNORE INTO search_documents(object_kind, object_id, room_id, "
            "container_id, author_id, content, created_at) "
            "SELECT 'DECISION', d.decision_id, d.room_id, '', d.created_by, "
            "d.title || char(10) || d.content, d.created_at "
            "FROM decisions d JOIN rooms r ON r.room_id = d.room_id"
        )

    async def search(
        self, user_id: str, match_query: str, room_id: str | None = None, limit: int = 50
    ) -> list[SearchHit]:
        placeholders = ", ".join("?" for _ in self._READING_ROLES)
        sql = (
            "SELECT d.object_kind, d.object_id, d.container_id, d.room_id, r.name AS room_name, "
            "d.author_id, d.created_at, "
            "snippet(search_documents_fts, 0, '[', ']', '…', 12) AS excerpt "
            "FROM search_documents_fts f "
            "JOIN search_documents d ON d.document_id = f.rowid "
            "JOIN search_indexed_kinds k ON k.object_kind = d.object_kind "
            "JOIN rooms r ON r.room_id = d.room_id "
            "JOIN room_members rm ON rm.room_id = d.room_id AND rm.user_id = ? "
            f"AND rm.role IN ({placeholders}) "
            "WHERE search_documents_fts MATCH ?"
        )
        params: tuple[Any, ...] = (user_id, *self._READING_ROLES, match_query)
        if room_id is not None:
            sql += " AND d.room_id = ?"
            params += (room_id,)
        sql += " ORDER BY rank LIMIT ?"
        params += (min(limit, 100),)
        rows = await self.db.fetch_all(sql, params)
        return [
            SearchHit(
                object_kind=SearchObjectKind(r["object_kind"]),
                object_id=r["object_id"],
                container_id=r["container_id"],
                room_id=r["room_id"],
                room_name=r["room_name"],
                author_id=r["author_id"],
                excerpt=r["excerpt"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]


class EventRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def _insert_chained(self, event: RoomEvent) -> RoomEvent:
        """Insert the event with its hash chained onto its predecessor's.

        The predecessor must already be visible: committed, or written earlier
        inside the transaction the caller owns. The canonical transactional
        append guarantees that; a direct append with a preallocated sequence is
        on its caller to insert in order.
        """
        if event.sequence > 1:
            prev = await self.db.fetch_one(
                "SELECT event_hash FROM room_events WHERE room_id = ? AND sequence = ?",
                (event.room_id, event.sequence - 1),
            )
            if prev is None or prev["event_hash"] is None:
                raise RuntimeError(
                    f"event chain for room {event.room_id} has no hashed predecessor "
                    f"for sequence {event.sequence}"
                )
            prev_hash = str(prev["event_hash"])
        else:
            prev_hash = GENESIS_HASH
        payload_json = json.dumps(event.payload, default=str)
        timestamp = serialize_datetime(event.timestamp) or ""
        event_hash = event_chain_hash(
            prev_hash,
            event.event_id,
            event.room_id,
            event.sequence,
            event.event_type.value,
            payload_json,
            event.actor_id,
            event.actor_type,
            timestamp,
            event.schema_version,
        )
        await self.db.execute(
            "INSERT INTO room_events(event_id, room_id, sequence, event_type, payload, "
            "actor_id, actor_type, timestamp, schema_version, prev_hash, event_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.room_id,
                event.sequence,
                event.event_type.value,
                payload_json,
                event.actor_id,
                event.actor_type,
                timestamp,
                event.schema_version,
                prev_hash,
                event_hash,
            ),
        )
        return event

    async def append(self, event: RoomEvent) -> RoomEvent:
        await self._insert_chained(event)
        await self.db.commit()
        return event

    async def append_with_next_sequence(self, event: RoomEvent) -> RoomEvent:
        """Atomically allocate a sequence and insert its canonical event."""
        async with self.db.transaction():
            return await self.append_with_next_sequence_in_transaction(event)

    async def append_with_next_sequence_in_transaction(self, event: RoomEvent) -> RoomEvent:
        """Append while the caller owns a wider atomic state transition."""
        if not self.db.owns_current_transaction:
            raise RuntimeError("canonical event append requires transaction ownership")
        cursor = await self.db.execute(
            "INSERT INTO room_sequences(room_id, seq) VALUES (?, 1) "
            "ON CONFLICT(room_id) DO UPDATE SET seq = seq + 1 "
            "RETURNING seq",
            (event.room_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        seq = int(row["seq"]) if row else 1
        return await self._insert_chained(replace(event, sequence=seq))

    async def get_next_sequence(self, room_id: str) -> int:
        """Atomically increment and return the sequence this call allocated.

        The increment and the read are one statement, via RETURNING, so the
        value handed back is provably the one this call wrote: two callers
        racing here each get their own distinct number, never a value read
        back after a second caller's increment had already moved it on. An
        earlier version split the write and the read into two statements,
        which let exactly that interleaving hand two racing callers the same
        number back.

        Nothing in this module preallocates a sequence and inserts the event
        under it later: every append computes and inserts in the same
        breath, so a caller reaching for this method is choosing the two
        step pattern this repository does not otherwise use.
        """
        cursor = await self.db.execute(
            "INSERT INTO room_sequences(room_id, seq) VALUES (?, 1) "
            "ON CONFLICT(room_id) DO UPDATE SET seq = seq + 1 "
            "RETURNING seq",
            (room_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        await self.db.commit()
        return int(row["seq"]) if row else 1

    async def list_since(
        self, room_id: str, after_sequence: int, limit: int = 500
    ) -> list[RoomEvent]:
        rows = await self.db.fetch_all(
            "SELECT * FROM room_events WHERE room_id = ? AND sequence > ? "
            "ORDER BY sequence ASC LIMIT ?",
            (room_id, after_sequence, limit),
        )
        return [
            RoomEvent(
                event_id=r["event_id"],
                room_id=r["room_id"],
                sequence=r["sequence"],
                event_type=EventType(r["event_type"]),
                payload=json.loads(r["payload"]),
                actor_id=r["actor_id"],
                actor_type=r["actor_type"],
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

    async def get_sequence_counter(self, room_id: str) -> int:
        """The room's own event counter — what an export's line count must equal."""
        row = await self.db.fetch_one(
            "SELECT seq FROM room_sequences WHERE room_id = ?", (room_id,)
        )
        return int(row["seq"]) if row else 0

    async def list_since_with_chain(
        self, room_id: str, after_sequence: int, limit: int = 500
    ) -> list[dict[str, Any]]:
        """One page of raw rows, hash-chain fields included, for an export.

        list_since's RoomEvent leaves prev_hash and event_hash off — nothing but
        an export needs them — and its own 500-row default is exactly the cap a
        caller reading the whole room has to page past rather than trust as a
        single read.
        """
        rows = await self.db.fetch_all(
            "SELECT event_id, room_id, sequence, event_type, payload, actor_id, actor_type, "
            "timestamp, schema_version, prev_hash, event_hash FROM room_events "
            "WHERE room_id = ? AND sequence > ? ORDER BY sequence ASC LIMIT ?",
            (room_id, after_sequence, limit),
        )
        return rows


class ArtifactRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, artifact: Artifact) -> Artifact:
        await self.db.execute(
            "INSERT INTO artifacts(artifact_id, room_id, name, artifact_type, description, "
            "current_version, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                artifact.artifact_id,
                artifact.room_id,
                artifact.name,
                artifact.artifact_type.value,
                artifact.description,
                artifact.current_version,
                artifact.created_by,
                serialize_datetime(artifact.created_at),
                serialize_datetime(artifact.updated_at),
            ),
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
            return await self.create_version_in_transaction(version)

    async def create_version_in_transaction(self, version: ArtifactVersion) -> ArtifactVersion:
        """The body of create_version for a caller that already owns the transaction."""
        await self.db.execute(
            "INSERT INTO artifact_versions(version_id, artifact_id, version_number, content, "
            "content_hash, provenance_hash, branch_synthesis_id, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version.version_id,
                version.artifact_id,
                version.version_number,
                version.content,
                version.content_hash,
                version.provenance_hash,
                version.branch_synthesis_id,
                version.created_by,
                serialize_datetime(version.created_at),
            ),
        )
        await self.db.execute(
            "UPDATE artifacts SET current_version = ?, updated_at = ? WHERE artifact_id = ?",
            (version.version_number, utcnow().isoformat(), version.artifact_id),
        )
        await self._index_version_in_transaction(version)
        return version

    async def _index_version_in_transaction(self, version: ArtifactVersion) -> None:
        """Index the artifact's newest text, replacing whatever version preceded it.

        Only one version of an artifact is searchable at a time. A superseded
        version is durable history that the version list still serves alongside its
        version number; as a search hit it is an excerpt with nothing to say it is
        stale, which reads as the artifact's current text.

        The version's content is indexed whole because the version list already
        returns it whole to any reader of the room. container_id carries the
        artifact id, which is what a client needs to fetch the version back.
        """
        row = await self.db.fetch_one(
            "SELECT room_id FROM artifacts WHERE artifact_id = ?", (version.artifact_id,)
        )
        if row is None:
            raise DomainError(f"artifact not found: {version.artifact_id}")
        await self.db.execute(
            "DELETE FROM search_documents WHERE object_kind = ? AND object_id IN "
            "(SELECT version_id FROM artifact_versions WHERE artifact_id = ?)",
            (SearchObjectKind.ARTIFACT_VERSION.value, version.artifact_id),
        )
        await SearchRepo(self.db).index(
            SearchObjectKind.ARTIFACT_VERSION,
            version.version_id,
            row["room_id"],
            version.created_by,
            version.content,
            version.created_at,
            container_id=version.artifact_id,
        )

    async def list_versions(self, artifact_id: str) -> list[ArtifactVersion]:
        rows = await self.db.fetch_all(
            "SELECT * FROM artifact_versions WHERE artifact_id = ? ORDER BY version_number DESC",
            (artifact_id,),
        )
        return [
            ArtifactVersion(
                version_id=r["version_id"],
                artifact_id=r["artifact_id"],
                version_number=r["version_number"],
                content=r["content"],
                content_hash=r["content_hash"],
                provenance_hash=r.get("provenance_hash", ""),
                branch_synthesis_id=r.get("branch_synthesis_id"),
                created_by=r["created_by"],
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]

    async def get_version(self, version_id: str) -> ArtifactVersion | None:
        row = await self.db.fetch_one(
            "SELECT * FROM artifact_versions WHERE version_id = ?", (version_id,)
        )
        if row is None:
            return None
        return ArtifactVersion(
            version_id=row["version_id"],
            artifact_id=row["artifact_id"],
            version_number=row["version_number"],
            content=row["content"],
            content_hash=row["content_hash"],
            provenance_hash=row.get("provenance_hash", ""),
            branch_synthesis_id=row.get("branch_synthesis_id"),
            created_by=row["created_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    async def resolve_decision_version(
        self, room_id: str, version_id: str | None = None
    ) -> tuple[Artifact, ArtifactVersion] | None:
        """Resolve one published Decision Brief without crossing the room boundary."""
        params: tuple[Any, ...]
        version_filter = ""
        if version_id is None:
            params = (room_id,)
        else:
            version_filter = "AND v.version_id = ? "
            params = (room_id, version_id)
        row = await self.db.fetch_one(
            "SELECT a.*, v.version_id AS resolved_version_id, "
            "v.version_number AS resolved_version_number, v.content AS resolved_content, "
            "v.content_hash AS resolved_content_hash, "
            "v.provenance_hash AS resolved_provenance_hash, "
            "v.branch_synthesis_id AS resolved_branch_synthesis_id, "
            "v.created_by AS resolved_created_by, v.created_at AS resolved_created_at "
            "FROM artifacts a JOIN artifact_versions v ON v.artifact_id = a.artifact_id "
            "WHERE a.room_id = ? AND a.name = 'Decision Brief' "
            f"{version_filter}"
            "ORDER BY v.version_number DESC LIMIT 1",
            params,
        )
        if row is None:
            return None
        artifact = self._from_row(row)
        version = ArtifactVersion(
            version_id=row["resolved_version_id"],
            artifact_id=row["artifact_id"],
            version_number=row["resolved_version_number"],
            content=row["resolved_content"],
            content_hash=row["resolved_content_hash"],
            provenance_hash=row["resolved_provenance_hash"],
            branch_synthesis_id=row.get("resolved_branch_synthesis_id"),
            created_by=row["resolved_created_by"],
            created_at=datetime.fromisoformat(row["resolved_created_at"]),
        )
        return artifact, version

    async def create_synthesis(
        self,
        artifact: Artifact,
        version: ArtifactVersion,
        claims_and_sources: list[tuple[ArtifactClaim, ClaimSource]],
        ontology_entities: list[OntologyEntity],
        ontology_relationships: list[OntologyRelationship],
        events: list[RoomEvent],
        *,
        create_artifact: bool,
        synthesis: BranchSynthesis | None = None,
    ) -> list[RoomEvent]:
        """Atomically publish a version, complete provenance graph, and events."""
        async with self.db.transaction():
            return await self.create_synthesis_in_transaction(
                artifact,
                version,
                claims_and_sources,
                ontology_entities,
                ontology_relationships,
                events,
                create_artifact=create_artifact,
                synthesis=synthesis,
            )

    async def create_synthesis_in_transaction(
        self,
        artifact: Artifact,
        version: ArtifactVersion,
        claims_and_sources: list[tuple[ArtifactClaim, ClaimSource]],
        ontology_entities: list[OntologyEntity],
        ontology_relationships: list[OntologyRelationship],
        events: list[RoomEvent],
        *,
        create_artifact: bool,
        synthesis: BranchSynthesis | None = None,
    ) -> list[RoomEvent]:
        """Body of :meth:`create_synthesis` for a caller that already owns the write
        transaction, so a membership re-check can share that same transaction."""
        if not self.db.owns_current_transaction:
            raise RuntimeError("create_synthesis_in_transaction requires transaction ownership")
        persisted_events: list[RoomEvent] = []
        if create_artifact:
            await self.db.execute(
                "INSERT INTO artifacts(artifact_id, room_id, name, artifact_type, description, "
                "current_version, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)",
                (
                    artifact.artifact_id,
                    artifact.room_id,
                    artifact.name,
                    artifact.artifact_type.value,
                    artifact.description,
                    artifact.created_by,
                    serialize_datetime(artifact.created_at),
                    serialize_datetime(artifact.updated_at),
                ),
            )
        await self.db.execute(
            "INSERT INTO artifact_versions(version_id, artifact_id, version_number, content, "
            "content_hash, provenance_hash, branch_synthesis_id, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version.version_id,
                version.artifact_id,
                version.version_number,
                version.content,
                version.content_hash,
                version.provenance_hash,
                version.branch_synthesis_id,
                version.created_by,
                serialize_datetime(version.created_at),
            ),
        )
        inserted_claim_ids: set[str] = set()
        for claim, source in claims_and_sources:
            if claim.claim_id not in inserted_claim_ids:
                await self.db.execute(
                    "INSERT INTO artifact_claims(claim_id, version_id, ordinal, text, "
                    "is_ai_derived, confidence) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        claim.claim_id,
                        claim.version_id,
                        claim.ordinal,
                        claim.text,
                        int(claim.is_ai_derived),
                        claim.confidence,
                    ),
                )
                inserted_claim_ids.add(claim.claim_id)
            await self.db.execute(
                "INSERT INTO artifact_claim_sources(claim_id, output_id, evidence, agent_id, "
                "execution_id, source_prompt, provider_input, provider_name, provider_model, "
                "provider_response_id, provider_interventions, provider_evidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source.claim_id,
                    source.output_id,
                    source.evidence,
                    source.agent_id,
                    source.execution_id,
                    source.source_prompt,
                    source.provider_input,
                    source.provider_name,
                    source.provider_model,
                    source.provider_response_id,
                    json.dumps(source.provider_interventions),
                    source.provider_evidence,
                ),
            )
        await self.db.execute(
            "UPDATE artifacts SET current_version = ?, updated_at = ? WHERE artifact_id = ?",
            (version.version_number, serialize_datetime(utcnow()), artifact.artifact_id),
        )
        await self._index_version_in_transaction(version)
        if synthesis is not None:
            await self.db.execute(
                "UPDATE branch_syntheses SET status = ?, provider_input = ?, "
                "provider_name = ?, provider_model = ?, provider_response_id = ?, "
                "provider_evidence = ?, simulated = ?, content = ?, "
                "artifact_version_id = ?, completed_at = ? WHERE synthesis_id = ?",
                (
                    BranchSynthesisStatus.COMPLETED.value,
                    synthesis.provider_input,
                    synthesis.provider_name,
                    synthesis.provider_model,
                    synthesis.provider_response_id,
                    synthesis.provider_evidence,
                    int(synthesis.simulated),
                    synthesis.content,
                    version.version_id,
                    serialize_datetime(synthesis.completed_at or utcnow()),
                    synthesis.synthesis_id,
                ),
            )
        for event in events:
            persisted = await EventRepo(self.db).append_with_next_sequence_in_transaction(event)
            persisted_events.append(persisted)
        # A structured action projects its own assertions inside its committing
        # transaction, positioned at the ordered event that announced them, so a
        # reader can derive their currency from the same axis as everything else.
        if ontology_entities or ontology_relationships:
            at = next(
                (
                    event.sequence
                    for event in persisted_events
                    if event.event_type is EventType.ONTOLOGY_MATERIALIZED
                ),
                persisted_events[-1].sequence if persisted_events else 0,
            )
            await OntologyRepo(self.db).materialize_in_transaction(
                [
                    replace(
                        entity,
                        extractor=OntologyExtractor.IMMEDIATE,
                        asserted_at_sequence=at,
                        evidence_event_sequences=(at,),
                    )
                    for entity in ontology_entities
                ],
                [
                    replace(
                        item,
                        extractor=OntologyExtractor.IMMEDIATE,
                        asserted_at_sequence=at,
                        evidence_event_sequences=(at,),
                    )
                    for item in ontology_relationships
                ],
            )
        return persisted_events

    async def get_version_provenance(self, version_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT c.claim_id, c.ordinal, c.text, c.is_ai_derived, c.confidence, "
            "s.output_id, s.evidence, s.agent_id, s.execution_id, s.source_prompt, "
            "s.provider_input, s.provider_name, s.provider_model, s.provider_response_id, "
            "s.provider_interventions, s.provider_evidence "
            "FROM artifact_claims c JOIN artifact_claim_sources s ON s.claim_id = c.claim_id "
            "WHERE c.version_id = ? ORDER BY c.ordinal, s.output_id",
            (version_id,),
        )
        provenance: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                interventions = json.loads(item["provider_interventions"])
            except (json.JSONDecodeError, TypeError):
                interventions = []
            item["provider_interventions"] = (
                interventions if isinstance(interventions, list) else []
            )
            provenance.append(item)
        return provenance

    async def get_version_provenance_bounded(
        self, version_id: str, limit: int
    ) -> tuple[list[dict[str, Any]], int]:
        """Return at most ten frozen claim/source rows and the exact available count."""
        bounded_limit = max(1, min(limit, 10))
        count_row = await self.db.fetch_one(
            "SELECT COUNT(*) AS count FROM artifact_claims WHERE version_id = ?",
            (version_id,),
        )
        total = int(count_row["count"]) if count_row else 0
        rows = await self.db.fetch_all(
            "SELECT c.claim_id, c.ordinal, c.text, c.is_ai_derived, c.confidence, "
            "s.output_id, s.evidence, s.agent_id, s.execution_id, s.source_prompt, "
            "s.provider_input, s.provider_name, s.provider_model, s.provider_response_id, "
            "s.provider_interventions, s.provider_evidence "
            "FROM artifact_claims c JOIN artifact_claim_sources s ON s.claim_id = c.claim_id "
            "WHERE c.version_id = ? ORDER BY c.ordinal, s.output_id LIMIT ?",
            (version_id, bounded_limit),
        )
        provenance: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                interventions = json.loads(item["provider_interventions"])
            except (json.JSONDecodeError, TypeError):
                interventions = []
            item["provider_interventions"] = (
                interventions if isinstance(interventions, list) else []
            )
            provenance.append(item)
        return provenance, total

    async def list_versions_without_provenance_hash(self) -> list[ArtifactVersion]:
        rows = await self.db.fetch_all(
            "SELECT * FROM artifact_versions WHERE provenance_hash = '' "
            "ORDER BY created_at, version_id"
        )
        return [
            ArtifactVersion(
                version_id=row["version_id"],
                artifact_id=row["artifact_id"],
                version_number=row["version_number"],
                content=row["content"],
                content_hash=row["content_hash"],
                provenance_hash="",
                branch_synthesis_id=row.get("branch_synthesis_id"),
                created_by=row["created_by"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    async def set_provenance_hash_if_empty(self, version_id: str, provenance_hash: str) -> None:
        await self.db.execute(
            "UPDATE artifact_versions SET provenance_hash = ? "
            "WHERE version_id = ? AND provenance_hash = ''",
            (provenance_hash, version_id),
        )

    def _from_row(self, row: dict[str, Any]) -> Artifact:
        return Artifact(
            artifact_id=row["artifact_id"],
            room_id=row["room_id"],
            name=row["name"],
            artifact_type=ArtifactType(row["artifact_type"]),
            description=row["description"],
            current_version=row["current_version"],
            created_by=row["created_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )


class ArtifactShareRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create_in_transaction(self, share: ArtifactShare) -> ArtifactShare:
        await self.db.execute(
            "INSERT INTO artifact_shares(share_id, artifact_id, room_id, token_hash, "
            "created_by, created_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                share.share_id,
                share.artifact_id,
                share.room_id,
                share.token_hash,
                share.created_by,
                serialize_datetime(share.created_at),
                serialize_datetime(share.revoked_at) if share.revoked_at else None,
            ),
        )
        return share

    async def get(self, share_id: str) -> ArtifactShare | None:
        row = await self.db.fetch_one(
            "SELECT * FROM artifact_shares WHERE share_id = ?", (share_id,)
        )
        return None if row is None else self._from_row(row)

    async def get_live_by_token_hash(self, token_hash: str) -> ArtifactShare | None:
        """The public route's only lookup — revoked and unknown tokens both miss here,
        so the caller answers both cases with the same 404 rather than distinguishing."""
        row = await self.db.fetch_one(
            "SELECT * FROM artifact_shares WHERE token_hash = ? AND revoked_at IS NULL",
            (token_hash,),
        )
        return None if row is None else self._from_row(row)

    async def list_by_artifact(self, artifact_id: str) -> list[ArtifactShare]:
        rows = await self.db.fetch_all(
            "SELECT * FROM artifact_shares WHERE artifact_id = ? ORDER BY created_at DESC",
            (artifact_id,),
        )
        return [self._from_row(r) for r in rows]

    async def revoke_in_transaction(self, share_id: str) -> ArtifactShare | None:
        """Soft-revoke a live share; returns None if it was already revoked."""
        current = await self.get(share_id)
        if current is None or current.revoked_at is not None:
            return None
        revoked_at = utcnow()
        await self.db.execute(
            "UPDATE artifact_shares SET revoked_at = ? WHERE share_id = ?",
            (serialize_datetime(revoked_at), share_id),
        )
        return replace(current, revoked_at=revoked_at)

    def _from_row(self, row: dict[str, Any]) -> ArtifactShare:
        return ArtifactShare(
            share_id=row["share_id"],
            artifact_id=row["artifact_id"],
            room_id=row["room_id"],
            token_hash=row["token_hash"],
            created_by=row["created_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
            revoked_at=deserialize_datetime(row.get("revoked_at")),
        )


class OntologyRepo:
    """Typed access to the bounded ontology projection and its review history."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def materialize_in_transaction(
        self,
        entities: list[OntologyEntity],
        relationships: list[OntologyRelationship],
    ) -> tuple[int, int, list[str]]:
        """Write assertions idempotently and return what each write touched.

        Every timing writes through here, so the transaction discipline, the
        deterministic-ID conflict rule and the inheritance rule below are written
        once rather than per writer.

        An entity is one assertion per source object, so a source row that moves
        re-asserts over the row that describes it rather than landing beside it.
        The conflict clause writes only when the projection genuinely differs and
        stands later in the room's order, which keeps a repeated pass a no-op.

        A reviewed assertion is exempt from that rewrite and is reconciled instead
        of skipped: the third return value names the assertions whose disagreement
        with their own row this pass recorded.
        """
        if not self.db.owns_current_transaction:
            raise RuntimeError("ontology materialization requires transaction ownership")
        entities_written = 0
        relationships_written = 0
        reconciled: list[str] = []
        for entity in entities:
            cursor = await self.db.execute(
                "INSERT INTO ontology_entities("
                "entity_id, room_id, kind, source_object_id, label, properties, "
                "derivation_kind, confidence, evidence_ids, source_ids, review_status, "
                "extractor, asserted_at_sequence, evidence_event_sequences, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(room_id, kind, source_object_id) DO UPDATE SET "
                "label = excluded.label, properties = excluded.properties, "
                "evidence_ids = excluded.evidence_ids, source_ids = excluded.source_ids, "
                "asserted_at_sequence = excluded.asserted_at_sequence, "
                "evidence_event_sequences = excluded.evidence_event_sequences, "
                "stale_at_sequence = NULL, updated_at = excluded.updated_at "
                "WHERE excluded.asserted_at_sequence > ontology_entities.asserted_at_sequence "
                "AND (ontology_entities.label <> excluded.label "
                "OR ontology_entities.properties <> excluded.properties) "
                # A reviewed assertion is a person's account of this object, so a
                # later pass never rewrites it; the reader learns the row moved from
                # the derived currency instead.
                "AND ontology_entities.review_status = 'UNCONFIRMED'",
                (
                    entity.entity_id,
                    entity.room_id,
                    entity.kind.value,
                    entity.source_object_id,
                    entity.label,
                    json.dumps(entity.properties, sort_keys=True),
                    entity.derivation_kind.value,
                    entity.confidence,
                    json.dumps(entity.evidence_ids),
                    json.dumps(entity.source_ids),
                    entity.review_status.value,
                    entity.extractor.value,
                    entity.asserted_at_sequence,
                    json.dumps(list(entity.evidence_event_sequences)),
                    serialize_datetime(entity.created_at),
                    serialize_datetime(entity.updated_at),
                ),
            )
            entities_written += cursor.rowcount if cursor.rowcount > 0 else 0
            if await self._reconcile_reviewed(entity):
                reconciled.append(entity.entity_id)
        for relationship in relationships:
            relationship = await self._inherit_the_weakest(relationship, entities)
            if not relationship.source_object_id:
                # SQLite cannot add a CHECK to a backfilled column without a table
                # rebuild, so the write path is where an edge that cannot be drilled
                # down is refused.
                raise ValueError("ontology relationship requires a source object")
            cursor = await self.db.execute(
                "INSERT INTO ontology_relationships("
                "relationship_id, room_id, kind, from_entity_id, to_entity_id, "
                "derivation_kind, confidence, evidence_ids, source_ids, review_status, "
                "source_object_kind, source_object_id, extractor, asserted_at_sequence, "
                "evidence_event_sequences, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(room_id, kind, from_entity_id, to_entity_id) DO NOTHING",
                (
                    relationship.relationship_id,
                    relationship.room_id,
                    relationship.kind.value,
                    relationship.from_entity_id,
                    relationship.to_entity_id,
                    relationship.derivation_kind.value,
                    relationship.confidence,
                    json.dumps(relationship.evidence_ids),
                    json.dumps(relationship.source_ids),
                    relationship.review_status.value,
                    relationship.source_object_kind,
                    relationship.source_object_id,
                    relationship.extractor.value,
                    relationship.asserted_at_sequence,
                    json.dumps(list(relationship.evidence_event_sequences)),
                    serialize_datetime(relationship.created_at),
                    serialize_datetime(relationship.updated_at),
                ),
            )
            relationships_written += cursor.rowcount if cursor.rowcount > 0 else 0
        return entities_written, relationships_written, reconciled

    async def _reconcile_reviewed(self, entity: OntologyEntity) -> bool:
        """Report that this pass saw a reviewed assertion disagree with its own row.

        The conflict clause above leaves a reviewed assertion alone, which is right
        — a person's account is not overwritten by a later machine pass — but on its
        own it also abandoned the rule that an assertion follows its row, and the
        cursor moved on. What the pass owes the log is that it looked and the two
        differed; the caller writes that down as an observation with a sequence.

        Nothing is written to the row. Whether the two still disagree is a question
        about the row as it stands when an answer is built, so it is asked there. It
        was asked here once, and the answer outlived the disagreement: a row that
        diverged and then came back kept a marker no pass could clear.
        """
        row = await self.db.fetch_one(
            "SELECT label, properties FROM ontology_entities "
            "WHERE room_id = ? AND kind = ? AND source_object_id = ? "
            "AND review_status <> 'UNCONFIRMED' AND asserted_at_sequence < ?",
            (
                entity.room_id,
                entity.kind.value,
                entity.source_object_id,
                entity.asserted_at_sequence,
            ),
        )
        if row is None:
            return False
        return bool(
            row["label"] != entity.label
            or row["properties"] != json.dumps(entity.properties, sort_keys=True)
        )

    async def _inherit_the_weakest(
        self,
        relationship: OntologyRelationship,
        batch: list[OntologyEntity],
    ) -> OntologyRelationship:
        """An edge whose inputs are assertions is only as good as its weakest input.

        An IMMEDIATE edge is projected from a structured row, so its derivation is a
        fact about that row and is left alone. An ASYNC or SCHEDULED edge is inferred
        by reading its endpoints, so it takes the weakest derivation kind and review
        status of them and a confidence no greater than either — otherwise a
        consolidation edge over two unconfirmed entities lands as confirmed truth.
        """
        if relationship.extractor is OntologyExtractor.IMMEDIATE:
            return relationship
        by_id = {entity.entity_id: entity for entity in batch}
        inputs: list[OntologyEntity] = []
        for entity_id in (relationship.from_entity_id, relationship.to_entity_id):
            endpoint = by_id.get(entity_id) or await self.get_entity(entity_id)
            if endpoint is not None:
                inputs.append(endpoint)
        if not inputs:
            return relationship
        derivation = weakest_derivation_kind(
            [relationship.derivation_kind, *(item.derivation_kind for item in inputs)]
        )
        review_status = weakest_review_status(
            [relationship.review_status, *(item.review_status for item in inputs)]
        )
        return replace(
            relationship,
            derivation_kind=derivation if derivation is not None else relationship.derivation_kind,
            review_status=(
                review_status if review_status is not None else relationship.review_status
            ),
            confidence=min(relationship.confidence, *(item.confidence for item in inputs)),
        )

    async def mark_stale_in_transaction(
        self, room_id: str, target_ids: list[str], stale_at_sequence: int
    ) -> list[str]:
        """Mark superseded assertions, never delete them: a removed one cannot be audited."""
        if not self.db.owns_current_transaction:
            raise RuntimeError("ontology staleness marking requires transaction ownership")
        statements = (
            "UPDATE ontology_entities SET stale_at_sequence = ? "
            "WHERE room_id = ? AND entity_id = ? AND stale_at_sequence IS NULL",
            "UPDATE ontology_relationships SET stale_at_sequence = ? "
            "WHERE room_id = ? AND relationship_id = ? AND stale_at_sequence IS NULL",
        )
        marked: list[str] = []
        for target_id in target_ids:
            for statement in statements:
                cursor = await self.db.execute(statement, (stale_at_sequence, room_id, target_id))
                if cursor.rowcount > 0:
                    marked.append(target_id)
        return marked

    async def invalidating_sequences(
        self, room_id: str, event_types: Sequence[str], after_sequence: int
    ) -> list[int]:
        """The events of one invalidation class that landed after a given sequence.

        Asked of the log as a question about those events. Counting a fetched page
        of the room's history instead answered only for the events that page
        happened to hold, so every assertion past the page reported itself current.
        """
        if not event_types:
            return []
        placeholders = ", ".join("?" for _ in event_types)
        rows = await self.db.fetch_all(
            "SELECT sequence FROM room_events WHERE room_id = ? "
            f"AND event_type IN ({placeholders}) AND sequence > ? ORDER BY sequence",
            (room_id, *event_types, after_sequence),
        )
        return [int(row["sequence"]) for row in rows]

    async def get_cursor(
        self, room_id: str, extractor: OntologyExtractor
    ) -> OntologyExtractionCursor | None:
        row = await self.db.fetch_one(
            "SELECT * FROM ontology_extraction_cursors WHERE room_id = ? AND extractor = ?",
            (room_id, extractor.value),
        )
        if row is None:
            return None
        return OntologyExtractionCursor(
            room_id=row["room_id"],
            extractor=OntologyExtractor(row["extractor"]),
            last_sequence=int(row["last_sequence"]),
            last_run_at=row["last_run_at"],
        )

    async def advance_cursor_in_transaction(
        self,
        room_id: str,
        extractor: OntologyExtractor,
        from_sequence: int,
        to_sequence: int,
        at: datetime,
    ) -> None:
        """Compare-and-swap the resume hint; a trigger stops it regressing anyway."""
        if not self.db.owns_current_transaction:
            raise RuntimeError("ontology cursor advance requires transaction ownership")
        cursor = await self.db.execute(
            "INSERT INTO ontology_extraction_cursors(room_id, extractor, last_sequence, "
            "last_run_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(room_id, extractor) DO UPDATE SET last_sequence = excluded.last_sequence, "
            "last_run_at = excluded.last_run_at "
            "WHERE ontology_extraction_cursors.last_sequence = ?",
            (
                room_id,
                extractor.value,
                to_sequence,
                serialize_datetime(at),
                from_sequence,
            ),
        )
        if cursor.rowcount < 1:
            raise DomainError("ontology extraction cursor moved under this pass")

    async def get_entity(self, entity_id: str) -> OntologyEntity | None:
        row = await self.db.fetch_one(
            "SELECT * FROM ontology_entities WHERE entity_id = ?", (entity_id,)
        )
        return None if row is None else self._entity_from_row(row)

    async def get_entity_by_source(
        self, room_id: str, kind: OntologyEntityKind, source_object_id: str
    ) -> OntologyEntity | None:
        """Resolve a single room-scoped projection node by its durable source ID."""
        row = await self.db.fetch_one(
            "SELECT * FROM ontology_entities "
            "WHERE room_id = ? AND kind = ? AND source_object_id = ? LIMIT 1",
            (room_id, kind.value, source_object_id),
        )
        return None if row is None else self._entity_from_row(row)

    async def get_latest_review(self, room_id: str, target_id: str) -> OntologyReview | None:
        row = await self.db.fetch_one(
            "SELECT * FROM ontology_reviews WHERE room_id = ? AND target_id = ? "
            "ORDER BY created_at DESC, review_id DESC LIMIT 1",
            (room_id, target_id),
        )
        return None if row is None else self._review_from_row(row)

    async def list_entities(self, room_id: str) -> list[OntologyEntity]:
        rows = await self.db.fetch_all(
            "SELECT * FROM ontology_entities WHERE room_id = ? ORDER BY created_at, entity_id",
            (room_id,),
        )
        return [self._entity_from_row(row) for row in rows]

    async def list_relationships(self, room_id: str) -> list[OntologyRelationship]:
        rows = await self.db.fetch_all(
            "SELECT * FROM ontology_relationships WHERE room_id = ? "
            "ORDER BY created_at, relationship_id",
            (room_id,),
        )
        return [self._relationship_from_row(row) for row in rows]

    async def get_relationship(self, relationship_id: str) -> OntologyRelationship | None:
        row = await self.db.fetch_one(
            "SELECT * FROM ontology_relationships WHERE relationship_id = ?",
            (relationship_id,),
        )
        return None if row is None else self._relationship_from_row(row)

    async def get_relationship_between(
        self, room_id: str, from_entity_id: str, to_entity_id: str
    ) -> OntologyRelationship | None:
        row = await self.db.fetch_one(
            "SELECT * FROM ontology_relationships WHERE room_id = ? "
            "AND from_entity_id = ? AND to_entity_id = ? "
            "ORDER BY updated_at DESC, relationship_id LIMIT 1",
            (room_id, from_entity_id, to_entity_id),
        )
        return None if row is None else self._relationship_from_row(row)

    async def list_reviews(self, room_id: str) -> list[OntologyReview]:
        rows = await self.db.fetch_all(
            "SELECT * FROM ontology_reviews WHERE room_id = ? ORDER BY created_at, review_id",
            (room_id,),
        )
        return [self._review_from_row(row) for row in rows]

    async def review_entity_in_transaction(
        self,
        entity: OntologyEntity,
        review_id: str,
        action: OntologyReviewAction,
        reviewed_by: str,
        reason: str,
        *,
        corrected_label: str | None,
        corrected_properties: dict[str, Any] | None,
        corrected_confidence: float | None,
        reviewed_at: datetime,
    ) -> tuple[OntologyEntity, OntologyReview]:
        if not self.db.owns_current_transaction:
            raise RuntimeError("ontology review requires transaction ownership")
        before = self._entity_value(entity)
        status = (
            OntologyReviewStatus.CONFIRMED
            if action == OntologyReviewAction.CONFIRM
            else OntologyReviewStatus.CORRECTED
        )
        updated = replace(
            entity,
            label=corrected_label if corrected_label is not None else entity.label,
            properties=(
                corrected_properties if corrected_properties is not None else entity.properties
            ),
            confidence=(
                corrected_confidence if corrected_confidence is not None else entity.confidence
            ),
            review_status=status,
            updated_at=reviewed_at,
        )
        after = self._entity_value(updated)
        review = OntologyReview(
            review_id=review_id,
            room_id=entity.room_id,
            target_type=OntologyReviewTarget.ENTITY,
            target_id=entity.entity_id,
            action=action,
            before_value=before,
            after_value=after,
            reason=reason,
            reviewed_by=reviewed_by,
            created_at=reviewed_at,
        )
        await self.db.execute(
            "UPDATE ontology_entities SET label = ?, properties = ?, confidence = ?, "
            "review_status = ?, updated_at = ? "
            "WHERE entity_id = ? AND room_id = ?",
            (
                updated.label,
                json.dumps(updated.properties, sort_keys=True),
                updated.confidence,
                updated.review_status.value,
                serialize_datetime(updated.updated_at),
                updated.entity_id,
                updated.room_id,
            ),
        )
        await self.db.execute(
            "INSERT INTO ontology_reviews("
            "review_id, room_id, target_type, target_id, action, before_value, "
            "after_value, reason, "
            "reviewed_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                review.review_id,
                review.room_id,
                review.target_type.value,
                review.target_id,
                review.action.value,
                json.dumps(review.before_value, sort_keys=True),
                json.dumps(review.after_value, sort_keys=True),
                review.reason,
                review.reviewed_by,
                serialize_datetime(review.created_at),
            ),
        )
        return updated, review

    async def review_relationship_in_transaction(
        self,
        relationship: OntologyRelationship,
        review_id: str,
        action: OntologyReviewAction,
        reviewed_by: str,
        reason: str,
        *,
        corrected_kind: OntologyRelationshipKind | None,
        corrected_confidence: float | None,
        reviewed_at: datetime,
    ) -> tuple[OntologyRelationship, OntologyReview]:
        if not self.db.owns_current_transaction:
            raise RuntimeError("ontology review requires transaction ownership")
        before = self._relationship_value(relationship)
        status = (
            OntologyReviewStatus.CONFIRMED
            if action == OntologyReviewAction.CONFIRM
            else OntologyReviewStatus.CORRECTED
        )
        updated = replace(
            relationship,
            kind=corrected_kind if corrected_kind is not None else relationship.kind,
            confidence=(
                corrected_confidence
                if corrected_confidence is not None
                else relationship.confidence
            ),
            review_status=status,
            updated_at=reviewed_at,
        )
        after = self._relationship_value(updated)
        review = OntologyReview(
            review_id=review_id,
            room_id=relationship.room_id,
            target_type=OntologyReviewTarget.RELATIONSHIP,
            target_id=relationship.relationship_id,
            action=action,
            before_value=before,
            after_value=after,
            reason=reason,
            reviewed_by=reviewed_by,
            created_at=reviewed_at,
        )
        await self.db.execute(
            "UPDATE ontology_relationships SET kind = ?, confidence = ?, review_status = ?, "
            "updated_at = ? WHERE relationship_id = ? AND room_id = ?",
            (
                updated.kind.value,
                updated.confidence,
                updated.review_status.value,
                serialize_datetime(updated.updated_at),
                updated.relationship_id,
                updated.room_id,
            ),
        )
        await self.db.execute(
            "INSERT INTO ontology_reviews("
            "review_id, room_id, target_type, target_id, action, before_value, after_value, "
            "reason, reviewed_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                review.review_id,
                review.room_id,
                review.target_type.value,
                review.target_id,
                review.action.value,
                json.dumps(review.before_value, sort_keys=True),
                json.dumps(review.after_value, sort_keys=True),
                review.reason,
                review.reviewed_by,
                serialize_datetime(review.created_at),
            ),
        )
        return updated, review

    @staticmethod
    def _entity_value(entity: OntologyEntity) -> dict[str, Any]:
        return {
            "label": entity.label,
            "properties": entity.properties,
            "confidence": entity.confidence,
            "review_status": entity.review_status.value,
        }

    @staticmethod
    def _relationship_value(relationship: OntologyRelationship) -> dict[str, Any]:
        return {
            "kind": relationship.kind.value,
            "from_entity_id": relationship.from_entity_id,
            "to_entity_id": relationship.to_entity_id,
            "confidence": relationship.confidence,
            "review_status": relationship.review_status.value,
        }

    @staticmethod
    def _json_tuple(value: str) -> tuple[str, ...]:
        parsed = json.loads(value)
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise ValueError("ontology identifier list is malformed")
        return tuple(parsed)

    @staticmethod
    def _json_sequences(value: str) -> tuple[int, ...]:
        parsed = json.loads(value)
        if not isinstance(parsed, list) or not all(isinstance(item, int) for item in parsed):
            raise ValueError("ontology evidence sequence list is malformed")
        return tuple(parsed)

    @classmethod
    def _entity_from_row(cls, row: dict[str, Any]) -> OntologyEntity:
        properties = json.loads(row["properties"])
        if not isinstance(properties, dict):
            raise ValueError("ontology entity properties are malformed")
        stale = row["stale_at_sequence"]
        return OntologyEntity(
            entity_id=row["entity_id"],
            room_id=row["room_id"],
            kind=OntologyEntityKind(row["kind"]),
            source_object_id=row["source_object_id"],
            label=row["label"],
            properties=properties,
            derivation_kind=OntologyDerivationKind(row["derivation_kind"]),
            confidence=float(row["confidence"]),
            evidence_ids=cls._json_tuple(row["evidence_ids"]),
            source_ids=cls._json_tuple(row["source_ids"]),
            review_status=OntologyReviewStatus(row["review_status"]),
            extractor=OntologyExtractor(row["extractor"]),
            asserted_at_sequence=int(row["asserted_at_sequence"]),
            evidence_event_sequences=cls._json_sequences(row["evidence_event_sequences"]),
            stale_at_sequence=None if stale is None else int(stale),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @classmethod
    def _relationship_from_row(cls, row: dict[str, Any]) -> OntologyRelationship:
        stale = row["stale_at_sequence"]
        return OntologyRelationship(
            relationship_id=row["relationship_id"],
            room_id=row["room_id"],
            kind=OntologyRelationshipKind(row["kind"]),
            from_entity_id=row["from_entity_id"],
            to_entity_id=row["to_entity_id"],
            derivation_kind=OntologyDerivationKind(row["derivation_kind"]),
            confidence=float(row["confidence"]),
            evidence_ids=cls._json_tuple(row["evidence_ids"]),
            source_ids=cls._json_tuple(row["source_ids"]),
            review_status=OntologyReviewStatus(row["review_status"]),
            source_object_kind=row["source_object_kind"],
            source_object_id=row["source_object_id"],
            extractor=OntologyExtractor(row["extractor"]),
            asserted_at_sequence=int(row["asserted_at_sequence"]),
            evidence_event_sequences=cls._json_sequences(row["evidence_event_sequences"]),
            stale_at_sequence=None if stale is None else int(stale),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _review_from_row(row: dict[str, Any]) -> OntologyReview:
        before = json.loads(row["before_value"])
        after = json.loads(row["after_value"])
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise ValueError("ontology review snapshot is malformed")
        return OntologyReview(
            review_id=row["review_id"],
            room_id=row["room_id"],
            target_type=OntologyReviewTarget(row["target_type"]),
            target_id=row["target_id"],
            action=OntologyReviewAction(row["action"]),
            before_value=before,
            after_value=after,
            reason=row["reason"],
            reviewed_by=row["reviewed_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )


class MetaRepo:
    """Every read a Meta answer makes, authorized inside the SQL that makes it.

    Existence-only membership is not authorization: ``room_members.role`` has no
    CHECK, so a row bearing any role string — including one the policy grants
    nothing for — satisfies ``m.user_id = :user_id`` and reads the room. Every
    statement below therefore carries the role predicate too, expanded from
    ``roles_with_capability(READ)`` rather than copied beside the policy, so an
    unrecognized role yields zero rows and a missing membership row yields zero
    rows without a forgettable Python branch.

    Aggregates are here for the same reason as rows: a freshness head or an unread
    count over content the asker may not read leaks that content's existence and
    rate, so it is computed inside the authorized scope or not at all.
    """

    _READING_ROLES = roles_with_capability(RoomCapability.READ)

    def __init__(self, db: Database) -> None:
        self.db = db

    @classmethod
    def _authorized(cls, select: str, alias: str, tail: str) -> str:
        """Build a Meta statement. There is no way through here without the join."""
        roles = ", ".join("?" for _ in cls._READING_ROLES)
        return (
            f"{select} JOIN room_members m ON m.room_id = {alias}.room_id "
            f"AND m.user_id = ? AND m.role IN ({roles}) {tail}"
        )

    def _params(self, user_id: str, *rest: Any) -> tuple[Any, ...]:
        return (user_id, *self._READING_ROLES, *rest)

    async def head(self, room_id: str, user_id: str) -> int | None:
        """The room's latest sequence as this reader may see it; None when they may not."""
        sql = self._authorized(
            "SELECT MAX(e.sequence) AS head FROM room_events e", "e", "WHERE e.room_id = ?"
        )
        row = await self.db.fetch_one(sql, self._params(user_id, room_id))
        head = None if row is None else row["head"]
        return None if head is None else int(head)

    async def entities(
        self,
        room_id: str,
        user_id: str,
        kinds: Sequence[OntologyEntityKind],
        *,
        since_sequence: int | None = None,
        statuses: Sequence[str] = (),
        limit: int = 50,
    ) -> list[OntologyEntity]:
        if not kinds:
            return []
        placeholders = ", ".join("?" for _ in kinds)
        tail = f"WHERE e.room_id = ? AND e.kind IN ({placeholders})"
        params: list[Any] = [room_id, *(kind.value for kind in kinds)]
        if since_sequence is not None:
            tail += " AND e.asserted_at_sequence > ?"
            params.append(since_sequence)
        if statuses:
            # Two questions that read the same entity kind and mean opposite things
            # are separated here, before the limit, so neither is a filtered copy of
            # the other's page.
            #
            # The bucket follows a source. The decision row states its own status, so
            # that is what is read; the assertion's copy answers only when no row
            # states it any more. A marker recorded by an earlier pass answers
            # nothing: it went on saying "active" after the row had gone back to
            # proposed, and the decision sat on the made list for ever.
            #
            # Only the decision kinds are asked for a status, so only the decision
            # row is joined. The Python side resolves the same status the same way.
            status_placeholders = ", ".join("?" for _ in statuses)
            tail += (
                " AND COALESCE("
                "(SELECT d.status FROM decisions d WHERE d.decision_id = e.source_object_id "
                "AND d.room_id = e.room_id), "
                "json_extract(e.properties, '$.status')"
                f") IN ({status_placeholders})"
            )
            params.extend(statuses)
        tail += " ORDER BY e.asserted_at_sequence, e.entity_id LIMIT ?"
        params.append(limit)
        sql = self._authorized("SELECT e.* FROM ontology_entities e", "e", tail)
        rows = await self.db.fetch_all(sql, self._params(user_id, *params))
        return [OntologyRepo._entity_from_row(row) for row in rows]

    async def relationships(
        self,
        room_id: str,
        user_id: str,
        kinds: Sequence[OntologyRelationshipKind],
        *,
        since_sequence: int | None = None,
        limit: int = 50,
    ) -> list[OntologyRelationship]:
        if not kinds:
            return []
        placeholders = ", ".join("?" for _ in kinds)
        tail = f"WHERE r.room_id = ? AND r.kind IN ({placeholders})"
        params: list[Any] = [room_id, *(kind.value for kind in kinds)]
        if since_sequence is not None:
            tail += " AND r.asserted_at_sequence > ?"
            params.append(since_sequence)
        tail += " ORDER BY r.asserted_at_sequence, r.relationship_id LIMIT ?"
        params.append(limit)
        sql = self._authorized("SELECT r.* FROM ontology_relationships r", "r", tail)
        rows = await self.db.fetch_all(sql, self._params(user_id, *params))
        return [OntologyRepo._relationship_from_row(row) for row in rows]

    async def entities_by_ids(
        self, room_id: str, user_id: str, entity_ids: Sequence[str]
    ) -> list[OntologyEntity]:
        if not entity_ids:
            return []
        placeholders = ", ".join("?" for _ in entity_ids)
        sql = self._authorized(
            "SELECT e.* FROM ontology_entities e",
            "e",
            f"WHERE e.room_id = ? AND e.entity_id IN ({placeholders})",
        )
        rows = await self.db.fetch_all(sql, self._params(user_id, room_id, *entity_ids))
        return [OntologyRepo._entity_from_row(row) for row in rows]

    async def entity_by_source(
        self, room_id: str, user_id: str, kind: OntologyEntityKind, source_object_id: str
    ) -> OntologyEntity | None:
        sql = self._authorized(
            "SELECT e.* FROM ontology_entities e",
            "e",
            "WHERE e.room_id = ? AND e.kind = ? AND e.source_object_id = ? LIMIT 1",
        )
        row = await self.db.fetch_one(
            sql, self._params(user_id, room_id, kind.value, source_object_id)
        )
        return None if row is None else OntologyRepo._entity_from_row(row)

    async def relationship_between(
        self, room_id: str, user_id: str, from_entity_id: str, to_entity_id: str
    ) -> OntologyRelationship | None:
        sql = self._authorized(
            "SELECT r.* FROM ontology_relationships r",
            "r",
            "WHERE r.room_id = ? AND r.from_entity_id = ? AND r.to_entity_id = ? "
            "ORDER BY r.updated_at DESC, r.relationship_id LIMIT 1",
        )
        row = await self.db.fetch_one(
            sql, self._params(user_id, room_id, from_entity_id, to_entity_id)
        )
        return None if row is None else OntologyRepo._relationship_from_row(row)

    async def latest_review(
        self, room_id: str, user_id: str, target_id: str
    ) -> OntologyReview | None:
        sql = self._authorized(
            "SELECT v.* FROM ontology_reviews v",
            "v",
            "WHERE v.room_id = ? AND v.target_id = ? "
            "ORDER BY v.created_at DESC, v.review_id DESC LIMIT 1",
        )
        row = await self.db.fetch_one(sql, self._params(user_id, room_id, target_id))
        return None if row is None else OntologyRepo._review_from_row(row)

    async def invalidating_sequences(
        self,
        room_id: str,
        user_id: str,
        event_types: Sequence[str],
        after_sequence: int,
        head: int,
    ) -> list[int]:
        """One grouped read per invalidation class per answer, never one per claim."""
        if not event_types:
            return []
        placeholders = ", ".join("?" for _ in event_types)
        sql = self._authorized(
            "SELECT e.sequence AS sequence FROM room_events e",
            "e",
            f"WHERE e.room_id = ? AND e.event_type IN ({placeholders}) "
            "AND e.sequence > ? AND e.sequence <= ? ORDER BY e.sequence",
        )
        rows = await self.db.fetch_all(
            sql, self._params(user_id, room_id, *event_types, after_sequence, head)
        )
        return [int(row["sequence"]) for row in rows]

    async def extraction_cursors(self, room_id: str, user_id: str) -> dict[str, int]:
        """Where each extractor resumes. A reader sees pending work; it decides nothing."""
        sql = self._authorized(
            "SELECT c.extractor AS extractor, c.last_sequence AS last_sequence "
            "FROM ontology_extraction_cursors c",
            "c",
            "WHERE c.room_id = ?",
        )
        rows = await self.db.fetch_all(sql, self._params(user_id, room_id))
        return {str(row["extractor"]): int(row["last_sequence"]) for row in rows}


class DecisionRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, decision: Decision) -> Decision:
        await self.db.execute(
            "INSERT INTO decisions(decision_id, room_id, title, content, reason, status, "
            "created_by, reviewed_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                decision.decision_id,
                decision.room_id,
                decision.title,
                decision.content,
                decision.reason,
                decision.status.value,
                decision.created_by,
                decision.reviewed_by,
                serialize_datetime(decision.created_at),
            ),
        )
        # Title and content, which the decision list returns to any reader of the
        # room. reason is deliberately excluded: it is the deliberation behind the
        # call, no read path returns it, and a snippet would surface it stripped of
        # the decision it belongs to.
        await SearchRepo(self.db).index(
            SearchObjectKind.DECISION,
            decision.decision_id,
            decision.room_id,
            decision.created_by,
            f"{decision.title}\n{decision.content}",
            decision.created_at,
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

    async def update_status(
        self, decision_id: str, status: DecisionStatus, reviewed_by: str = ""
    ) -> None:
        await self.db.execute(
            "UPDATE decisions SET status = ?, reviewed_by = ? WHERE decision_id = ?",
            (status.value, reviewed_by, decision_id),
        )
        await self.db.commit()

    def _from_row(self, row: dict[str, Any]) -> Decision:
        return Decision(
            decision_id=row["decision_id"],
            room_id=row["room_id"],
            title=row["title"],
            content=row["content"],
            reason=row["reason"],
            status=DecisionStatus(row["status"]),
            created_by=row["created_by"],
            reviewed_by=row["reviewed_by"],
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
            (
                memory.memory_id,
                memory.room_id,
                memory.workspace_id,
                memory.org_id,
                memory.scope.value,
                memory.content,
                memory.memory_type,
                int(memory.is_authoritative),
                memory.superseded_by,
                memory.created_by,
                serialize_datetime(memory.created_at),
            ),
        )
        await self.db.commit()
        return memory

    async def list_by_room(self, room_id: str) -> list[Memory]:
        rows = await self.db.fetch_all(
            "SELECT * FROM memories WHERE room_id = ? AND superseded_by IS NULL "
            "ORDER BY created_at",
            (room_id,),
        )
        return [self._from_row(r) for r in rows]

    async def list_by_workspace(self, workspace_id: str) -> list[Memory]:
        rows = await self.db.fetch_all(
            "SELECT * FROM memories WHERE workspace_id = ? AND superseded_by IS NULL "
            "ORDER BY created_at",
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
            memory_id=row["memory_id"],
            room_id=row.get("room_id"),
            workspace_id=row.get("workspace_id"),
            org_id=row.get("org_id"),
            scope=MemoryScope(row["scope"]),
            content=row["content"],
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
            "action_description, authorized_by, status, reviewer_id, review_comment, "
            "requested_at, reviewed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                approval.approval_id,
                approval.room_id,
                approval.execution_id,
                approval.agent_id,
                approval.action_description,
                approval.authorized_by,
                approval.status.value,
                approval.reviewer_id,
                approval.review_comment,
                serialize_datetime(approval.requested_at),
                serialize_datetime(approval.reviewed_at),
            ),
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
            "SELECT * FROM approvals WHERE room_id = ? AND status = 'PENDING' "
            "ORDER BY requested_at",
            (room_id,),
        )
        return [self._from_row(r) for r in rows]

    async def list_pending_by_execution(self, execution_id: str) -> list[Approval]:
        """The undecided approvals of one run, so settling it can close them too."""
        rows = await self.db.fetch_all(
            "SELECT * FROM approvals WHERE execution_id = ? AND status = 'PENDING' "
            "ORDER BY requested_at",
            (execution_id,),
        )
        return [self._from_row(r) for r in rows]

    async def update(self, approval: Approval) -> Approval:
        await self.db.execute(
            "UPDATE approvals SET status = ?, reviewer_id = ?, review_comment = ?, reviewed_at = ? "
            "WHERE approval_id = ?",
            (
                approval.status.value,
                approval.reviewer_id,
                approval.review_comment,
                serialize_datetime(approval.reviewed_at),
                approval.approval_id,
            ),
        )
        await self.db.commit()
        return approval

    def _from_row(self, row: dict[str, Any]) -> Approval:
        return Approval(
            approval_id=row["approval_id"],
            room_id=row["room_id"],
            execution_id=row["execution_id"],
            agent_id=row["agent_id"],
            action_description=row["action_description"],
            authorized_by=row.get("authorized_by") or "",
            status=ApprovalStatus(row["status"]),
            reviewer_id=row.get("reviewer_id"),
            review_comment=row["review_comment"],
            requested_at=datetime.fromisoformat(row["requested_at"]),
            reviewed_at=datetime.fromisoformat(row["reviewed_at"])
            if row.get("reviewed_at")
            else None,
        )


class NotificationRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, notification: Notification) -> Notification:
        await self.db.execute(
            "INSERT INTO notifications(notification_id, user_id, room_id, title, body, "
            "notification_type, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                notification.notification_id,
                notification.user_id,
                notification.room_id,
                notification.title,
                notification.body,
                notification.notification_type,
                notification.status.value,
                serialize_datetime(notification.created_at),
            ),
        )
        await self.db.commit()
        return notification

    async def list_unread(self, user_id: str) -> list[Notification]:
        rows = await self.db.fetch_all(
            "SELECT * FROM notifications WHERE user_id = ? AND status = 'UNREAD' "
            "ORDER BY created_at DESC",
            (user_id,),
        )
        return [
            Notification(
                notification_id=r["notification_id"],
                user_id=r["user_id"],
                room_id=r.get("room_id"),
                title=r["title"],
                body=r["body"],
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


class IdempotencyRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(
        self, scope_id: str, user_id: str, idempotency_key: str
    ) -> IdempotencyRecord | None:
        row = await self.db.fetch_one(
            "SELECT * FROM idempotency_keys "
            "WHERE scope_id = ? AND user_id = ? AND idempotency_key = ?",
            (scope_id, user_id, idempotency_key),
        )
        if row is None:
            return None
        return IdempotencyRecord(
            scope_id=row["scope_id"],
            user_id=row["user_id"],
            idempotency_key=row["idempotency_key"],
            operation=row["operation"],
            request_hash=row["request_hash"],
            result_ref=row["result_ref"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    async def create_in_transaction(self, record: IdempotencyRecord) -> None:
        """Claim a key while the caller owns the transaction that produces its result."""
        if not self.db.owns_current_transaction:
            raise RuntimeError("idempotency claim requires transaction ownership")
        await self.db.execute(
            "INSERT INTO idempotency_keys(scope_id, user_id, idempotency_key, operation, "
            "request_hash, result_ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                record.scope_id,
                record.user_id,
                record.idempotency_key,
                record.operation,
                record.request_hash,
                record.result_ref,
                serialize_datetime(record.created_at),
            ),
        )


class ToolPermissionRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, perm: ToolPermission) -> ToolPermission:
        await self.db.execute(
            "INSERT INTO tool_permissions(permission_id, agent_id, room_id, tool_name, "
            "allowed, requires_approval, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                perm.permission_id,
                perm.agent_id,
                perm.room_id,
                perm.tool_name,
                int(perm.allowed),
                int(perm.requires_approval),
                serialize_datetime(perm.created_at),
            ),
        )
        await self.db.commit()
        return perm

    async def get(self, agent_id: str, room_id: str, tool_name: str) -> ToolPermission | None:
        row = await self.db.fetch_one(
            "SELECT * FROM tool_permissions WHERE agent_id = ? AND room_id = ? AND tool_name = ?",
            (agent_id, room_id, tool_name),
        )
        return (
            None
            if row is None
            else ToolPermission(
                permission_id=row["permission_id"],
                agent_id=row["agent_id"],
                room_id=row["room_id"],
                tool_name=row["tool_name"],
                allowed=bool(row["allowed"]),
                requires_approval=bool(row["requires_approval"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
        )

    async def list_by_agent_room(self, agent_id: str, room_id: str) -> list[ToolPermission]:
        rows = await self.db.fetch_all(
            "SELECT * FROM tool_permissions WHERE agent_id = ? AND room_id = ?",
            (agent_id, room_id),
        )
        return [
            ToolPermission(
                permission_id=r["permission_id"],
                agent_id=r["agent_id"],
                room_id=r["room_id"],
                tool_name=r["tool_name"],
                allowed=bool(r["allowed"]),
                requires_approval=bool(r["requires_approval"]),
                created_at=datetime.fromisoformat(r["created_at"]),
            )
            for r in rows
        ]
