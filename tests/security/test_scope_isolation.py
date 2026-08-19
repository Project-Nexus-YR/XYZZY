"""Scope isolation tests: ensure rooms/workspaces don't leak state."""

import pytest
from multiplayer.db.connection import Database
from multiplayer.services.service import MultiplayerService
from multiplayer.domain.models import (
    DomainError,
    MessageRole,
    ArtifactType,
    MemoryScope,
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
async def test_messages_isolated_to_room(service):
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room_a = await service.create_room(ws.workspace_id, "A", "u1")
    room_b = await service.create_room(ws.workspace_id, "B", "u1")

    await service.send_message(room_a.room_id, MessageRole.HUMAN, "u1", "Hello A")
    await service.send_message(room_b.room_id, MessageRole.HUMAN, "u1", "Hello B")

    msgs_a = await service.list_room_messages(room_a.room_id)
    msgs_b = await service.list_room_messages(room_b.room_id)

    assert len(msgs_a) == 1
    assert msgs_a[0].content == "Hello A"
    assert len(msgs_b) == 1
    assert msgs_b[0].content == "Hello B"


@pytest.mark.asyncio
async def test_agents_isolated_to_room(service):
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room_a = await service.create_room(ws.workspace_id, "A", "u1")
    room_b = await service.create_room(ws.workspace_id, "B", "u1")
    templates = await service.list_agent_templates()

    await service.spawn_agent(room_a.room_id, templates[0].template_id, "Agent A")
    await service.spawn_agent(room_b.room_id, templates[0].template_id, "Agent B")

    agents_a = await service.list_room_agents(room_a.room_id)
    agents_b = await service.list_room_agents(room_b.room_id)

    assert len(agents_a) == 1
    assert agents_a[0].name == "Agent A"
    assert len(agents_b) == 1
    assert agents_b[0].name == "Agent B"


@pytest.mark.asyncio
async def test_artifacts_isolated_to_room(service):
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room_a = await service.create_room(ws.workspace_id, "A", "u1")
    room_b = await service.create_room(ws.workspace_id, "B", "u1")

    await service.create_artifact(room_a.room_id, "doc_a.md", ArtifactType.DOCUMENT, "A doc", "u1", "content a")
    await service.create_artifact(room_b.room_id, "doc_b.md", ArtifactType.DOCUMENT, "B doc", "u1", "content b")

    arts_a = await service.list_room_artifacts(room_a.room_id)
    arts_b = await service.list_room_artifacts(room_b.room_id)

    assert len(arts_a) == 1
    assert arts_a[0].name == "doc_a.md"
    assert len(arts_b) == 1
    assert arts_b[0].name == "doc_b.md"


@pytest.mark.asyncio
async def test_tasks_isolated_to_room(service):
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room_a = await service.create_room(ws.workspace_id, "A", "u1")
    room_b = await service.create_room(ws.workspace_id, "B", "u1")

    await service.create_task(room_a.room_id, "Task A")
    await service.create_task(room_b.room_id, "Task B")

    tasks_a = await service.list_room_tasks(room_a.room_id)
    tasks_b = await service.list_room_tasks(room_b.room_id)

    assert len(tasks_a) == 1
    assert tasks_a[0].title == "Task A"
    assert len(tasks_b) == 1
    assert tasks_b[0].title == "Task B"


@pytest.mark.asyncio
async def test_events_isolated_to_room(service):
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room_a = await service.create_room(ws.workspace_id, "A", "u1")
    room_b = await service.create_room(ws.workspace_id, "B", "u1")

    events_a = await service.get_room_events(room_a.room_id)
    events_b = await service.get_room_events(room_b.room_id)

    # Each room has only its own creation event
    assert len(events_a) == 1
    assert events_a[0].event_type.value == "room.created"
    assert len(events_b) == 1
    assert events_b[0].event_type.value == "room.created"


@pytest.mark.asyncio
async def test_memories_isolated_to_room(service):
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room_a = await service.create_room(ws.workspace_id, "A", "u1")
    room_b = await service.create_room(ws.workspace_id, "B", "u1")

    await service.create_memory(room_a.room_id, None, None, MemoryScope.ROOM, "Memory A", "fact", "u1")
    await service.create_memory(room_b.room_id, None, None, MemoryScope.ROOM, "Memory B", "fact", "u1")

    mems_a = await service.list_room_memories(room_a.room_id)
    mems_b = await service.list_room_memories(room_b.room_id)

    assert len(mems_a) == 1
    assert mems_a[0].content == "Memory A"
    assert len(mems_b) == 1
    assert mems_b[0].content == "Memory B"


@pytest.mark.asyncio
async def test_room_state_isolation(service):
    """Full room state snapshot contains only that room's data."""
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room_a = await service.create_room(ws.workspace_id, "A", "u1")
    room_b = await service.create_room(ws.workspace_id, "B", "u1")
    templates = await service.list_agent_templates()

    await service.spawn_agent(room_a.room_id, templates[0].template_id, "Agent A")
    await service.send_message(room_a.room_id, MessageRole.HUMAN, "u1", "msg A")
    await service.send_message(room_b.room_id, MessageRole.HUMAN, "u1", "msg B")

    state_a = await service.get_room_state(room_a.room_id)
    state_b = await service.get_room_state(room_b.room_id)

    assert state_a["room"]["name"] == "A"
    assert len(state_a["agents"]) == 1
    assert len(state_a["messages"]) == 1
    assert state_a["messages"][0]["content"] == "msg A"

    assert state_b["room"]["name"] == "B"
    assert len(state_b["agents"]) == 0
    assert len(state_b["messages"]) == 1
    assert state_b["messages"][0]["content"] == "msg B"


@pytest.mark.asyncio
async def test_agent_must_be_in_room_for_session(service):
    """Cannot start a session for an agent in a different room."""
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room_a = await service.create_room(ws.workspace_id, "A", "u1")
    room_b = await service.create_room(ws.workspace_id, "B", "u1")
    templates = await service.list_agent_templates()

    agent = await service.spawn_agent(room_a.room_id, templates[0].template_id)

    with pytest.raises(DomainError, match="not in this room"):
        await service.start_agent_session(room_b.room_id, agent.agent_id)
