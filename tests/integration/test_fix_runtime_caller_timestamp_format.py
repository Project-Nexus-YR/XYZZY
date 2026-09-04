"""Finding 60: execution_callers.first_acted_at is written in one shape.

029's own triggers wrote ``strftime('%Y-%m-%dT%H:%M:%fZ', 'now')`` ('Z'
suffix) while ``ExecutionRepo.record_caller`` writes Python's
``utcnow().isoformat()`` ('+00:00' suffix). Sorted as text, 'Z' sorts after
'+', so the two shapes could not be ordered or compared correctly, and
``deserialize_datetime`` produced mixed sub-second precision. Migration 051
respells the triggers to match the repository's own shape.
"""

from __future__ import annotations

import asyncio
import re

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService

_ISO_WITH_OFFSET = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$")


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner"}))
    await svc.initialize()
    yield svc
    await db.close()


async def _room_and_execution(svc: MultiplayerService, slug: str) -> tuple[str, str]:
    org = await svc.create_organization("Timestamp org", slug, "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(room.room_id, templates[0].template_id, requested_by="owner")
    session = await svc.start_agent_session(room.room_id, agent.agent_id)
    execution = await svc.start_execution(session.session_id, "owner")
    return room.room_id, execution.execution_id


@pytest.mark.asyncio
async def test_a_trigger_written_row_matches_the_repositorys_own_timestamp_shape(
    service: MultiplayerService,
) -> None:
    """``agent_runs_record_acting_caller`` fires when a run's acting_user_id is
    advanced to someone other than who authorized it, exactly what a reviewer's
    grant of a delegate's parked call does."""
    svc = service
    _, execution_id = await _room_and_execution(svc, "timestamp-org")
    run = await svc.repos.agent_runs.get_by_execution(execution_id)
    assert run is not None

    await svc.repos.agent_runs.advance(
        run.run_id, run.harness_state, run.lease_expires_at, "second-caller"
    )

    row = await svc.db.fetch_one(
        "SELECT first_acted_at FROM execution_callers WHERE execution_id = ? AND caller_id = ?",
        (execution_id, "second-caller"),
    )
    assert row is not None
    assert _ISO_WITH_OFFSET.match(str(row["first_acted_at"])), row["first_acted_at"]


@pytest.mark.asyncio
async def test_two_trigger_written_rows_still_sort_in_call_order(
    service: MultiplayerService,
) -> None:
    """Two rows from the same clock source order correctly as text: the
    structural 'Z' versus '+00:00' bug that always sorted one shape after
    the other regardless of real time is what this fixes, not the last
    microsecond of a shared millisecond across two different clocks."""
    svc = service
    _, execution_id = await _room_and_execution(svc, "timestamp-org2")
    run = await svc.repos.agent_runs.get_by_execution(execution_id)
    assert run is not None

    await svc.repos.agent_runs.advance(
        run.run_id, run.harness_state, run.lease_expires_at, "second-caller"
    )
    await asyncio.sleep(0.01)
    await svc.repos.agent_runs.advance(
        run.run_id, run.harness_state, run.lease_expires_at, "third-caller"
    )

    rows = await svc.db.fetch_all(
        "SELECT caller_id, first_acted_at FROM execution_callers "
        "WHERE execution_id = ? AND caller_id != 'owner' ORDER BY first_acted_at",
        (execution_id,),
    )
    assert [str(r["caller_id"]) for r in rows] == ["second-caller", "third-caller"]
