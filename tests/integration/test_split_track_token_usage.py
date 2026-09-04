"""Finding 69, stored half: a turn and a synthesis persist what they spent.

Round 1 surfaced provider token usage into /metrics but never wrote it to a row,
so nobody could answer what one branch cost. Migration 044 adds the column to
both places that settle, the two settlement sites persist it in the same
transaction that writes the row, and both API views the branch page reads
carry it through.
"""

from __future__ import annotations

from typing import Any

import pytest

import multiplayer.api.routes as routes_module
import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import BranchMode, OutputDisposition
from multiplayer.harness import MODEL_PROVIDER_HARNESS_ID
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.auth import AuthenticatedUser
from multiplayer.services.service import MultiplayerService

TURN_TOKENS = 123
SYNTHESIS_TOKENS = 77


class _MeteredBranchProvider:
    """Reports a known, different token count for a turn and for a synthesis."""

    async def acomplete(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        del response_schema
        if "You are the synthesis stage" in prompt:
            content = (
                '{"summary": "ok", "recommendation": "ship it", '
                '"claims": [{"text": "a", "source_output_ids": PLACEHOLDER, '
                '"confidence": 0.7}], "risks": [], "uncertainties": [], '
                '"next_action": "none"}'
            )
            return {
                "action": "finish",
                "output": {"content": content, "provider": "test-model", "model": "synth-test"},
                "token_usage": SYNTHESIS_TOKENS,
                "provider_name": "test-model",
                "provider_model": "synth-test",
                "provider_response_id": "resp_synth",
                "provider_evidence": content,
            }
        return {
            "action": "finish",
            "output": {"content": "the specialist's answer", "provider": "test-model"},
            "token_usage": TURN_TOKENS,
            "provider_name": "test-model",
            "provider_model": "turn-test",
            "provider_response_id": "resp_turn",
            "provider_evidence": "the specialist's answer",
        }


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner"}))
    await svc.initialize()
    svc.nexus = NexusAgentBridge(model_provider=_MeteredBranchProvider())
    routes_module.set_service(svc)
    yield svc
    routes_module.set_service(None)
    await db.close()


@pytest.mark.asyncio
async def test_a_turn_and_a_synthesis_persist_and_serve_their_token_usage(
    service: MultiplayerService,
) -> None:
    svc = service
    org = await svc.create_organization("Cost org", "cost-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(
        room.room_id,
        templates[0].template_id,
        requested_by="owner",
        harness_id=MODEL_PROVIDER_HARNESS_ID,
    )

    branch, runs = await svc.start_branch(
        room.room_id,
        BranchMode.TURN_LOCKED_SINGLE,
        "Choose the migration sequence.",
        "owner",
        [agent.agent_id],
    )
    result = await svc.execute_branch_run(branch.branch_id, runs[0].execution_id)
    output_id = str(result["output_id"])

    # BEFORE this change, Execution carried no token_usage field at all and this
    # assertion (like the ones after it) had no attribute to read.
    execution = await svc.repos.executions.get(runs[0].execution_id)
    assert execution is not None
    assert execution.token_usage == TURN_TOKENS

    await svc.select_branch_output(branch.branch_id, output_id, OutputDisposition.INCLUDED, "owner")
    # The claims' source ids are only known after the output exists, so the
    # provider closes over them via string substitution rather than a fixture.
    provider = svc.nexus.model_provider
    assert isinstance(provider, _MeteredBranchProvider)
    original_acomplete = provider.acomplete

    async def _acomplete_with_output_id(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        result = await original_acomplete(prompt, schema)
        content = result["output"]["content"]
        if "PLACEHOLDER" in content:
            content = content.replace("PLACEHOLDER", f'["{output_id}"]')
            result = {**result, "output": {**result["output"], "content": content}}
            result["provider_evidence"] = content
        return result

    provider.acomplete = _acomplete_with_output_id  # type: ignore[method-assign]

    _artifact, version = await svc.synthesize_branch_decision_brief(
        branch.branch_id, "Migration", "owner"
    )
    assert version.branch_synthesis_id is not None
    synthesis = await svc.repos.branch_syntheses.get(version.branch_synthesis_id)
    assert synthesis is not None
    assert synthesis.token_usage == SYNTHESIS_TOKENS

    # Both API views the branch page reads: the run's own token_usage, and the
    # branch's token_usage_total (its included outputs plus its synthesis).
    response = await routes_module.get_branch(branch.branch_id, AuthenticatedUser(user_id="owner"))
    run_records = {r["execution_id"]: r for r in response["runs"]}
    assert run_records[runs[0].execution_id]["token_usage"] == TURN_TOKENS
    assert response["branch"]["token_usage_total"] == TURN_TOKENS + SYNTHESIS_TOKENS
