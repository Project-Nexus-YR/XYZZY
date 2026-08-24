"""FastAPI routes for the multiplayer AI workspace."""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from ..domain.meta import MetaQuestionKind
from ..domain.models import (
    AddressingMode,
    AgentAddressing,
    AgentInstance,
    Approval,
    Artifact,
    ArtifactType,
    ArtifactVersion,
    Branch,
    BranchMode,
    DomainError,
    Execution,
    IdempotencyConflict,
    MemoryScope,
    Message,
    MessageRole,
    OntologyExtractor,
    OntologyRelationshipKind,
    OntologyReviewAction,
    OutputDisposition,
    Session,
    Task,
    TaskPriority,
)
from ..domain.provenance import (
    normalize_provenance_author,
    normalize_provenance_timestamp,
)
from ..domain.synthesis import SynthesisType
from ..security import (
    AuthenticatedUser,
    AuthenticationError,
    AuthorizationError,
    RoomCapability,
    TokenAuthenticator,
    allowed_tools,
)
from ..services.service import MultiplayerService

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["multiplayer"])

# ── Service reference (set at startup) ───────────────────────────────────────
_svc: MultiplayerService | None = None
_authenticator: TokenAuthenticator | None = None


def set_service(svc: MultiplayerService | None) -> None:
    global _svc
    _svc = svc


def set_authenticator(authenticator: TokenAuthenticator | None) -> None:
    global _authenticator
    _authenticator = authenticator


def _svc_or_404() -> MultiplayerService:
    if _svc is None:
        raise HTTPException(503, "service not ready")
    return _svc


