"""API integration tests using httpx + FastAPI TestClient."""

import pytest
from httpx import ASGITransport, AsyncClient

from multiplayer.server import create_app


@pytest.fixture
async def client():
    app = create_app(":memory:")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        # Wait for lifespan startup
        async with app.router.lifespan_context(app):
            yield c


@pytest.mark.asyncio
async def test_health(client):
    r = await client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_full_api_workflow(client):
    # Create org
    r = await client.post("/api/v1/organizations", json={"name": "Acme", "slug": "acme"})
    assert r.status_code == 200
    org = r.json()
    assert org["name"] == "Acme"

    # Create workspace
    r = await client.post(f"/api/v1/organizations/{org['org_id']}/workspaces",
                          json={"name": "Main", "slug": "main"})
    assert r.status_code == 200
    ws = r.json()

    # Create room
    r = await client.post(f"/api/v1/workspaces/{ws['workspace_id']}/rooms",
                          json={"name": "Auth Migration"})
    assert r.status_code == 200
    room = r.json()

    # Get room
    r = await client.get(f"/api/v1/rooms/{room['room_id']}")
    assert r.status_code == 200
    assert r.json()["name"] == "Auth Migration"

    # Join room
    r = await client.post(f"/api/v1/rooms/{room['room_id']}/join?user_id=user_1")
    assert r.status_code == 200

    # List agents templates
    r = await client.get("/api/v1/agent-templates")
    assert r.status_code == 200
    templates = r.json()
    assert len(templates) >= 4

    # Spawn agent
    r = await client.post(f"/api/v1/rooms/{room['room_id']}/agents",
                          json={"template_id": templates[0]["template_id"], "name": "Forge"})
    assert r.status_code == 200
    agent = r.json()
    assert agent["name"] == "Forge"

    # Start session
    r = await client.post(f"/api/v1/rooms/{room['room_id']}/agents/{agent['agent_id']}/sessions")
    assert r.status_code == 200
    session = r.json()

    # Start execution
    r = await client.post(f"/api/v1/sessions/{session['session_id']}/execute")
    assert r.status_code == 200
    execution = r.json()

    # Execute step
    r = await client.post(f"/api/v1/executions/{execution['execution_id']}/step",
                          json={"prompt": "Analyze the codebase"})
    assert r.status_code == 200

    # Pause
    r = await client.post(f"/api/v1/executions/{execution['execution_id']}/pause")
    assert r.status_code == 200

    # Resume
    r = await client.post(f"/api/v1/executions/{execution['execution_id']}/resume")
    assert r.status_code == 200

    # Cancel
    r = await client.post(f"/api/v1/executions/{execution['execution_id']}/cancel")
    assert r.status_code == 200

    # Create task
    r = await client.post(f"/api/v1/rooms/{room['room_id']}/tasks",
                          json={"title": "Build auth", "priority": "HIGH"})
    assert r.status_code == 200
    task = r.json()

    # Assign task
    r = await client.post(f"/api/v1/tasks/{task['task_id']}/assign",
                          json={"agent_id": agent["agent_id"]})
    assert r.status_code == 200

    # Complete task
    r = await client.post(f"/api/v1/tasks/{task['task_id']}/complete")
    assert r.status_code == 200

    # Send message
    r = await client.post(f"/api/v1/rooms/{room['room_id']}/messages",
                          json={"content": "Hello!", "role": "HUMAN"})
    assert r.status_code == 200
    msg = r.json()
    assert msg["content"] == "Hello!"

    # List messages
    r = await client.get(f"/api/v1/rooms/{room['room_id']}/messages")
    assert r.status_code == 200
    assert len(r.json()) >= 1

    # Create artifact
    r = await client.post(f"/api/v1/rooms/{room['room_id']}/artifacts",
                          json={"name": "design.md", "artifact_type": "DOCUMENT",
                                "content": "# Design"})
    assert r.status_code == 200
    art = r.json()

    # List artifacts
    r = await client.get(f"/api/v1/rooms/{room['room_id']}/artifacts")
    assert r.status_code == 200
    assert len(r.json()) == 1

    # Update artifact
    r = await client.post(f"/api/v1/artifacts/{art['artifact_id']}/versions",
                          json={"content": "# Design v2"})
    assert r.status_code == 200

    # Create decision
    r = await client.post(f"/api/v1/rooms/{room['room_id']}/decisions",
                          json={"title": "Use OAuth2", "content": "Industry standard"})
    assert r.status_code == 200

    # List decisions
    r = await client.get(f"/api/v1/rooms/{room['room_id']}/decisions")
    assert r.status_code == 200
    assert len(r.json()) == 1

    # Create memory
    r = await client.post(f"/api/v1/rooms/{room['room_id']}/memories",
                          json={"content": "OAuth2 decided", "scope": "ROOM"})
    assert r.status_code == 200

    # List memories
    r = await client.get(f"/api/v1/rooms/{room['room_id']}/memories")
    assert r.status_code == 200
    assert len(r.json()) == 1

    # Get room events
    r = await client.get(f"/api/v1/rooms/{room['room_id']}/events")
    assert r.status_code == 200
    events = r.json()
    assert len(events) > 0

    # Get full room state
    r = await client.get(f"/api/v1/rooms/{room['room_id']}/state")
    assert r.status_code == 200
    state = r.json()
    assert "room" in state
    assert "agents" in state
    assert "tasks" in state
    assert "messages" in state

    # Interrupt agent
    r = await client.post(f"/api/v1/agents/{agent['agent_id']}/interrupt",
                          json={"reason": "Stop"})
    assert r.status_code == 200

    # Redirect agent
    r = await client.post(f"/api/v1/agents/{agent['agent_id']}/redirect",
                          json={"instruction": "Use Redis"})
    assert r.status_code == 200

    # List rooms
    r = await client.get(f"/api/v1/workspaces/{ws['workspace_id']}/rooms")
    assert r.status_code == 200
    assert len(r.json()) == 1

    # List agents in room
    r = await client.get(f"/api/v1/rooms/{room['room_id']}/agents")
    assert r.status_code == 200
    assert len(r.json()) == 1

    # Presence
    r = await client.get(f"/api/v1/rooms/{room['room_id']}/presence")
    assert r.status_code == 200
