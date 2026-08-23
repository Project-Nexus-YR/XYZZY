"""Failure injection tests: verify graceful handling of errors."""

import pytest

from multiplayer.db.connection import Database
from multiplayer.domain.models import DomainError, MessageRole
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
async def test_broadcast_failure_does_not_rollback_event(service):
    """If broadcast fails, the event is still persisted."""
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room = await service.create_room(ws.workspace_id, "R", "u1")

    # Make broadcast fail
    original_broadcast = service.hub.broadcast_room_event

    async def failing_broadcast(event):
        raise RuntimeError("broadcast exploded")

    service.hub.broadcast_room_event = failing_broadcast

    # This should succeed even though broadcast fails
    msg = await service.send_message(room.room_id, MessageRole.HUMAN, "u1", "Test msg")
    assert msg.content == "Test msg"

    # Event should still be in the DB
    events = await service.get_room_events(room.room_id)
    event_types = [e.event_type.value for e in events]
    assert "message.created" in event_types

    # Restore
    service.hub.broadcast_room_event = original_broadcast


@pytest.mark.asyncio
async def test_nonexistent_room_raises_domain_error(service):
    with pytest.raises(DomainError, match="room not found"):
        await service.get_room("nonexistent_room")


@pytest.mark.asyncio
async def test_nonexistent_agent_raises_domain_error(service):
    with pytest.raises(DomainError, match="agent not found"):
        await service.get_agent("nonexistent_agent")


@pytest.mark.asyncio
async def test_nonexistent_task_raises_domain_error(service):
    with pytest.raises(DomainError, match="task not found"):
        await service.assign_task("nonexistent_task", "agent_1")


@pytest.mark.asyncio
async def test_nonexistent_session_raises_domain_error(service):
    with pytest.raises(DomainError, match="session not found"):
        await service.start_execution("nonexistent_session", "u1")


@pytest.mark.asyncio
async def test_nonexistent_execution_raises_domain_error(service):
    with pytest.raises(DomainError, match="execution not found"):
        await service.execute_agent_step("nonexistent_exec", "prompt")


@pytest.mark.asyncio
async def test_empty_name_rejected(service):
    with pytest.raises(DomainError, match="must not be empty"):
        await service.create_organization("", "slug", "u1")

    with pytest.raises(DomainError, match="must not be empty"):
        await service.create_task("room", "", "desc")


@pytest.mark.asyncio
async def test_empty_message_rejected(service):
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room = await service.create_room(ws.workspace_id, "R", "u1")

    with pytest.raises(DomainError, match="must not be empty"):
        await service.send_message(room.room_id, MessageRole.HUMAN, "u1", "")


@pytest.mark.asyncio
async def test_input_validation_trims_whitespace(service):
    org = await service.create_organization("  Acme  ", "  acme  ", "u1")
    assert org.name == "Acme"
    assert org.slug == "acme"


@pytest.mark.asyncio
async def test_limit_validation_clamps(service):
    """Message limit should be clamped to [1, 500]."""
    assert MultiplayerService._validate_limit(0) == 1
    assert MultiplayerService._validate_limit(-5) == 1
    assert MultiplayerService._validate_limit(10000) == 500
    assert MultiplayerService._validate_limit(100) == 100


@pytest.mark.asyncio
async def test_id_too_long_rejected(service):
    with pytest.raises(DomainError, match="too long"):
        MultiplayerService._validate_id("x" * 300, "test_id")


@pytest.mark.asyncio
async def test_nexus_bridge_stub_step():
    """Credential-free mode must be conspicuously labelled as simulated."""
    from multiplayer.domain.models import AgentInstance, Execution, Session
    from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge

    bridge = NexusAgentBridge()
    agent = AgentInstance(
        agent_id="a1",
        template_id="t1",
        room_id="r1",
        name="Test",
        role="Coder",
        system_prompt="test",
        capabilities=frozenset(),
        model_provider="",
        model_name="",
    )
    session = Session(session_id="s1", room_id="r1", agent_id="a1")
    execution = Execution(execution_id="e1", session_id="s1", agent_id="a1")

    await bridge.create_execution(agent, session, "test task", execution)
    result = await bridge.execute_step("e1", "do something")

    assert result["status"] == "ok"
    assert result["action"] == "finish"
    assert result["result"]["simulated"] is True
    assert "SIMULATED WORKFLOW OUTPUT" in result["result"]["content"]
    assert "stub response" not in result["result"]["content"]


@pytest.mark.asyncio
async def test_nexus_bridge_execute_step_unknown_execution():
    """Executing on unknown execution must raise DomainError."""
    from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge

    bridge = NexusAgentBridge()
    with pytest.raises(DomainError, match="no active run"):
        await bridge.execute_step("unknown_exec", "prompt")


@pytest.mark.asyncio
async def test_nexus_bridge_pause_unknown_returns_false():
    from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge

    bridge = NexusAgentBridge()
    assert await bridge.pause_execution("unknown") is False
    assert await bridge.resume_execution("unknown") is False
    assert await bridge.cancel_execution("unknown") is False
