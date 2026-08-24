"""An agent on the model-provider harness must record where its answer came from.

The two harnesses return provenance in different places: the NEXUS one carries it
inside the turn output, and the model-provider one returns it in the ``TurnResult``
field named for it. The service read only the output, so every agent on the
model-provider harness recorded an ``AgentOutput`` with no provider input, no model,
no response id and no evidence — while the selection and synthesis paths above it
promise that any claim drills back to its exact source.

Nothing failed. The output was written, the synthesis cited it, and the provenance
behind the citation was empty.
"""

from __future__ import annotations

from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.harness import MODEL_PROVIDER_HARNESS_ID
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService

_PROMPT_MARKER = "migrate the session store"


class _AttributedProvider:
    """A provider that reports who answered, as a real one does."""

    async def acomplete(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        del response_schema
        return {
            "action": "finish",
            "output": {"content": "Managed identity, with a rollback window."},
            "provider_name": "openai",
            "provider_model": "gpt-test",
            "provider_response_id": "resp_abc123",
            "provider_evidence": "two prior incidents in the session store",
        }


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner"}))
    await svc.initialize()
    svc.nexus = NexusAgentBridge(model_provider=_AttributedProvider())
    yield svc
    await db.close()


@pytest.mark.asyncio
async def test_a_model_provider_agent_records_its_provenance(
    service: MultiplayerService,
) -> None:
    svc = service
    org = await svc.create_organization("Provenance org", "provenance-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(
        room.room_id,
        next(t.template_id for t in templates if t.name == "Researcher"),
        name="Researcher",
        requested_by="owner",
        harness_id=MODEL_PROVIDER_HARNESS_ID,
    )

    session = await svc.start_agent_session(room.room_id, agent.agent_id)
    execution = await svc.start_execution(session.session_id, "owner")
    await svc.execute_agent_step(execution.execution_id, _PROMPT_MARKER, acting_as="owner")

    outputs = await svc.list_room_outputs(room.room_id)
    assert outputs, "the agent produced no output at all"
    output = outputs[-1]

    instance = await svc.get_agent(agent.agent_id)
    assert instance.harness_id == MODEL_PROVIDER_HARNESS_ID

    # The exact prompt that reached the model is the drill-down target; without it a
    # synthesis claim cites an output whose own source is unrecorded.
    assert output.provider_input, "provider input was not recorded"
    assert _PROMPT_MARKER in output.provider_input
    assert output.provider_name == "openai"
    assert output.provider_model == "gpt-test"
    assert output.provider_response_id == "resp_abc123"
