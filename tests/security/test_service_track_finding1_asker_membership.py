"""Finding 1: continue/cancel_agent_task must reread room membership.

``_asker_task`` authorized a human asker once, at task-creation time, and never
rechecked it. A member removed from the room (or demoted to a role without
MUTATE) kept the ability to resume or cancel their own task by calling
``continue_agent_task`` or ``cancel_agent_task`` directly, in a room every
other surface now refuses them. This asserts membership is reread at act time.
"""

from __future__ import annotations

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.agent_tasks import Part, PartKind, TaskNotFoundError
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService

OWNER = "owner"
ASKER = "asker"
ASK = (Part(kind=PartKind.TEXT, content="assess the migration"),)


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({OWNER, ASKER}))
    await svc.initialize()
    yield svc
    await db.close()


async def _room_and_agent(svc: MultiplayerService) -> tuple[str, str]:
    org = await svc.create_organization("Finding1 org", "finding1-org", OWNER)
    workspace = await svc.create_workspace(org.org_id, "Main", "main", OWNER)
    room = await svc.create_room(workspace.workspace_id, "Decision", OWNER)
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(
        room.room_id,
        templates[0].template_id,
        name=templates[0].name,
        requested_by=OWNER,
    )
    return room.room_id, agent.agent_id


@pytest.mark.asyncio
async def test_continue_agent_task_refuses_a_removed_member(service):
    room_id, agent_id = await _room_and_agent(service)
    await service.invite_room_member(room_id, ASKER, "editor", OWNER)
    task = await service.open_agent_task(room_id, agent_id, ASK, requested_by=ASKER)
    await service.start_agent_task(task.task_id)
    await service.require_agent_task_input(task.task_id, ASK, by_agent_id=agent_id)

    await service.remove_room_member(room_id, ASKER, OWNER)

    with pytest.raises(TaskNotFoundError):
        await service.continue_agent_task(task.task_id, ASK, requested_by=ASKER)


@pytest.mark.asyncio
async def test_cancel_agent_task_refuses_a_removed_member(service):
    room_id, agent_id = await _room_and_agent(service)
    await service.invite_room_member(room_id, ASKER, "editor", OWNER)
    task = await service.open_agent_task(room_id, agent_id, ASK, requested_by=ASKER)

    await service.remove_room_member(room_id, ASKER, OWNER)

    with pytest.raises(TaskNotFoundError):
        await service.cancel_agent_task(task.task_id, requested_by=ASKER)


@pytest.mark.asyncio
async def test_continue_agent_task_refuses_a_viewer_without_mutate(service):
    room_id, agent_id = await _room_and_agent(service)
    await service.invite_room_member(room_id, ASKER, "editor", OWNER)
    task = await service.open_agent_task(room_id, agent_id, ASK, requested_by=ASKER)
    await service.start_agent_task(task.task_id)
    await service.require_agent_task_input(task.task_id, ASK, by_agent_id=agent_id)

    await service.repos.room_members.update_role(room_id, ASKER, "viewer")

    with pytest.raises(TaskNotFoundError):
        await service.continue_agent_task(task.task_id, ASK, requested_by=ASKER)
