"""Both built-in harnesses receive the same bounded specialist context."""

from __future__ import annotations

import re

import pytest

from multiplayer.domain.models import BranchMode, MessageRole
from multiplayer.harness import MODEL_PROVIDER_HARNESS_ID, NEXUS_HARNESS_ID
from tests.security.test_model_tool_gateway import _room, _service, _ToolChoosingTransport


@pytest.mark.asyncio
async def test_harnesses_deliver_specialist_instructions_once_and_preserve_exact_provenance():
    transport = _ToolChoosingTransport("channel.read_context")
    # Both calls answer immediately, so the captured prompts can be compared.
    transport.requests.append({})
    svc = await _service(transport)
    try:
        room_id = await _room(svc)
        template = next(t for t in await svc.list_agent_templates() if t.name == "Researcher")
        instructions = "Apply the SPECIALIST_CONTEXT_MARKER checklist."
        agents = [
            await svc.spawn_agent(
                room_id,
                template.template_id,
                name="Migration reviewer",
                system_prompt=instructions,
                requested_by="owner",
                harness_id=harness_id,
            )
            for harness_id in (NEXUS_HARNESS_ID, MODEL_PROVIDER_HARNESS_ID)
        ]
        await svc.send_message(room_id, MessageRole.HUMAN, "owner", "FROZEN_CHANNEL_EVIDENCE")
        branch, runs = await svc.start_branch(
            room_id,
            BranchMode.PARALLEL,
            "Assess the migration.",
            "owner",
            [agent.agent_id for agent in agents],
        )
        await svc.send_message(room_id, MessageRole.HUMAN, "owner", "LATER_CHANNEL_CONTENT")
        captured = []
        for run in runs:
            result = await svc.execute_branch_run(branch.branch_id, run.execution_id, "owner")
            payload = transport.requests[-1]["input"]
            captured.append(payload)
            output = await svc.repos.agent_outputs.get(result["output_id"])
            assert output.provider_input == payload
            assert output.source_prompt == branch.initiating_prompt
            assert payload.count(instructions) == 1
            assert "Specialist name: Migration reviewer\n" in payload
            assert f"Specialist role: {template.role}\n" in payload
            assert "FROZEN_CHANNEL_EVIDENCE" in payload
            assert "LATER_CHANNEL_CONTENT" not in payload
            assert branch.context_hash in payload

        # Only the untrusted-data delimiter is intentionally fresh per prompt.
        def normalize(prompt):
            return re.sub(r"#[0-9a-f]{8}", "#fence", prompt)

        assert normalize(captured[0]) == normalize(captured[1])
    finally:
        await svc.db.close()
