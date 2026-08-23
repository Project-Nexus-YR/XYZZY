"""Deterministic five-way capability enforcement and the tool gateway (PRD §13, §14).

Effective capability = user ∩ agent ∩ skill ∩ channel ∩ workspace, computed from durable
records alone. A tool outside that set is never offered to the model and is rejected if
requested anyway; a tool inside it runs, and an approval-gated tool runs only after a human
grants it. Every outcome lands on the ordered log.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import (
    AddressingMode,
    BranchMode,
    DomainError,
    MessageRole,
    ParticipantType,
    RoomMember,
)
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.capabilities import (
    CAPABILITIES,
    CapabilityTerms,
    allowed_tools,
    decide,
    policy_capabilities,
    user_capabilities,
)
from multiplayer.services.service import MultiplayerService


class _ToolRequestingProvider:
    """Asks for one tool on every step, and records the schema it was offered."""

    def __init__(self, tool: str, tool_input: dict[str, Any] | None = None) -> None:
        self.tool = tool
        self.tool_input = tool_input or {}
        self.offered_schemas: list[dict[str, Any]] = []

    async def acomplete(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        del prompt
        self.offered_schemas.append(response_schema)
        return {
            "action": "tool",
            "tool": self.tool,
            "input": self.tool_input,
            "output": {"content": "requesting a tool"},
            "provider_name": "test-model",
            "provider_model": "gateway-test",
            "provider_response_id": "response_tool",
            "provider_evidence": "tool request",
        }


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub())
    await svc.initialize()
    yield svc
    await db.close()


async def _room(svc: MultiplayerService) -> tuple[str, str]:
    org = await svc.create_organization("Cap org", "cap-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    return room.room_id, workspace.workspace_id


async def _template_id(svc: MultiplayerService, name: str) -> str:
    templates = await svc.list_agent_templates()
    return next(t.template_id for t in templates if t.name == name)


async def _run_tool_step(
    svc: MultiplayerService,
    room_id: str,
    template_name: str,
    provider: _ToolRequestingProvider,
    initiated_by: str = "owner",
) -> dict[str, Any]:
    """Drive one managed branch run to the point where the agent asks for its tool."""
    svc.nexus = NexusAgentBridge(model_provider=provider)
    agent = await svc.spawn_agent(room_id, await _template_id(svc, template_name))
    _, runs = await svc.start_branch(
        room_id,
        BranchMode.TURN_LOCKED_SINGLE,
        "Do the work.",
        initiated_by,
        [agent.agent_id],
    )
    return await svc.execute_branch_run(runs[0].branch_id, runs[0].execution_id)


# ── The pure intersection ────────────────────────────────────────────────────


def test_effective_capability_is_the_intersection_of_all_five_terms() -> None:
    terms = CapabilityTerms(
        user=CAPABILITIES,
        agent=frozenset({"coding", "testing", "review"}),
        skill=frozenset({"coding", "testing", "review"}),
        channel=frozenset({"coding", "review"}),
        workspace=frozenset({"review"}),
    )
    assert terms.effective == frozenset({"review"})


def test_a_viewer_lends_no_mutating_capability() -> None:
    assert "coding" in user_capabilities("editor")
    assert "coding" not in user_capabilities("viewer")
    assert "analysis" in user_capabilities("viewer")
    assert user_capabilities(None) == frozenset()


def test_a_policy_never_set_allows_the_full_vocabulary_and_an_empty_one_allows_nothing() -> None:
    assert policy_capabilities(None) == CAPABILITIES
    assert policy_capabilities([]) == frozenset()
    assert policy_capabilities(["coding", "not-a-capability"]) == frozenset({"coding"})


def test_only_tools_inside_the_effective_set_are_offered_and_allowed() -> None:
    assert allowed_tools(frozenset({"retrieval"})) == ["channel.read_context"]
    assert allowed_tools(frozenset()) == []
    assert decide("channel.read_context", frozenset({"retrieval"})).allowed
    assert not decide("channel.read_context", frozenset({"writing"})).allowed
    unknown = decide("shell.exec", CAPABILITIES)
    assert not unknown.allowed
    assert unknown.reason == "unknown tool"


# ── The gateway, end to end ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_authorized_tool_executes_and_is_audited(service: MultiplayerService) -> None:
    svc = service
    room_id, _ = await _room(svc)
    await svc.send_message(room_id, MessageRole.HUMAN, "owner", "prior channel evidence")
    provider = _ToolRequestingProvider("channel.read_context")
    result = await _run_tool_step(svc, room_id, "Researcher", provider)

    assert result["action"] == "tool"
    request = result["tool_request"]
    assert request["status"] == "EXECUTED"
    assert request["tool"] == "channel.read_context"
    assert request["result"]["messages"], request
    types = [e.event_type.value for e in await svc.get_room_events(room_id)]
    assert "tool.call_started" in types
    assert "tool.call_completed" in types


@pytest.mark.asyncio
async def test_a_tool_outside_the_effective_set_is_never_offered(
    service: MultiplayerService,
) -> None:
    """The Coder has no retrieval, so the read tool is absent from the offered schema."""
    svc = service
    room_id, _ = await _room(svc)
    provider = _ToolRequestingProvider("channel.read_context")
    result = await _run_tool_step(svc, room_id, "Coder", provider)

    offered = provider.offered_schemas[0]["properties"]
    assert "tool" not in offered, offered
    assert "tool" not in offered["action"]["enum"], offered
    request = result["tool_request"]
    assert request["status"] == "REJECTED"
    assert request["required_capability"] == "retrieval"
    types = [e.event_type.value for e in await svc.get_room_events(room_id)]
    assert "tool.call_rejected" in types
    assert "tool.call_completed" not in types


@pytest.mark.asyncio
async def test_an_unknown_tool_is_rejected(service: MultiplayerService) -> None:
    svc = service
    room_id, _ = await _room(svc)
    provider = _ToolRequestingProvider("shell.exec")
    result = await _run_tool_step(svc, room_id, "Researcher", provider)

    request = result["tool_request"]
    assert request["status"] == "REJECTED"
    assert request["reason"] == "unknown tool"
    assert "channel.read_context" in provider.offered_schemas[0]["properties"]["tool"]["enum"]


@pytest.mark.asyncio
async def test_a_channel_policy_denies_a_capability_the_agent_holds(
    service: MultiplayerService,
) -> None:
    svc = service
    room_id, _ = await _room(svc)
    await svc.set_room_policy(room_id, ["analysis"], "owner")
    provider = _ToolRequestingProvider("channel.read_context")
    result = await _run_tool_step(svc, room_id, "Researcher", provider)

    assert result["tool_request"]["status"] == "REJECTED"
    assert result["tool_request"]["effective"] == ["analysis"]


@pytest.mark.asyncio
async def test_a_workspace_policy_denies_a_capability_the_channel_allows(
    service: MultiplayerService,
) -> None:
    svc = service
    room_id, workspace_id = await _room(svc)
    await svc.set_workspace_policy(workspace_id, ["analysis"], "owner")
    provider = _ToolRequestingProvider("channel.read_context")
    result = await _run_tool_step(svc, room_id, "Researcher", provider)

    assert result["tool_request"]["status"] == "REJECTED"
    assert result["tool_request"]["effective"] == ["analysis"]


@pytest.mark.asyncio
async def test_a_per_member_grant_narrows_what_the_initiator_can_lend(
    service: MultiplayerService,
) -> None:
    """The human's own grant bounds the run, even where every other term allows the tool."""
    svc = service
    room_id, _ = await _room(svc)
    await svc.set_member_capabilities(room_id, "owner", ["analysis"], "owner")
    provider = _ToolRequestingProvider("channel.read_context")
    result = await _run_tool_step(svc, room_id, "Researcher", provider)

    assert result["tool_request"]["status"] == "REJECTED"
    assert result["tool_request"]["effective"] == ["analysis"]


