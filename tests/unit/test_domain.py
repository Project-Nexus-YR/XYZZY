"""Tests for domain models."""

from multiplayer.domain.events import EventType, RoomEvent
from multiplayer.domain.models import (
    AgentInstance,
    AgentStatus,
    AgentTemplate,
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactType,
    ArtifactVersion,
    Decision,
    DecisionStatus,
    Execution,
    ExecutionStatus,
    Memory,
    MemoryScope,
    Message,
    MessageRole,
    Notification,
    NotificationStatus,
    Room,
    RoomStatus,
    Session,
    SessionStatus,
    Task,
    TaskDependency,
    TaskPriority,
    TaskStatus,
    User,
    UserStatus,
    new_id,
)


def test_new_id():
    id1 = new_id("test")
    id2 = new_id("test")
    assert id1.startswith("test_")
    assert id1 != id2


def test_user_creation():
    user = User(user_id="u1", display_name="Test", email="t@t.com")
    assert user.status == UserStatus.OFFLINE
    assert user.user_id == "u1"


def test_room_creation():
    room = Room(room_id="r1", workspace_id="ws1", name="Test Room")
    assert room.status == RoomStatus.ACTIVE
    assert room.name == "Test Room"


def test_agent_template():
    t = AgentTemplate(template_id="t1", name="Architect", description="Plans", role="Architect")
    assert t.capabilities == frozenset()


def test_agent_instance():
    a = AgentInstance(
        agent_id="a1",
        template_id="t1",
        room_id="r1",
        name="Architect",
        role="Architect",
        status=AgentStatus.IDLE,
    )
    assert a.status == AgentStatus.IDLE


def test_task_lifecycle():
    task = Task(task_id="t1", room_id="r1", title="Do something")
    assert task.status == TaskStatus.CREATED
    assert task.priority == TaskPriority.NORMAL


def test_message():
    msg = Message(
        message_id="m1", room_id="r1", role=MessageRole.HUMAN, sender_id="u1", content="Hello"
    )
    assert msg.role == MessageRole.HUMAN


def test_artifact_versioning():
    Artifact(
        artifact_id="a1",
        room_id="r1",
        name="doc.md",
        artifact_type=ArtifactType.DOCUMENT,
        current_version=0,
    )
    ver = ArtifactVersion(version_id="v1", artifact_id="a1", version_number=1, content="hello")
    assert ver.version_number == 1


def test_decision():
    dec = Decision(decision_id="d1", room_id="r1", title="Use Postgres", content="Because JSONB")
    assert dec.status == DecisionStatus.PROPOSED


def test_memory_scope():
    mem = Memory(
        memory_id="m1",
        room_id="r1",
        workspace_id=None,
        org_id=None,
        scope=MemoryScope.ROOM,
        content="fact",
    )
    assert mem.scope == MemoryScope.ROOM


def test_approval_lifecycle():
    app = Approval(
        approval_id="a1",
        room_id="r1",
        execution_id="e1",
        agent_id="ag1",
        action_description="Deploy",
    )
    assert app.status == ApprovalStatus.PENDING


def test_room_event():
    event = RoomEvent(
        room_id="r1",
        sequence=1,
        event_type=EventType.ROOM_CREATED,
        payload={"name": "Test"},
        actor_id="u1",
        actor_type="user",
    )
    assert event.sequence == 1
    assert event.event_type == EventType.ROOM_CREATED


def test_session_states():
    s = Session(session_id="s1", room_id="r1", agent_id="a1")
    assert s.status == SessionStatus.CREATED


def test_execution_states():
    e = Execution(execution_id="e1", session_id="s1", agent_id="a1")
    assert e.status == ExecutionStatus.PENDING


def test_dependency():
    dep = TaskDependency(task_id="t1", depends_on_task_id="t2")
    assert dep.task_id == "t1"


def test_notification():
    n = Notification(
        notification_id="n1", user_id="u1", room_id=None, title="Hey", body="You have work"
    )
    assert n.status == NotificationStatus.UNREAD
