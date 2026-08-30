"""Integration tests for the database layer."""

from pathlib import Path

import pytest

from multiplayer.db.connection import Database
from multiplayer.db.repositories import Repos
from multiplayer.domain.events import EventType, RoomEvent
from multiplayer.domain.models import (
    AgentInstance,
    AgentStatus,
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactType,
    ArtifactVersion,
    Decision,
    DecisionStatus,
    Memory,
    MemoryScope,
    Message,
    MessageRole,
    Notification,
    Organization,
    OrgMember,
    Room,
    RoomMember,
    Task,
    TaskStatus,
    ToolPermission,
    User,
    UserStatus,
    Workspace,
    WorkspaceMember,
    utcnow,
)


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.connect()
    for migration in sorted(Path("src/multiplayer/migrations").glob("*.sql")):
        await database.execute_script(migration.read_text())
    yield database
    await database.close()


@pytest.fixture
async def repos(db):
    # Seed prerequisite entities for FK constraints
    await db.execute(
        "INSERT INTO organizations(org_id, name, slug, created_at) VALUES (?, ?, ?, ?)",
        ("org1", "Acme", "acme", utcnow().isoformat()),
    )
    await db.execute(
        "INSERT INTO workspaces(workspace_id, org_id, name, slug, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("ws1", "org1", "Main", "main", utcnow().isoformat()),
    )
    await db.execute(
        "INSERT INTO rooms(room_id, workspace_id, name, description, status, created_by, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("r1", "ws1", "Test Room", "", "ACTIVE", "u1", utcnow().isoformat()),
    )
    await db.execute(
        "INSERT INTO users(user_id, display_name, email, avatar_url, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("u1", "Alice", "alice@test.com", "", "OFFLINE", utcnow().isoformat()),
    )
    await db.execute(
        "INSERT INTO agent_templates(template_id, name, description, role, system_prompt, "
        "capabilities, preferred_tools, avatar_url, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("t1", "Architect", "", "Architect", "", "[]", "[]", "", utcnow().isoformat()),
    )
    await db.commit()
    return Repos(db)


@pytest.mark.asyncio
async def test_user_crud(repos):
    user = User(user_id="u2", display_name="Bob", email="bob@test.com")
    await repos.users.create(user)
    found = await repos.users.get("u2")
    assert found is not None
    assert found.display_name == "Bob"

    by_email = await repos.users.get_by_email("bob@test.com")
    assert by_email is not None

    await repos.users.update_status("u2", UserStatus.ONLINE)
    found = await repos.users.get("u2")
    assert found.status == UserStatus.ONLINE


@pytest.mark.asyncio
async def test_org_crud(repos):
    org = Organization(org_id="org2", name="Beta", slug="beta")
    await repos.orgs.create(org)
    found = await repos.orgs.get("org2")
    assert found is not None
    assert found.name == "Beta"

    member = OrgMember(org_id="org2", user_id="u1", role="admin")
    await repos.orgs.add_member(member)
    members = await repos.orgs.list_members("org2")
    assert len(members) == 1


@pytest.mark.asyncio
async def test_workspace_crud(repos):
    ws = Workspace(workspace_id="ws2", org_id="org1", name="Secondary", slug="sec")
    await repos.workspaces.create(ws)
    found = await repos.workspaces.get("ws2")
    assert found is not None

    await repos.workspaces.add_member(WorkspaceMember(workspace_id="ws2", user_id="u1"))
    wss = await repos.workspaces.list_by_org("org1")
    assert len(wss) >= 2


@pytest.mark.asyncio
async def test_room_crud(repos):
    room = Room(room_id="r2", workspace_id="ws1", name="Dev", created_by="u1")
    await repos.rooms.create(room)
    found = await repos.rooms.get("r2")
    assert found.name == "Dev"

    await repos.room_members.add(RoomMember(room_id="r2", user_id="u1"))
    members = await repos.room_members.list("r2")
    assert len(members) == 1

    is_member = await repos.room_members.is_member("r2", "u1")
    assert is_member


@pytest.mark.asyncio
async def test_room_member_display_names_joins_users_and_falls_back_to_user_id(repos):
    # u1 has a users row ("Alice") from the fixture; u3 does not.
    await repos.room_members.add(RoomMember(room_id="r1", user_id="u1"))
    await repos.room_members.add(RoomMember(room_id="r1", user_id="u3"))

    names = await repos.room_members.display_names("r1")
    assert names == {"u1": "Alice", "u3": "u3"}


@pytest.mark.asyncio
async def test_agent_crud(repos):
    agent = AgentInstance(
        agent_id="a1", template_id="t1", room_id="r1", name="Architect", role="Architect"
    )
    await repos.agents.create_instance(agent)
    inst = await repos.agents.get_instance("a1")
    assert inst.name == "Architect"

    await repos.agents.update_status("a1", AgentStatus.WORKING)
    inst = await repos.agents.get_instance("a1")
    assert inst.status == AgentStatus.WORKING