# ── Agent reactions ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_agent_reacts_through_the_gateway_under_its_own_name(
    service: MultiplayerService,
) -> None:
    """The whole of the agent-reaction path: a run asks, the gateway decides, it runs."""
    svc = service
    room_id, _ = await _room(svc)
    message = await svc.send_message(room_id, MessageRole.HUMAN, "owner", "Ship it")
    provider = _ToolRequestingProvider(
        "message.react", {"message_id": message.message_id, "emoji": "\U0001f440"}
    )
    result = await _run_tool_step(svc, room_id, "Synthesizer", provider)

    assert "message.react" in provider.offered_schemas[0]["properties"]["tool"]["enum"]
    request = result["tool_request"]
    assert request["status"] == "EXECUTED", request
    assert request["required_capability"] == "writing"
    assert request["approval_id"] is None
    assert request["result"] == {"message_id": message.message_id, "emoji": "\U0001f440"}

    agent_id = (await svc.list_room_agents(room_id))[0].agent_id
    live = await svc.list_reactions(message.message_id)
    assert [(r.actor_id, r.actor_type) for r in live] == [(agent_id, ParticipantType.AGENT)]

    types = [e.event_type.value for e in await svc.get_room_events(room_id)]
    assert "tool.call_completed" in types
    assert "message.reaction_added" in types


