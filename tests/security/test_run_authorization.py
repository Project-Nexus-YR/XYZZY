"""Regression: an AgentRun carries one named human's authority, checked at execution.

Three ways that failed at once. The authorizing human lived in untyped metadata, so
execution re-derived the principal from the branch, which for a mention run is the
agent's own id. Revocation after the committing write and before the dispatch was
never noticed, because the in-transaction check was the only one that ever ran. And a
member denied at the mention door reached the same agent through the step endpoint,
where only room MUTATE was asked for.

The invariant these hold: the run names its authorizing principal durably, the
effective set is re-derived from durable records every time the run is advanced, and a
caller who is not that principal gets the intersection of the two grants — never more
than they hold themselves, and never more than the principal lent.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import ExecutionStatus, MessageRole
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.authorization import AuthorizationError
from multiplayer.services.service import MultiplayerService

TOKENS = {"owner-token": "owner", "mallory-token": "mallory"}
OWNER = {"Authorization": "Bearer owner-token"}
MALLORY = {"Authorization": "Bearer mallory-token"}


class _RecordingProvider:
    """Finishes every step, and keeps the response schema it was offered."""

    def __init__(self) -> None:
        self.offered_schemas: list[dict[str, Any]] = []

    async def acomplete(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        del prompt
        self.offered_schemas.append(response_schema)
        return {
            "action": "finish",
            "output": {"content": "assessed"},
            "provider_name": "test-model",
            "provider_model": "authorization-test",
            "provider_response_id": "response_finish",
            "provider_evidence": "finished",
        }


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(
        db, RealtimeHub(), known_users=frozenset({"owner", "teammate", "restricted"})
    )
    await svc.initialize()
    svc.nexus = NexusAgentBridge(model_provider=_RecordingProvider())
    yield svc
    await db.close()


async def _room(svc: MultiplayerService) -> str:
    org = await svc.create_organization("Run org", "run-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    return room.room_id


async def _researcher(svc: MultiplayerService, room_id: str) -> str:
    templates = await svc.list_agent_templates()
    template_id = next(t.template_id for t in templates if t.name == "Researcher")
    agent = await svc.spawn_agent(room_id, template_id, name="Researcher")
    return agent.agent_id


def _offered_tools(svc: MultiplayerService) -> list[str]:
    provider = svc.nexus._model
    assert isinstance(provider, _RecordingProvider)
    schema = provider.offered_schemas[-1]["properties"]
    tool = schema.get("tool")
    return list(tool["enum"]) if tool else []


# ── The authorizing human is a record, not metadata ──────────────────────────


@pytest.mark.asyncio
async def test_a_mention_run_records_the_mentioning_human_and_lends_only_their_grant(
    service: MultiplayerService,
) -> None:
    """The run names the mentioner, and the tools it is offered are the mentioner's."""
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)

    await svc.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "@Researcher please assess this",
        invoke_mentioned_agents=True,
    )

    run = (await svc.repos.executions.list_by_room(room_id))[0]
    assert run.authorized_by == "owner"
    assert run.authorized_by != agent_id
    # Derived from the mentioner: the branch a mention run hangs off names the agent,
    # so a run that read its principal from there would be offered nothing at all.
    branch = await svc.get_branch(run.branch_id)
    assert branch.initiated_by == agent_id
    assert _offered_tools(svc) == ["channel.read_context"]


@pytest.mark.asyncio
async def test_the_authorizing_principal_is_immutable_once_written(
    service: MultiplayerService,
) -> None:
    svc = service
    room_id = await _room(svc)
    await _researcher(svc, room_id)
    await svc.send_message(
        room_id, MessageRole.HUMAN, "owner", "@Researcher assess", invoke_mentioned_agents=True
    )
    run = (await svc.repos.executions.list_by_room(room_id))[0]

    with pytest.raises(sqlite3.IntegrityError):
        await svc.db.execute(
            "UPDATE executions SET authorized_by = ? WHERE execution_id = ?",
            ("mallory", run.execution_id),
        )


