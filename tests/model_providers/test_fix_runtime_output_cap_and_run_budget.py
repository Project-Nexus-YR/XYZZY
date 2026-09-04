"""Finding 7: no output-token cap was sent to either provider, and no per-run
spend ceiling existed, so a room member who can run an agent could spend the
operator's API key without any bound the server itself enforced.

Two independent guards close it: both providers now send a configured output
cap on every request, and the step loop refuses to run a step once a run's own
cumulative spend reaches ``XYZZY_RUN_TOKEN_BUDGET``, settling the run instead
of prompting again.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import ExecutionStatus, RunSettlement
from multiplayer.model_providers import (
    OpenAIChatCompletionsProvider,
    OpenAIResponsesProvider,
    model_provider_from_environment,
)
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService
from multiplayer.services.steps import _DEFAULT_RUN_TOKEN_BUDGET, _run_token_budget


def _responses_body(text: str) -> dict[str, Any]:
    return {
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"total_tokens": 21},
    }


class _CapturingResponsesTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        return httpx.Response(
            200,
            request=request,
            json=_responses_body(json.dumps({"action": "finish", "output": {"content": "ok"}})),
        )


class _CapturingChatTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        content = json.dumps({"action": "finish", "output": {"content": "ok"}})
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"total_tokens": 21},
            },
        )


@pytest.mark.asyncio
async def test_the_responses_provider_sends_a_configured_output_cap() -> None:
    transport = _CapturingResponsesTransport()
    provider = OpenAIResponsesProvider(
        api_key="sk-test", max_output_tokens=777, async_transport=transport
    )

    await provider.acomplete("hello", {"type": "object", "properties": {"action": {}}})

    assert transport.requests[0]["max_output_tokens"] == 777


@pytest.mark.asyncio
async def test_the_chat_completions_provider_sends_a_configured_output_cap() -> None:
    transport = _CapturingChatTransport()
    provider = OpenAIChatCompletionsProvider(
        base_url="http://localhost:9999/v1",
        model="local",
        max_output_tokens=333,
        async_transport=transport,
    )

    await provider.acomplete("hello", {"type": "object", "properties": {"action": {}}})

    assert transport.requests[0]["max_tokens"] == 333


def test_model_provider_from_environment_honours_the_configured_cap() -> None:
    provider = model_provider_from_environment(
        {"OPENAI_API_KEY": "sk-test", "XYZZY_MODEL_MAX_OUTPUT_TOKENS": "2048"}
    )
    assert isinstance(provider, OpenAIResponsesProvider)
    assert provider.max_output_tokens == 2048


def test_the_default_max_output_tokens_is_a_sane_positive_number() -> None:
    provider = model_provider_from_environment({"OPENAI_API_KEY": "sk-test"})
    assert isinstance(provider, OpenAIResponsesProvider)
    assert provider.max_output_tokens > 0


class _ToolThenAnswer:
    """A tool call that costs tokens, then the answer it would have enabled.

    A tool call does not end the turn: the gateway executes it and the loop
    prompts again with the result, which is exactly the second prompt a
    per-run budget has to stop once the first one already spent past it.
    """

    def __init__(self, tokens_per_step: int) -> None:
        self.tokens_per_step = tokens_per_step
        self.prompts: list[str] = []

    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del schema
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            return {
                "action": "tool",
                "tool": "channel.read_context",
                "input": {},
                "output": {"content": "reading the channel"},
                "token_usage": self.tokens_per_step,
            }
        return {
            "action": "finish",
            "output": {"content": "answered"},
            "token_usage": self.tokens_per_step,
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


async def _room_with_agent(svc: MultiplayerService, provider: Any) -> tuple[str, str]:
    org = await svc.create_organization("Budget org", "budget-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    svc.nexus = NexusAgentBridge(model_provider=provider)
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(
        room.room_id,
        next(t.template_id for t in templates if t.name == "Researcher"),
        requested_by="owner",
    )
    return room.room_id, agent.agent_id


def test_the_default_run_token_budget_is_a_sane_positive_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XYZZY_RUN_TOKEN_BUDGET", raising=False)
    assert _run_token_budget() == _DEFAULT_RUN_TOKEN_BUDGET
    assert _DEFAULT_RUN_TOKEN_BUDGET > 0


@pytest.mark.asyncio
async def test_a_run_that_reaches_its_token_budget_mid_turn_is_settled_not_prompted_again(
    service: MultiplayerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first prompt's tool call already spent past the budget; the loop's
    own second prompt, which would run inside the very same call, never
    happens, and the run is settled MAX_TOKENS instead."""
    from multiplayer.domain.models import DomainError

    monkeypatch.setenv("XYZZY_RUN_TOKEN_BUDGET", "50")
    svc = service
    provider = _ToolThenAnswer(tokens_per_step=60)
    room_id, agent_id = await _room_with_agent(svc, provider)
    session = await svc.start_agent_session(room_id, agent_id)
    run = await svc.start_execution(session.session_id, "owner")

    with pytest.raises(DomainError, match="token budget"):
        await svc.execute_agent_step(run.execution_id, "Assess the deploy.", "owner")

    # The tool call itself ran and was charged; the model was never prompted
    # a second time for the answer that call would have enabled.
    assert len(provider.prompts) == 1
    execution = await svc.repos.executions.get(run.execution_id)
    assert execution is not None
    assert execution.token_usage == 60
    assert execution.status is ExecutionStatus.FAILED
    settled_run = await svc.db.fetch_one(
        "SELECT settlement FROM agent_runs WHERE execution_id = ?", (run.execution_id,)
    )
    assert settled_run is not None
    assert settled_run["settlement"] == RunSettlement.MAX_TOKENS.value