def _current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> AuthenticatedUser:
    if _authenticator is None:
        raise HTTPException(503, "authentication service not ready")
    try:
        return _authenticator.authenticate(authorization)
    except AuthenticationError as exc:
        raise HTTPException(
            401,
            "authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


CurrentUser = Annotated[AuthenticatedUser, Depends(_current_user)]
# Optional `Idempotency-Key` header: a retried write with the same key replays
# the original result instead of appending a second ordered event.
IdempotencyKey = Annotated[str | None, Header()]


async def _require_room(
    room_id: str,
    principal: AuthenticatedUser,
    capability: RoomCapability,
) -> None:
    try:
        await _svc_or_404().authorization.require(room_id, principal.user_id, capability)
    except AuthorizationError as exc:
        raise HTTPException(403, "room access forbidden") from exc


async def _require_workspace(workspace_id: str, principal: AuthenticatedUser) -> None:
    try:
        await _svc_or_404().authorization.require_workspace_member(workspace_id, principal.user_id)
    except AuthorizationError as exc:
        raise HTTPException(403, "workspace access forbidden") from exc


async def _require_org(org_id: str, principal: AuthenticatedUser) -> None:
    try:
        await _svc_or_404().authorization.require_org_member(org_id, principal.user_id)
    except AuthorizationError as exc:
        raise HTTPException(403, "organization access forbidden") from exc


async def _authorized_agent(
    agent_id: str,
    principal: AuthenticatedUser,
    capability: RoomCapability,
) -> AgentInstance:
    agent = await _svc_or_404().repos.agents.get_instance(agent_id)
    if agent is None:
        raise HTTPException(404, "agent not found")
    await _require_room(agent.room_id, principal, capability)
    return agent


async def _authorized_session(
    session_id: str,
    principal: AuthenticatedUser,
    capability: RoomCapability,
) -> Session:
    session = await _svc_or_404().repos.sessions.get(session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    await _require_room(session.room_id, principal, capability)
    return session


async def _authorized_execution(
    execution_id: str,
    principal: AuthenticatedUser,
    capability: RoomCapability,
) -> Execution:
    svc = _svc_or_404()
    execution = await svc.repos.executions.get(execution_id)
    if execution is None:
        raise HTTPException(404, "execution not found")
    session = await svc.repos.sessions.get(execution.session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    await _require_room(session.room_id, principal, capability)
    return execution


async def _authorized_branch(
    branch_id: str,
    principal: AuthenticatedUser,
    capability: RoomCapability,
) -> Branch:
    try:
        branch = await _svc_or_404().get_branch(branch_id)
    except DomainError as exc:
        raise HTTPException(404, "branch not found") from exc
    await _require_room(branch.room_id, principal, capability)
    return branch


async def _authorized_task(
    task_id: str, principal: AuthenticatedUser, capability: RoomCapability
) -> Task:
    task = await _svc_or_404().repos.tasks.get(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    await _require_room(task.room_id, principal, capability)
    return task


async def _authorized_artifact(
    artifact_id: str, principal: AuthenticatedUser, capability: RoomCapability
) -> Artifact:
    artifact = await _svc_or_404().repos.artifacts.get(artifact_id)
    if artifact is None:
        raise HTTPException(404, "artifact not found")
    await _require_room(artifact.room_id, principal, capability)
    return artifact


async def _authorized_approval(
    approval_id: str, principal: AuthenticatedUser, capability: RoomCapability
) -> Approval:
    approval = await _svc_or_404().repos.approvals.get(approval_id)
    if approval is None:
        raise HTTPException(404, "approval not found")
    await _require_room(approval.room_id, principal, capability)
    return approval


# ── Request / Response models ────────────────────────────────────────────────


class CreateOrgRequest(BaseModel):
    name: str
    slug: str


class CreateWorkspaceRequest(BaseModel):
    name: str
    slug: str


class CreateRoomRequest(BaseModel):
    name: str
    description: str = ""


class BootstrapWorkspaceRequest(BaseModel):
    display_name: str
    room_name: str


class InviteRoomMemberRequest(BaseModel):
    user_id: str
    role: str = "viewer"


class UpdateRoomMemberRequest(BaseModel):
    role: str


class PolicyRequest(BaseModel):
    """A capability list, or null to lift the policy entirely."""

    allowed_capabilities: list[str] | None = None


class SpawnAgentRequest(BaseModel):
    template_id: str
    name: str | None = None
    system_prompt: str | None = None
    model_provider: str = ""
    model_name: str = ""


class SendInstructionRequest(BaseModel):
    prompt: str


class StartBranchRequest(BaseModel):
    mode: str
    prompt: str
    agent_ids: list[str]


class CreateTaskRequest(BaseModel):
    title: str
    description: str = ""
    priority: str = "NORMAL"


class AssignTaskRequest(BaseModel):
    agent_id: str


class DelegateTaskRequest(BaseModel):
    to_agent_id: str
    description: str = ""


class CreateMessageRequest(BaseModel):
    content: str
    role: str = "HUMAN"
    sender_id: str = ""
    # Mentions are derived from the content, never accepted from the client.
    # Addressing an agent only records and notifies unless this is explicitly set.
    invoke_mentioned_agents: bool = False


class CreateReplyRequest(BaseModel):
    content: str
    broadcast_to_room: bool = False
    invoke_mentioned_agents: bool = False


class ReactionRequest(BaseModel):
    emoji: str


class ReadCursorRequest(BaseModel):
    last_read_sequence: int


class CreateArtifactRequest(BaseModel):
    name: str
    artifact_type: str = "DOCUMENT"
    description: str = ""
    content: str = ""


class UpdateArtifactRequest(BaseModel):
    content: str


class CreateDecisionRequest(BaseModel):
    title: str
    content: str
    reason: str = ""


class CreateMemoryRequest(BaseModel):
    content: str
    scope: str = "ROOM"
    memory_type: str = "fact"


class RedirectAgentRequest(BaseModel):
    instruction: str


class InterruptAgentRequest(BaseModel):
    reason: str = ""


class ApproveActionRequest(BaseModel):
    comment: str = ""


class RejectActionRequest(BaseModel):
    comment: str = ""
    # A refused tool may still leave a useful turn. Either way the run does not stay
    # where it was: false settles it, true puts it back on a fresh lease.
    continue_turn: bool = False


class SetAddressingRequest(BaseModel):
    mode: str
    owner_user_id: str | None = None
    allowlist: list[str] = []


class SelectOutputRequest(BaseModel):
    disposition: str


class SynthesizeDecisionBriefRequest(BaseModel):
    title: str = "Authentication migration decision"


class SynthesizeBranchRequest(BaseModel):
    title: str = "Authentication migration decision"
    synthesis_type: str = SynthesisType.DECISION_BRIEF.value


class RunOntologyExtractionRequest(BaseModel):
    extractor: str


class ReviewOntologyEntityRequest(BaseModel):
    action: str
    reason: str = ""
    corrected_label: str | None = None
    corrected_properties: dict[str, Any] | None = None
    corrected_confidence: float | None = None


class ReviewOntologyRelationshipRequest(BaseModel):
    action: str
    reason: str = ""
    corrected_kind: str | None = None
    corrected_confidence: float | None = None


# ── Helpers ──────────────────────────────────────────────────────────────────


EnumT = TypeVar("EnumT", bound=StrEnum)


def _safe_enum(value: str, enum_cls: type[EnumT], name: str) -> EnumT:
    """Convert a string to an enum value, returning 400 on failure."""
    try:
        return enum_cls(value)
    except ValueError as exc:
        valid = [e.value for e in enum_cls]
        raise HTTPException(400, f"invalid {name}: '{value}'. valid: {valid}") from exc


# ── Health ───────────────────────────────────────────────────────────────────


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ── Current principal ────────────────────────────────────────────────────────


@router.get("/me/context")
async def get_my_context(
    principal: CurrentUser,
) -> dict[str, Any]:
    """Return only collaboration boundaries visible to the bearer principal."""
    organizations, workspaces, rooms = await _svc_or_404().get_user_context(principal.user_id)
    return {
        "user_id": principal.user_id,
        "organizations": [
            {"org_id": org.org_id, "name": org.name, "slug": org.slug} for org in organizations
        ],
        "workspaces": [
            {
                "workspace_id": workspace.workspace_id,
                "org_id": workspace.org_id,
                "name": workspace.name,
                "slug": workspace.slug,
            }
            for workspace in workspaces
        ],
        "rooms": [
            {
                "room_id": room.room_id,
                "workspace_id": room.workspace_id,
                "name": room.name,
                "description": room.description,
                "status": room.status.value,
            }
            for room in rooms
        ],
    }


@router.post("/me/bootstrap")
async def bootstrap_my_workspace(
    req: BootstrapWorkspaceRequest,
    principal: CurrentUser,
) -> dict[str, Any]:
    """Idempotently establish one stable first collaboration boundary."""
    try:
        organization, workspace, room = await _svc_or_404().bootstrap_user_workspace(
            principal.user_id,
            req.display_name,
            req.room_name,
        )
    except DomainError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "organization": {
            "org_id": organization.org_id,
            "name": organization.name,
            "slug": organization.slug,
        },
        "workspace": {
            "workspace_id": workspace.workspace_id,
            "org_id": workspace.org_id,
            "name": workspace.name,
            "slug": workspace.slug,
        },
        "room": {
            "room_id": room.room_id,
            "workspace_id": room.workspace_id,
            "name": room.name,
            "description": room.description,
            "status": room.status.value,
        },
    }


# ── Organizations ────────────────────────────────────────────────────────────


@router.post("/organizations")
async def create_organization(
    req: CreateOrgRequest,
    principal: CurrentUser,
) -> dict[str, Any]:
    svc = _svc_or_404()
    try:
        org = await svc.create_organization(req.name, req.slug, principal.user_id)
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"org_id": org.org_id, "name": org.name, "slug": org.slug}


@router.get("/organizations/{org_id}/workspaces")
async def list_workspaces(
    org_id: str,
    principal: CurrentUser,
) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    await _require_org(org_id, principal)
    workspaces = await svc.list_workspaces(org_id)
    return [{"workspace_id": w.workspace_id, "name": w.name, "slug": w.slug} for w in workspaces]


# ── Workspaces ───────────────────────────────────────────────────────────────


@router.post("/organizations/{org_id}/workspaces")
async def create_workspace(
    org_id: str, req: CreateWorkspaceRequest, principal: CurrentUser
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _require_org(org_id, principal)
    try:
        ws = await svc.create_workspace(org_id, req.name, req.slug, principal.user_id)
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"workspace_id": ws.workspace_id, "name": ws.name, "slug": ws.slug}


@router.get("/workspaces/{workspace_id}/rooms")
async def list_rooms(
    workspace_id: str,
    principal: CurrentUser,
) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    await _require_workspace(workspace_id, principal)
    rooms = await svc.list_rooms(workspace_id)
    return [
        {
            "room_id": r.room_id,
            "name": r.name,
            "description": r.description,
            "status": r.status.value,
        }
        for r in rooms
    ]


# ── Rooms ────────────────────────────────────────────────────────────────────


@router.post("/workspaces/{workspace_id}/rooms")
async def create_room(
    workspace_id: str, req: CreateRoomRequest, principal: CurrentUser
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _require_workspace(workspace_id, principal)
    try:
        room = await svc.create_room(workspace_id, req.name, principal.user_id, req.description)
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"room_id": room.room_id, "name": room.name, "description": room.description}


@router.get("/rooms/{room_id}")
async def get_room(
    room_id: str,
    principal: CurrentUser,
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.READ)
    try:
        room = await svc.get_room(room_id)
    except DomainError as e:
        raise HTTPException(404, str(e)) from e
    return {
        "room_id": room.room_id,
        "name": room.name,
        "description": room.description,
        "status": room.status.value,
        "workspace_id": room.workspace_id,
    }


@router.get("/rooms/{room_id}/state")
async def get_room_state(
    room_id: str,
    principal: CurrentUser,
    last_sequence: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Full room state for reconnect/recovery."""
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.READ)
    try:
        return await svc.get_room_state(room_id, last_sequence, principal.user_id)
    except DomainError as e:
        raise HTTPException(404, str(e)) from e


@router.post("/rooms/{room_id}/join")
async def join_room(
    room_id: str,
    principal: CurrentUser,
) -> dict[str, str]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.MUTATE)
    try:
        await svc.join_room(room_id, principal.user_id)
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"status": "joined"}


@router.post("/rooms/{room_id}/leave")
async def leave_room(
    room_id: str,
    principal: CurrentUser,
) -> dict[str, str]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.READ)
    try:
        await svc.leave_room(room_id, principal.user_id)
    except DomainError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "left"}


@router.post("/rooms/{room_id}/members/invitations")
async def invite_room_member(
    room_id: str,
    req: InviteRoomMemberRequest,
    principal: CurrentUser,
) -> dict[str, str]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.MUTATE)
    try:
        member = await svc.invite_room_member(room_id, req.user_id, req.role, principal.user_id)
    except DomainError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"user_id": member.user_id, "role": member.role}


@router.get("/rooms/{room_id}/members")
async def list_room_members(
    room_id: str,
    principal: CurrentUser,
) -> list[dict[str, str]]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.READ)
    members = await svc.get_room_members(room_id)
    return [{"user_id": m.user_id, "role": m.role} for m in members]


@router.patch("/rooms/{room_id}/members/{user_id}")
async def update_room_member(
    room_id: str,
    user_id: str,
    req: UpdateRoomMemberRequest,
    principal: CurrentUser,
) -> dict[str, str]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.ADMINISTER)
    try:
        member = await svc.update_room_member_role(room_id, user_id, req.role, principal.user_id)
    except DomainError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"user_id": member.user_id, "role": member.role}


@router.get("/rooms/{room_id}/agents/{agent_id}/capabilities")
async def agent_capabilities(
    room_id: str,
    agent_id: str,
    principal: CurrentUser,
) -> dict[str, Any]:
    """The five terms and what they permit for a run this caller would initiate."""
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.READ)
    try:
        terms = await svc.agent_capability_terms(agent_id, principal.user_id)
    except DomainError as exc:
        raise HTTPException(404, str(exc)) from exc
    effective = terms.effective
    return {
        "terms": terms.as_dict(),
        "effective": sorted(effective),
        "tools": allowed_tools(effective),
    }


@router.patch("/rooms/{room_id}/policy")
async def set_room_policy(
    room_id: str,
    req: PolicyRequest,
    principal: CurrentUser,
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.ADMINISTER)
    try:
        await svc.set_room_policy(room_id, req.allowed_capabilities, principal.user_id)
    except DomainError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"allowed_capabilities": req.allowed_capabilities}


@router.patch("/rooms/{room_id}/members/{user_id}/capabilities")
async def set_member_capabilities(
    room_id: str,
    user_id: str,
    req: PolicyRequest,
    principal: CurrentUser,
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.ADMINISTER)
    try:
        await svc.set_member_capabilities(
            room_id, user_id, req.allowed_capabilities, principal.user_id
        )
    except DomainError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"user_id": user_id, "allowed_capabilities": req.allowed_capabilities}


@router.patch("/workspaces/{workspace_id}/policy")
async def set_workspace_policy(
    workspace_id: str,
    req: PolicyRequest,
    principal: CurrentUser,
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _require_workspace(workspace_id, principal)
    try:
        await svc.set_workspace_policy(workspace_id, req.allowed_capabilities, principal.user_id)
    except DomainError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"allowed_capabilities": req.allowed_capabilities}


@router.delete("/rooms/{room_id}/members/{user_id}")
async def remove_room_member(
    room_id: str,
    user_id: str,
    principal: CurrentUser,
) -> dict[str, str]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.ADMINISTER)
    try:
        await svc.remove_room_member(room_id, user_id, principal.user_id)
    except DomainError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "removed"}


# ── Branches ────────────────────────────────────────────────────────────────


def _branch_record(branch: Branch) -> dict[str, Any]:
    return {
        "branch_id": branch.branch_id,
        "room_id": branch.room_id,
        "mode": branch.mode.value,
        "status": branch.status.value,
        "initiated_by": branch.initiated_by,
        "initiating_prompt": branch.initiating_prompt,
        "context_event_sequence": branch.context_event_sequence,
        "context_message_ids": list(branch.context_message_ids),
        "context_snapshot": branch.context_snapshot,
        "context_hash": branch.context_hash,
        "lifecycle_managed": branch.lifecycle_managed,
        "created_at": branch.created_at.isoformat(),
        "updated_at": branch.updated_at.isoformat(),
        "completed_at": branch.completed_at.isoformat() if branch.completed_at else None,
    }


def _run_record(run: Execution) -> dict[str, Any]:
    return {
        "execution_id": run.execution_id,
        "branch_id": run.branch_id,
        "session_id": run.session_id,
        "agent_id": run.agent_id,
        "run_id": run.run_id,
        "status": run.status.value,
        # Why the agent spoke, on every run a reader can see.
        "triggered_by": run.triggered_by.value,
        "started_at": run.started_at.isoformat(),
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "error": run.error,
    }


@router.post("/rooms/{room_id}/branches")
async def start_branch(
    room_id: str,
    req: StartBranchRequest,
    principal: CurrentUser,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.MUTATE)
    mode = _safe_enum(req.mode.upper(), BranchMode, "branch mode")
    try:
        branch, runs = await svc.start_branch(
            room_id,
            mode,
            req.prompt,
            principal.user_id,
            req.agent_ids,
            idempotency_key=idempotency_key,
        )
    except IdempotencyConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except DomainError as exc:
        status = 409 if "turn is locked" in str(exc) else 400
        raise HTTPException(status, str(exc)) from exc
    return {"branch": _branch_record(branch), "runs": [_run_record(run) for run in runs]}


@router.get("/rooms/{room_id}/branches")
async def list_room_branches(
    room_id: str,
    principal: CurrentUser,
) -> list[dict[str, Any]]:
    await _require_room(room_id, principal, RoomCapability.READ)
    branches = await _svc_or_404().list_room_branches(room_id)
    return [_branch_record(branch) for branch in branches]


@router.get("/branches/{branch_id}")
async def get_branch(
    branch_id: str,
    principal: CurrentUser,
) -> dict[str, Any]:
    branch = await _authorized_branch(branch_id, principal, RoomCapability.READ)
    runs = await _svc_or_404().list_branch_runs(branch_id)
    return {"branch": _branch_record(branch), "runs": [_run_record(run) for run in runs]}


@router.post("/branches/{branch_id}/runs/{execution_id}/execute")
async def execute_branch_run(
    branch_id: str,
    execution_id: str,
    principal: CurrentUser,
) -> dict[str, Any]:
    await _authorized_branch(branch_id, principal, RoomCapability.MUTATE)
    execution = await _authorized_execution(execution_id, principal, RoomCapability.MUTATE)
    if execution.branch_id != branch_id:
        raise HTTPException(404, "agent run not found in branch")
    try:
        return await _svc_or_404().execute_branch_run(branch_id, execution_id, principal.user_id)
    except DomainError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/rooms/{room_id}/events")
async def list_room_events(
    room_id: str,
    principal: CurrentUser,
    after: int = Query(0, ge=0),
) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.READ)
    events = await svc.get_room_events(room_id, after)
    return [
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
    ]


# ── Agents ───────────────────────────────────────────────────────────────────


@router.get("/agent-templates")
async def list_agent_templates(
    principal: CurrentUser,
) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    templates = await svc.list_agent_templates()
    return [
        {
            "template_id": t.template_id,
            "name": t.name,
            "description": t.description,
            "role": t.role,
            "capabilities": sorted(t.capabilities),
        }
        for t in templates
    ]


def _addressing_record(addressing: AgentAddressing) -> dict[str, Any]:
    return {
        "agent_id": addressing.agent_id,
        "room_id": addressing.room_id,
        "mode": addressing.mode.value,
        "owner_user_id": addressing.owner_user_id,
        "allowlist": sorted(addressing.allowlist),
        "updated_by": addressing.updated_by,
        "updated_at": addressing.updated_at.isoformat(),
    }


@router.post("/rooms/{room_id}/agents")
async def spawn_agent(
    room_id: str,
    req: SpawnAgentRequest,
    principal: CurrentUser,
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.MUTATE)
    try:
        agent = await svc.spawn_agent(
            room_id,
            req.template_id,
            req.name,
            req.system_prompt,
            req.model_provider,
            req.model_name,
            requested_by=principal.user_id,
            require_member=True,
        )
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {
        "agent_id": agent.agent_id,
        "name": agent.name,
        "role": agent.role,
        "status": agent.status.value,
    }


@router.get("/rooms/{room_id}/agents/{agent_id}/addressing")
async def get_agent_addressing(
    room_id: str,
    agent_id: str,
    principal: CurrentUser,
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.READ)
    try:
        addressing = await svc.get_agent_addressing(agent_id)
    except DomainError as e:
        raise HTTPException(404, str(e)) from e
    if addressing.room_id != room_id:
        raise HTTPException(404, "agent not found in room")
    return _addressing_record(addressing)


@router.patch("/rooms/{room_id}/agents/{agent_id}/addressing")
async def set_agent_addressing(
    room_id: str,
    agent_id: str,
    req: SetAddressingRequest,
    principal: CurrentUser,
) -> dict[str, Any]:
    """Who may point this agent. A grant, so it needs room ADMINISTER."""
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.ADMINISTER)
    try:
        mode = AddressingMode(req.mode)
    except ValueError as e:
        raise HTTPException(400, f"unknown addressing mode: {req.mode}") from e
    try:
        addressing = await svc.set_agent_addressing(
            agent_id,
            mode,
            principal.user_id,
            owner_user_id=req.owner_user_id,
            allowlist=frozenset(req.allowlist),
            require_member=True,
        )
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return _addressing_record(addressing)


@router.post("/rooms/{room_id}/agents/{agent_id}/identity/revocations")
async def revoke_agent_identity(
    room_id: str,
    agent_id: str,
    principal: CurrentUser,
) -> dict[str, str]:
    """Revoke once, not per run: no later run of this agent may launch."""
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.ADMINISTER)
    try:
        await svc.revoke_agent_identity(agent_id, principal.user_id, require_member=True)
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"status": "revoked"}


@router.delete("/rooms/{room_id}/agents/{agent_id}")
async def remove_agent_from_room(
    room_id: str,
    agent_id: str,
    principal: CurrentUser,
) -> dict[str, str]:
    """Take the agent out and settle everything it had in flight, deterministically."""
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.ADMINISTER)
    try:
        await svc.remove_agent_from_room(agent_id, room_id, principal.user_id, require_member=True)
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"status": "removed"}


@router.post("/rooms/{room_id}/agents/{agent_id}/memberships")
async def rejoin_agent_to_room(
    room_id: str,
    agent_id: str,
    principal: CurrentUser,
) -> dict[str, str]:
    """Put a removed agent back, as a new membership beside the departure it follows."""
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.ADMINISTER)
    try:
        membership = await svc.rejoin_agent_to_room(
            agent_id, room_id, principal.user_id, require_member=True
        )
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {
        "status": "rejoined",
        "membership_id": membership.membership_id,
        "rejoined_from_membership_id": membership.rejoined_from_membership_id or "",
    }


@router.get("/rooms/{room_id}/agents")
async def list_room_agents(
    room_id: str,
    principal: CurrentUser,
) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.READ)
    agents = await svc.list_room_agents(room_id)
    return [
        {"agent_id": a.agent_id, "name": a.name, "role": a.role, "status": a.status.value}
        for a in agents
    ]


@router.get("/rooms/{room_id}/outputs")
async def list_room_outputs(
    room_id: str,
    principal: CurrentUser,
) -> list[dict[str, Any]]:
    """List immutable agent outputs for inspection and later selection."""
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.READ)
    try:
        outputs = await svc.list_room_outputs(room_id)
    except DomainError as e:
        raise HTTPException(404, str(e)) from e
    return [
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
    ]


@router.put("/rooms/{room_id}/output-selections/{output_id}")
async def select_room_output(
    room_id: str,
    output_id: str,
    req: SelectOutputRequest,
    principal: CurrentUser,
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.MUTATE)
    disposition = _safe_enum(req.disposition.upper(), OutputDisposition, "disposition")
    try:
        selection = await svc.select_output(room_id, output_id, disposition, principal.user_id)
    except DomainError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "output_id": selection.output_id,
        "branch_id": selection.branch_id,
        "disposition": selection.disposition.value,
        "decided_by": selection.decided_by,
        "updated_at": selection.updated_at.isoformat(),
    }


@router.get("/rooms/{room_id}/output-selections")
async def list_room_output_selections(
    room_id: str,
    principal: CurrentUser,
) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.READ)
    selections = await svc.list_output_selections(room_id)
    return [
        {
            "output_id": selection.output_id,
            "branch_id": selection.branch_id,
            "disposition": selection.disposition.value,
            "decided_by": selection.decided_by,
            "updated_at": selection.updated_at.isoformat(),
        }
        for selection in selections
    ]


@router.put("/branches/{branch_id}/output-selections/{output_id}")
async def select_branch_output(
    branch_id: str,
    output_id: str,
    req: SelectOutputRequest,
    principal: CurrentUser,
) -> dict[str, Any]:
    await _authorized_branch(branch_id, principal, RoomCapability.MUTATE)
    disposition = _safe_enum(req.disposition.upper(), OutputDisposition, "disposition")
    try:
        selection = await _svc_or_404().select_branch_output(
            branch_id, output_id, disposition, principal.user_id
        )
    except DomainError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "branch_id": selection.branch_id,
        "output_id": selection.output_id,
        "disposition": selection.disposition.value,
        "decided_by": selection.decided_by,
        "updated_at": selection.updated_at.isoformat(),
    }


def _synthesis_response(
    svc: MultiplayerService,
    artifact: Artifact,
    version: ArtifactVersion,
    provenance: list[dict[str, Any]],
    synthesis_type: str = SynthesisType.DECISION_BRIEF.value,
) -> dict[str, Any]:
    return {
        "synthesis_type": synthesis_type,
        "artifact_name": artifact.name,
        "artifact_id": artifact.artifact_id,
        "version_id": version.version_id,
        "branch_synthesis_id": version.branch_synthesis_id,
        "version_number": version.version_number,
        "content": version.content,
        "content_hash": version.content_hash,
        "provenance_hash": version.provenance_hash,
        "created_by": normalize_provenance_author(version.created_by),
        "created_at": normalize_provenance_timestamp(version.created_at),
        "provenance_hash_verified": svc.verify_artifact_provenance_hash(version, provenance),
        "claims": provenance,
    }


@router.post("/branches/{branch_id}/syntheses")
async def synthesize_branch(
    branch_id: str,
    req: SynthesizeBranchRequest,
    principal: CurrentUser,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    """Publish one of the three PRD synthesis types over this Branch's selected outputs."""
    svc = _svc_or_404()
    try:
        synthesis_type = SynthesisType(req.synthesis_type)
    except ValueError as exc:
        raise HTTPException(400, f"unknown synthesis type: {req.synthesis_type}") from exc
    await _authorized_branch(branch_id, principal, RoomCapability.MUTATE)
    try:
        artifact, version = await svc.synthesize_branch(
            branch_id,
            req.title,
            principal.user_id,
            synthesis_type=synthesis_type,
            idempotency_key=idempotency_key,
        )
    except IdempotencyConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except DomainError as exc:
        raise HTTPException(400, str(exc)) from exc
    provenance = await svc.repos.artifacts.get_version_provenance(version.version_id)
    # Report the type the row actually carries, so the response is a reading of the record
    # rather than an echo of the request.
    stored = await svc.repos.branch_syntheses.get(str(version.branch_synthesis_id))
    published = stored.synthesis_type if stored else synthesis_type.value
    return _synthesis_response(svc, artifact, version, provenance, published)


@router.post("/branches/{branch_id}/syntheses/decision-brief")
async def synthesize_branch_decision_brief(
    branch_id: str,
    req: SynthesizeDecisionBriefRequest,
    principal: CurrentUser,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _authorized_branch(branch_id, principal, RoomCapability.MUTATE)
    try:
        artifact, version = await svc.synthesize_branch_decision_brief(
            branch_id, req.title, principal.user_id, idempotency_key=idempotency_key
        )
    except IdempotencyConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except DomainError as exc:
        raise HTTPException(400, str(exc)) from exc
    provenance = await svc.repos.artifacts.get_version_provenance(version.version_id)
    return _synthesis_response(svc, artifact, version, provenance)


@router.post("/rooms/{room_id}/syntheses/decision-brief")
async def synthesize_decision_brief(
    room_id: str,
    req: SynthesizeDecisionBriefRequest,
    principal: CurrentUser,
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.MUTATE)
    try:
        artifact, version = await svc.synthesize_decision_brief(
            room_id, req.title, principal.user_id
        )
    except DomainError as exc:
        raise HTTPException(400, str(exc)) from exc
    provenance = await svc.repos.artifacts.get_version_provenance(version.version_id)
    return _synthesis_response(svc, artifact, version, provenance)


@router.get("/rooms/{room_id}/ontology")
async def get_room_ontology(
    room_id: str,
    principal: CurrentUser,
) -> dict[str, Any]:
    """Inspect only this room's typed assertions and exact evidence identifiers."""
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.READ)
    try:
        return await svc.get_room_ontology(room_id)
    except DomainError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/rooms/{room_id}/meta")
async def ask_room_meta(
    room_id: str,
    principal: CurrentUser,
    kind: str | None = Query(None, min_length=1, max_length=100),
    question: str | None = Query(None, min_length=1, max_length=500),
    version_id: str | None = Query(None, min_length=1, max_length=100),
    limit: int = Query(10, ge=1, le=10),
) -> dict[str, Any]:
    """Answer a bounded decision question from this room's governed evidence graph.

    `kind` names one member of the closed set Meta answers; `question` is free text
    that is recorded and never parsed when a kind is named. Either alone is enough,
    so every supported question is reachable without guessing a phrasing.
    """
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.READ)
    question_kind = (
        None if kind is None else _safe_enum(kind.upper(), MetaQuestionKind, "Meta question kind")
    )
    try:
        return await svc.answer_decision_meta(
            room_id,
            question,
            kind=question_kind,
            user_id=principal.user_id,
            version_id=version_id,
            limit=limit,
        )
    except DomainError as exc:
        status_code = 404 if "not found" in str(exc) or "not available" in str(exc) else 400
        raise HTTPException(status_code, str(exc)) from exc


@router.post("/rooms/{room_id}/ontology/extractions")
async def run_ontology_extraction(
    room_id: str,
    req: RunOntologyExtractionRequest,
    principal: CurrentUser,
) -> dict[str, Any]:
    """Run one bounded extraction pass. No read path triggers this."""
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.ADMINISTER)
    extractor = _safe_enum(req.extractor.upper(), OntologyExtractor, "ontology extractor")
    try:
        return await svc.run_ontology_extraction(room_id, extractor, actor_id=principal.user_id)
    except DomainError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/rooms/{room_id}/ontology/entities/{entity_id}/reviews")
async def review_ontology_entity(
    room_id: str,
    entity_id: str,
    req: ReviewOntologyEntityRequest,
    principal: CurrentUser,
) -> dict[str, Any]:
    """Confirm or correct one assertion with append-only history and an audit event."""
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.MUTATE)
    action = _safe_enum(req.action.upper(), OntologyReviewAction, "ontology review action")
    try:
        entity, review = await svc.review_ontology_entity(
            room_id,
            entity_id,
            action,
            principal.user_id,
            req.reason,
            require_member=True,
            corrected_label=req.corrected_label,
            corrected_properties=req.corrected_properties,
            corrected_confidence=req.corrected_confidence,
        )
    except DomainError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "entity": svc._ontology_entity_record(entity),
        "review": svc._ontology_review_record(review),
    }