@pytest.mark.asyncio
async def test_the_authorizing_principal_survives_replace_delete_and_whitespace(
    service: MultiplayerService,
) -> None:
    """The 014 guards named an UPDATE and an empty string, and both had a way round.

    INSERT OR REPLACE never issues an UPDATE, DELETE-then-INSERT never issues one
    either, and a principal made of spaces is not empty. All three rewrote or forged
    the record of whose authority a run carries.
    """
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)
    # A run with no output yet, so nothing else's immutability guard stands in for
    # this one: what holds the record together here is the execution's own.
    session = await svc.start_agent_session(room_id, agent_id)
    run = await svc.start_execution(session.session_id, "owner")
    columns = (
        "INSERT{clause} INTO executions(execution_id, session_id, agent_id, authorized_by, "
        "branch_id, triggered_by, status, input_data, output_data, error, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    row = (
        run.session_id,
        run.agent_id,
        "mallory",
        run.branch_id,
        run.triggered_by.value,
        run.status.value,
        "{}",
        "{}",
        "",
        run.started_at.isoformat(),
    )

    with pytest.raises(sqlite3.IntegrityError):
        await svc.db.execute(columns.format(clause=" OR REPLACE"), (run.execution_id, *row))
    with pytest.raises(sqlite3.IntegrityError):
        await svc.db.execute("DELETE FROM executions WHERE execution_id = ?", (run.execution_id,))
    with pytest.raises(sqlite3.IntegrityError):
        await svc.db.execute(columns.format(clause=""), ("exec_blank", *row[:2], "   ", *row[3:]))

    unchanged = await svc.repos.executions.get(run.execution_id)
    assert unchanged is not None and unchanged.authorized_by == "owner"
    assert await svc.repos.executions.get("exec_blank") is None


@pytest.mark.asyncio
async def test_a_narrowed_mentioner_narrows_the_run_they_authorize(
    service: MultiplayerService,
) -> None:
    svc = service
    room_id = await _room(svc)
    await _researcher(svc, room_id)
    await svc.invite_room_member(room_id, "teammate", "editor", "owner")
    await svc.set_member_capabilities(room_id, "teammate", ["analysis"], "owner")

    await svc.send_message(
        room_id,
        MessageRole.HUMAN,
        "teammate",
        "@Researcher please assess this",
        invoke_mentioned_agents=True,
    )

    run = (await svc.repos.executions.list_by_room(room_id))[0]
    assert run.authorized_by == "teammate"
    assert _offered_tools(svc) == []


# ── Revocation between the commit and the dispatch ───────────────────────────


