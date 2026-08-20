"""Integration tests for the multiplayer service layer."""

import pytest

from multiplayer.db.connection import Database
from multiplayer.domain.models import (
    AgentStatus,
    ArtifactType,
    MemoryScope,
    MessageRole,
)
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService


@pytest.fixture
async def service():
    db = Database(":memory:")
    await db.connect()
    hub = RealtimeHub()
    svc = MultiplayerService(db, hub)
    await svc.initialize()
    yield svc
    await db.close()


@pytest.mark.asyncio
async def test_full_workflow(service):
    # Create org + workspace
    org = await service.create_organization("Acme", "acme", "u1")
    assert org.name == "Acme"

    ws = await service.create_workspace(org.org_id, "Main", "main", "u1")
    assert ws.name == "Main"

    # Create room
    room = await service.create_room(ws.workspace_id, "Auth Migration", "u1", "Migrate auth")
    assert room.name == "Auth Migration"

    # Join room
    await service.join_room(room.room_id, "u1")
    presence = await service.presence.get_room_presence(room.room_id)
    assert len(presence) == 1

    # Spawn agents
    templates = await service.list_agent_templates()
    assert len(templates) >= 4

    architect = await service.spawn_agent(room.room_id, templates[0].template_id, "Forge Architect")
    assert architect.name == "Forge Architect"
    assert architect.status == AgentStatus.IDLE

    coder = await service.spawn_agent(room.room_id, templates[2].template_id, "Forge Coder")

    # Start agent session
    session = await service.start_agent_session(room.room_id, architect.agent_id)
    assert session.status.value == "CREATED"

    # Start execution
    execution = await service.start_execution(session.session_id)
    assert execution.status.value == "PENDING"

    # Execute a step
    result = await service.execute_agent_step(execution.execution_id, "Analyze the codebase")
    assert result["status"] == "ok"

    # Create task
    task = await service.create_task(room.room_id, "Implement auth", "Build OAuth2 flow")
    assert task.title == "Implement auth"

    # Assign task
    task = await service.assign_task(task.task_id, coder.agent_id)
    assert task.status.value == "ASSIGNED"

    # Delegate task
    templates2 = await service.list_agent_templates()
    security = await service.spawn_agent(room.room_id, templates2[3].template_id)
    child = await service.delegate_task(
        task.task_id, architect.agent_id, security.agent_id, "Security review"
    )
    assert child.parent_task_id == task.task_id

    # Complete task
    task = await service.complete_task(task.task_id)
    assert task.status.value == "COMPLETED"

    # Send message
    msg = await service.send_message(room.room_id, MessageRole.HUMAN, "u1", "Looks good!")
    assert msg.content == "Looks good!"

    # Create artifact
    art = await service.create_artifact(
        room.room_id,
        "auth_design.md",
        ArtifactType.DOCUMENT,
        "Auth design doc",
        "u1",
        "# Auth Design\n\nOAuth2 flow",
    )
    assert art.current_version == 1

    # Update artifact
    ver = await service.update_artifact(art.artifact_id, "# Auth Design v2\n\nUpdated flow", "u1")
    assert ver.version_number == 2

    # Create decision
    dec = await service.create_decision(room.room_id, "Use OAuth2", "Industry standard")
    assert dec.title == "Use OAuth2"

    # Create memory
    mem = await service.create_memory(
        room.room_id, None, None, MemoryScope.ROOM, "We decided OAuth2 is the way", "decision", "u1"
    )
    assert mem.memory_type == "decision"

    # Request approval
    approval = await service.request_approval(
        room.room_id, execution.execution_id, architect.agent_id, "Deploy to production"
    )
    assert approval.status.value == "PENDING"

    # Approve
    approved = await service.approve_action(approval.approval_id, "u1", "Go ahead")
    assert approved.status.value == "APPROVED"

    # Interrupt agent
    await service.interrupt_agent(architect.agent_id, "u1", "Stop and reconsider")
    agent = await service.get_agent(architect.agent_id)
    assert agent.status == AgentStatus.PAUSED

    # Redirect agent
    await service.redirect_agent(architect.agent_id, "u1", "Use Redis instead of Memcached")
    # Events should exist
    events = await service.get_room_events(room.room_id)
    assert len(events) > 10


@pytest.mark.asyncio
async def test_room_state_reconnect(service):
    org = await service.create_organization("Acme", "acme", "u1")
    ws = await service.create_workspace(org.org_id, "Main", "main", "u1")
    room = await service.create_room(ws.workspace_id, "Test", "u1")

    # Add some state
    await service.join_room(room.room_id, "u1")
    templates = await service.list_agent_templates()
    await service.spawn_agent(room.room_id, templates[0].template_id)
    await service.send_message(room.room_id, MessageRole.HUMAN, "u1", "Hello")

    # Get full state
    state = await service.get_room_state(room.room_id)
    assert len(state["members"]) == 1
    assert len(state["agents"]) == 1
    assert len(state["messages"]) == 1

    # Simulate reconnect with sequence
    events = await service.get_room_events(room.room_id)
    last_seq = events[-1].sequence if events else 0
    state2 = await service.get_room_state(room.room_id, last_seq)
    assert len(state2["events_since"]) == 0


@pytest.mark.asyncio
async def test_notification(service):
    await service.create_notification(
        "u1", "Task Complete", "Auth is done", notification_type="info"
    )
    notifs = await service.list_notifications("u1")
    assert len(notifs) == 1
    assert notifs[0].title == "Task Complete"


@pytest.mark.asyncio
async def test_leave_room_events(service):
    org = await service.create_organization("Acme", "acme", "u1")
    ws = await service.create_workspace(org.org_id, "Main", "main", "u1")
    room = await service.create_room(ws.workspace_id, "Test", "u1")
    await service.join_room(room.room_id, "u1")
    await service.leave_room(room.room_id, "u1")
    events = await service.get_room_events(room.room_id)
    types = [e.event_type.value for e in events]
    assert "user.joined_room" in types
    assert "user.left_room" in types