@router.post("/rooms/{room_id}/ontology/relationships/{relationship_id}/reviews")
async def review_ontology_relationship(
    room_id: str,
    relationship_id: str,
    req: ReviewOntologyRelationshipRequest,
    principal: CurrentUser,
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.MUTATE)
    action = _safe_enum(req.action.upper(), OntologyReviewAction, "ontology review action")
    corrected_kind = (
        _safe_enum(
            req.corrected_kind.upper(),
            OntologyRelationshipKind,
            "ontology relationship kind",
        )
        if req.corrected_kind is not None
        else None
    )
    try:
        relationship, review = await svc.review_ontology_relationship(
            room_id,
            relationship_id,
            action,
            principal.user_id,
            req.reason,
            require_member=True,
            corrected_kind=corrected_kind,
            corrected_confidence=req.corrected_confidence,
        )
    except DomainError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "relationship": svc._ontology_relationship_record(relationship),
        "review": svc._ontology_review_record(review),
    }


@router.get("/agents/{agent_id}")
async def get_agent(
    agent_id: str,
    principal: CurrentUser,
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _authorized_agent(agent_id, principal, RoomCapability.READ)
    try:
        agent = await svc.get_agent(agent_id)
    except DomainError as e:
        raise HTTPException(404, str(e)) from e
    return {
        "agent_id": agent.agent_id,
        "name": agent.name,
        "role": agent.role,
        "status": agent.status.value,
        "room_id": agent.room_id,
    }


# ── Sessions & Execution ─────────────────────────────────────────────────────


@router.post("/rooms/{room_id}/agents/{agent_id}/sessions")
async def start_session(
    room_id: str,
    agent_id: str,
    principal: CurrentUser,
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.MUTATE)
    await _authorized_agent(agent_id, principal, RoomCapability.MUTATE)
    try:
        session = await svc.start_agent_session(room_id, agent_id)
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"session_id": session.session_id, "agent_id": agent_id, "status": session.status.value}


