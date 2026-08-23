"""Removing an agent settles everything it had in flight, deterministically.

``remove_agent_from_room`` did not exist, ``AGENT_LEFT_ROOM`` was declared and never
emitted, and ``_running_executions`` was declared on the service and never read. The
verb requires room ADMINISTER and, in one transaction, stamps the membership removed,
moves every non-settled run for that agent in that room through CANCEL_REQUESTED to
SETTLED/AGENT_REMOVED, and appends ``agent.left_room`` plus one ``agent.run.settled``
per run.

Settlement is decided by the database and telling the harness is best-effort, so an
in-flight turn can still land. The credential does not stop it, because the in-flight
write path is ``complete_execution``, which consulted neither ``agent_runs`` nor any
credential. It re-reads its run inside the transaction it already opens and refuses when
the run is settled. That refusal, not the credential, is what stops a settled run
writing.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.events import EventType, RoomEvent
from multiplayer.domain.models import (
    AddressingMode,
    AgentOutput,
    DomainError,
    ExecutionStatus,
    HarnessState,
    MessageRole,
    RunSettlement,
    new_id,
)
from multiplayer.harness import SessionUpdate, UpdateKind
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.authorization import AuthorizationError
from multiplayer.server import create_app
from multiplayer.services.service import MultiplayerService

TOKENS = {"owner-token": "owner", "sam-token": "sam"}
OWNER = {"Authorization": "Bearer owner-token"}
SAM = {"Authorization": "Bearer sam-token"}


class _ArtifactProvider:
    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, schema
        return {
            "action": "tool",
            "tool": "artifact.write",
            "input": {"name": "Rollout plan"},
            "output": {"content": "requesting a tool"},
        }


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner", "sam"}))
    await svc.initialize()
    svc.nexus = NexusAgentBridge(model_provider=_ArtifactProvider())
    yield svc
    await db.close()


async def _room_with_synthesizer(svc: MultiplayerService) -> tuple[str, str]:
    org = await svc.create_organization("Removal org", "rm-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    await svc.invite_room_member(room.room_id, "sam", "editor", "owner")
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(
        room.room_id,
        next(t.template_id for t in templates if t.name == "Synthesizer"),
        name="Synthesizer",
        requested_by="owner",
    )
    return room.room_id, agent.agent_id


async def _three_runs(svc: MultiplayerService, room_id: str, agent_id: str) -> list[str]:
    """Two turns in flight, and one waiting on a reviewer."""
    in_flight: list[str] = []
    for _ in range(2):
        session = await svc.start_agent_session(room_id, agent_id)
        execution = await svc.start_execution(session.session_id, "owner")
        in_flight.append(execution.execution_id)
    await svc.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "@Synthesizer draft the plan",
        invoke_mentioned_agents=True,
    )
    approvals = await svc.list_pending_approvals(room_id)
    assert len(approvals) == 1
    in_flight.append(approvals[0].execution_id)
    return in_flight


# ── The verb ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_removing_an_agent_settles_every_run_it_had_in_flight(
    service: MultiplayerService,
) -> None:
    svc = service
    room_id, agent_id = await _room_with_synthesizer(svc)
    execution_ids = await _three_runs(svc, room_id, agent_id)

    await svc.remove_agent_from_room(agent_id, room_id, "owner", require_member=True)

    runs = await svc.db.fetch_all("SELECT * FROM agent_runs ORDER BY created_at, run_id")
    assert len(runs) == 3
    assert {row["harness_state"] for row in runs} == {HarnessState.SETTLED.value}
    assert {row["settlement"] for row in runs} == {RunSettlement.AGENT_REMOVED.value}
    for execution_id in execution_ids:
        execution = await svc.repos.executions.get(execution_id)
        assert execution is not None
        assert execution.status is ExecutionStatus.CANCELLED

    events = await svc.get_room_events(room_id)
    types = [event.event_type.value for event in events]
    assert types.count("agent.run.settled") == 3
    assert types.count("agent.left_room") == 1
    left = next(e for e in events if e.event_type.value == "agent.left_room")
    assert len(left.payload["settled_run_ids"]) == 3
    assert not await svc.repos.agents.has_room_membership(agent_id, room_id)


@pytest.mark.asyncio
async def test_removal_requires_room_administer(service: MultiplayerService) -> None:
    svc = service
    room_id, agent_id = await _room_with_synthesizer(svc)
    await _three_runs(svc, room_id, agent_id)

    with pytest.raises(AuthorizationError):
        await svc.remove_agent_from_room(agent_id, room_id, "sam", require_member=True)

    runs = await svc.db.fetch_all("SELECT harness_state FROM agent_runs")
    assert HarnessState.SETTLED.value not in {row["harness_state"] for row in runs}
    assert await svc.repos.agents.has_room_membership(agent_id, room_id)


# ── A settled run does not write, whatever arrives late ──────────────────────


@pytest.mark.asyncio
async def test_a_late_complete_execution_raises_and_writes_no_output(
    service: MultiplayerService,
) -> None:
    """The in-flight turn lands after the settlement, and the writer refuses it."""
    svc = service
    room_id, agent_id = await _room_with_synthesizer(svc)
    execution_ids = await _three_runs(svc, room_id, agent_id)
    landing = execution_ids[0]
    execution = await svc.repos.executions.get(landing)
    assert execution is not None

    await svc.remove_agent_from_room(agent_id, room_id, "owner", require_member=True)

    output = AgentOutput(
        output_id=new_id("out"),
        room_id=room_id,
        session_id=execution.session_id,
        execution_id=landing,
        agent_id=agent_id,
        content="a turn that was already in flight",
        branch_id=execution.branch_id,
    )
    with pytest.raises(DomainError, match="settled"):
        await svc.repos.agent_outputs.complete_execution(
            output,
            [
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.AGENT_OUTPUT_CREATED,
                    payload={"output_id": output.output_id},
                    actor_id=agent_id,
                    actor_type="agent",
                )
            ],
            ExecutionStatus.PENDING,
        )

    assert await svc.repos.agent_outputs.list_by_room(room_id) == []
    settled = await svc.repos.agent_runs.get_by_execution(landing)
    assert settled is not None
    assert settled.settlement is RunSettlement.AGENT_REMOVED
    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert "agent.output.created" not in types


@pytest.mark.asyncio
async def test_a_late_session_update_is_rejected(service: MultiplayerService) -> None:
    svc = service
    room_id, agent_id = await _room_with_synthesizer(svc)
    execution_ids = await _three_runs(svc, room_id, agent_id)
    run = await svc.repos.agent_runs.get_by_execution(execution_ids[0])
    assert run is not None
    credential = svc._run_credentials[run.run_id]
    update = SessionUpdate(run_id=run.run_id, kind=UpdateKind.MESSAGE_DELTA, payload={})

    # Before the removal the credential is accepted and the lease is renewed.
    await svc.record_session_update(run.run_id, credential, update)

    await svc.remove_agent_from_room(agent_id, room_id, "owner", require_member=True)

    with pytest.raises(DomainError, match="settled"):
        await svc.record_session_update(run.run_id, credential, update)
    # And a credential that was never issued is refused whatever the state.
    with pytest.raises(AuthorizationError):
        await svc.record_session_update(run.run_id, "not-the-credential", update)


@pytest.mark.asyncio
async def test_a_removed_agent_cannot_be_addressed_again(service: MultiplayerService) -> None:
    svc = service
    room_id, agent_id = await _room_with_synthesizer(svc)
    await svc.remove_agent_from_room(agent_id, room_id, "owner", require_member=True)
    before = len(await svc.repos.executions.list_by_room(room_id))

    await svc.set_agent_addressing(agent_id, AddressingMode.NOBODY, "owner")

    with pytest.raises(AuthorizationError):
        await svc.send_message(
            room_id,
            MessageRole.HUMAN,
            "owner",
            "@Synthesizer once more",
            invoke_mentioned_agents=True,
        )
    assert len(await svc.repos.executions.list_by_room(room_id)) == before


# ── The same verb, over HTTP ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_removal_endpoint_needs_administer_and_settles_the_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    app = create_app(":memory:", auth_tokens=TOKENS)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            bootstrap = (
                await client.post(
                    "/api/v1/me/bootstrap",
                    headers=OWNER,
                    json={"display_name": "Owner", "room_name": "Decision"},
                )
            ).json()
            room_id = bootstrap["room"]["room_id"]
            assert (
                await client.post(
                    f"/api/v1/rooms/{room_id}/members/invitations",
                    headers=OWNER,
                    json={"user_id": "sam", "role": "editor"},
                )
            ).status_code == 200
            templates = (await client.get("/api/v1/agent-templates", headers=OWNER)).json()
            researcher = next(t for t in templates if t["name"] == "Researcher")
            agent_id = (
                await client.post(
                    f"/api/v1/rooms/{room_id}/agents",
                    headers=OWNER,
                    json={"template_id": researcher["template_id"]},
                )
            ).json()["agent_id"]
            session_id = (
                await client.post(
                    f"/api/v1/rooms/{room_id}/agents/{agent_id}/sessions", headers=OWNER
                )
            ).json()["session_id"]
            assert (
                await client.post(f"/api/v1/sessions/{session_id}/execute", headers=OWNER)
            ).status_code == 200

            # An editor may not remove an agent from the room.
            refused = await client.delete(f"/api/v1/rooms/{room_id}/agents/{agent_id}", headers=SAM)
            assert refused.status_code == 403, refused.text

            removed = await client.delete(
                f"/api/v1/rooms/{room_id}/agents/{agent_id}", headers=OWNER
            )
            assert removed.status_code == 200, removed.text

            events = (await client.get(f"/api/v1/rooms/{room_id}/events", headers=OWNER)).json()
            types = [event["event_type"] for event in events]
            assert "agent.left_room" in types
            assert types.count("agent.run.settled") == 1
            outputs = (await client.get(f"/api/v1/rooms/{room_id}/outputs", headers=OWNER)).json()
            assert outputs == []