@pytest.mark.asyncio
async def test_a_run_that_may_not_write_is_never_offered_the_reaction(
    service: MultiplayerService,
) -> None:
    """A reaction is a durable write, so a read-only run does not get one for free."""
    svc = service
    room_id, _ = await _room(svc)
    message = await svc.send_message(room_id, MessageRole.HUMAN, "owner", "Ship it")
    provider = _ToolRequestingProvider(
        "message.react", {"message_id": message.message_id, "emoji": "\U0001f440"}
    )
    result = await _run_tool_step(svc, room_id, "Researcher", provider)

    offered = provider.offered_schemas[0]["properties"]["tool"]["enum"]
    assert "message.react" not in offered, offered
    assert result["tool_request"]["status"] == "REJECTED"
    assert result["tool_request"]["required_capability"] == "writing"
    assert await svc.list_reactions(message.message_id) == []


@pytest.mark.asyncio
async def test_a_reaction_aimed_at_another_channel_fails_inside_the_gateway(
    service: MultiplayerService,
) -> None:
    """Channel isolation, and it settles as a FAILED request rather than an exception."""
    svc = service
    room_id, workspace_id = await _room(svc)
    elsewhere = await svc.create_room(workspace_id, "Elsewhere", "owner")
    foreign = await svc.send_message(elsewhere.room_id, MessageRole.HUMAN, "owner", "Not yours")
    provider = _ToolRequestingProvider(
        "message.react", {"message_id": foreign.message_id, "emoji": "\U0001f440"}
    )
    result = await _run_tool_step(svc, room_id, "Synthesizer", provider)

    assert result["tool_request"]["status"] == "FAILED"
    assert result["tool_request"]["reason"] == "message is not in this channel"
    assert await svc.list_reactions(foreign.message_id) == []


# ── The approval gate ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_gated_tool_waits_for_approval_then_executes(
    service: MultiplayerService,
) -> None:
    svc = service
    room_id, _ = await _room(svc)
    provider = _ToolRequestingProvider("task.create", {"title": "Draft the brief"})
    result = await _run_tool_step(svc, room_id, "Synthesizer", provider)

    request = result["tool_request"]
    assert request["status"] == "PENDING_APPROVAL"
    assert request["approval_id"]
    assert not await svc.repos.tasks.list_by_room(room_id), "gated tool ran before approval"

    await svc.approve_action(request["approval_id"], "owner")
    tasks = await svc.repos.tasks.list_by_room(room_id)
    assert [task.title for task in tasks] == ["Draft the brief"]
    stored = await svc.repos.tool_requests.get(request["request_id"])
    assert stored is not None and stored.status == "EXECUTED"
    types = [e.event_type.value for e in await svc.get_room_events(room_id)]
    assert types.index("approval.granted") < types.index("tool.call_completed")


@pytest.mark.asyncio
async def test_a_rejected_approval_never_runs_the_tool(service: MultiplayerService) -> None:
    svc = service
    room_id, _ = await _room(svc)
    provider = _ToolRequestingProvider("task.create", {"title": "Draft the brief"})
    result = await _run_tool_step(svc, room_id, "Synthesizer", provider)
    request = result["tool_request"]

    await svc.reject_action(request["approval_id"], "owner")
    assert not await svc.repos.tasks.list_by_room(room_id)
    stored = await svc.repos.tool_requests.get(request["request_id"])
    assert stored is not None and stored.status == "REJECTED"
    types = [e.event_type.value for e in await svc.get_room_events(room_id)]
    assert "tool.call_rejected" in types
    assert "tool.call_completed" not in types


@pytest.mark.asyncio
async def test_the_decision_is_durable_and_carries_its_evidence(
    service: MultiplayerService,
) -> None:
    svc = service
    room_id, _ = await _room(svc)
    provider = _ToolRequestingProvider("channel.read_context")
    result = await _run_tool_step(svc, room_id, "Coder", provider)

    stored = await svc.repos.tool_requests.get(result["tool_request"]["request_id"])
    assert stored is not None
    assert stored.status == "REJECTED"
    assert stored.required_capability == "retrieval"
    assert "retrieval" not in json.loads(stored.effective_json)
    assert stored.resolved_at is None or stored.status == "REJECTED"