@router.post("/sessions/{session_id}/execute")
async def start_execution(
    session_id: str,
    principal: CurrentUser,
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _authorized_session(session_id, principal, RoomCapability.MUTATE)
    try:
        execution = await svc.start_execution(session_id, principal.user_id)
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {
        "execution_id": execution.execution_id,
        "status": execution.status.value,
        "triggered_by": execution.triggered_by.value,
    }


@router.post("/executions/{execution_id}/step")
async def execute_step(
    execution_id: str,
    req: SendInstructionRequest,
    principal: CurrentUser,
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _authorized_execution(execution_id, principal, RoomCapability.MUTATE)
    try:
        result = await svc.execute_agent_step(execution_id, req.prompt, principal.user_id)
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return result


@router.post("/executions/{execution_id}/pause")
async def pause_execution(
    execution_id: str,
    principal: CurrentUser,
) -> dict[str, str]:
    svc = _svc_or_404()
    await _authorized_execution(execution_id, principal, RoomCapability.MUTATE)
    try:
        ok = await svc.pause_execution(execution_id, principal.user_id)
    except DomainError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "paused" if ok else "failed"}


@router.post("/executions/{execution_id}/resume")
async def resume_execution(
    execution_id: str,
    principal: CurrentUser,
) -> dict[str, str]:
    svc = _svc_or_404()
    await _authorized_execution(execution_id, principal, RoomCapability.MUTATE)
    try:
        ok = await svc.resume_execution(execution_id, principal.user_id)
    except DomainError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "resumed" if ok else "failed"}


