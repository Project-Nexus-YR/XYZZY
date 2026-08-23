"""State machine transition tests: verify all valid/invalid transitions."""

import pytest

from multiplayer.db.connection import Database
from multiplayer.domain.models import (
    AgentStatus,
    DomainError,
    SessionStatus,
    Task,
    TaskStatus,
)
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import (
    VALID_TASK_TRANSITIONS,
    MultiplayerService,
    _validate_transition,
)


@pytest.fixture
async def service():
    db = Database(":memory:")
    await db.connect()
    hub = RealtimeHub()
    svc = MultiplayerService(db, hub)
    await svc.initialize()
    yield svc
    await db.close()


# ── Task state machine ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_valid_transitions(service):
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room = await service.create_room(ws.workspace_id, "R", "u1")
    templates = await service.list_agent_templates()
    agent = await service.spawn_agent(room.room_id, templates[0].template_id)

    # CREATED -> ASSIGNED -> COMPLETED
    task = await service.create_task(room.room_id, "T1")
    task = await service.assign_task(task.task_id, agent.agent_id)
    assert task.status == TaskStatus.ASSIGNED
    task = await service.complete_task(task.task_id)
    assert task.status == TaskStatus.COMPLETED

    # CREATED -> CANCELLED
    task2 = await service.create_task(room.room_id, "T2")
    task2 = await service.cancel_task(task2.task_id)
    assert task2.status == TaskStatus.CANCELLED

    # CREATED -> ASSIGNED -> IN_PROGRESS -> COMPLETED
    task3 = await service.create_task(room.room_id, "T3")
    task3 = await service.assign_task(task3.task_id, agent.agent_id)
    task3_in_progress = Task(
        task_id=task3.task_id,
        room_id=task3.room_id,
        title=task3.title,
        description=task3.description,
        status=TaskStatus.IN_PROGRESS,
        priority=task3.priority,
        assigned_agent_id=task3.assigned_agent_id,
        created_by=task3.created_by,
        parent_task_id=task3.parent_task_id,
        delegation_id=task3.delegation_id,
    )
    await service.repos.tasks.update(task3_in_progress)
    task3 = await service.repos.tasks.get(task3.task_id)
    task3 = await service.complete_task(task3.task_id)
    assert task3.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_task_invalid_transitions(service):
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room = await service.create_room(ws.workspace_id, "R", "u1")
    templates = await service.list_agent_templates()
    agent = await service.spawn_agent(room.room_id, templates[0].template_id)

    # COMPLETED -> anything is invalid
    task = await service.create_task(room.room_id, "T1")
    task = await service.assign_task(task.task_id, agent.agent_id)
    task = await service.complete_task(task.task_id)

    with pytest.raises(DomainError, match="invalid task transition"):
        await service.cancel_task(task.task_id)

    # CANCELLED -> ASSIGNED is invalid (only -> CREATED)
    task2 = await service.create_task(room.room_id, "T2")
    task2 = await service.cancel_task(task2.task_id)
    with pytest.raises(DomainError, match="invalid task transition"):
        await service.assign_task(task2.task_id, agent.agent_id)

    # CREATED -> COMPLETED is invalid (must go through ASSIGNED)
    task3 = await service.create_task(room.room_id, "T3")
    with pytest.raises(DomainError, match="invalid task transition"):
        await service.complete_task(task3.task_id)


# ── Agent state machine ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_valid_transitions(service):
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room = await service.create_room(ws.workspace_id, "R", "u1")
    templates = await service.list_agent_templates()
    agent = await service.spawn_agent(room.room_id, templates[0].template_id)
    assert agent.status == AgentStatus.IDLE

    # IDLE -> WORKING
    await service.update_agent_status(agent.agent_id, AgentStatus.WORKING)
    a = await service.get_agent(agent.agent_id)
    assert a.status == AgentStatus.WORKING

    # WORKING -> COMPLETED
    await service.update_agent_status(agent.agent_id, AgentStatus.COMPLETED)
    a = await service.get_agent(agent.agent_id)
    assert a.status == AgentStatus.COMPLETED

    # COMPLETED -> IDLE
    await service.update_agent_status(agent.agent_id, AgentStatus.IDLE)
    a = await service.get_agent(agent.agent_id)
    assert a.status == AgentStatus.IDLE


@pytest.mark.asyncio
async def test_agent_invalid_transitions(service):
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room = await service.create_room(ws.workspace_id, "R", "u1")
    templates = await service.list_agent_templates()
    agent = await service.spawn_agent(room.room_id, templates[0].template_id)

    # IDLE -> COMPLETED is invalid
    with pytest.raises(DomainError, match="invalid agent transition"):
        await service.update_agent_status(agent.agent_id, AgentStatus.COMPLETED)

    # IDLE -> WAITING_APPROVAL is invalid
    with pytest.raises(DomainError, match="invalid agent transition"):
        await service.update_agent_status(agent.agent_id, AgentStatus.WAITING_APPROVAL)


# ── Session state machine ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_valid_transitions(service):
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room = await service.create_room(ws.workspace_id, "R", "u1")
    templates = await service.list_agent_templates()
    agent = await service.spawn_agent(room.room_id, templates[0].template_id)

    session = await service.start_agent_session(room.room_id, agent.agent_id)
    assert session.status == SessionStatus.CREATED

    # CREATED -> ACTIVE (via start_execution)
    await service.start_execution(session.session_id, "u1")
    session = await service.repos.sessions.get(session.session_id)
    assert session.status == SessionStatus.ACTIVE


@pytest.mark.asyncio
async def test_session_invalid_start_execution_twice(service):
    """Cannot start_execution on an already ACTIVE session."""
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room = await service.create_room(ws.workspace_id, "R", "u1")
    templates = await service.list_agent_templates()
    agent = await service.spawn_agent(room.room_id, templates[0].template_id)

    session = await service.start_agent_session(room.room_id, agent.agent_id)
    await service.start_execution(session.session_id, "u1")

    with pytest.raises(DomainError, match="invalid session transition"):
        await service.start_execution(session.session_id, "u1")


# ── _validate_transition edge cases ────────────────────────────────────────


def test_validate_transition_unknown_state():
    """Unknown source state (not in transition table) should raise DomainError."""
    with pytest.raises(DomainError, match="invalid task transition"):
        # Use a valid but unexpected state pair where source isn't in the table
        # All valid states are in the table, so we test with a source that has
        # no outgoing transitions for the desired target
        _validate_transition(
            TaskStatus.COMPLETED, TaskStatus.IN_PROGRESS, VALID_TASK_TRANSITIONS, "task"
        )


def test_validate_transition_to_self():
    """Terminal states should not allow self-transition if not in valid set."""
    with pytest.raises(DomainError):
        _validate_transition(
            TaskStatus.COMPLETED, TaskStatus.COMPLETED, VALID_TASK_TRANSITIONS, "task"
        )
