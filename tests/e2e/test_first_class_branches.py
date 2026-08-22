"""Acceptance coverage for first-class Branch and branch-backed synthesis."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import (
    BranchMode,
    BranchStatus,
    ExecutionStatus,
    MessageRole,
    OutputDisposition,
)
from multiplayer.model_providers import ModelProviderError
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService


class _BranchAwareProvider:
    def __init__(self) -> None:
        self.inputs: list[str] = []
        self._specialist_index = 0

    async def acomplete(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        self.inputs.append(prompt)
        if "You are the synthesis stage" in prompt:
            output_ids = [
                line.split()[1] for line in prompt.splitlines() if line.startswith("AgentOutput ")
            ]
            document = {
                "summary": "The model reconciled the selected specialist evidence.",
                "recommendation": "Adopt the staged migration with an explicit rollback gate.",
                "claims": [
                    {
                        "text": "A staged migration best balances delivery and control risk.",
                        "source_output_ids": output_ids,
                        "confidence": 0.82,
                    }
                ],
                "risks": ["Rollback coverage may be incomplete."],
                "uncertainties": ["Production traffic shape remains unverified."],
                "next_action": "Run a regional rollback exercise.",
            }
            content = json.dumps(document)
            return {
                "action": "finish",
                "output": {
                    "content": content,
                    "provider": "test-model",
                    "model": "synthesis-test",
                    "simulated": False,
                },
                "provider_name": "test-model",
                "provider_model": "synthesis-test",
                "provider_response_id": "response_synthesis",
                "provider_evidence": content,
            }
        self._specialist_index += 1
        content = f"Independent specialist result {self._specialist_index}"
        return {
            "action": "finish",
            "output": {
                "content": content,
                "provider": "test-model",
                "model": "specialist-test",
                "simulated": False,
            },
            "provider_name": "test-model",
            "provider_model": "specialist-test",
            "provider_response_id": f"response_{self._specialist_index}",
            "provider_evidence": content,
        }


class _FailingProvider:
    async def acomplete(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, response_schema
        raise ModelProviderError("controlled provider failure")


@pytest.fixture
async def branch_service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    service = MultiplayerService(db, RealtimeHub())
    await service.initialize()
    yield service
    await db.close()


async def _room_and_agents(service: MultiplayerService, count: int) -> tuple[str, list[str]]:
    org = await service.create_organization("Branch org", "branch-org", "owner")
    workspace = await service.create_workspace(org.org_id, "Main", "main", "owner")
    room = await service.create_room(workspace.workspace_id, "Decision", "owner")
    templates = await service.list_agent_templates()
    agents = [
        await service.spawn_agent(room.room_id, template.template_id)
        for template in templates[:count]
    ]
    return room.room_id, [agent.agent_id for agent in agents]


@pytest.mark.asyncio
async def test_parallel_branch_partial_status_and_exact_immutable_context(
    branch_service: MultiplayerService,
) -> None:
    service = branch_service
    room_id, agent_ids = await _room_and_agents(service, 2)
    message = await service.send_message(
        room_id, MessageRole.HUMAN, "owner", "Prior accepted channel evidence"
    )
    branch, runs = await service.start_branch(
        room_id,
        BranchMode.PARALLEL,
        "Should we stage the authentication migration?",
        "owner",
        agent_ids,
    )

    assert branch.context_message_ids == (message.message_id,)
    assert branch.context_snapshot["messages"][0]["content"] == message.content
    assert branch.context_event_sequence >= 1
    assert len(branch.context_snapshot["messages"]) <= 50
    assert len(branch.context_snapshot["events"]) <= 100
    canonical_context = json.dumps(
        {
            "initiating_prompt": branch.initiating_prompt,
            "context_event_sequence": branch.context_event_sequence,
            "context_message_ids": list(branch.context_message_ids),
            "context_snapshot": branch.context_snapshot,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert branch.context_hash == hashlib.sha256(canonical_context).hexdigest()
    with pytest.raises(sqlite3.IntegrityError, match="context boundary is immutable"):
        await service.db.execute(
            "UPDATE branches SET context_hash = 'forged' WHERE branch_id = ?",
            (branch.branch_id,),
        )

    first = await service.execute_branch_run(branch.branch_id, runs[0].execution_id)
    assert first["status"] == "ok"
    assert (await service.get_branch(branch.branch_id)).status == BranchStatus.RUNNING
    assert await service.cancel_execution(runs[1].execution_id, "owner")
    terminal = await service.get_branch(branch.branch_id)
    assert terminal.status == BranchStatus.PARTIAL
    assert {run.status for run in await service.list_branch_runs(branch.branch_id)} == {
        ExecutionStatus.COMPLETED,
        ExecutionStatus.CANCELLED,
    }
    outputs = await service.repos.agent_outputs.list_by_branch(branch.branch_id)
    assert len(outputs) == 1
    assert outputs[0].branch_id == branch.branch_id


@pytest.mark.asyncio
async def test_model_synthesis_uses_selected_branch_outputs_and_links_artifact_version(
    branch_service: MultiplayerService,
) -> None:
    service = branch_service
    provider = _BranchAwareProvider()
    service.nexus = NexusAgentBridge(model_provider=provider)
    room_id, agent_ids = await _room_and_agents(service, 3)
    branch, runs = await service.start_branch(
        room_id,
        BranchMode.PARALLEL,
        "Choose a safe authentication migration strategy.",
        "owner",
        agent_ids,
    )
    output_ids: list[str] = []
    for run in runs:
        result = await service.execute_branch_run(branch.branch_id, run.execution_id)
        output_ids.append(str(result["output_id"]))
    assert (await service.get_branch(branch.branch_id)).status == BranchStatus.COMPLETED
    persisted_outputs = await service.repos.agent_outputs.list_by_branch(branch.branch_id)
    assert all(output.source_prompt == branch.initiating_prompt for output in persisted_outputs)
    assert all(branch.context_hash in output.provider_input for output in persisted_outputs)

    for output_id, disposition in zip(
        output_ids,
        (OutputDisposition.INCLUDED, OutputDisposition.INCLUDED, OutputDisposition.EXCLUDED),
        strict=True,
    ):
        await service.select_branch_output(branch.branch_id, output_id, disposition, "owner")

    artifact, version = await service.synthesize_branch_decision_brief(
        branch.branch_id, "Authentication migration", "owner"
    )
    assert artifact.current_version == 1
    assert version.branch_synthesis_id is not None
    synthesis = await service.repos.branch_syntheses.get(version.branch_synthesis_id)
    assert synthesis is not None
    assert synthesis.status.value == "COMPLETED"
    assert synthesis.simulated is False
    assert synthesis.artifact_version_id == version.version_id
    inputs = await service.repos.branch_syntheses.list_inputs(synthesis.synthesis_id)
    assert [item.output_id for item in inputs] == output_ids[:2]
    assert output_ids[2] not in synthesis.provider_input
    assert output_ids[0] in synthesis.provider_input and output_ids[1] in synthesis.provider_input
    assert "The model reconciled" in version.content

    claims = await service.repos.artifacts.get_version_provenance(version.version_id)
    assert {claim["output_id"] for claim in claims} == set(output_ids[:2])
    assert all(claim["text"].startswith("A staged migration") for claim in claims)
    assert len(await service.repos.agent_outputs.list_by_branch(branch.branch_id)) == 3


@pytest.mark.asyncio
async def test_deterministic_synthesis_fallback_is_conspicuously_marked(
    branch_service: MultiplayerService,
) -> None:
    service = branch_service
    room_id, agent_ids = await _room_and_agents(service, 2)
    branch, runs = await service.start_branch(
        room_id,
        BranchMode.PARALLEL,
        "Evaluate a migration.",
        "owner",
        agent_ids,
    )
    output_ids = [
        str((await service.execute_branch_run(branch.branch_id, run.execution_id))["output_id"])
        for run in runs
    ]
    for output_id in output_ids:
        await service.select_branch_output(
            branch.branch_id, output_id, OutputDisposition.INCLUDED, "owner"
        )
    _artifact, version = await service.synthesize_branch_decision_brief(
        branch.branch_id, "Fallback decision", "owner"
    )
    synthesis = await service.repos.branch_syntheses.get(str(version.branch_synthesis_id))
    assert synthesis is not None and synthesis.simulated is True
    assert "SIMULATED SYNTHESIS" in version.content
    assert "No decision recommendation was generated" in version.content


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_path", ["failure", "cancellation"])
async def test_turn_lock_releases_on_failure_or_cancellation(
    branch_service: MultiplayerService,
    terminal_path: str,
) -> None:
    service = branch_service
    room_id, agent_ids = await _room_and_agents(service, 1)
    branch, runs = await service.start_branch(
        room_id,
        BranchMode.TURN_LOCKED_SINGLE,
        "Complete or release this turn.",
        "owner",
        agent_ids,
    )
    if terminal_path == "failure":
        service.nexus = NexusAgentBridge(model_provider=_FailingProvider())
        result = await service.execute_branch_run(branch.branch_id, runs[0].execution_id)
        assert result == {"status": "error", "error": "controlled provider failure"}
        assert (await service.get_branch(branch.branch_id)).status == BranchStatus.FAILED
    else:
        assert await service.cancel_execution(runs[0].execution_id, "owner")
        assert (await service.get_branch(branch.branch_id)).status == BranchStatus.CANCELLED
    accepted = await service.send_message(
        room_id, MessageRole.HUMAN, "owner", f"Accepted after {terminal_path}"
    )
    assert accepted.content == f"Accepted after {terminal_path}"