@router.post("/executions/{execution_id}/cancel")
async def cancel_execution(
    execution_id: str,
    principal: CurrentUser,
) -> dict[str, str]:
    svc = _svc_or_404()
    await _authorized_execution(execution_id, principal, RoomCapability.MUTATE)
    try:
        ok = await svc.cancel_execution(execution_id, principal.user_id, require_member=True)
    except DomainError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "cancelled" if ok else "failed"}


@router.post("/executions/{execution_id}/intervene")
async def intervene_execution(
    execution_id: str,
    req: RedirectAgentRequest,
    principal: CurrentUser,
) -> dict[str, str]:
    svc = _svc_or_404()
    await _authorized_execution(execution_id, principal, RoomCapability.MUTATE)
    try:
        await svc.intervene_execution(
            execution_id, principal.user_id, req.instruction, require_member=True
        )
    except DomainError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"status": "intervention_recorded"}


# ── Tasks ────────────────────────────────────────────────────────────────────


@router.post("/rooms/{room_id}/tasks")
async def create_task(
    room_id: str, req: CreateTaskRequest, principal: CurrentUser
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.MUTATE)
    priority = _safe_enum(req.priority, TaskPriority, "priority")
    try:
        task = await svc.create_task(
            room_id, req.title, req.description, priority, principal.user_id, require_member=True
        )
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"task_id": task.task_id, "title": task.title, "status": task.status.value}


