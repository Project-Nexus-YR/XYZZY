"""A tool request's terminal state and the event that explains it are one fact.

``ToolRequestRepository.resolve()`` used to self-commit ahead of a second,
separate commit for the canonical event (service.py's TOOL_CALL_COMPLETED,
TOOL_CALL_REJECTED and TOOL_CALL_FAILED appends): a crash between the two left
a terminal status on the row with no event recording it, or — for a
successfully executed call — an EXECUTED status with no TOOL_CALL_COMPLETED
event at all. ``_resolve_tool_request_terminal`` now wraps both writes in one
transaction; these tests force the event append to fail and assert the
request is left exactly as it was, never landed in the terminal state the
failed write was supposed to explain.
"""

from __future__ import annotations

import pytest

from multiplayer.db.connection import Database
from multiplayer.db.repositories import EventRepo
from multiplayer.domain.models import DomainError, ToolRequest
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService

OWNER = "owner"


@pytest.fixture
async def service():
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({OWNER}))
    await svc.initialize()
    yield svc
    await db.close()


async def _room_and_agent(svc: MultiplayerService) -> tuple[str, str]:
    org = await svc.create_organization("Org", "org", OWNER)
    workspace = await svc.create_workspace(org.org_id, "Main", "main", OWNER)
    room = await svc.create_room(workspace.workspace_id, "Room", OWNER)
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(room.room_id, templates[0].template_id, requested_by=OWNER)
    return room.room_id, agent.agent_id


def _request(room_id: str, agent_id: str) -> ToolRequest:
    return ToolRequest(
        request_id="toolreq_test1",
        room_id=room_id,
        execution_id="exec_test1",
        agent_id=agent_id,
        requested_by=agent_id,
        authorized_by=OWNER,
        tool="noop",
        status="PENDING_APPROVAL",
    )


async def _boom(*_args, **_kwargs):
    raise RuntimeError("event append failed")


async def test_a_failed_event_append_leaves_a_successful_call_unresolved(service, monkeypatch):
    room_id, agent_id = await _room_and_agent(service)
    request = _request(room_id, agent_id)
    await service.repos.tool_requests.create(request)

    async def _ok_tool(_req):
        return {"ok": True}

    monkeypatch.setattr(service, "_run_tool", _ok_tool)
    monkeypatch.setattr(EventRepo, "append_with_next_sequence_in_transaction", _boom)

    with pytest.raises(RuntimeError):
        await service._execute_tool_request(request)

    row = await service.repos.tool_requests.get(request.request_id)
    assert row is not None
    # Never moved to EXECUTED: the write that would have said so rolled back
    # with the event it could not record beside it.
    assert row.status == "PENDING_APPROVAL"
    assert row.result_json == "{}"
    assert row.resolved_at is None


async def test_a_failed_event_append_leaves_a_domain_failure_unresolved(service, monkeypatch):
    room_id, agent_id = await _room_and_agent(service)
    request = _request(room_id, agent_id)
    await service.repos.tool_requests.create(request)

    async def _blow_up(_req):
        raise DomainError("tool refused its own input")

    monkeypatch.setattr(service, "_run_tool", _blow_up)
    monkeypatch.setattr(EventRepo, "append_with_next_sequence_in_transaction", _boom)

    with pytest.raises(RuntimeError):
        await service._execute_tool_request(request)

    row = await service.repos.tool_requests.get(request.request_id)
    assert row is not None
    assert row.status == "PENDING_APPROVAL"  # never moved to FAILED
    assert row.resolved_at is None


async def test_a_failed_event_append_leaves_an_unnamed_exception_unresolved(service, monkeypatch):
    room_id, agent_id = await _room_and_agent(service)
    request = _request(room_id, agent_id)
    await service.repos.tool_requests.create(request)

    async def _blow_up(_req):
        raise ValueError("nobody anticipated this one")

    monkeypatch.setattr(service, "_run_tool", _blow_up)
    monkeypatch.setattr(EventRepo, "append_with_next_sequence_in_transaction", _boom)

    with pytest.raises(RuntimeError):
        await service._execute_tool_request(request)

    row = await service.repos.tool_requests.get(request.request_id)
    assert row is not None
    assert row.status == "PENDING_APPROVAL"  # never moved to FAILED
    assert row.resolved_at is None