# ── Revocation between the request and the grant ─────────────────────────────


async def _pending_task_request(svc: MultiplayerService, room_id: str) -> dict[str, Any]:
    provider = _ToolRequestingProvider("task.create", {"title": "Draft the brief"})
    result = await _run_tool_step(svc, room_id, "Synthesizer", provider)
    request = result["tool_request"]
    assert request["status"] == "PENDING_APPROVAL", request
    return request


@pytest.mark.asyncio
async def test_narrowing_the_channel_policy_before_approval_denies_the_tool(
    service: MultiplayerService,
) -> None:
    """A human's approval cannot restore what the policy no longer permits."""
    svc = service
    room_id, _ = await _room(svc)
    request = await _pending_task_request(svc, room_id)

    await svc.set_room_policy(room_id, ["analysis"], "owner")
    await svc.approve_action(request["approval_id"], "owner")

    assert not await svc.repos.tasks.list_by_room(room_id)
    stored = await svc.repos.tool_requests.get(request["request_id"])
    assert stored is not None and stored.status == "REJECTED"
    assert json.loads(stored.effective_json) == ["analysis"], stored.effective_json
    types = [e.event_type.value for e in await svc.get_room_events(room_id)]
    assert "tool.call_rejected" in types
    assert "tool.call_completed" not in types


@pytest.mark.asyncio
async def test_narrowing_the_member_grant_before_approval_denies_the_tool(
    service: MultiplayerService,
) -> None:
    svc = service
    room_id, _ = await _room(svc)
    request = await _pending_task_request(svc, room_id)

    await svc.set_member_capabilities(room_id, "owner", ["analysis"], "owner")
    await svc.approve_action(request["approval_id"], "owner")

    assert not await svc.repos.tasks.list_by_room(room_id)
    stored = await svc.repos.tool_requests.get(request["request_id"])
    assert stored is not None and stored.status == "REJECTED"


@pytest.mark.asyncio
async def test_removing_the_initiator_before_approval_denies_the_tool(
    service: MultiplayerService,
) -> None:
    """A run lends its initiator's capabilities; a non-member lends nothing."""
    svc = service
    room_id, _ = await _room(svc)
    request = await _pending_task_request(svc, room_id)
    # The run above was initiated by the owner. Remove that membership entirely: the
    # initiator now lends nothing, so the pending tool must not run on approval.
    await svc.repos.room_members.remove(room_id, "owner")

    await svc.approve_action(request["approval_id"], "owner")

    assert not await svc.repos.tasks.list_by_room(room_id)
    stored = await svc.repos.tool_requests.get(request["request_id"])
    assert stored is not None and stored.status == "REJECTED"
    assert json.loads(stored.effective_json) == []


@pytest.mark.asyncio
async def test_an_approval_executes_its_tool_exactly_once(
    service: MultiplayerService,
) -> None:
    svc = service
    room_id, _ = await _room(svc)
    request = await _pending_task_request(svc, room_id)

    await svc.approve_action(request["approval_id"], "owner")
    with pytest.raises(DomainError):
        await svc.approve_action(request["approval_id"], "owner")

    assert len(await svc.repos.tasks.list_by_room(room_id)) == 1
    types = [e.event_type.value for e in await svc.get_room_events(room_id)]
    assert types.count("tool.call_completed") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mangled",
    ["Task.Create", "task.create ", " task.create", "taskе.create", "", "../task.create"],
)
async def test_a_mangled_tool_name_is_not_the_tool(
    service: MultiplayerService, mangled: str
) -> None:
    """Lookup is exact: no case folding, trimming or lookalike resolves to a real tool."""
    svc = service
    room_id, _ = await _room(svc)
    provider = _ToolRequestingProvider(mangled, {"title": "Sneaky"})
    result = await _run_tool_step(svc, room_id, "Synthesizer", provider)

    assert result["tool_request"]["status"] == "REJECTED"
    assert result["tool_request"]["reason"] == "unknown tool"
    assert not await svc.repos.tasks.list_by_room(room_id)


