"""Finding 46: recording who called a run must commit in the same transaction
that creates it.

Before the fix, ``record_caller`` for a dispatched agent task and for a
resumed execution ran as a separate, autocommitted write after the
transaction that created the run had already committed. A crash, or
``SQLITE_BUSY`` from an external writer, landing between the two left a
WORKING task or resumed execution whose bound omitted its asker or resumer
until the next spend named the same principal, since adding a caller can only
narrow a run's bound, never widen it. Moving the write inside the same
transaction (``record_caller_in_transaction``) makes the two one atomic
fact: either both exist, or neither does.
"""

from __future__ import annotations

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.agent_tasks import Part, PartKind
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService

OWNER = "owner"
ASKER = "asker"
ASK = (Part(kind=PartKind.TEXT, content="assess the migration"),)


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({OWNER, ASKER}))
    await svc.initialize()
    yield svc
    await db.close()


async def _room_and_agent(svc: MultiplayerService) -> tuple[str, str]:
    org = await svc.create_organization("Finding46 org", "finding46-org", OWNER)
    workspace = await svc.create_workspace(org.org_id, "Main", "main", OWNER)
    room = await svc.create_room(workspace.workspace_id, "Decision", OWNER)
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(
        room.room_id, templates[0].template_id, name=templates[0].name, requested_by=OWNER
    )
    return room.room_id, agent.agent_id


@pytest.mark.asyncio
async def test_a_failure_recording_the_asker_rolls_back_the_dispatched_run_too(
    service: MultiplayerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = service
    room_id, agent_id = await _room_and_agent(svc)
    await svc.invite_room_member(room_id, ASKER, "editor", OWNER)
    task = await svc.open_agent_task(room_id, agent_id, ASK, requested_by=ASKER)

    async def boom(execution_id: str, caller_id: str) -> None:
        del execution_id, caller_id
        raise RuntimeError("simulated crash recording the caller")

    monkeypatch.setattr(svc.repos.executions, "record_caller_in_transaction", boom)

    with pytest.raises(RuntimeError, match="simulated crash"):
        await svc.start_agent_task(task.task_id)

    # One atomic fact: the run the failed write would have belonged to was
    # never left behind half-created.
    reloaded = await svc.get_agent_task(task.task_id, viewer_id=ASKER)
    assert reloaded.execution_id is None
    rows = await svc.db.fetch_all("SELECT * FROM executions")
    assert rows == []


@pytest.mark.asyncio
async def test_starting_a_task_records_the_asker_as_a_caller_of_the_new_run(
    service: MultiplayerService,
) -> None:
    """The ordinary path still records the row, just inside the same commit."""
    svc = service
    room_id, agent_id = await _room_and_agent(svc)
    await svc.invite_room_member(room_id, ASKER, "editor", OWNER)
    task = await svc.open_agent_task(room_id, agent_id, ASK, requested_by=ASKER)

    started = await svc.start_agent_task(task.task_id)

    assert started.execution_id is not None
    principals = await svc.repos.executions.bounding_principals(started.execution_id)
    assert ASKER in principals


@pytest.mark.asyncio
async def test_a_failure_recording_the_resumer_rolls_back_the_resumed_run_too(
    service: MultiplayerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = service
    room_id, agent_id = await _room_and_agent(svc)
    session = await svc.start_agent_session(room_id, agent_id)
    run = await svc.start_execution(session.session_id, OWNER)
    await svc.cancel_execution(run.execution_id, OWNER)
    settled = await svc.repos.agent_runs.get_by_execution(run.execution_id)
    assert settled is not None

    async def boom(execution_id: str, caller_id: str) -> None:
        del execution_id, caller_id
        raise RuntimeError("simulated crash recording the resumer")

    monkeypatch.setattr(svc.repos.executions, "record_caller_in_transaction", boom)
    before = list(await svc.repos.executions.list_by_room(room_id))

    with pytest.raises(RuntimeError, match="simulated crash"):
        await svc.resume_agent_run(settled.run_id, OWNER)

    after = await svc.repos.executions.list_by_room(room_id)
    assert len(after) == len(before)