@router.get("/rooms/{room_id}/tasks")
async def list_tasks(
    room_id: str,
    principal: CurrentUser,
) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.READ)
    tasks = await svc.list_room_tasks(room_id)
    return [
        {
            "task_id": t.task_id,
            "title": t.title,
            "status": t.status.value,
            "priority": t.priority.value,
            "assigned_agent_id": t.assigned_agent_id,
        }
        for t in tasks
    ]


@router.post("/tasks/{task_id}/assign")
async def assign_task(
    task_id: str,
    req: AssignTaskRequest,
    principal: CurrentUser,
) -> dict[str, str]:
    svc = _svc_or_404()
    task = await _authorized_task(task_id, principal, RoomCapability.MUTATE)
    agent = await _authorized_agent(req.agent_id, principal, RoomCapability.MUTATE)
    if agent.room_id != task.room_id:
        raise HTTPException(400, "agent is not in task room")
    try:
        await svc.assign_task(
            task_id, req.agent_id, requested_by=principal.user_id, require_member=True
        )
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"status": "assigned"}


@router.post("/tasks/{task_id}/delegate")
async def delegate_task(
    task_id: str,
    req: DelegateTaskRequest,
    principal: CurrentUser,
) -> dict[str, Any]:
    svc = _svc_or_404()
    task = await _authorized_task(task_id, principal, RoomCapability.MUTATE)
    agent = await _authorized_agent(req.to_agent_id, principal, RoomCapability.MUTATE)
    if agent.room_id != task.room_id:
        raise HTTPException(400, "agent is not in task room")
    try:
        child = await svc.delegate_task(
            task_id,
            principal.user_id,
            req.to_agent_id,
            req.description,
            requested_by=principal.user_id,
            require_member=True,
        )
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"child_task_id": child.task_id, "status": child.status.value}


@router.post("/tasks/{task_id}/complete")
async def complete_task(
    task_id: str,
    principal: CurrentUser,
) -> dict[str, str]:
    svc = _svc_or_404()
    await _authorized_task(task_id, principal, RoomCapability.MUTATE)
    try:
        await svc.complete_task(task_id, requested_by=principal.user_id, require_member=True)
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"status": "completed"}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    principal: CurrentUser,
) -> dict[str, str]:
    svc = _svc_or_404()
    await _authorized_task(task_id, principal, RoomCapability.MUTATE)
    try:
        await svc.cancel_task(task_id, requested_by=principal.user_id, require_member=True)
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"status": "cancelled"}


# ── Messages ─────────────────────────────────────────────────────────────────


@router.post("/rooms/{room_id}/messages")
async def send_message(
    room_id: str,
    req: CreateMessageRequest,
    principal: CurrentUser,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.MUTATE)
    role = _safe_enum(req.role, MessageRole, "role")
    if role != MessageRole.HUMAN:
        raise HTTPException(403, "authenticated users may only author HUMAN messages")
    if req.sender_id and req.sender_id != principal.user_id:
        raise HTTPException(403, "sender identity cannot be overridden")
    sender = principal.user_id
    try:
        msg = await svc.send_message(
            room_id,
            role,
            sender,
            req.content,
            idempotency_key=idempotency_key,
            invoke_mentioned_agents=req.invoke_mentioned_agents,
        )
    except IdempotencyConflict as e:
        raise HTTPException(409, str(e)) from e
    except DomainError as e:
        status = 409 if "turn is locked" in str(e) else 400
        raise HTTPException(status, str(e)) from e
    return await _message_response(msg, invocation_requested=req.invoke_mentioned_agents)


@router.get("/rooms/{room_id}/messages")
async def list_messages(
    room_id: str,
    principal: CurrentUser,
    limit: int = Query(100, ge=1, le=500),
    after_sequence: int | None = Query(None, ge=0),
) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.READ)
    messages = await svc.list_room_messages(room_id, limit, after_sequence)
    return [_message_summary(m) for m in messages]


# ── Threads, mentions, reactions, read state, search ─────────────────────────


def _message_summary(message: Message) -> dict[str, Any]:
    return {
        "message_id": message.message_id,
        "role": message.role.value,
        "sender_id": message.sender_id,
        "content": message.content,
        # An agent's message names the output it came from here; the output stays
        # the inspectable record and this is the pointer to it.
        "metadata": message.metadata,
        "sequence": message.event_sequence,
        "parent_message_id": message.parent_message_id,
        "root_message_id": message.root_message_id,
        "thread_depth": message.thread_depth,
        "broadcast_to_room": message.broadcast_to_room,
        "created_at": message.created_at.isoformat(),
    }


async def _message_response(
    message: Message, *, invocation_requested: bool = False
) -> dict[str, Any]:
    """A written message, the mentions the server derived, and the ones it could not.

    unrecognized_mentions is the difference between a mention that reached somebody
    and one that reached nobody. Without it an author who misspells a handle gets
    the same 200 and the same empty mention list as an author who wrote no mention
    at all, and waits for an answer that was never going to arrive.

    uninvocable_mentions is the third case, and it was the silent one: the handle
    reached somebody who is not an agent, so a request to invoke opened no run and
    named no failure. It is reported only when a turn was actually asked for.
    """
    svc = _svc_or_404()
    mentions = await svc.list_message_mentions(message.message_id)
    return {
        **_message_summary(message),
        "mentions": [
            {
                "target_type": mention.target_type.value,
                "target_id": mention.target_id,
                "handle": mention.handle,
                "invoked_execution_id": mention.invoked_execution_id,
            }
            for mention in mentions
        ],
        "unrecognized_mentions": await svc.unrecognized_mention_handles(
            message.room_id, message.content
        ),
        "uninvocable_mentions": (
            await svc.uninvocable_mention_handles(message.message_id)
            if invocation_requested
            else []
        ),
    }


