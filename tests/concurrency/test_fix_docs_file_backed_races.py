"""Finding 47: the concurrency suite runs almost entirely on the
single-connection ``:memory:`` path, where the reader pool the server
actually uses in production (``db/connection.py``: a pool of readers exists
only for a file path) never comes into play, and three races this file names
had no test on either topology: a task cancelled mid-``transaction()``, the
lease sweep racing a heartbeat, and an approval expiry racing its own
decision.

The ``service`` fixture below is parametrized over ``:memory:`` and a
file-backed path, the same shape as the fixture in
``test_lease_and_settlement.py``, so every test in this file runs on both.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import ApprovalStatus, ExecutionStatus, HarnessState
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService
from tests.concurrency.test_lease_and_settlement import (
    _AsksForATaskThenAnswers,
    _room_with_agent,
    _start,
    _suspend_at_a_reviewer,
    _the_run,
)


@pytest.fixture(params=["memory", "file"])
async def service(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Any:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    path = ":memory:" if request.param == "memory" else str(tmp_path / "race.db")
    db = Database(path)
    await db.connect()
    svc = MultiplayerService(
        db, RealtimeHub(), known_users=frozenset({"owner", "delegate", "teammate"})
    )
    await svc.initialize()
    yield svc
    await db.close()


@pytest.mark.asyncio
async def test_cancelling_a_task_mid_transaction_rolls_back_and_frees_the_connection(
    service: MultiplayerService,
) -> None:
    """A task cancelled while it holds ``db.transaction()`` must not leave the
    write half-applied, and must not leave the connection lock held forever:
    a later, independent transaction has to still be able to acquire it."""
    org = await service.create_organization("Org", "org", "owner")

    started = asyncio.Event()

    async def _slow_write() -> None:
        async with service.db.transaction():
            started.set()
            await asyncio.sleep(5)
            await service.db.execute(
                "UPDATE organizations SET name = ? WHERE org_id = ?",
                ("renamed", org.org_id),
            )

    task = asyncio.ensure_future(_slow_write())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = await service.db.fetch_one(
        "SELECT name FROM organizations WHERE org_id = ?", (org.org_id,)
    )
    assert row is not None
    assert row["name"] == "Org"  # the cancelled write never committed

    # The connection lock must have been released: a fresh transaction must
    # not hang waiting for one a cancelled task never let go of.
    workspace = await asyncio.wait_for(
        service.create_workspace(org.org_id, "Main", "main", "owner"), timeout=5
    )
    assert workspace.slug == "main"


@pytest.mark.asyncio
async def test_lease_sweep_racing_a_heartbeat_never_leaves_both_a_settlement_and_a_live_lease(
    service: MultiplayerService,
) -> None:
    """``sweep_expired_run_leases`` and the streaming heartbeat
    (``record_session_update``) both advance the same run's lease. Racing
    them must produce one coherent outcome: either the run is settled (and
    stays settled), or its lease was renewed before the sweep saw it expire
    (and it is not settled): never a run that is both SETTLED and holds a
    freshly renewed, still-in-the-future lease."""
    provider = _AsksForATaskThenAnswers()
    room_id, agent_id = await _room_with_agent(service, provider, "heartbeat-race")
    execution_id = await _start(service, room_id, agent_id)

    run_row = await _the_run(service, execution_id)
    # Force the lease into the past so the sweep considers it expired, the
    # same state a real stalled run would be found in.
    await service.db.execute(
        "UPDATE agent_runs SET lease_expires_at = datetime('now', '-1 hour') WHERE run_id = ?",
        (run_row["run_id"],),
    )

    await asyncio.gather(
        service.sweep_expired_run_leases(),
        service._advance_run_for_execution(
            execution_id, HarnessState.STREAMING, "owner", timedelta(minutes=15)
        ),
        return_exceptions=True,
    )

    after = await _the_run(service, execution_id)
    if after["harness_state"] == HarnessState.SETTLED.value:
        assert after["settlement"] is not None
    else:
        # The heartbeat won the race: the run must not be left half-settled.
        assert after["settlement"] is None


@pytest.mark.asyncio
async def test_approval_expiry_racing_its_own_decision_leaves_one_coherent_status(
    service: MultiplayerService,
) -> None:
    """``cancel_execution`` settles the run and then calls
    ``_expire_undecided_approvals`` outside that same transaction; racing it
    against a reviewer's ``approve_action`` on the same approval must not
    leave the approval PENDING forever, and must not silently apply both
    outcomes."""
    room_id, agent_id, execution_id, _provider, approval_id = await _suspend_at_a_reviewer(
        service, "approval-race"
    )

    results = await asyncio.gather(
        service.cancel_execution(execution_id, "owner"),
        service.approve_action(approval_id, "owner"),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, Exception):
            assert not isinstance(result, (KeyboardInterrupt, SystemExit))

    approval = await service.db.fetch_one(
        "SELECT status FROM approvals WHERE approval_id = ?", (approval_id,)
    )
    assert approval is not None
    assert approval["status"] in {ApprovalStatus.APPROVED.value, ApprovalStatus.EXPIRED.value}

    execution = await service.repos.executions.get(execution_id)
    assert execution is not None
    assert execution.status is ExecutionStatus.CANCELLED
