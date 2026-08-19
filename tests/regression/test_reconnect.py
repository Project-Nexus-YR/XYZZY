"""Reconnect correctness tests: verify state reconstruction."""

import pytest
from multiplayer.db.connection import Database
from multiplayer.services.service import MultiplayerService
from multiplayer.domain.models import (
    MessageRole,
    ArtifactType,
    MemoryScope,
    AgentStatus,
    TaskStatus,
)
from multiplayer.realtime.hub import RealtimeHub


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
async def test_full_room_state_after_activities(service):
    """Room state snapshot captures all activity types."""
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room = await service.create_room(ws.workspace_id, "R", "u1")
    templates = await service.list_agent_templates()

    # Build up state
    await service.send_message(room.room_id, MessageRole.HUMAN, "u1", "Hello")
    agent = await service.spawn_agent(room.room_id, templates[0].template_id, "Coder")
    await service.create_task(room.room_id, "Build API", "REST endpoints")
    await service.create_artifact(
        room.room_id, "api.md", ArtifactType.DOCUMENT, "API doc", "u1", "# API"
    )
    await service.create_decision(room.room_id, "Use FastAPI", "It's async")
    await service.create_memory(
        room.room_id, None, None, MemoryScope.ROOM, "We use Python 3.13", "fact", "u1"
    )

    state = await service.get_room_state(room.room_id)

    assert len(state["members"]) >= 1
    assert len(state["agents"]) == 1
    assert state["agents"][0]["name"] == "Coder"
    assert state["agents"][0]["status"] == AgentStatus.IDLE.value
    assert len(state["tasks"]) == 1
    assert state["tasks"][0]["title"] == "Build API"
    assert len(state["messages"]) == 1
    assert state["messages"][0]["content"] == "Hello"
    assert len(state["artifacts"]) == 1
    assert state["artifacts"][0]["name"] == "api.md"
    assert len(state["decisions"]) == 1
    assert state["decisions"][0]["title"] == "Use FastAPI"
    assert len(state["memories"]) == 1
    assert state["memories"][0]["content"] == "We use Python 3.13"


@pytest.mark.asyncio
async def test_reconnect_with_sequence_filter(service):
    """Reconnecting with a last_sequence returns only newer events."""
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room = await service.create_room(ws.workspace_id, "R", "u1")

    await service.send_message(room.room_id, MessageRole.HUMAN, "u1", "msg1")
    await service.send_message(room.room_id, MessageRole.HUMAN, "u1", "msg2")
    await service.send_message(room.room_id, MessageRole.HUMAN, "u1", "msg3")

    events = await service.get_room_events(room.room_id)
    assert len(events) == 4  # room_created + 3 messages

    # Get only events after sequence 2
    recent = await service.get_room_events(room.room_id, after_sequence=2)
    assert len(recent) == 2
    assert recent[0].sequence == 3
    assert recent[1].sequence == 4


@pytest.mark.asyncio
async def test_reconnect_full_state_has_no_events_when_caught_up(service):
    """When last_sequence matches latest, events_since is empty."""
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room = await service.create_room(ws.workspace_id, "R", "u1")

    await service.send_message(room.room_id, MessageRole.HUMAN, "u1", "Hello")

    events = await service.get_room_events(room.room_id)
    last_seq = events[-1].sequence

    state = await service.get_room_state(room.room_id, last_seq)
    assert len(state["events_since"]) == 0


@pytest.mark.asyncio
async def test_reconnect_partial_state_has_only_new_events(service):
    """Reconnecting with a mid-range sequence returns only newer events."""
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room = await service.create_room(ws.workspace_id, "R", "u1")

    await service.send_message(room.room_id, MessageRole.HUMAN, "u1", "msg1")
    events_at_1 = await service.get_room_events(room.room_id)
    seq1 = events_at_1[-1].sequence  # Should be 2 (room_created=1, msg1=2)

    await service.send_message(room.room_id, MessageRole.HUMAN, "u1", "msg2")
    await service.send_message(room.room_id, MessageRole.HUMAN, "u1", "msg3")

    state = await service.get_room_state(room.room_id, seq1)
    assert len(state["events_since"]) == 2  # msg2 and msg3


@pytest.mark.asyncio
async def test_reconnect_preserves_agent_status(service):
    """Agent status should be reflected in reconnect state."""
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room = await service.create_room(ws.workspace_id, "R", "u1")
    templates = await service.list_agent_templates()
    agent = await service.spawn_agent(room.room_id, templates[0].template_id, "Worker")

    # Change agent status
    await service.update_agent_status(agent.agent_id, AgentStatus.WORKING)
    await service.update_agent_status(agent.agent_id, AgentStatus.THINKING)

    state = await service.get_room_state(room.room_id)
    assert state["agents"][0]["status"] == AgentStatus.THINKING.value


@pytest.mark.asyncio
async def test_reconnect_preserves_task_status(service):
    """Task status should be reflected in reconnect state."""
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room = await service.create_room(ws.workspace_id, "R", "u1")
    templates = await service.list_agent_templates()
    agent = await service.spawn_agent(room.room_id, templates[0].template_id)

    task = await service.create_task(room.room_id, "Task 1")
    task = await service.assign_task(task.task_id, agent.agent_id)

    state = await service.get_room_state(room.room_id)
    assert state["tasks"][0]["status"] == TaskStatus.ASSIGNED.value
    assert state["tasks"][0]["assigned_agent_id"] == agent.agent_id


@pytest.mark.asyncio
async def test_reconnect_sequence_zero_gets_all(service):
    """last_sequence=0 returns all events."""
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room = await service.create_room(ws.workspace_id, "R", "u1")
    await service.send_message(room.room_id, MessageRole.HUMAN, "u1", "Hello")

    state = await service.get_room_state(room.room_id, 0)
    assert len(state["events_since"]) > 0
