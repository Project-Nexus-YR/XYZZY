"""FastAPI routes for the multiplayer AI workspace."""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel

from ..domain.events import EventType
from ..domain.models import (
    AgentInstance,
    Approval,
    Artifact,
    ArtifactType,
    DomainError,
    Execution,
    MemoryScope,
    MessageRole,
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
from ..security import (
    AuthenticatedUser,
    AuthenticationError,
    AuthorizationError,
    RoomCapability,
    TokenAuthenticator,
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


class SpawnAgentRequest(BaseModel):
    template_id: str
    name: str | None = None
    system_prompt: str | None = None
    model_provider: str = ""
    model_name: str = ""


class SendInstructionRequest(BaseModel):
    prompt: str


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


class SelectOutputRequest(BaseModel):
    disposition: str


class SynthesizeDecisionBriefRequest(BaseModel):
    title: str = "Authentication migration decision"


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
        return await svc.get_room_state(room_id, last_sequence)
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
    await _require_room(room_id, principal, RoomCapability.MUTATE)
    await svc.leave_room(room_id, principal.user_id)
    return {"status": "left"}


@router.post("/rooms/{room_id}/members/invitations")
async def invite_room_member(
    room_id: str,
    req: InviteRoomMemberRequest,
    principal: CurrentUser,
) -> dict[str, str]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.ADMINISTER)
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
        )
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {
        "agent_id": agent.agent_id,
        "name": agent.name,
        "role": agent.role,
        "status": agent.status.value,
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
            "disposition": selection.disposition.value,
            "decided_by": selection.decided_by,
            "updated_at": selection.updated_at.isoformat(),
        }
        for selection in selections
    ]


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
    return {
        "artifact_id": artifact.artifact_id,
        "version_id": version.version_id,
        "version_number": version.version_number,
        "content": version.content,
        "content_hash": version.content_hash,
        "provenance_hash": version.provenance_hash,
        "created_by": normalize_provenance_author(version.created_by),
        "created_at": normalize_provenance_timestamp(version.created_at),
        "provenance_hash_verified": svc.verify_artifact_provenance_hash(version, provenance),
        "claims": provenance,
    }


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
    question: str = Query(..., min_length=1, max_length=500),
    version_id: str | None = Query(None, min_length=1, max_length=100),
    limit: int = Query(10, ge=1, le=10),
) -> dict[str, Any]:
    """Answer a bounded decision question from this room's governed evidence graph."""
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.READ)
    try:
        return await svc.answer_decision_meta(
            room_id,
            question,
            version_id=version_id,
            limit=limit,
        )
    except DomainError as exc:
        status_code = 404 if "not found" in str(exc) or "not available" in str(exc) else 400
        raise HTTPException(status_code, str(exc)) from exc


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
        execution = await svc.start_execution(session_id)
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"execution_id": execution.execution_id, "status": execution.status.value}


@router.post("/executions/{execution_id}/step")
async def execute_step(
    execution_id: str,
    req: SendInstructionRequest,
    principal: CurrentUser,
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _authorized_execution(execution_id, principal, RoomCapability.MUTATE)
    try:
        result = await svc.execute_agent_step(execution_id, req.prompt)
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
    ok = await svc.nexus.pause_execution(execution_id)
    return {"status": "paused" if ok else "failed"}


@router.post("/executions/{execution_id}/resume")
async def resume_execution(
    execution_id: str,
    principal: CurrentUser,
) -> dict[str, str]:
    svc = _svc_or_404()
    await _authorized_execution(execution_id, principal, RoomCapability.MUTATE)
    ok = await svc.nexus.resume_execution(execution_id)
    return {"status": "resumed" if ok else "failed"}


@router.post("/executions/{execution_id}/cancel")
async def cancel_execution(
    execution_id: str,
    principal: CurrentUser,
) -> dict[str, str]:
    svc = _svc_or_404()
    await _authorized_execution(execution_id, principal, RoomCapability.MUTATE)
    ok = await svc.nexus.cancel_execution(execution_id)
    return {"status": "cancelled" if ok else "failed"}


@router.post("/executions/{execution_id}/intervene")
async def intervene_execution(
    execution_id: str,
    req: RedirectAgentRequest,
    principal: CurrentUser,
) -> dict[str, str]:
    svc = _svc_or_404()
    execution = await _authorized_execution(execution_id, principal, RoomCapability.MUTATE)
    try:
        agent = await svc.get_agent(execution.agent_id)
    except DomainError:
        raise HTTPException(404, "agent not found") from None
    await svc.nexus.add_execution_intervention(execution_id, req.instruction)
    await svc._append_room_event(
        agent.room_id,
        EventType.HUMAN_REDIRECTED_AGENT,
        {"agent_id": execution.agent_id, "instruction": req.instruction},
        principal.user_id,
        "user",
    )
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
            room_id, req.title, req.description, priority, principal.user_id
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
        await svc.assign_task(task_id, req.agent_id)
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
            task_id, principal.user_id, req.to_agent_id, req.description
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
        await svc.complete_task(task_id)
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
        await svc.cancel_task(task_id)
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"status": "cancelled"}


# ── Messages ─────────────────────────────────────────────────────────────────


@router.post("/rooms/{room_id}/messages")
async def send_message(
    room_id: str, req: CreateMessageRequest, principal: CurrentUser
) -> dict[str, Any]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.MUTATE)
    role = _safe_enum(req.role, MessageRole, "role")
    if req.sender_id and req.sender_id != principal.user_id:
        raise HTTPException(403, "sender identity cannot be overridden")
    sender = principal.user_id
    try:
        msg = await svc.send_message(room_id, role, sender, req.content)
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"message_id": msg.message_id, "role": msg.role.value, "content": msg.content}


@router.get("/rooms/{room_id}/messages")
async def list_messages(
    room_id: str,
    principal: CurrentUser,
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    await _require_room(room_id, principal, RoomCapability.READ)
    messages = await svc.list_room_messages(room_id, limit)
    return [
        {
            "message_id": m.message_id,
            "role": m.role.value,
            "sender_id": m.sender_id,
            "content": m.content,
            "created_at": m.created_at.isoformat(),
        }
        for m in messages
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
            room_id, req.name, artifact_type, req.description, principal.user_id, req.content
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
        ver = await svc.update_artifact(artifact_id, req.content, principal.user_id)
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
    return {
        "version_id": version_id,
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
            room_id, req.title, req.content, req.reason, principal.user_id
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
            room_id, None, None, scope, req.content, req.memory_type, principal.user_id
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
        approval = await svc.request_approval(room_id, execution_id, agent_id, action)
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
        await svc.approve_action(approval_id, principal.user_id, req.comment)
    except DomainError as e:
        raise HTTPException(400, str(e)) from e
    return {"status": "approved"}


@router.post("/approvals/{approval_id}/reject")
async def reject_action(
    approval_id: str,
    req: ApproveActionRequest,
    principal: CurrentUser,
) -> dict[str, str]:
    svc = _svc_or_404()
    await _authorized_approval(approval_id, principal, RoomCapability.ADMINISTER)
    try:
        await svc.reject_action(approval_id, principal.user_id, req.comment)
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
        await svc.interrupt_agent(agent_id, principal.user_id, req.reason)
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
        await svc.redirect_agent(agent_id, principal.user_id, req.instruction)
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
