"""Synthesis starts, deadlines and terminal events survive cancellation/restart."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

import multiplayer.services.branches as branches_module
from multiplayer.db.connection import Database
from multiplayer.db.repositories import EventRepo
from multiplayer.domain.events import EventType
from multiplayer.domain.models import (
    BranchMode,
    BranchSynthesisStatus,
    DomainError,
    IdempotencyConflict,
    OutputDisposition,
)
from multiplayer.model_providers import WorkflowOnlyModelProvider
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService
from tests.e2e.test_first_class_branches import _BranchAwareProvider, _room_and_agents


class _HeldSynthesisProvider(_BranchAwareProvider):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def acomplete(self, prompt, response_schema):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        return await super().acomplete(prompt, response_schema)


@pytest.fixture
async def case(tmp_path):
    db = Database(tmp_path / "synthesis.db")
    await db.connect()
    svc = MultiplayerService(
        db, RealtimeHub(), nexus=NexusAgentBridge(model_provider=WorkflowOnlyModelProvider())
    )
    await svc.initialize()
    room, agents = await _room_and_agents(svc, 2)
    branch, runs = await svc.start_branch(
        room, BranchMode.PARALLEL, "Choose the rollout", "owner", agents
    )
    for run in runs:
        await svc.execute_branch_run(branch.branch_id, run.execution_id, "owner")
    for output in await svc.repos.agent_outputs.list_by_branch(branch.branch_id):
        await svc.select_branch_output(
            branch.branch_id, output.output_id, OutputDisposition.INCLUDED, "owner"
        )
    provider = _HeldSynthesisProvider()
    svc.nexus = NexusAgentBridge(model_provider=provider)
    yield svc, provider, branch, tmp_path / "synthesis.db"
    await db.close()


async def _start(case, key="synthesis-recovery"):
    svc, provider, branch, _ = case
    task = asyncio.create_task(
        svc.synthesize_branch(branch.branch_id, "Rollout", "owner", idempotency_key=key)
    )
    await asyncio.wait_for(provider.entered.wait(), 5)
    return task


async def _events(svc, branch):
    return [
        event
        for event in await svc.get_room_events(branch.room_id)
        if event.event_type.value.startswith("branch.synthesis.")
    ]


@pytest.mark.asyncio
async def test_cancellation_commits_failure_and_same_key_has_a_recoverable_refusal(case):
    svc, _, branch, _ = case
    work = await _start(case)
    assert [event.event_type for event in await _events(svc, branch)] == [
        EventType.BRANCH_SYNTHESIS_STARTED
    ]
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    row = (await svc.repos.branch_syntheses.list_by_branch(branch.branch_id))[0]
    assert row.status is BranchSynthesisStatus.FAILED
    assert row.completed_at is not None
    assert [event.event_type for event in await _events(svc, branch)] == [
        EventType.BRANCH_SYNTHESIS_STARTED,
        EventType.BRANCH_SYNTHESIS_FAILED,
    ]
    with pytest.raises(IdempotencyConflict, match="failed; retry with a new"):
        await svc.synthesize_branch(
            branch.branch_id, "Rollout", "owner", idempotency_key="synthesis-recovery"
        )
    assert await svc.list_room_artifacts(branch.room_id) == []


@pytest.mark.asyncio
async def test_restart_expires_old_synthesis_once_and_refuses_late_publication(case):
    svc, provider, branch, path = case
    work = await _start(case)
    await svc.db.execute(
        "UPDATE branch_syntheses SET created_at = ?", ("2000-01-01T00:00:00+00:00",)
    )
    await svc.db.commit()
    other_db = Database(path)
    await other_db.connect()
    other = MultiplayerService(other_db, RealtimeHub())
    try:
        await other.initialize()
        await other.sweep_expired_run_leases()
        row = (await other.repos.branch_syntheses.list_by_branch(branch.branch_id))[0]
        assert row.status is BranchSynthesisStatus.FAILED
        assert [event.event_type for event in await _events(other, branch)].count(
            EventType.BRANCH_SYNTHESIS_FAILED
        ) == 1
        provider.release.set()
        with pytest.raises(DomainError):
            await work
        assert await svc.list_room_artifacts(branch.room_id) == []
    finally:
        provider.release.set()
        await asyncio.gather(work, return_exceptions=True)
        await other_db.close()


@pytest.mark.asyncio
async def test_sibling_startup_preserves_live_synthesis_and_same_key_runs_only_once(case):
    svc, provider, branch, path = case
    work = await _start(case)
    other_db = Database(path)
    await other_db.connect()
    other = MultiplayerService(other_db, RealtimeHub())
    try:
        await other.initialize()
        row = (await other.repos.branch_syntheses.list_by_branch(branch.branch_id))[0]
        assert row.status is BranchSynthesisStatus.RUNNING
        with pytest.raises(IdempotencyConflict, match="still running"):
            await other.synthesize_branch(
                branch.branch_id, "Rollout", "owner", idempotency_key="synthesis-recovery"
            )
        assert provider.calls == 1
        provider.release.set()
        artifact, version = await work
        replay_artifact, replay_version = await other.synthesize_branch(
            branch.branch_id, "Rollout", "owner", idempotency_key="synthesis-recovery"
        )
        assert (replay_artifact.artifact_id, replay_version.version_id) == (
            artifact.artifact_id,
            version.version_id,
        )
        assert [event.event_type for event in await _events(other, branch)] == [
            EventType.BRANCH_SYNTHESIS_STARTED,
            EventType.BRANCH_SYNTHESIS_COMPLETED,
        ]
    finally:
        provider.release.set()
        await asyncio.gather(work, return_exceptions=True)
        await other_db.close()


@pytest.mark.asyncio
async def test_start_event_fault_rolls_back_synthesis_key_and_provider_dispatch(case, monkeypatch):
    svc, provider, branch, _ = case
    append = EventRepo.append_with_next_sequence_in_transaction

    async def fail_start(self, event):
        if event.event_type is EventType.BRANCH_SYNTHESIS_STARTED:
            raise RuntimeError("start event unavailable")
        return await append(self, event)

    monkeypatch.setattr(EventRepo, "append_with_next_sequence_in_transaction", fail_start)
    provider.release.set()
    with pytest.raises(RuntimeError, match="start event unavailable"):
        await svc.synthesize_branch(
            branch.branch_id, "Rollout", "owner", idempotency_key="atomic-start"
        )
    assert provider.calls == 0
    assert await svc.repos.branch_syntheses.list_by_branch(branch.branch_id) == []
    assert await svc.db.fetch_all("SELECT * FROM idempotency_keys") == []


@pytest.mark.asyncio
async def test_provider_deadline_fails_running_synthesis_without_restart(case, monkeypatch):
    svc, _, branch, _ = case
    monkeypatch.setattr(branches_module, "_SYNTHESIS_MAX_DURATION", timedelta(milliseconds=40))
    with pytest.raises(DomainError, match="deadline"):
        await svc.synthesize_branch(branch.branch_id, "Rollout", "owner")
    row = (await svc.repos.branch_syntheses.list_by_branch(branch.branch_id))[0]
    assert row.status is BranchSynthesisStatus.FAILED
    assert [event.event_type for event in await _events(svc, branch)] == [
        EventType.BRANCH_SYNTHESIS_STARTED,
        EventType.BRANCH_SYNTHESIS_FAILED,
    ]


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_interrupt_failure_commit(case, monkeypatch):
    svc, _, branch, _ = case
    work = await _start(case)
    entered, release = asyncio.Event(), asyncio.Event()
    original = svc._fail_branch_synthesis

    async def delayed_failure(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(svc, "_fail_branch_synthesis", delayed_failure)
    work.cancel()
    await asyncio.wait_for(entered.wait(), 5)
    work.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await work
    row = (await svc.repos.branch_syntheses.list_by_branch(branch.branch_id))[0]
    assert row.status is BranchSynthesisStatus.FAILED
    assert [event.event_type for event in await _events(svc, branch)].count(
        EventType.BRANCH_SYNTHESIS_FAILED
    ) == 1


@pytest.mark.asyncio
async def test_failure_event_fault_rolls_back_status_transition(case, monkeypatch):
    svc, provider, branch, _ = case
    work = await _start(case)
    row = (await svc.repos.branch_syntheses.list_by_branch(branch.branch_id))[0]
    original = EventRepo.append_with_next_sequence_in_transaction

    async def fail_event(self, event):
        if event.event_type is EventType.BRANCH_SYNTHESIS_FAILED:
            raise RuntimeError("failure event unavailable")
        return await original(self, event)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(EventRepo, "append_with_next_sequence_in_transaction", fail_event)
            with pytest.raises(RuntimeError, match="failure event unavailable"):
                await svc._fail_branch_synthesis(row, "injected failure")
        assert (
            await svc.repos.branch_syntheses.get(row.synthesis_id)
        ).status is BranchSynthesisStatus.RUNNING
        provider.release.set()
        await work
        assert (
            await svc.repos.branch_syntheses.get(row.synthesis_id)
        ).status is BranchSynthesisStatus.COMPLETED
    finally:
        provider.release.set()
        await asyncio.gather(work, return_exceptions=True)