async def _authorized_message(
    message_id: str, principal: AuthenticatedUser, capability: RoomCapability
) -> Message:
    try:
        message = await _svc_or_404().get_message(message_id)
    except DomainError as exc:
        raise HTTPException(404, "message not found") from exc
    await _require_room(message.room_id, principal, capability)
    return message


@router.post("/messages/{message_id}/replies")
async def reply_to_message(
    message_id: str,
    req: CreateReplyRequest,
    principal: CurrentUser,
    idempotency_key: IdempotencyKey = None,
) -> dict[str, Any]:
    svc = _svc_or_404()
    parent = await _authorized_message(message_id, principal, RoomCapability.MUTATE)
    try:
        reply = await svc.send_message(
            parent.room_id,
            MessageRole.HUMAN,
            principal.user_id,
            req.content,
            idempotency_key=idempotency_key,
            parent_message_id=parent.message_id,
            broadcast_to_room=req.broadcast_to_room,
            invoke_mentioned_agents=req.invoke_mentioned_agents,
        )
    except IdempotencyConflict as e:
        raise HTTPException(409, str(e)) from e
    except DomainError as e:
        status = 409 if "turn is locked" in str(e) else 400
        raise HTTPException(status, str(e)) from e
    return await _message_response(reply, invocation_requested=req.invoke_mentioned_agents)


@router.get("/messages/{message_id}/thread")
async def get_thread(
    message_id: str,
    principal: CurrentUser,
    limit: int = Query(200, ge=1, le=500),
) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    await _authorized_message(message_id, principal, RoomCapability.READ)
    thread = await svc.list_thread(message_id, limit)
    return [
        # reply_count is counted over the reply rows on this read, never stored.
        {**_message_summary(entry.message), "reply_count": entry.reply_count}
        for entry in thread
    ]


@router.post("/messages/{message_id}/reactions")
async def add_reaction(
    message_id: str, req: ReactionRequest, principal: CurrentUser
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _authorized_message(message_id, principal, RoomCapability.MUTATE)
    try:
        reaction = await svc.add_reaction(message_id, principal.user_id, req.emoji)
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"message_id": message_id, "emoji": reaction.emoji, "removed": False}


@router.delete("/messages/{message_id}/reactions/{emoji}")
async def remove_reaction(message_id: str, emoji: str, principal: CurrentUser) -> dict[str, Any]:
    svc = _svc_or_404()
    await _authorized_message(message_id, principal, RoomCapability.MUTATE)
    try:
        reaction = await svc.remove_reaction(message_id, principal.user_id, emoji)
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"message_id": message_id, "emoji": reaction.emoji, "removed": True}


@router.get("/messages/{message_id}/reactions")
async def list_reactions(message_id: str, principal: CurrentUser) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    await _authorized_message(message_id, principal, RoomCapability.READ)
    return [
        {
            "emoji": r.emoji,
            "actor_id": r.actor_id,
            "actor_type": r.actor_type.value,
            "created_at": r.created_at.isoformat(),
        }
        for r in await svc.list_reactions(message_id)
    ]


@router.get("/rooms/{room_id}/read-cursor")
async def get_read_cursor(room_id: str, principal: CurrentUser) -> dict[str, Any]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.READ)
    return await svc.get_read_cursor(room_id, principal.user_id)


@router.put("/rooms/{room_id}/read-cursor")
async def set_read_cursor(
    room_id: str, req: ReadCursorRequest, principal: CurrentUser
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.READ)
    try:
        return await svc.set_read_cursor(room_id, principal.user_id, req.last_read_sequence)
    except DomainError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/search")
async def search(
    principal: CurrentUser,
    q: str = Query(..., min_length=1),
    room_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
) -> list[dict[str, Any]]:
    """Room membership constrains the SQL itself, so a non-member matches nothing.

    There is deliberately no authorization step in Python here: adding one would
    hide whether the query is the thing enforcing isolation.
    """
    svc = _svc_or_404()
    try:
        hits = await svc.search(principal.user_id, q, room_id, limit)
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return [
        {
            "object_kind": hit.object_kind.value,
            "object_id": hit.object_id,
            # What it is and where it lives: the kind names the endpoint family, and
            # container_id is the extra id that family needs when object_id and
            # room_id alone do not address the object.
            "container_id": hit.container_id,
            "room_id": hit.room_id,
            "room_name": hit.room_name,
            "author_id": hit.author_id,
            "excerpt": hit.excerpt,
            "created_at": hit.created_at.isoformat(),
        }
        for hit in hits
    ]


# ── Artifacts ────────────────────────────────────────────────────────────────


@router.post("/rooms/{room_id}/artifacts")
async def create_artifact(
    room_id: str, req: CreateArtifactRequest, principal: CurrentUser
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.MUTATE)
    artifact_type = _safe_enum(req.artifact_type, ArtifactType, "artifact_type")
    try:
        art = await svc.create_artifact(
            room_id,
            req.name,
            artifact_type,
            req.description,
            principal.user_id,
            req.content,
            require_member=True,
        )
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"artifact_id": art.artifact_id, "name": art.name, "version": art.current_version}


@router.get("/rooms/{room_id}/artifacts")
async def list_artifacts(
    room_id: str,
    principal: CurrentUser,
) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.READ)
    arts = await svc.list_room_artifacts(room_id)
    return [
        {
            "artifact_id": a.artifact_id,
            "name": a.name,
            "type": a.artifact_type.value,
            "version": a.current_version,
        }
        for a in arts
    ]


@router.post("/artifacts/{artifact_id}/versions")
async def update_artifact(
    artifact_id: str,
    req: UpdateArtifactRequest,
    principal: CurrentUser,
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _authorized_artifact(artifact_id, principal, RoomCapability.MUTATE)
    try:
        ver = await svc.update_artifact(
            artifact_id, req.content, principal.user_id, require_member=True
        )
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"version_id": ver.version_id, "version_number": ver.version_number}


@router.get("/artifacts/{artifact_id}/versions")
async def list_artifact_versions(
    artifact_id: str,
    principal: CurrentUser,
) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    await _authorized_artifact(artifact_id, principal, RoomCapability.READ)
    versions = await svc.repos.artifacts.list_versions(artifact_id)
    return [
        {
            "version_id": v.version_id,
            "version_number": v.version_number,
            "content": v.content,
            "content_hash": v.content_hash,
            "provenance_hash": v.provenance_hash,
            "branch_synthesis_id": v.branch_synthesis_id,
            "created_by": normalize_provenance_author(v.created_by),
            "created_at": normalize_provenance_timestamp(v.created_at),
        }
        for v in versions
    ]


