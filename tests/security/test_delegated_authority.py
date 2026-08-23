"""Regression: no capability set is ever an input to a later decision.

The recurring defect class here is check-then-use: a decision taken when the run was
requested and never revisited at the write. The rule these hold is that the effective
set is re-derived from durable records at every point a run can spend it — before the
prompt, at the gateway decision, and inside each tool writer's own transaction.

The last leg is the one that was missing. It cannot be wrapped around ``_run_tool``,
because ``_run_tool`` calls writers that open their own transactions and
``Database.transaction()`` refuses to nest, so a check there would sit outside the write
and relocate check-then-use rather than end it. It is pushed down instead: each writer
re-derives inside the transaction it already opens, making check and write one
transaction by construction.
"""

from __future__ import annotations

from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import ArtifactType, HarnessState, MessageRole, RunSettlement
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.capabilities import RunAuthorization
from multiplayer.services.service import MultiplayerService

OWNER = "owner"
AUTHOR = "author"


class _ArtifactProvider:
    """Asks to write an artifact on every step."""

    def __init__(self, on_call: Any = None) -> None:
        self._on_call = on_call

    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, schema
        if self._on_call is not None:
            await self._on_call()
        return {
            "action": "tool",
            "tool": "artifact.write",
            "input": {"name": "Rollout plan", "description": "the plan"},
            "output": {"content": "requesting a tool"},
            "provider_name": "test-model",
            "provider_model": "authority-test",
            "provider_response_id": "response_tool",
            "provider_evidence": "tool request",
        }


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({OWNER, AUTHOR}))
    await svc.initialize()
    yield svc
    await db.close()


async def _room_with_synthesizer(
    svc: MultiplayerService, provider: _ArtifactProvider
) -> tuple[str, str]:
    org = await svc.create_organization("Authority org", "auth-org", OWNER)
    workspace = await svc.create_workspace(org.org_id, "Main", "main", OWNER)
    room = await svc.create_room(workspace.workspace_id, "Decision", OWNER)
    await svc.invite_room_member(room.room_id, AUTHOR, "editor", OWNER)
    svc.nexus = NexusAgentBridge(model_provider=provider)
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(
        room.room_id,
        next(t.template_id for t in templates if t.name == "Synthesizer"),
        name="Synthesizer",
        requested_by=OWNER,
    )
    return room.room_id, agent.agent_id


async def _ask_for_the_artifact(svc: MultiplayerService, room_id: str, asked_by: str) -> str:
    """Drive one turn to the point where the agent's tool call is waiting on a human."""
    await svc.send_message(
        room_id,
        MessageRole.HUMAN,
        asked_by,
        "@Synthesizer draft the rollout plan",
        invoke_mentioned_agents=True,
    )
    approvals = await svc.list_pending_approvals(room_id)
    assert len(approvals) == 1, approvals
    return approvals[0].approval_id


def _events(svc: MultiplayerService, room_id: str) -> Any:
    return svc.get_room_events(room_id)


# ── The gateway leg: the authority is gone before the grant ──────────────────


@pytest.mark.asyncio
async def test_removing_the_authorizing_member_refuses_the_approved_tool(
    service: MultiplayerService,
) -> None:
    svc = service
    room_id, _ = await _room_with_synthesizer(svc, _ArtifactProvider())
    approval_id = await _ask_for_the_artifact(svc, room_id, AUTHOR)

    await svc.repos.room_members.remove(room_id, AUTHOR)
    await svc.approve_action(approval_id, OWNER)

    assert await svc.repos.artifacts.list_by_room(room_id) == []
    requests = await svc.db.fetch_all("SELECT status, authorized_by FROM tool_requests")
    assert [row["status"] for row in requests] == ["REJECTED"]
    # requested_by holds the agent; authorized_by names the human it acted for.
    assert [row["authorized_by"] for row in requests] == [AUTHOR]
    types = [event.event_type.value for event in await _events(svc, room_id)]
    assert "tool.call_rejected" in types
    assert "tool.call_completed" not in types


# ── The prompt leg: demoted while the model was thinking ─────────────────────


@pytest.mark.asyncio
async def test_a_demotion_during_the_provider_call_refuses_the_tool_it_asks_for(
    service: MultiplayerService,
) -> None:
    """The terms that decide a tool call are read after the call, not before it."""
    svc = service
    room_id: str = ""

    async def demote() -> None:
        await svc.repos.room_members.update_role(room_id, AUTHOR, "viewer")

    provider = _ArtifactProvider(on_call=demote)
    room_id, _ = await _room_with_synthesizer(svc, provider)

    await svc.send_message(
        room_id,
        MessageRole.HUMAN,
        AUTHOR,
        "@Synthesizer draft the rollout plan",
        invoke_mentioned_agents=True,
    )

    # A viewer lends no mutating capability, so artifact.write never reaches approval.
    assert await svc.list_pending_approvals(room_id) == []
    assert await svc.repos.artifacts.list_by_room(room_id) == []
    statuses = [row["status"] for row in await svc.db.fetch_all("SELECT status FROM tool_requests")]
    assert statuses == ["REJECTED"]


