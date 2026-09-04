"""Finding 4: the message append and the state transition are one transaction.

``continue_agent_task``, ``require_agent_task_input`` and ``complete_agent_task``
used to write the message and CAS the state in two separate transactions, so a
failure between them left the message standing in the append-only task log as
a turn that never happened. This proves each is now atomic by making the
second write of the pair fail and asserting neither write landed.
"""

from __future__ import annotations

import sqlite3

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.domain.agent_tasks import AgentTaskState, Part, PartKind
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService
from tests.failure.fault_injection import FaultInjectingDatabase

OWNER = "owner"
ASK = (Part(kind=PartKind.TEXT, content="assess the migration"),)


async def _room_and_agent(svc: MultiplayerService) -> tuple[str, str]:
    org = await svc.create_organization("Finding4 org", "finding4-org", OWNER)
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
async def test_continue_agent_task_is_atomic_across_a_mid_write_fault(monkeypatch):
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    # Fault injected once the fixture setup finishes, so it lands on the pair
    # continue_agent_task itself writes rather than on the setup that drives
    # the task to input-required.
    db = FaultInjectingDatabase(":memory:", fail_on_execute=0)
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({OWNER}))
    await svc.initialize()
    room_id, agent_id = await _room_and_agent(svc)
    task = await svc.open_agent_task(room_id, agent_id, ASK, requested_by=OWNER)
    await svc.start_agent_task(task.task_id)
    await svc.require_agent_task_input(task.task_id, ASK, by_agent_id=agent_id)
    before = await svc.repos.agent_tasks.list_messages(task.task_id)

    # Count the writes continue_agent_task makes so the fault lands on the
    # transition's UPDATE, the second of the pair, not the message INSERT.
    db.fail_on_execute = db.execute_count + 2

    with pytest.raises(sqlite3.OperationalError):
        await svc.continue_agent_task(task.task_id, ASK, requested_by=OWNER)

    after = await svc.repos.agent_tasks.list_messages(task.task_id)
    reread = await svc.repos.agent_tasks.get(task.task_id)
    assert len(after) == len(before), "the asker's message must not survive alone"
    assert reread is not None
    assert reread.state is AgentTaskState.INPUT_REQUIRED
    await db.close()


@pytest.mark.asyncio
async def test_require_agent_task_input_is_atomic_across_a_mid_write_fault(monkeypatch):
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = FaultInjectingDatabase(":memory:", fail_on_execute=0)
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({OWNER}))
    await svc.initialize()
    room_id, agent_id = await _room_and_agent(svc)
    task = await svc.open_agent_task(room_id, agent_id, ASK, requested_by=OWNER)
    await svc.start_agent_task(task.task_id)
    before = await svc.repos.agent_tasks.list_messages(task.task_id)

    db.fail_on_execute = db.execute_count + 2
    with pytest.raises(sqlite3.OperationalError):
        await svc.require_agent_task_input(task.task_id, ASK, by_agent_id=agent_id)

    after = await svc.repos.agent_tasks.list_messages(task.task_id)
    reread = await svc.repos.agent_tasks.get(task.task_id)
    assert len(after) == len(before)
    assert reread is not None
    assert reread.state is AgentTaskState.WORKING
    await db.close()


@pytest.mark.asyncio
async def test_complete_agent_task_is_atomic_across_a_mid_write_fault(monkeypatch):
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = FaultInjectingDatabase(":memory:", fail_on_execute=0)
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({OWNER}))
    await svc.initialize()
    room_id, agent_id = await _room_and_agent(svc)
    task = await svc.open_agent_task(room_id, agent_id, ASK, requested_by=OWNER)
    await svc.start_agent_task(task.task_id)
    before = await svc.repos.agent_tasks.list_messages(task.task_id)

    db.fail_on_execute = db.execute_count + 2
    with pytest.raises(sqlite3.OperationalError):
        await svc.complete_agent_task(task.task_id, ASK, by_agent_id=agent_id)

    after = await svc.repos.agent_tasks.list_messages(task.task_id)
    reread = await svc.repos.agent_tasks.get(task.task_id)
    assert len(after) == len(before)
    assert reread is not None
    assert reread.state is AgentTaskState.WORKING
    await db.close()
