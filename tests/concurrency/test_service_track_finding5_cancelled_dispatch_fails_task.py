"""Finding 5: a dispatch cancelled mid-turn fails its task instead of stranding it.

``asyncio.CancelledError`` derives from ``BaseException``, so it used to escape
the ``except Exception`` in ``_dispatch_agent_task_run`` uncaught: shutdown
cancels every fire-and-forget A2A dispatch (server.py), and a task cancelled
mid-turn stayed WORKING forever, with nothing left running that could ever
move it. This proves a cancelled dispatch now fails the task before the
cancellation propagates.

The graceful-shutdown fix above is only half of the finding's own impact
statement: a harder kill (SIGKILL, an OOM, the process dying outright) leaves
no handler running to catch anything at all, cancelled or not. The dispatch
task is simply gone. ``sweep_expired_run_leases`` later settles the run it was
driving (ORPHANED, once its lease is found expired), but nothing revisited the
*task* row, because ``sweep_stale_submitted_agent_tasks`` only looks at
SUBMITTED. ``test_a_task_orphaned_by_a_hard_kill_is_recovered_on_restart``
below reproduces exactly that: the dispatch is abandoned outright (never
cancelled, matching a kill -9 rather than a clean shutdown), the run's lease
is forced into the past directly in SQL the way a real stalled lease would be
found, and a second service instance reopening the same file-backed database
(a restart) is asserted to recover the task rather than leave it WORKING
forever.
"""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import suppress
from pathlib import Path

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.agent_tasks import AgentTaskState, Part, PartKind
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService

OWNER = "owner"
ASK = (Part(kind=PartKind.TEXT, content="assess the migration"),)


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({OWNER}))
    await svc.initialize()
    yield svc
    await db.close()


async def _room_and_agent(svc: MultiplayerService) -> tuple[str, str]:
    org = await svc.create_organization("Finding5 org", "finding5-org", OWNER)
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
async def test_a_dispatch_cancelled_mid_turn_fails_its_task(
    service: MultiplayerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    room_id, agent_id = await _room_and_agent(service)
    task = await service.open_agent_task(room_id, agent_id, ASK, requested_by=OWNER)

    entered = asyncio.Event()
    hanging = asyncio.Event()

    async def hangs_forever(*args, **kwargs):
        entered.set()
        await hanging.wait()
        raise AssertionError("should have been cancelled before this ever resumes")

    monkeypatch.setattr(service, "execute_agent_step", hangs_forever)

    service.dispatch_agent_task_in_background(task)
    await asyncio.wait_for(entered.wait(), timeout=5)

    running = next(iter(service._background_tasks))
    running.cancel()
    with suppress(asyncio.CancelledError):
        await running

    reread = await service.repos.agent_tasks.get(task.task_id)
    assert reread is not None
    assert reread.state is AgentTaskState.FAILED


@pytest.mark.asyncio
async def test_a_task_orphaned_by_a_hard_kill_is_recovered_on_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "hardkill.db")

        # Process 1: dispatch a task, drive it to WORKING, then vanish without
        # ever cancelling the dispatch and without closing the database — a
        # kill -9 gives no handler the chance to run, let alone to catch
        # CancelledError.
        db1 = Database(db_path)
        await db1.connect()
        svc1 = MultiplayerService(db1, RealtimeHub(), known_users=frozenset({OWNER}))
        await svc1.initialize()
        room_id, agent_id = await _room_and_agent(svc1)
        task = await svc1.open_agent_task(room_id, agent_id, ASK, requested_by=OWNER)

        entered = asyncio.Event()
        hanging = asyncio.Event()

        async def hangs_forever(*args, **kwargs):
            entered.set()
            await hanging.wait()
            raise AssertionError("nothing should ever resume this")

        monkeypatch.setattr(svc1, "execute_agent_step", hangs_forever)
        svc1.dispatch_agent_task_in_background(task)
        await asyncio.wait_for(entered.wait(), timeout=5)
        abandoned = next(iter(svc1._background_tasks))

        stuck = await svc1.repos.agent_tasks.get(task.task_id)
        assert stuck is not None
        assert stuck.state is AgentTaskState.WORKING

        try:
            # Process 1 is gone. The abandoned asyncio.Task is never cancelled
            # or awaited here, and the connection is left open rather than
            # closed, the way a killed process leaves both behind.

            # Process 2: a restart, reopening the same file. The lease is
            # forced into the past directly in SQL, the way a real stalled
            # lease would already be past due by the time anything looks at
            # it again.
            db2 = Database(db_path)
            await db2.connect()
            await db2.execute(
                "UPDATE agent_runs SET lease_expires_at = ? WHERE execution_id = ?",
                ("2000-01-01T00:00:00+00:00", stuck.execution_id),
            )
            await db2.commit()
            svc2 = MultiplayerService(db2, RealtimeHub(), known_users=frozenset({OWNER}))
            await svc2.initialize()

            recovered = await svc2.repos.agent_tasks.get(task.task_id)
            assert recovered is not None
            assert recovered.state is not AgentTaskState.WORKING, (
                "a task orphaned by a hard kill must not stay WORKING across a restart"
            )
            assert recovered.state is AgentTaskState.FAILED
            await db2.close()
        finally:
            # Test hygiene only, no part of the recipe above: a real killed
            # process leaves both behind forever, but this one still has to
            # let the temp file go and not leak a task into the next test.
            abandoned.cancel()
            with suppress(asyncio.CancelledError):
                await abandoned
            await db1.close()
