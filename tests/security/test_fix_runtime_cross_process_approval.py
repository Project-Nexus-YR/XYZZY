"""Finding 2: an approval decided on another process, or after a restart, must
still resume the suspended turn.

``NexusAgentBridge`` keeps its session table in memory, so a fresh instance
(a second process sharing the database file, or the same process after a
restart) has never heard of an execution another instance opened. Before the
fix, the step that resumes the turn read ``execution.run_id`` (durable, so it
is set) and skipped registering a bridge session, then the bridge's
``execute_step`` raised ``DomainError: no active run for execution ...``; the
tool the reviewer approved had already run, and the turn was settled FAILED
with no answer delivered. The fix rehydrates the bridge session from the
durable execution and agent_run rows whenever the bridge has no live one,
which is exactly the check ``get_run_id_for_execution`` answers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import ExecutionStatus, HarnessState, MessageRole, RunSettlement
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService


class _ToolThenAnswer:
    """One tool call, then the answer it enables."""

    def __init__(self, tool: str, tool_input: dict[str, Any] | None = None) -> None:
        self.tool = tool
        self.tool_input = tool_input or {}
        self.prompts: list[str] = []

    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del schema
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            return {
                "action": "tool",
                "tool": self.tool,
                "input": self.tool_input,
                "output": {"content": f"requesting {self.tool}"},
            }
        return {"action": "finish", "output": {"content": "here is the answer"}}


async def _open(db_path: Path, provider: Any) -> MultiplayerService:
    db = Database(str(db_path))
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner"}))
    await svc.initialize()
    svc.nexus = NexusAgentBridge(model_provider=provider)
    return svc


async def _park_a_turn_at_a_reviewer(svc: MultiplayerService, provider: Any) -> tuple[str, str]:
    """Drive a turn to the point where it suspends awaiting a human decision."""
    org = await svc.create_organization("Continue org", "continue-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    templates = await svc.list_agent_templates()
    await svc.spawn_agent(
        room.room_id,
        next(t.template_id for t in templates if t.name == "Synthesizer"),
        name="Synthesizer",
        requested_by="owner",
    )
    await svc.send_message(
        room.room_id,
        MessageRole.HUMAN,
        "owner",
        "@Synthesizer open a task for it",
        invoke_mentioned_agents=True,
    )
    assert len(provider.prompts) == 1
    approval = (await svc.list_pending_approvals(room.room_id))[0]
    return room.room_id, approval.approval_id


@pytest.mark.asyncio
async def test_approval_decided_on_a_second_service_instance_resumes_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reviewer answering through a second, concurrently live process still
    gets the answer, not a settled FAILED run."""
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db_path = tmp_path / "app.db"
    provider = _ToolThenAnswer("task.create", {"title": "Cut the auth migration"})

    svc1 = await _open(db_path, provider)
    room_id, approval_id = await _park_a_turn_at_a_reviewer(svc1, provider)

    # A second instance over the same file, live at the same time as the first
    # and whose bridge has never heard of this execution: this is what a second
    # worker process in the documented multi-process deployment looks like.
    svc2 = await _open(db_path, provider)
    try:
        await svc2.approve_action(approval_id, "owner", require_member=True)

        assert [task.title for task in await svc2.repos.tasks.list_by_room(room_id)] == [
            "Cut the auth migration"
        ]
        assert len(provider.prompts) == 2
        execution = (await svc2.repos.executions.list_by_room(room_id))[0]
        assert execution.status is ExecutionStatus.COMPLETED
        assert execution.error is None or execution.error == ""
        run = await svc2.db.fetch_one(
            "SELECT harness_state, settlement FROM agent_runs ORDER BY created_at LIMIT 1"
        )
        assert run is not None
        assert run["harness_state"] == HarnessState.SETTLED.value
        assert run["settlement"] == RunSettlement.END_TURN.value
    finally:
        await svc2.db.close()
        await svc1.db.close()


@pytest.mark.asyncio
async def test_approval_decided_after_a_restart_between_request_and_decision_resumes_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process that parked the turn exits and a fresh one opens the same
    file: no instance, no bridge, no in-memory table survives, only the
    database does."""
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db_path = tmp_path / "app.db"
    provider = _ToolThenAnswer("task.create", {"title": "Cut the auth migration"})

    svc1 = await _open(db_path, provider)
    room_id, approval_id = await _park_a_turn_at_a_reviewer(svc1, provider)
    # The restart: the process, its Database connection and its NexusAgentBridge
    # are gone; only the file on disk survives.
    await svc1.db.close()

    svc2 = await _open(db_path, provider)
    try:
        await svc2.approve_action(approval_id, "owner", require_member=True)

        assert [task.title for task in await svc2.repos.tasks.list_by_room(room_id)] == [
            "Cut the auth migration"
        ]
        assert len(provider.prompts) == 2
        execution = (await svc2.repos.executions.list_by_room(room_id))[0]
        assert execution.status is ExecutionStatus.COMPLETED
    finally:
        await svc2.db.close()
