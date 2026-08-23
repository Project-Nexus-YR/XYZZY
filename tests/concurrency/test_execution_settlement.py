"""Concurrency acceptance: two processes may not corrupt one AgentRun between them.

Two failures met here. Execution status was written unconditionally, so a settlement
pass could mark a healthy mid-flight run FAILED and the dispatcher could then flip the
same run COMPLETED and write an agent message — one execution reading
`agent.run.started, execution.failed, agent.run.completed`, final status COMPLETED
carrying a dispatcher error. And the startup sweep could not tell a run orphaned by a
crash from one another process was actively dispatching, so it settled both.

The invariants: every status write is conditional on the status its writer read, so a
transition that is no longer valid touches zero rows and is detected; and the sweep
reads only runs no dispatcher ever claimed, so it loses this race rather than winning
it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import ExecutionStatus, MessageRole
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService


class _GatedProvider:
    """Holds the run inside the provider call until the test lets it finish."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, schema
        self.entered.set()
        await self.release.wait()
        return {
            "action": "finish",
            "output": {"content": "assessed"},
            "provider_name": "test-model",
            "provider_model": "settlement-test",
            "provider_response_id": "response_finish",
            "provider_evidence": "finished",
        }


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner"}))
    await svc.initialize()
    yield svc
    await db.close()


async def _room_with_agent(svc: MultiplayerService) -> str:
    org = await svc.create_organization("Settle org", "settle-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    templates = await svc.list_agent_templates()
    template_id = next(t.template_id for t in templates if t.name == "Researcher")
    await svc.spawn_agent(room.room_id, template_id, name="Researcher")
    return room.room_id


async def _mention_in_flight(
    svc: MultiplayerService, room_id: str, provider: _GatedProvider
) -> tuple[asyncio.Task[Any], str]:
    """Start a mention run and hand it back while it sits inside the provider."""
    svc.nexus = NexusAgentBridge(model_provider=provider)
    sending = asyncio.create_task(
        svc.send_message(
            room_id,
            MessageRole.HUMAN,
            "owner",
            "@Researcher please assess this",
            invoke_mentioned_agents=True,
        )
    )
    await asyncio.wait_for(provider.entered.wait(), timeout=5)
    run = (await svc.repos.executions.list_by_room(room_id))[0]
    return sending, run.execution_id


@pytest.mark.asyncio
async def test_a_settled_run_can_neither_complete_nor_speak(
    service: MultiplayerService,
) -> None:
    """The settlement wins the status, and the completion that follows writes nothing."""
    svc = service
    room_id = await _room_with_agent(svc)
    provider = _GatedProvider()
    sending, execution_id = await _mention_in_flight(svc, room_id, provider)

    # The destructive write: another process settles this healthy mid-flight run.
    await svc._settle_undispatched_run(execution_id, "dispatcher stopped before the run started")
    provider.release.set()
    await sending

    settled = await svc.repos.executions.get(execution_id)
    assert settled is not None
    assert settled.status is ExecutionStatus.FAILED
    assert settled.error == "dispatcher stopped before the run started"
    assert await svc.repos.agent_outputs.list_by_room(room_id) == []
    assert [m.role for m in await svc.list_room_messages(room_id)] == [MessageRole.HUMAN]
    types = [e.event_type.value for e in await svc.get_room_events(room_id)]
    assert "agent.run.completed" not in types
    assert types.count("execution.failed") == 1


@pytest.mark.asyncio
async def test_two_settlement_passes_leave_a_live_run_alone(
    service: MultiplayerService,
) -> None:
    """Two processes starting up over one in-flight run settle nothing."""
    svc = service
    room_id = await _room_with_agent(svc)
    provider = _GatedProvider()
    sending, execution_id = await _mention_in_flight(svc, room_id, provider)

    await svc._settle_orphaned_mention_runs()
    await svc._settle_orphaned_mention_runs()
    provider.release.set()
    await sending

    finished = await svc.repos.executions.get(execution_id)
    assert finished is not None
    assert finished.status is ExecutionStatus.COMPLETED
    assert finished.error == ""
    assert len(await svc.repos.agent_outputs.list_by_room(room_id)) == 1
    assert [m.role for m in await svc.list_room_messages(room_id)] == [
        MessageRole.HUMAN,
        MessageRole.AGENT,
    ]
    types = [e.event_type.value for e in await svc.get_room_events(room_id)]
    assert "execution.failed" not in types
    assert types.count("agent.run.completed") == 1


@pytest.mark.asyncio
async def test_the_sweep_settles_an_unclaimed_run_and_spares_a_claimed_one(
    service: MultiplayerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim is the difference between a crash orphan and somebody else's work."""
    svc = service
    room_id = await _room_with_agent(svc)

    async def claim_only(execution_id: str, prompt: str) -> None:
        # A dispatcher that took the run and is still preparing it: the run is
        # PENDING, and it is emphatically not an orphan.
        await svc.repos.executions.claim_for_dispatch(execution_id, svc._dispatch_claim)

    monkeypatch.setattr(svc, "_dispatch_mention_run", claim_only)
    await svc.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "@Researcher assess the claimed one",
        invoke_mentioned_agents=True,
    )
    claimed = (await svc.repos.executions.list_by_room(room_id))[0]

    async def never_dispatched(execution_id: str, prompt: str) -> None:
        # A process that died before it could claim anything.
        return None

    monkeypatch.setattr(svc, "_dispatch_mention_run", never_dispatched)
    await svc.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "@Researcher assess the orphan",
        invoke_mentioned_agents=True,
    )
    runs = await svc.repos.executions.list_by_room(room_id)
    orphan = next(run for run in runs if run.execution_id != claimed.execution_id)

    restarted = MultiplayerService(svc.db, RealtimeHub())
    await restarted._settle_orphaned_mention_runs()

    spared = await svc.repos.executions.get(claimed.execution_id)
    settled = await svc.repos.executions.get(orphan.execution_id)
    assert spared is not None and spared.status is ExecutionStatus.PENDING
    assert settled is not None and settled.status is ExecutionStatus.FAILED
    failures = [
        e.payload["execution_id"]
        for e in await svc.get_room_events(room_id)
        if e.event_type.value == "execution.failed"
    ]
    assert failures == [orphan.execution_id]