# ── The writer leg: the authority goes after _run_tool is entered ────────────


@pytest.mark.asyncio
async def test_authority_lost_after_run_tool_is_entered_is_still_caught(
    service: MultiplayerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the writer's own transaction is still holding the pen at this point."""
    svc = service
    room_id, agent_id = await _room_with_synthesizer(svc, _ArtifactProvider())
    approval_id = await _ask_for_the_artifact(svc, room_id, AUTHOR)
    real_authorization = svc._run_authorization

    async def revoke_after_dispatch(request: Any) -> RunAuthorization:
        # The gateway has already decided and _run_tool has already been entered.
        authorization = await real_authorization(request)
        await svc.repos.room_members.remove(room_id, AUTHOR)
        return authorization

    monkeypatch.setattr(svc, "_run_authorization", revoke_after_dispatch)
    await svc.approve_action(approval_id, OWNER)

    assert await svc.repos.artifacts.list_by_room(room_id) == []
    statuses = [row["status"] for row in await svc.db.fetch_all("SELECT status FROM tool_requests")]
    assert statuses == ["REJECTED"]

    revoked = [
        event.payload
        for event in await _events(svc, room_id)
        if event.event_type.value == "agent.run.authority_revoked"
    ]
    assert len(revoked) == 1, revoked
    assert revoked[0]["stage"] == "artifact.write"
    assert revoked[0]["authorized_by"] == AUTHOR
    assert revoked[0]["missing_capability"] == "writing"

    run = (await svc.db.fetch_all("SELECT harness_state, settlement FROM agent_runs"))[0]
    assert run["harness_state"] == HarnessState.SETTLED.value
    assert run["settlement"] == RunSettlement.AUTHORITY_REVOKED.value


@pytest.mark.asyncio
async def test_the_task_writer_carries_the_same_re_check(
    service: MultiplayerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The leg is per writer, so the other gated tool has to hold it too."""
    svc = service

    class _TaskProvider:
        async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
            del prompt, schema
            return {
                "action": "tool",
                "tool": "task.create",
                "input": {"title": "Draft the brief"},
                "output": {"content": "requesting a tool"},
            }

    org = await svc.create_organization("Authority org", "auth-org", OWNER)
    workspace = await svc.create_workspace(org.org_id, "Main", "main", OWNER)
    room = await svc.create_room(workspace.workspace_id, "Decision", OWNER)
    await svc.invite_room_member(room.room_id, AUTHOR, "editor", OWNER)
    svc.nexus = NexusAgentBridge(model_provider=_TaskProvider())
    templates = await svc.list_agent_templates()
    await svc.spawn_agent(
        room.room_id,
        next(t.template_id for t in templates if t.name == "Synthesizer"),
        name="Synthesizer",
        requested_by=OWNER,
    )
    await svc.send_message(
        room.room_id,
        MessageRole.HUMAN,
        AUTHOR,
        "@Synthesizer open a task",
        invoke_mentioned_agents=True,
    )
    approval_id = (await svc.list_pending_approvals(room.room_id))[0].approval_id
    real_authorization = svc._run_authorization

    async def revoke_after_dispatch(request: Any) -> RunAuthorization:
        authorization = await real_authorization(request)
        await svc.repos.room_members.remove(room.room_id, AUTHOR)
        return authorization

    monkeypatch.setattr(svc, "_run_authorization", revoke_after_dispatch)
    await svc.approve_action(approval_id, OWNER)

    assert await svc.repos.tasks.list_by_room(room.room_id) == []
    revoked = [
        event.payload
        for event in await _events(svc, room.room_id)
        if event.event_type.value == "agent.run.authority_revoked"
    ]
    assert [payload["stage"] for payload in revoked] == ["task.create"]


@pytest.mark.asyncio
async def test_a_human_caller_is_unaffected_by_the_run_re_check(
    service: MultiplayerService,
) -> None:
    """authorization=None is the human path, guarded by the membership check beside it."""
    svc = service
    room_id, _ = await _room_with_synthesizer(svc, _ArtifactProvider())

    artifact = await svc.create_artifact(
        room_id, "Human plan", ArtifactType.DOCUMENT, created_by=OWNER, require_member=True
    )

    assert artifact.created_by == OWNER
    assert [a.artifact_id for a in await svc.repos.artifacts.list_by_room(room_id)] == [
        artifact.artifact_id
    ]
