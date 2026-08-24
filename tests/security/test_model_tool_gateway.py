"""The tool gateway reached the way production reaches it: through the real provider.

``OpenAIResponsesProvider._decode_response`` sent the run's step schema to the model —
``action`` enum, offered tools and all — read the text back and returned
``{"action": "finish"}`` unconditionally. ``service.py`` dispatches a tool only when the
result says ``"tool"``, so no model-backed agent ever requested one: the five-way
intersection, the gateway, the approval flow and every tool in the registry were
exercised only by tests that substituted a provider object for the one the server builds.

Everything below stubs the HTTP transport instead, so the whole path runs — Responses
request, decoded action, harness, service, gateway, audit event — against the provider
class the server actually configures, under both registered harnesses.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import ExecutionStatus, MessageRole
from multiplayer.harness import MODEL_PROVIDER_HARNESS_ID, NEXUS_HARNESS_ID
from multiplayer.model_providers import OpenAIResponsesProvider
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService

HARNESSES = [NEXUS_HARNESS_ID, MODEL_PROVIDER_HARNESS_ID]


class _ToolChoosingTransport(httpx.AsyncBaseTransport):
    """The actions a model would choose: one tool call, then the answer it enables.

    The second answer matters as much as the first. A turn is not over when the
    gateway has run the tool — the room is waiting on what the agent says about the
    result — so a transport that only ever asks for tools would exercise the bound
    rather than the gateway.
    """

    def __init__(self, tool: str, tool_input: dict[str, Any] | None = None) -> None:
        self.tool = tool
        self.tool_input = tool_input or {}
        self.requests: list[dict[str, Any]] = []
        self.before_answering: Any = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        if self.before_answering is not None:
            await self.before_answering()
        body = json.dumps(
            {
                "action": "tool",
                "tool": self.tool,
                "input": self.tool_input,
                "output": {"content": f"requesting {self.tool}"},
            }
            if len(self.requests) == 1
            else {"action": "finish", "output": {"content": f"answered using {self.tool}"}}
        )
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "resp_tool_1",
                "output_text": body,
                "usage": {"total_tokens": 12},
            },
        )


def _offered_tools(transport: _ToolChoosingTransport, index: int = -1) -> list[str]:
    schema = transport.requests[index]["text"]["format"]["schema"]
    tool = schema["properties"].get("tool")
    return list(tool["enum"]) if tool else []


async def _tool_requests(svc: MultiplayerService) -> list[dict[str, Any]]:
    return list(
        await svc.db.fetch_all(
            "SELECT tool, status, reason, required_capability, effective_json, result_json "
            "FROM tool_requests ORDER BY created_at, request_id"
        )
    )


async def _service(transport: httpx.AsyncBaseTransport) -> MultiplayerService:
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner"}))
    await svc.initialize()
    svc.nexus = NexusAgentBridge(
        model_provider=OpenAIResponsesProvider(
            api_key="sk-test-never-persisted",
            model="gpt-gateway-test",
            async_transport=transport,
        )
    )
    return svc


async def _room(svc: MultiplayerService) -> str:
    org = await svc.create_organization("Gateway org", "gateway-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    return room.room_id


async def _run(svc: MultiplayerService, room_id: str, template: str, harness_id: str) -> str:
    templates = await svc.list_agent_templates()
    template_id = next(t.template_id for t in templates if t.name == template)
    agent = await svc.spawn_agent(room_id, template_id, requested_by="owner", harness_id=harness_id)
    session = await svc.start_agent_session(room_id, agent.agent_id)
    execution = await svc.start_execution(session.session_id, "owner")
    return execution.execution_id


@pytest.mark.parametrize("harness_id", HARNESSES)
@pytest.mark.asyncio
async def test_a_model_that_asks_for_an_offered_tool_reaches_the_gateway_and_the_audit_log(
    monkeypatch: pytest.MonkeyPatch, harness_id: str
) -> None:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    transport = _ToolChoosingTransport("channel.read_context")
    svc = await _service(transport)
    try:
        room_id = await _room(svc)
        await svc.send_message(
            room_id, MessageRole.HUMAN, "owner", "The migration is blocked on auth."
        )
        execution_id = await _run(svc, room_id, "Researcher", harness_id)

        result = await svc.execute_agent_step(execution_id, "Assess the deploy.", "owner")

        # The run offered the tool, the model asked for it, and the answer survived
        # the decode instead of being flattened into a finish.
        assert _offered_tools(transport, 0) == ["channel.read_context"]
        requests = await _tool_requests(svc)
        assert [(r["tool"], r["status"]) for r in requests] == [
            ("channel.read_context", "EXECUTED")
        ]
        assert requests[0]["required_capability"] == "retrieval"
        assert "The migration is blocked on auth." in str(requests[0]["result_json"])
        # And the turn did not end at the gateway: the tool result went back to the
        # model and the run produced the output the room was waiting for.
        assert result["action"] == "finish"
        assert len(transport.requests) == 2
        assert len(await svc.repos.agent_outputs.list_by_room(room_id)) == 1

        types = [e.event_type.value for e in await svc.get_room_events(room_id)]
        assert types.count("tool.call_started") == 1
        assert types.count("tool.call_completed") == 1
        assert types.count("agent.run.completed") == 1
        assert "tool.call_rejected" not in types
    finally:
        await svc.db.close()


@pytest.mark.parametrize("harness_id", HARNESSES)
@pytest.mark.asyncio
async def test_a_run_whose_effective_set_excludes_the_tool_is_never_offered_it(
    monkeypatch: pytest.MonkeyPatch, harness_id: str
) -> None:
    """The narrowed run offers nothing, and a model that asks anyway is refused."""
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    transport = _ToolChoosingTransport("channel.read_context")
    svc = await _service(transport)
    try:
        room_id = await _room(svc)
        await svc.set_member_capabilities(room_id, "owner", ["analysis"], "owner")
        execution_id = await _run(svc, room_id, "Researcher", harness_id)

        result = await svc.execute_agent_step(execution_id, "Assess the deploy.", "owner")

        # No tool is offered, so "tool" is not one of the actions either, and the
        # answer fails to decode rather than being flattened into a finish. "finish"
        # is the only action left: "delegate" and "wait" were offered while no branch
        # handled either, which left the run open for the lease sweep to mislabel.
        assert _offered_tools(transport) == []
        assert transport.requests[-1]["text"]["format"]["schema"]["properties"]["action"][
            "enum"
        ] == ["finish"]
        assert result["status"] == "error"
        assert result["error"] == "model provider chose an action this run did not offer"
        execution = await svc.repos.executions.get(execution_id)
        assert execution is not None and execution.status is ExecutionStatus.FAILED
        types = [e.event_type.value for e in await svc.get_room_events(room_id)]
        assert "tool.call_started" not in types
        assert "tool.call_completed" not in types
    finally:
        await svc.db.close()


@pytest.mark.parametrize("harness_id", HARNESSES)
@pytest.mark.asyncio
async def test_a_capability_withdrawn_while_the_model_thinks_is_rejected_at_the_gateway(
    monkeypatch: pytest.MonkeyPatch, harness_id: str
) -> None:
    """The offer was honest when it was made; the gateway decides on the records now."""
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    transport = _ToolChoosingTransport("channel.read_context")
    svc = await _service(transport)
    try:
        room_id = await _room(svc)
        execution_id = await _run(svc, room_id, "Researcher", harness_id)

        async def withdraw() -> None:
            await svc.set_member_capabilities(room_id, "owner", ["analysis"], "owner")

        transport.before_answering = withdraw
        await svc.execute_agent_step(execution_id, "Assess the deploy.", "owner")

        # The offer was honest when it was made, and the gateway refused on the
        # records as they stood when the answer came back.
        assert _offered_tools(transport, 0) == ["channel.read_context"]
        requests = await _tool_requests(svc)
        assert [r["status"] for r in requests] == ["REJECTED"]
        assert json.loads(str(requests[0]["effective_json"])) == ["analysis"]
        assert "retrieval" in str(requests[0]["reason"])
        types = [e.event_type.value for e in await svc.get_room_events(room_id)]
        assert "tool.call_rejected" in types
        assert "tool.call_completed" not in types
    finally:
        await svc.db.close()


@pytest.mark.parametrize("harness_id", HARNESSES)
@pytest.mark.asyncio
async def test_a_tool_that_requires_approval_stops_at_the_reviewer(
    monkeypatch: pytest.MonkeyPatch, harness_id: str
) -> None:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    transport = _ToolChoosingTransport("artifact.write", {"name": "Rollout plan"})
    svc = await _service(transport)
    try:
        room_id = await _room(svc)
        execution_id = await _run(svc, room_id, "Synthesizer", harness_id)

        result = await svc.execute_agent_step(execution_id, "Write the plan.", "owner")

        assert result["tool_request"]["status"] == "PENDING_APPROVAL"
        assert result["tool_request"]["approval_id"]
        assert await svc.list_room_artifacts(room_id) == []
        pending = await svc.list_pending_approvals(room_id)
        assert [a.approval_id for a in pending] == [result["tool_request"]["approval_id"]]

        await svc.approve_action(pending[0].approval_id, "owner", require_member=True)

        artifacts = await svc.list_room_artifacts(room_id)
        assert [a.name for a in artifacts] == ["Rollout plan"]
        types = [e.event_type.value for e in await svc.get_room_events(room_id)]
        assert types.count("tool.call_completed") == 1
    finally:
        await svc.db.close()
