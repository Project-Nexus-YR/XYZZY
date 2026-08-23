"""Model-provider contract tests without live network calls or credentials."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import AgentInstance, Execution, ExecutionStatus, Session
from multiplayer.model_providers import (
    ModelProviderError,
    OpenAIResponsesProvider,
    WorkflowOnlyModelProvider,
    model_provider_from_environment,
)
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService


class _RoleAwareTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        prompt = str(payload["input"])
        role_line = next(
            line for line in prompt.splitlines() if line.startswith("Specialist role:")
        )
        role = role_line.removeprefix("Specialist role:").strip()
        content = f"{role} recommendation based on its distinct decision lens."
        await asyncio.sleep(0)
        return httpx.Response(
            200,
            request=request,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": content}],
                    }
                ],
                "usage": {"total_tokens": 21},
            },
        )


class _FailureTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            request=request,
            json={"error": "upstream echoed sk-test-must-never-be-persisted"},
        )


class _TimeoutTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream timeout details are suppressed", request=request)


def _agent(index: int, role: str) -> AgentInstance:
    return AgentInstance(
        agent_id=f"agent_{index}",
        template_id=f"template_{index}",
        room_id="room_1",
        name=f"{role} specialist",
        role=role,
        system_prompt=f"Apply the {role.lower()} review checklist.",
        capabilities=frozenset(),
        model_provider="openai",
        model_name="gpt-test",
    )


@pytest.mark.asyncio
async def test_parallel_specialists_receive_minimum_context_and_return_distinct_real_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    transport = _RoleAwareTransport()
    provider = OpenAIResponsesProvider(
        api_key="sk-test-not-real",
        model="gpt-test",
        async_transport=transport,
    )
    bridge = NexusAgentBridge(model_provider=provider)
    roles = ["Architect", "Security Reviewer", "Product Researcher"]

    async def run(index: int, role: str) -> dict[str, Any]:
        agent = _agent(index, role)
        session = Session(session_id=f"session_{index}", room_id="room_1", agent_id=agent.agent_id)
        execution = Execution(
            execution_id=f"execution_{index}",
            session_id=session.session_id,
            agent_id=agent.agent_id,
        )
        await bridge.create_execution(agent, session, "unused", execution)
        return await bridge.execute_step(execution.execution_id, "Should we migrate auth?")

    results = await asyncio.gather(*(run(index, role) for index, role in enumerate(roles)))

    assert len(transport.requests) == 3
    assert all(request["store"] is False for request in transport.requests)
    assert all(request["model"] == "gpt-test" for request in transport.requests)
    assert all("Should we migrate auth?" in request["input"] for request in transport.requests)
    assert all(result["status"] == "ok" and result["action"] == "finish" for result in results)
    assert {result["result"]["analysis_role"] for result in results} == set(roles)
    assert len({result["result"]["content"] for result in results}) == 3
    assert all(result["result"]["simulated"] is False for result in results)
    assert all("stub response" not in result["result"]["content"] for result in results)


@pytest.mark.asyncio
async def test_credential_free_fallback_is_conspicuously_labelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    bridge = NexusAgentBridge(model_provider=WorkflowOnlyModelProvider())
    agent = _agent(1, "Architect")
    session = Session(session_id="session_1", room_id="room_1", agent_id=agent.agent_id)
    execution = Execution(
        execution_id="execution_1", session_id=session.session_id, agent_id=agent.agent_id
    )
    await bridge.create_execution(agent, session, "unused", execution)

    result = await bridge.execute_step(execution.execution_id, "Should we migrate auth?")

    assert result["status"] == "ok"
    assert result["result"]["simulated"] is True
    assert result["result"]["provider"] == "workflow-only"
    assert "SIMULATED WORKFLOW OUTPUT" in result["result"]["content"]
    assert "not an AI analysis" in result["result"]["content"]
    assert "stub response" not in result["result"]["content"]


def test_environment_configuration_does_not_require_a_request_credential() -> None:
    fallback = model_provider_from_environment({})
    configured = model_provider_from_environment(
        {
            "OPENAI_API_KEY": "sk-test-not-real",
            "MULTIAI_OPENAI_MODEL": "gpt-test",
            "MULTIAI_MODEL_TIMEOUT_SECONDS": "7",
        }
    )

    assert isinstance(fallback, WorkflowOnlyModelProvider)
    assert isinstance(configured, OpenAIResponsesProvider)
    assert configured.model == "gpt-test"
    assert configured.timeout_seconds == 7


@pytest.mark.asyncio
async def test_synthesis_schema_opts_into_responses_structured_output() -> None:
    transport = _RoleAwareTransport()
    provider = OpenAIResponsesProvider(
        api_key="sk-test-not-real",
        model="gpt-test",
        async_transport=transport,
    )
    schema = {
        "type": "object",
        "properties": {"claims": {"type": "array", "items": {"type": "string"}}},
        "required": ["claims"],
        "additionalProperties": False,
    }

    # The transport only needs a Specialist role marker to synthesize its test
    # response; this assertion is about the outgoing Responses request.
    await provider.acomplete("Specialist role: Synthesizer", schema)

    request = transport.requests[0]
    assert request["text"]["format"] == {
        "type": "json_schema",
        "name": "multiai_response",
        "strict": True,
        "schema": schema,
    }


@pytest.mark.asyncio
async def test_provider_timeout_is_explicit_and_sanitized() -> None:
    provider = OpenAIResponsesProvider(
        api_key="sk-test-must-never-appear",
        model="gpt-test",
        timeout_seconds=2,
        async_transport=_TimeoutTransport(),
    )

    with pytest.raises(ModelProviderError) as caught:
        await provider.acomplete("decision", {})

    assert str(caught.value) == "model request timed out after 2 seconds"
    assert "sk-test" not in str(caught.value)


@pytest.mark.asyncio
async def test_provider_failure_persists_failed_execution_without_publishing_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    database = Database(":memory:")
    await database.connect()
    service = MultiplayerService(database, RealtimeHub())
    await service.initialize()
    service.nexus = NexusAgentBridge(
        model_provider=OpenAIResponsesProvider(
            api_key="sk-test-must-never-be-persisted",
            model="gpt-test",
            async_transport=_FailureTransport(),
        )
    )
    try:
        org = await service.create_organization("Acme", "acme", "user_1")
        workspace = await service.create_workspace(org.org_id, "Main", "main", "user_1")
        room = await service.create_room(workspace.workspace_id, "Decision", "user_1")
        template = (await service.list_agent_templates())[0]
        agent = await service.spawn_agent(room.room_id, template.template_id)
        session = await service.start_agent_session(room.room_id, agent.agent_id)
        execution = await service.start_execution(session.session_id, "user_1")

        result = await service.execute_agent_step(execution.execution_id, "Decide safely")

        persisted = await service.repos.executions.get(execution.execution_id)
        assert result == {"status": "error", "error": "model provider returned HTTP 503"}
        assert persisted is not None and persisted.status == ExecutionStatus.FAILED
        assert persisted.error == "model provider returned HTTP 503"
        assert await service.repos.agent_outputs.list_by_room(room.room_id) == []
        assert "sk-test" not in persisted.error
    finally:
        await database.close()