# ── Identity is a gate, never a term ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_valid_owner_addressed_identity_still_cannot_write_for_a_viewer(
    service: MultiplayerService,
) -> None:
    """Identity says who acted. It can refuse earlier; it can never widen the set.

    A sixth term would break the invariant that effective capabilities are the
    intersection of user, agent, skill, channel and workspace, so the run below has
    everything identity can give it — live, unrevoked, and addressed by its own owner —
    and still gets nothing the viewer does not hold. The signed-challenge variant of
    the same gate lives in tests/security/test_agent_identity.py.
    """
    svc = service
    room_id, _ = await _room(svc)
    await svc.repos.room_members.add(
        RoomMember(room_id=room_id, user_id="viewer_member", role="viewer")
    )
    provider = _ToolRequestingProvider("artifact.write", {"name": "Rollout plan"})
    svc.nexus = NexusAgentBridge(model_provider=provider)
    agent = await svc.spawn_agent(
        room_id, await _template_id(svc, "Synthesizer"), name="Synthesizer", requested_by="owner"
    )
    await svc.set_agent_addressing(
        agent.agent_id, AddressingMode.OWNER_ONLY, "owner", owner_user_id="viewer_member"
    )
    identity = await svc.get_agent_identity(agent.agent_id)
    assert identity.revoked_at is None
    assert (await svc.get_agent_addressing(agent.agent_id)).owner_user_id == "viewer_member"

    with_identity = (await svc.agent_capability_terms(agent.agent_id, "viewer_member")).effective
    session = await svc.start_agent_session(room_id, agent.agent_id)
    execution = await svc.start_execution(session.session_id, "viewer_member")
    await svc.execute_agent_step(execution.execution_id, "Draft the rollout plan.")

    statuses = [row["status"] for row in await svc.db.fetch_all("SELECT status FROM tool_requests")]
    assert statuses == ["REJECTED"]
    assert await svc.repos.artifacts.list_by_room(room_id) == []

    # Revoking the identity refuses the next launch and changes no term at all: a
    # gate can close the door earlier, and it contributes nothing to the set inside.
    await svc.revoke_agent_identity(agent.agent_id, "owner")
    without_identity = (await svc.agent_capability_terms(agent.agent_id, "viewer_member")).effective
    assert with_identity == without_identity
    assert "writing" not in with_identity


# ── The API surface ──────────────────────────────────────────────────────────

TOKENS = {"owner-token": "owner", "sam-token": "sam"}
OWNER = {"Authorization": "Bearer owner-token"}
SAM = {"Authorization": "Bearer sam-token"}


@pytest.mark.asyncio
async def test_the_api_reports_the_five_terms_and_gates_policy_on_administer() -> None:
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
            workspace_id = bootstrap["workspace"]["workspace_id"]
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

            seen = await client.get(
                f"/api/v1/rooms/{room_id}/agents/{agent_id}/capabilities", headers=OWNER
            )
            assert seen.status_code == 200, seen.text
            body = seen.json()
            assert set(body["terms"]) == {"user", "agent", "skill", "channel", "workspace"}
            assert body["tools"] == ["channel.read_context"]

            # An editor may not set the channel policy; an admin may.
            assert (
                await client.patch(
                    f"/api/v1/rooms/{room_id}/policy",
                    headers=SAM,
                    json={"allowed_capabilities": ["analysis"]},
                )
            ).status_code == 403
            assert (
                await client.patch(
                    f"/api/v1/rooms/{room_id}/policy",
                    headers=OWNER,
                    json={"allowed_capabilities": ["analysis"]},
                )
            ).status_code == 200

            # The policy is durable and immediately narrows what the run may call.
            narrowed = (
                await client.get(
                    f"/api/v1/rooms/{room_id}/agents/{agent_id}/capabilities", headers=OWNER
                )
            ).json()
            assert narrowed["effective"] == ["analysis"]
            assert narrowed["tools"] == []
            types = [
                e["event_type"]
                for e in (await client.get(f"/api/v1/rooms/{room_id}/events", headers=OWNER)).json()
            ]
            assert "room.policy_updated" in types

            # A workspace policy bounds the channel in turn.
            assert (
                await client.patch(
                    f"/api/v1/workspaces/{workspace_id}/policy",
                    headers=OWNER,
                    json={"allowed_capabilities": []},
                )
            ).status_code == 200
            emptied = (
                await client.get(
                    f"/api/v1/rooms/{room_id}/agents/{agent_id}/capabilities", headers=OWNER
                )
            ).json()
            assert emptied["effective"] == []