@router.get("/artifact-versions/{version_id}/provenance")
async def get_artifact_version_provenance(
    version_id: str,
    principal: CurrentUser,
) -> dict[str, Any]:
    svc = _svc_or_404()
    version = await svc.repos.artifacts.get_version(version_id)
    if version is None:
        raise HTTPException(404, "artifact version not found")
    await _authorized_artifact(version.artifact_id, principal, RoomCapability.READ)
    claims = await svc.repos.artifacts.get_version_provenance(version_id)
    synthesis = (
        await svc.repos.branch_syntheses.get(version.branch_synthesis_id)
        if version.branch_synthesis_id
        else None
    )
    inputs = (
        await svc.repos.branch_syntheses.list_inputs(synthesis.synthesis_id)
        if synthesis is not None
        else []
    )
    return {
        "version_id": version_id,
        "branch_synthesis": (
            {
                "synthesis_id": synthesis.synthesis_id,
                "branch_id": synthesis.branch_id,
                "status": synthesis.status.value,
                "provider_input": synthesis.provider_input,
                "provider_name": synthesis.provider_name,
                "provider_model": synthesis.provider_model,
                "provider_response_id": synthesis.provider_response_id,
                "provider_evidence": synthesis.provider_evidence,
                "simulated": synthesis.simulated,
                "selected_output_ids": [item.output_id for item in inputs],
            }
            if synthesis is not None
            else None
        ),
        "content_hash": version.content_hash,
        "provenance_hash": version.provenance_hash,
        "created_by": normalize_provenance_author(version.created_by),
        "created_at": normalize_provenance_timestamp(version.created_at),
        "provenance_hash_verified": svc.verify_artifact_provenance_hash(version, claims),
        "claims": claims,
    }


# ── Decisions ────────────────────────────────────────────────────────────────


@router.post("/rooms/{room_id}/decisions")
async def create_decision(
    room_id: str, req: CreateDecisionRequest, principal: CurrentUser
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.MUTATE)
    try:
        dec = await svc.create_decision(
            room_id, req.title, req.content, req.reason, principal.user_id, require_member=True
        )
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"decision_id": dec.decision_id, "title": dec.title, "status": dec.status.value}


@router.get("/rooms/{room_id}/decisions")
async def list_decisions(
    room_id: str,
    principal: CurrentUser,
) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.READ)
    decs = await svc.list_room_decisions(room_id)
    return [
        {
            "decision_id": d.decision_id,
            "title": d.title,
            "content": d.content,
            "status": d.status.value,
            "created_by": d.created_by,
        }
        for d in decs
    ]


# ── Memory ───────────────────────────────────────────────────────────────────


@router.post("/rooms/{room_id}/memories")
async def create_memory(
    room_id: str, req: CreateMemoryRequest, principal: CurrentUser
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.MUTATE)
    scope = _safe_enum(req.scope, MemoryScope, "scope")
    try:
        mem = await svc.create_memory(
            room_id,
            None,
            None,
            scope,
            req.content,
            req.memory_type,
            principal.user_id,
            require_member=True,
        )
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"memory_id": mem.memory_id, "type": mem.memory_type, "scope": mem.scope.value}


@router.get("/rooms/{room_id}/memories")
async def list_memories(
    room_id: str,
    principal: CurrentUser,
) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.READ)
    mems = await svc.list_room_memories(room_id)
    return [
        {
            "memory_id": m.memory_id,
            "content": m.content,
            "type": m.memory_type,
            "scope": m.scope.value,
            "is_authoritative": m.is_authoritative,
        }
        for m in mems
    ]


# ── Approvals ────────────────────────────────────────────────────────────────


@router.post("/rooms/{room_id}/approvals")
async def request_approval(
    room_id: str,
    principal: CurrentUser,
    execution_id: str = Query(""),
    agent_id: str = Query(""),
    action: str = Query(""),
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.MUTATE)
    execution = await _authorized_execution(execution_id, principal, RoomCapability.MUTATE)
    agent = await _authorized_agent(agent_id, principal, RoomCapability.MUTATE)
    session = await svc.repos.sessions.get(execution.session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    if (
        session.room_id != room_id
        or agent.room_id != room_id
        or execution.agent_id != agent.agent_id
    ):
        raise HTTPException(400, "execution and agent must belong to the approval room")
    try:
        approval = await svc.request_approval(
            room_id,
            execution_id,
            agent_id,
            action,
            requested_by=principal.user_id,
            require_member=True,
        )
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"approval_id": approval.approval_id, "status": approval.status.value}


@router.get("/rooms/{room_id}/approvals")
async def list_approvals(
    room_id: str,
    principal: CurrentUser,
) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.READ)
    approvals = await svc.list_pending_approvals(room_id)
    return [
        {
            "approval_id": a.approval_id,
            "action": a.action_description,
            "agent_id": a.agent_id,
            "status": a.status.value,
        }
        for a in approvals
    ]


@router.post("/approvals/{approval_id}/approve")
async def approve_action(
    approval_id: str,
    req: ApproveActionRequest,
    principal: CurrentUser,
) -> dict[str, str]:
    svc = _svc_or_404()
    await _authorized_approval(approval_id, principal, RoomCapability.ADMINISTER)
    try:
        await svc.approve_action(approval_id, principal.user_id, req.comment, require_member=True)
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"status": "approved"}


@router.post("/approvals/{approval_id}/reject")
async def reject_action(
    approval_id: str,
    req: RejectActionRequest,
    principal: CurrentUser,
) -> dict[str, str]:
    svc = _svc_or_404()
    await _authorized_approval(approval_id, principal, RoomCapability.ADMINISTER)
    try:
        await svc.reject_action(
            approval_id,
            principal.user_id,
            req.comment,
            require_member=True,
            continue_turn=req.continue_turn,
        )
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"status": "rejected"}


# ── Human Intervention ───────────────────────────────────────────────────────


@router.post("/agents/{agent_id}/interrupt")
async def interrupt_agent(
    agent_id: str, req: InterruptAgentRequest, principal: CurrentUser
) -> dict[str, str]:
    svc = _svc_or_404()
    await _authorized_agent(agent_id, principal, RoomCapability.MUTATE)
    try:
        await svc.interrupt_agent(agent_id, principal.user_id, req.reason, require_member=True)
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"status": "interrupted"}


@router.post("/agents/{agent_id}/redirect")
async def redirect_agent(
    agent_id: str, req: RedirectAgentRequest, principal: CurrentUser
) -> dict[str, str]:
    svc = _svc_or_404()
    await _authorized_agent(agent_id, principal, RoomCapability.MUTATE)
    try:
        await svc.redirect_agent(agent_id, principal.user_id, req.instruction, require_member=True)
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"status": "redirected"}


# ── Notifications ────────────────────────────────────────────────────────────


@router.get("/notifications")
async def list_notifications(
    principal: CurrentUser,
) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    notifs = await svc.list_notifications(principal.user_id)
    return [
        {
            "notification_id": n.notification_id,
            "title": n.title,
            "body": n.body,
            "type": n.notification_type,
            "room_id": n.room_id,
            "created_at": n.created_at.isoformat(),
        }
        for n in notifs
    ]


# ── Presence ─────────────────────────────────────────────────────────────────


@router.get("/rooms/{room_id}/presence")
async def get_presence(
    room_id: str,
    principal: CurrentUser,
) -> list[dict[str, str]]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.READ)
    presence = await svc.presence.get_room_presence(room_id)
    return [{"user_id": p.user_id, "status": p.status.value} for p in presence]