@pytest.mark.asyncio
async def test_task_crud(repos):
    task = Task(task_id="t1", room_id="r1", title="Build auth")
    await repos.tasks.create(task)
    found = await repos.tasks.get("t1")
    assert found.title == "Build auth"

    task = Task(
        task_id="t1",
        room_id="r1",
        title="Build auth",
        status=TaskStatus.ASSIGNED,
        assigned_agent_id="a1",
        updated_at=utcnow(),
    )
    await repos.tasks.update(task)
    found = await repos.tasks.get("t1")
    assert found.status == TaskStatus.ASSIGNED


@pytest.mark.asyncio
async def test_message_crud(repos):
    msg = Message(
        message_id="m1", room_id="r1", role=MessageRole.HUMAN, sender_id="u1", content="Hello world"
    )
    await repos.messages.create(msg)
    messages = await repos.messages.list_by_room("r1")
    assert len(messages) == 1
    assert messages[0].content == "Hello world"


@pytest.mark.asyncio
async def test_event_crud(repos):
    # Use atomic sequence generation
    seq1 = await repos.events.get_next_sequence("r1")
    assert seq1 == 1
    event = RoomEvent(
        room_id="r1",
        sequence=seq1,
        event_type=EventType.ROOM_CREATED,
        payload={"name": "Test"},
        actor_id="u1",
        actor_type="user",
    )
    await repos.events.append(event)

    seq2 = await repos.events.get_next_sequence("r1")
    assert seq2 == 2

    events = await repos.events.list_since("r1", 0)
    assert len(events) == 1
    assert events[0].event_type == EventType.ROOM_CREATED

    latest = await repos.events.get_latest_sequence("r1")
    assert latest == 1


@pytest.mark.asyncio
async def test_artifact_crud(repos):
    art = Artifact(
        artifact_id="a1",
        room_id="r1",
        name="doc.md",
        artifact_type=ArtifactType.DOCUMENT,
        current_version=0,
    )
    await repos.artifacts.create(art)

    ver = ArtifactVersion(
        version_id="v1", artifact_id="a1", version_number=1, content="hello", content_hash="abc"
    )
    await repos.artifacts.create_version(ver)

    versions = await repos.artifacts.list_versions("a1")
    assert len(versions) == 1
    assert versions[0].content == "hello"

    art = await repos.artifacts.get("a1")
    assert art.current_version == 1


@pytest.mark.asyncio
async def test_decision_crud(repos):
    dec = Decision(decision_id="d1", room_id="r1", title="Use Postgres", content="JSONB")
    await repos.decisions.create(dec)
    decs = await repos.decisions.list_by_room("r1")
    assert len(decs) == 1

    await repos.decisions.update_status("d1", DecisionStatus.ACTIVE)
    found = await repos.decisions.get("d1")
    assert found.status == DecisionStatus.ACTIVE


@pytest.mark.asyncio
async def test_memory_crud(repos):
    mem = Memory(
        memory_id="m1",
        room_id="r1",
        workspace_id=None,
        org_id=None,
        scope=MemoryScope.ROOM,
        content="fact",
    )
    await repos.memories.create(mem)
    mems = await repos.memories.list_by_room("r1")
    assert len(mems) == 1

    mem2 = Memory(
        memory_id="m2",
        room_id="r1",
        workspace_id=None,
        org_id=None,
        scope=MemoryScope.ROOM,
        content="new fact",
    )
    await repos.memories.create(mem2)
    await repos.memories.supersede("m1", "m2")
    mems = await repos.memories.list_by_room("r1")
    assert len(mems) == 1
    assert mems[0].memory_id == "m2"


@pytest.mark.asyncio
async def test_approval_crud(repos):
    app = Approval(
        approval_id="a1",
        room_id="r1",
        execution_id="e1",
        agent_id="ag1",
        action_description="Deploy",
    )
    await repos.approvals.create(app)
    pending = await repos.approvals.list_pending_by_room("r1")
    assert len(pending) == 1

    app = Approval(
        approval_id="a1",
        room_id="r1",
        execution_id="e1",
        agent_id="ag1",
        action_description="Deploy",
        status=ApprovalStatus.APPROVED,
        reviewer_id="u1",
        reviewed_at=utcnow(),
    )
    await repos.approvals.update(app)
    pending = await repos.approvals.list_pending_by_room("r1")
    assert len(pending) == 0


@pytest.mark.asyncio
async def test_notification_crud(repos):
    notif = Notification(notification_id="n1", user_id="u1", room_id=None, title="Hey", body="Work")
    await repos.notifications.create(notif)
    unread = await repos.notifications.list_unread("u1")
    assert len(unread) == 1

    await repos.notifications.mark_read("n1")
    unread = await repos.notifications.list_unread("u1")
    assert len(unread) == 0


@pytest.mark.asyncio
async def test_tool_permission_crud(repos):
    perm = ToolPermission(
        permission_id="p1",
        agent_id="a1",
        room_id="r1",
        tool_name="github",
        allowed=True,
        requires_approval=True,
    )
    await repos.tool_permissions.create(perm)
    found = await repos.tool_permissions.get("a1", "r1", "github")
    assert found is not None
    assert found.requires_approval is True

    perms = await repos.tool_permissions.list_by_agent_room("a1", "r1")
    assert len(perms) == 1
