"""Finding 5: a steer queued on one process must reach the prompt of a step
that runs on another, not merely be marked consumed.

Delivery used to live in ``NexusAgentBridge``'s own in-memory queue
(``_interventions``/``_pending_execution_interventions``), which a second
process, or the same process after a restart, never shares. The step that
consumed the steer read the durable rows only to mark them spent; the actual
text reached the model only if the same bridge instance that queued it also
ran the step. The fix builds the prompt's steer block in ``steps.py`` from the
durable ``interventions`` rows every time, so delivery no longer depends on
which process's memory holds the queue.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService


class _EchoesWhetherSteered:
    """Reports, in its own output, whether its prompt carried a steer."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del schema
        self.prompts.append(prompt)
        steered = "HUMAN INTERVENTION" in prompt
        return {"action": "finish", "output": {"content": f"steered={steered}"}}


async def _open(db_path: Path, provider: Any) -> MultiplayerService:
    db = Database(str(db_path))
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner", "narrow"}))
    await svc.initialize()
    svc.nexus = NexusAgentBridge(model_provider=provider)
    return svc


@pytest.mark.asyncio
async def test_a_steer_queued_on_one_process_reaches_the_prompt_of_a_step_on_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db_path = tmp_path / "app.db"
    provider = _EchoesWhetherSteered()

    svc1 = await _open(db_path, provider)
    org = await svc1.create_organization("Steer org", "steer-org", "owner")
    workspace = await svc1.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc1.create_room(workspace.workspace_id, "Decision", "owner")
    templates = await svc1.list_agent_templates()
    agent = await svc1.spawn_agent(
        room.room_id,
        next(t.template_id for t in templates if t.name == "Researcher"),
        requested_by="owner",
    )
    session = await svc1.start_agent_session(room.room_id, agent.agent_id)
    run = await svc1.start_execution(session.session_id, "owner")

    await svc1.intervene_execution(
        run.execution_id, "owner", "Read the channel and quote it back", require_member=True
    )
    before = await svc1.repos.interventions.list_unconsumed(run.execution_id)
    assert [steer.intervened_by for steer in before] == ["owner"]

    # A second instance over the same file, whose bridge has never heard of
    # this execution and holds no queued intervention of its own.
    svc2 = await _open(db_path, provider)
    try:
        result = await svc2.execute_agent_step(run.execution_id, "Assess the deploy.", "owner")

        assert "Read the channel and quote it back" in provider.prompts[0]
        assert result.get("action") == "finish"
        assert result["result"]["content"] == "steered=True"
        after = await svc2.repos.interventions.list_unconsumed(run.execution_id)
        assert after == []
    finally:
        await svc2.db.close()
        await svc1.db.close()
