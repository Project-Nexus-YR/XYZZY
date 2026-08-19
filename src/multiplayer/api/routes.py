"""FastAPI routes for the multiplayer AI workspace."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..domain.events import EventType
from ..domain.models import (
    ArtifactType,
    DomainError,
    MemoryScope,
    MessageRole,
    TaskPriority,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["multiplayer"])

# ── Service reference (set at startup) ───────────────────────────────────────
_svc: Any = None


def set_service(svc: Any) -> None:
    global _svc
    _svc = svc


def _svc_or_404():
    if _svc is None:
        raise HTTPException(503, "service not ready")
    return _svc


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


# ── Helpers ──────────────────────────────────────────────────────────────────


def _safe_enum(value: str, enum_cls: type, name: str):
    """Convert a string to an enum value, returning 400 on failure."""
    try:
        return enum_cls(value)
    except ValueError:
        valid = [e.value for e in enum_cls]
        raise HTTPException(400, f"invalid {name}: '{value}'. valid: {valid}")


def _handle_domain_error(fn):
    """Catch DomainError and convert to appropriate HTTP error."""
    import functools
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except DomainError as e:
            msg = str(e)
            if "not found" in msg:
                raise HTTPException(404, msg)
            raise HTTPException(400, msg)
    return wrapper


# ── Health ───────────────────────────────────────────────────────────────────


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# ── Organizations ────────────────────────────────────────────────────────────


@router.post("/organizations")
async def create_organization(req: CreateOrgRequest, user_id: str = Query("user_1")) -> dict[str, Any]:
    svc = _svc_or_404()
    try:
        org = await svc.create_organization(req.name, req.slug, user_id)
    except DomainError as e:
        raise HTTPException(400, str(e))
    return {"org_id": org.org_id, "name": org.name, "slug": org.slug}


@router.get("/organizations/{org_id}/workspaces")
async def list_workspaces(org_id: str) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    workspaces = await svc.list_workspaces(org_id)
    return [{"workspace_id": w.workspace_id, "name": w.name, "slug": w.slug} for w in workspaces]


# ── Workspaces ───────────────────────────────────────────────────────────────


@router.post("/organizations/{org_id}/workspaces")
async def create_workspace(org_id: str, req: CreateWorkspaceRequest,
                           user_id: str = Query("user_1")) -> dict[str, Any]:
    svc = _svc_or_404()
    try:
        ws = await svc.create_workspace(org_id, req.name, req.slug, user_id)
    except DomainError as e:
        raise HTTPException(400, str(e))
    return {"workspace_id": ws.workspace_id, "name": ws.name, "slug": ws.slug}


@router.get("/workspaces/{workspace_id}/rooms")
async def list_rooms(workspace_id: str) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    rooms = await svc.list_rooms(workspace_id)
    return [{"room_id": r.room_id, "name": r.name, "description": r.description,
             "status": r.status.value} for r in rooms]


# ── Rooms ────────────────────────────────────────────────────────────────────


@router.post("/workspaces/{workspace_id}/rooms")
async def create_room(workspace_id: str, req: CreateRoomRequest,
                      user_id: str = Query("user_1")) -> dict[str, Any]:
    svc = _svc_or_404()
    try:
        room = await svc.create_room(workspace_id, req.name, user_id, req.description)
    except DomainError as e:
        raise HTTPException(400, str(e))
    return {"room_id": room.room_id, "name": room.name, "description": room.description}


@router.get("/rooms/{room_id}")
async def get_room(room_id: str) -> dict[str, Any]:
    svc = _svc_or_404()
    try:
        room = await svc.get_room(room_id)
    except DomainError as e:
        raise HTTPException(404, str(e))
    return {"room_id": room.room_id, "name": room.name, "description": room.description,
            "status": room.status.value, "workspace_id": room.workspace_id}


@router.get("/rooms/{room_id}/state")
async def get_room_state(room_id: str, last_sequence: int = Query(0, ge=0)) -> dict[str, Any]:
    """Full room state for reconnect/recovery."""
    svc = _svc_or_404()
    try:
        return await svc.get_room_state(room_id, last_sequence)
    except DomainError as e:
        raise HTTPException(404, str(e))


@router.post("/rooms/{room_id}/join")
async def join_room(room_id: str, user_id: str = Query("user_1")) -> dict[str, str]:
    svc = _svc_or_404()
    try:
        await svc.join_room(room_id, user_id)
    except DomainError as e:
        raise HTTPException(400, str(e))
    return {"status": "joined"}


@router.post("/rooms/{room_id}/leave")
async def leave_room(room_id: str, user_id: str = Query("user_1")) -> dict[str, str]:
    svc = _svc_or_404()
    await svc.leave_room(room_id, user_id)
    return {"status": "left"}


@router.get("/rooms/{room_id}/members")
async def list_room_members(room_id: str) -> list[dict[str, str]]:
    svc = _svc_or_404()
    members = await svc.get_room_members(room_id)
    return [{"user_id": m.user_id, "role": m.role} for m in members]


@router.get("/rooms/{room_id}/events")
async def list_room_events(room_id: str, after: int = Query(0, ge=0)) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    events = await svc.get_room_events(room_id, after)
    return [{"event_id": e.event_id, "sequence": e.sequence,
             "event_type": e.event_type.value, "payload": e.payload,
             "actor_id": e.actor_id, "actor_type": e.actor_type,
             "timestamp": e.timestamp.isoformat()} for e in events]


# ── Agents ───────────────────────────────────────────────────────────────────


@router.get("/agent-templates")
async def list_agent_templates() -> list[dict[str, Any]]:
    svc = _svc_or_404()
    templates = await svc.list_agent_templates()
    return [{"template_id": t.template_id, "name": t.name, "description": t.description,
             "role": t.role, "capabilities": sorted(t.capabilities)} for t in templates]


@router.post("/rooms/{room_id}/agents")
async def spawn_agent(room_id: str, req: SpawnAgentRequest) -> dict[str, Any]:
    svc = _svc_or_404()
    try:
        agent = await svc.spawn_agent(room_id, req.template_id, req.name,
                                      req.system_prompt, req.model_provider, req.model_name)
    except DomainError as e:
        raise HTTPException(400, str(e))
    return {"agent_id": agent.agent_id, "name": agent.name, "role": agent.role,
            "status": agent.status.value}


@router.get("/rooms/{room_id}/agents")
async def list_room_agents(room_id: str) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    agents = await svc.list_room_agents(room_id)
    return [{"agent_id": a.agent_id, "name": a.name, "role": a.role,
             "status": a.status.value} for a in agents]


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str) -> dict[str, Any]:
    svc = _svc_or_404()
    try:
        agent = await svc.get_agent(agent_id)
    except DomainError as e:
        raise HTTPException(404, str(e))
    return {"agent_id": agent.agent_id, "name": agent.name, "role": agent.role,
            "status": agent.status.value, "room_id": agent.room_id}


# ── Sessions & Execution ─────────────────────────────────────────────────────


@router.post("/rooms/{room_id}/agents/{agent_id}/sessions")
async def start_session(room_id: str, agent_id: str) -> dict[str, Any]:
    svc = _svc_or_404()
    try:
        session = await svc.start_agent_session(room_id, agent_id)
    except DomainError as e:
        raise HTTPException(400, str(e))
    return {"session_id": session.session_id, "agent_id": agent_id, "status": session.status.value}


@router.post("/sessions/{session_id}/execute")
async def start_execution(session_id: str) -> dict[str, Any]:
    svc = _svc_or_404()
    try:
        execution = await svc.start_execution(session_id)
    except DomainError as e:
        raise HTTPException(400, str(e))
    return {"execution_id": execution.execution_id, "status": execution.status.value}


@router.post("/executions/{execution_id}/step")
async def execute_step(execution_id: str, req: SendInstructionRequest) -> dict[str, Any]:
    svc = _svc_or_404()
    try:
        result = await svc.execute_agent_step(execution_id, req.prompt)
    except DomainError as e:
        raise HTTPException(400, str(e))
    return result


@router.post("/executions/{execution_id}/pause")
async def pause_execution(execution_id: str) -> dict[str, str]:
    svc = _svc_or_404()
    ok = await svc.nexus.pause_execution(execution_id)
    return {"status": "paused" if ok else "failed"}


@router.post("/executions/{execution_id}/resume")
async def resume_execution(execution_id: str) -> dict[str, str]:
    svc = _svc_or_404()
    ok = await svc.nexus.resume_execution(execution_id)
    return {"status": "resumed" if ok else "failed"}


@router.post("/executions/{execution_id}/cancel")
async def cancel_execution(execution_id: str) -> dict[str, str]:
    svc = _svc_or_404()
    ok = await svc.nexus.cancel_execution(execution_id)
    return {"status": "cancelled" if ok else "failed"}


@router.post("/executions/{execution_id}/intervene")
async def intervene_execution(execution_id: str, req: RedirectAgentRequest,
                              user_id: str = Query("user_1")) -> dict[str, str]:
    svc = _svc_or_404()
    execution = await svc.repos.executions.get(execution_id)
    if not execution:
        raise HTTPException(404, "execution not found")
    try:
        agent = await svc.get_agent(execution.agent_id)
    except DomainError:
        raise HTTPException(404, "agent not found")
    run_id = await svc.nexus.get_run_id_for_execution(execution_id)
    if run_id:
        await svc.nexus.add_intervention(run_id, req.instruction)
    await svc._append_room_event(agent.room_id, EventType.HUMAN_REDIRECTED_AGENT,
        {"agent_id": execution.agent_id, "instruction": req.instruction},
        user_id, "user")
    return {"status": "intervention_recorded"}


# ── Tasks ────────────────────────────────────────────────────────────────────


@router.post("/rooms/{room_id}/tasks")
async def create_task(room_id: str, req: CreateTaskRequest,
                      user_id: str = Query("user_1")) -> dict[str, Any]:
    svc = _svc_or_404()
    priority = _safe_enum(req.priority, TaskPriority, "priority")
    try:
        task = await svc.create_task(room_id, req.title, req.description, priority, user_id)
    except DomainError as e:
        raise HTTPException(400, str(e))
    return {"task_id": task.task_id, "title": task.title, "status": task.status.value}


@router.get("/rooms/{room_id}/tasks")
async def list_tasks(room_id: str) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    tasks = await svc.list_room_tasks(room_id)
    return [{"task_id": t.task_id, "title": t.title, "status": t.status.value,
             "priority": t.priority.value, "assigned_agent_id": t.assigned_agent_id} for t in tasks]


@router.post("/tasks/{task_id}/assign")
async def assign_task(task_id: str, req: AssignTaskRequest) -> dict[str, str]:
    svc = _svc_or_404()
    try:
        await svc.assign_task(task_id, req.agent_id)
    except DomainError as e:
        raise HTTPException(400, str(e))
    return {"status": "assigned"}


@router.post("/tasks/{task_id}/delegate")
async def delegate_task(task_id: str, req: DelegateTaskRequest,
                        from_agent_id: str = Query("")) -> dict[str, Any]:
    svc = _svc_or_404()
    try:
        child = await svc.delegate_task(task_id, from_agent_id, req.to_agent_id, req.description)
    except DomainError as e:
        raise HTTPException(400, str(e))
    return {"child_task_id": child.task_id, "status": child.status.value}


@router.post("/tasks/{task_id}/complete")
async def complete_task(task_id: str) -> dict[str, str]:
    svc = _svc_or_404()
    try:
        await svc.complete_task(task_id)
    except DomainError as e:
        raise HTTPException(400, str(e))
    return {"status": "completed"}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str) -> dict[str, str]:
    svc = _svc_or_404()
    try:
        await svc.cancel_task(task_id)
    except DomainError as e:
        raise HTTPException(400, str(e))
    return {"status": "cancelled"}


# ── Messages ─────────────────────────────────────────────────────────────────


@router.post("/rooms/{room_id}/messages")
async def send_message(room_id: str, req: CreateMessageRequest,
                       user_id: str = Query("user_1")) -> dict[str, Any]:
    svc = _svc_or_404()
    role = _safe_enum(req.role, MessageRole, "role")
    sender = req.sender_id or user_id
    try:
        msg = await svc.send_message(room_id, role, sender, req.content)
    except DomainError as e:
        raise HTTPException(400, str(e))
    return {"message_id": msg.message_id, "role": msg.role.value, "content": msg.content}


@router.get("/rooms/{room_id}/messages")
async def list_messages(room_id: str, limit: int = Query(100, ge=1, le=500)) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    messages = await svc.list_room_messages(room_id, limit)
    return [{"message_id": m.message_id, "role": m.role.value, "sender_id": m.sender_id,
             "content": m.content, "created_at": m.created_at.isoformat()} for m in messages]


# ── Artifacts ────────────────────────────────────────────────────────────────


@router.post("/rooms/{room_id}/artifacts")
async def create_artifact(room_id: str, req: CreateArtifactRequest,
                          user_id: str = Query("user_1")) -> dict[str, Any]:
    svc = _svc_or_404()
    artifact_type = _safe_enum(req.artifact_type, ArtifactType, "artifact_type")
    try:
        art = await svc.create_artifact(room_id, req.name, artifact_type,
                                        req.description, user_id, req.content)
    except DomainError as e:
        raise HTTPException(400, str(e))
    return {"artifact_id": art.artifact_id, "name": art.name, "version": art.current_version}


@router.get("/rooms/{room_id}/artifacts")
async def list_artifacts(room_id: str) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    arts = await svc.list_room_artifacts(room_id)
    return [{"artifact_id": a.artifact_id, "name": a.name, "type": a.artifact_type.value,
             "version": a.current_version} for a in arts]


@router.post("/artifacts/{artifact_id}/versions")
async def update_artifact(artifact_id: str, req: UpdateArtifactRequest,
                          user_id: str = Query("user_1")) -> dict[str, Any]:
    svc = _svc_or_404()
    try:
        ver = await svc.update_artifact(artifact_id, req.content, user_id)
    except DomainError as e:
        raise HTTPException(400, str(e))
    return {"version_id": ver.version_id, "version_number": ver.version_number}


@router.get("/artifacts/{artifact_id}/versions")
async def list_artifact_versions(artifact_id: str) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    versions = await svc.repos.artifacts.list_versions(artifact_id)
    return [{"version_id": v.version_id, "version_number": v.version_number,
             "content": v.content, "created_by": v.created_by,
             "created_at": v.created_at.isoformat()} for v in versions]


# ── Decisions ────────────────────────────────────────────────────────────────


@router.post("/rooms/{room_id}/decisions")
async def create_decision(room_id: str, req: CreateDecisionRequest,
                          user_id: str = Query("user_1")) -> dict[str, Any]:
    svc = _svc_or_404()
    try:
        dec = await svc.create_decision(room_id, req.title, req.content, req.reason, user_id)
    except DomainError as e:
        raise HTTPException(400, str(e))
    return {"decision_id": dec.decision_id, "title": dec.title, "status": dec.status.value}


@router.get("/rooms/{room_id}/decisions")
async def list_decisions(room_id: str) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    decs = await svc.list_room_decisions(room_id)
    return [{"decision_id": d.decision_id, "title": d.title, "content": d.content,
             "status": d.status.value, "created_by": d.created_by} for d in decs]


# ── Memory ───────────────────────────────────────────────────────────────────


@router.post("/rooms/{room_id}/memories")
async def create_memory(room_id: str, req: CreateMemoryRequest,
                        user_id: str = Query("user_1")) -> dict[str, Any]:
    svc = _svc_or_404()
    scope = _safe_enum(req.scope, MemoryScope, "scope")
    try:
        mem = await svc.create_memory(room_id, None, None, scope,
                                      req.content, req.memory_type, user_id)
    except DomainError as e:
        raise HTTPException(400, str(e))
    return {"memory_id": mem.memory_id, "type": mem.memory_type, "scope": mem.scope.value}


@router.get("/rooms/{room_id}/memories")
async def list_memories(room_id: str) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    mems = await svc.list_room_memories(room_id)
    return [{"memory_id": m.memory_id, "content": m.content, "type": m.memory_type,
             "scope": m.scope.value, "is_authoritative": m.is_authoritative} for m in mems]


# ── Approvals ────────────────────────────────────────────────────────────────


@router.post("/rooms/{room_id}/approvals")
async def request_approval(room_id: str, execution_id: str = Query(""),
                           agent_id: str = Query(""),
                           action: str = Query("")) -> dict[str, Any]:
    svc = _svc_or_404()
    try:
        approval = await svc.request_approval(room_id, execution_id, agent_id, action)
    except DomainError as e:
        raise HTTPException(400, str(e))
    return {"approval_id": approval.approval_id, "status": approval.status.value}


@router.get("/rooms/{room_id}/approvals")
async def list_approvals(room_id: str) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    approvals = await svc.list_pending_approvals(room_id)
    return [{"approval_id": a.approval_id, "action": a.action_description,
             "agent_id": a.agent_id, "status": a.status.value} for a in approvals]


@router.post("/approvals/{approval_id}/approve")
async def approve_action(approval_id: str, req: ApproveActionRequest,
                         user_id: str = Query("user_1")) -> dict[str, str]:
    svc = _svc_or_404()
    try:
        await svc.approve_action(approval_id, user_id, req.comment)
    except DomainError as e:
        raise HTTPException(400, str(e))
    return {"status": "approved"}


@router.post("/approvals/{approval_id}/reject")
async def reject_action(approval_id: str, req: ApproveActionRequest,
                        user_id: str = Query("user_1")) -> dict[str, str]:
    svc = _svc_or_404()
    try:
        await svc.reject_action(approval_id, user_id, req.comment)
    except DomainError as e:
        raise HTTPException(400, str(e))
    return {"status": "rejected"}


# ── Human Intervention ───────────────────────────────────────────────────────


@router.post("/agents/{agent_id}/interrupt")
async def interrupt_agent(agent_id: str, req: InterruptAgentRequest,
                          user_id: str = Query("user_1")) -> dict[str, str]:
    svc = _svc_or_404()
    try:
        await svc.interrupt_agent(agent_id, user_id, req.reason)
    except DomainError as e:
        raise HTTPException(400, str(e))
    return {"status": "interrupted"}


@router.post("/agents/{agent_id}/redirect")
async def redirect_agent(agent_id: str, req: RedirectAgentRequest,
                         user_id: str = Query("user_1")) -> dict[str, str]:
    svc = _svc_or_404()
    try:
        await svc.redirect_agent(agent_id, user_id, req.instruction)
    except DomainError as e:
        raise HTTPException(400, str(e))
    return {"status": "redirected"}


# ── Notifications ────────────────────────────────────────────────────────────


@router.get("/notifications")
async def list_notifications(user_id: str = Query("user_1")) -> list[dict[str, Any]]:
    svc = _svc_or_404()
    notifs = await svc.list_notifications(user_id)
    return [{"notification_id": n.notification_id, "title": n.title, "body": n.body,
             "type": n.notification_type, "room_id": n.room_id,
             "created_at": n.created_at.isoformat()} for n in notifs]


# ── Presence ─────────────────────────────────────────────────────────────────


@router.get("/rooms/{room_id}/presence")
async def get_presence(room_id: str) -> list[dict[str, str]]:
    svc = _svc_or_404()
    presence = await svc.presence.get_room_presence(room_id)
    return [{"user_id": p.user_id, "status": p.status.value} for p in presence]