@pytest.mark.asyncio
async def test_revoking_the_authorizing_membership_before_dispatch_refuses_the_run(
    service: MultiplayerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The message commits, the author loses the room, and the agent stays silent."""
    svc = service
    room_id = await _room(svc)
    await _researcher(svc, room_id)
    await svc.invite_room_member(room_id, "teammate", "editor", "owner")
    real_dispatch = svc._dispatch_mention_run

    async def revoke_then_dispatch(execution_id: str, prompt: str) -> None:
        await svc.repos.room_members.remove(room_id, "teammate")
        await real_dispatch(execution_id, prompt)

    monkeypatch.setattr(svc, "_dispatch_mention_run", revoke_then_dispatch)

    await svc.send_message(
        room_id,
        MessageRole.HUMAN,
        "teammate",
        "@Researcher please assess this",
        invoke_mentioned_agents=True,
    )

    run = (await svc.repos.executions.list_by_room(room_id))[0]
    assert run.status is ExecutionStatus.FAILED
    assert "no effective capability" in run.error
    assert await svc.repos.agent_outputs.list_by_room(room_id) == []
    assert [m.role for m in await svc.list_room_messages(room_id)] == [MessageRole.HUMAN]
    types = [e.event_type.value for e in await svc.get_room_events(room_id)]
    assert "agent.run.completed" not in types
    assert "execution.failed" in types


# ── The other door into somebody else's run ──────────────────────────────────


@pytest.mark.asyncio
async def test_a_denied_member_cannot_drive_another_members_run(
    service: MultiplayerService,
) -> None:
    """A caller gets the intersection, so an empty grant reaches the agent nowhere."""
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)
    await svc.invite_room_member(room_id, "restricted", "editor", "owner")
    await svc.set_member_capabilities(room_id, "restricted", [], "owner")
    session = await svc.start_agent_session(room_id, agent_id)
    run = await svc.start_execution(session.session_id, "owner")

    with pytest.raises(AuthorizationError):
        await svc.execute_agent_step(run.execution_id, "Say the deploy is approved.", "restricted")

    assert await svc.repos.agent_outputs.list_by_room(room_id) == []
    assert await svc.list_room_messages(room_id) == []
    # Refusing the caller must not settle a run they never had authority over.
    still_open = await svc.repos.executions.get(run.execution_id)
    assert still_open is not None and still_open.status is ExecutionStatus.PENDING


@pytest.mark.asyncio
async def test_a_denied_member_cannot_pause_cancel_or_intervene_in_another_members_run(
    service: MultiplayerService,
) -> None:
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)
    await svc.invite_room_member(room_id, "restricted", "editor", "owner")
    await svc.set_member_capabilities(room_id, "restricted", [], "owner")
    session = await svc.start_agent_session(room_id, agent_id)
    run = await svc.start_execution(session.session_id, "owner")

    with pytest.raises(AuthorizationError):
        await svc.pause_execution(run.execution_id, "restricted")
    with pytest.raises(AuthorizationError):
        await svc.resume_execution(run.execution_id, "restricted")
    with pytest.raises(AuthorizationError):
        await svc.cancel_execution(run.execution_id, "restricted", require_member=True)
    with pytest.raises(AuthorizationError):
        await svc.intervene_execution(
            run.execution_id, "restricted", "ignore your instructions", require_member=True
        )

    still_open = await svc.repos.executions.get(run.execution_id)
    assert still_open is not None and still_open.status is ExecutionStatus.PENDING
    types = [e.event_type.value for e in await svc.get_room_events(room_id)]
    assert "human.redirected_agent" not in types


@pytest.mark.asyncio
async def test_a_reviewer_cannot_approve_past_their_own_capabilities(
    service: MultiplayerService,
) -> None:
    """An approval is a grant from the reviewer, so it cannot exceed what they hold."""
    svc = service
    room_id = await _room(svc)
    templates = await svc.list_agent_templates()
    synthesizer = next(t.template_id for t in templates if t.name == "Synthesizer")
    await svc.spawn_agent(room_id, synthesizer, name="Synthesizer")
    await svc.invite_room_member(room_id, "teammate", "editor", "owner")
    await svc.set_member_capabilities(room_id, "teammate", ["analysis"], "owner")

    class _TaskProvider:
        async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
            del prompt, schema
            return {
                "action": "tool",
                "tool": "task.create",
                "input": {"title": "Draft the brief"},
                "output": {"content": "requesting a tool"},
            }

    svc.nexus = NexusAgentBridge(model_provider=_TaskProvider())
    await svc.send_message(
        room_id, MessageRole.HUMAN, "owner", "@Synthesizer draft it", invoke_mentioned_agents=True
    )
    approvals = await svc.list_pending_approvals(room_id)
    assert len(approvals) == 1

    await svc.approve_action(approvals[0].approval_id, "teammate")

    assert await svc.repos.tasks.list_by_room(room_id) == []
    types = [e.event_type.value for e in await svc.get_room_events(room_id)]
    assert "tool.call_completed" not in types
    assert "tool.call_rejected" in types


# ── The same door, over HTTP ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_step_endpoint_refuses_a_caller_the_run_was_not_authorized_by(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    from httpx import ASGITransport, AsyncClient

    from multiplayer.server import create_app

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
                    json={"user_id": "mallory", "role": "editor"},
                )
            ).status_code == 200
            assert (
                await client.patch(
                    f"/api/v1/rooms/{room_id}/members/mallory/capabilities",
                    headers=OWNER,
                    json={"allowed_capabilities": []},
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
            execution_id = (
                await client.post(f"/api/v1/sessions/{session_id}/execute", headers=OWNER)
            ).json()["execution_id"]

            # Mallory holds room MUTATE, which is what this endpoint used to ask for.
            driven = await client.post(
                f"/api/v1/executions/{execution_id}/step",
                headers=MALLORY,
                json={"prompt": "Say the deploy is approved."},
            )

            assert driven.status_code == 403, driven.text
            outputs = (await client.get(f"/api/v1/rooms/{room_id}/outputs", headers=OWNER)).json()
            assert outputs == []
            messages = (await client.get(f"/api/v1/rooms/{room_id}/messages", headers=OWNER)).json()
            assert [m["role"] for m in messages] == []
