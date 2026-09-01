"""Spawn refuses configuration this process cannot honor.

The API accepted a per-agent ``model_provider``/``model_name`` while execution
ran on one global provider per process, so a caller could name a provider that
was never consulted and have it read back from the agent row as if it had
been honored - and that unverified string could then leak into the audit
trail through provenance's fallback. The fix is refuse-what-you-won't-honor:
a mismatched identity is a spawn-time error, and provenance never trusts an
agent-declared string.
"""

from __future__ import annotations

from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import DomainError
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService


class _NamedProvider:
    """A provider with a verified identity, and a response that may omit it."""

    provider_name = "openai"
    provider_model = "gpt-configured"

    def __init__(self, *, include_identity: bool = True) -> None:
        self._include_identity = include_identity

    async def acomplete(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, response_schema
        response: dict[str, Any] = {
            "action": "finish",
            "output": {"content": "answer"},
        }
        if self._include_identity:
            response["provider_name"] = self.provider_name
            response["provider_model"] = self.provider_model
        return response


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner"}))
    await svc.initialize()
    svc.nexus = NexusAgentBridge(model_provider=_NamedProvider())
    yield svc
    await db.close()


async def _room(svc: MultiplayerService) -> str:
    org = await svc.create_organization("Identity org", "identity-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    return room.room_id


@pytest.mark.asyncio
async def test_spawn_with_matching_identity_is_accepted(service: MultiplayerService) -> None:
    room_id = await _room(service)
    template = (await service.list_agent_templates())[0]

    agent = await service.spawn_agent(
        room_id,
        template.template_id,
        model_provider="openai",
        model_name="gpt-configured",
        requested_by="owner",
        require_member=True,
    )

    assert agent.model_provider == "openai"
    assert agent.model_name == "gpt-configured"


@pytest.mark.asyncio
async def test_spawn_with_mismatched_provider_is_refused(service: MultiplayerService) -> None:
    room_id = await _room(service)
    template = (await service.list_agent_templates())[0]

    with pytest.raises(DomainError, match="anthropic.*openai"):
        await service.spawn_agent(
            room_id,
            template.template_id,
            model_provider="anthropic",
            model_name="gpt-configured",
            requested_by="owner",
            require_member=True,
        )


@pytest.mark.asyncio
async def test_spawn_with_mismatched_model_is_refused(service: MultiplayerService) -> None:
    room_id = await _room(service)
    template = (await service.list_agent_templates())[0]

    with pytest.raises(DomainError, match="gpt-other.*gpt-configured"):
        await service.spawn_agent(
            room_id,
            template.template_id,
            model_provider="openai",
            model_name="gpt-other",
            requested_by="owner",
            require_member=True,
        )


@pytest.mark.asyncio
async def test_spawn_with_empty_fields_stores_the_configured_identity(
    service: MultiplayerService,
) -> None:
    room_id = await _room(service)
    template = (await service.list_agent_templates())[0]

    agent = await service.spawn_agent(
        room_id,
        template.template_id,
        requested_by="owner",
        require_member=True,
    )

    assert agent.model_provider == "openai"
    assert agent.model_name == "gpt-configured"


@pytest.mark.asyncio
async def test_provenance_for_a_response_missing_identity_records_the_configured_provider(
    service: MultiplayerService,
) -> None:
    svc = service
    svc.nexus = NexusAgentBridge(model_provider=_NamedProvider(include_identity=False))
    room_id = await _room(svc)
    template = (await svc.list_agent_templates())[0]

    # A room-scoped template carries no model claim of its own, so the agent
    # row is self-describing off the empty-fields path exercised above.
    agent = await svc.spawn_agent(
        room_id,
        template.template_id,
        requested_by="owner",
        require_member=True,
    )
    session = await svc.start_agent_session(room_id, agent.agent_id)
    execution = await svc.start_execution(session.session_id, "owner")
    await svc.execute_agent_step(execution.execution_id, "decide", acting_as="owner")

    outputs = await svc.list_room_outputs(room_id)
    output = outputs[-1]

    # The provider's own response omitted its identity; the agent row's
    # (now merely configured-and-stored) strings must not be read back as if
    # they were the response's provenance.
    assert output.provider_name == "openai"
    assert output.provider_model == "gpt-configured"


@pytest.mark.asyncio
async def test_room_template_spawns_store_the_configured_identity(
    service: MultiplayerService,
) -> None:
    """The room-template path writes agents without a caller in the loop, and
    it must land on the same self-describing rows a direct spawn produces."""
    org = await service.create_organization("Identity org", "identity-org", "owner")
    workspace = await service.create_workspace(org.org_id, "Main", "main", "owner")
    template = (await service.list_agent_templates())[0]
    recipe = await service.create_room_template(
        workspace.workspace_id, "War room", "", [template.template_id], "owner"
    )

    room = await service.create_room(
        workspace.workspace_id, "From recipe", "owner", room_template_id=recipe.template_id
    )

    agents = await service.list_room_agents(room.room_id)
    assert agents
    assert all(a.model_provider == "openai" for a in agents)
    assert all(a.model_name == "gpt-configured" for a in agents)
