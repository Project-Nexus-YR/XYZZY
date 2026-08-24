"""A turn made of several prompts is authorized at every one of them.

A turn used to be one provider call, so re-deriving the effective set once per call
and re-deriving it once per turn were the same thing. They stopped being the same
thing when a tool result started going back to the harness: the second prompt of a
turn spends authority that was read before the first, unless it reads it again.

Two invariants live here. A grant withdrawn between two tool calls stops the second
one, and the run reaches a described terminal state rather than continuing on an
authority nobody holds. And a tool that needs a human suspends the turn in the run's
own approval state instead of spinning — the model is not prompted again while the
reviewer thinks, and granting the approval is what resumes the turn and produces the
answer the room is waiting for.
"""

from __future__ import annotations

from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import (
    ExecutionStatus,
    HarnessState,
    MessageRole,
    RunSettlement,
    ToolRequest,
)
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security import boundary
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


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner"}))
    await svc.initialize()
    yield svc
    await db.close()


async def _room_with_agent(svc: MultiplayerService, provider: Any, template: str) -> str:
    org = await svc.create_organization("Continue org", "continue-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    svc.nexus = NexusAgentBridge(model_provider=provider)
    templates = await svc.list_agent_templates()
    await svc.spawn_agent(
        room.room_id,
        next(t.template_id for t in templates if t.name == template),
        name=template,
        requested_by="owner",
    )
    return room.room_id


async def _the_run(svc: MultiplayerService) -> dict[str, Any]:
    rows = await svc.db.fetch_all("SELECT * FROM agent_runs ORDER BY created_at, run_id")
    assert len(rows) == 1, rows
    return rows[0]


@pytest.mark.asyncio
async def test_a_grant_withdrawn_between_two_tool_calls_stops_the_second(
    service: MultiplayerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first call was authorized. The prompt after it re-reads the records."""
    svc = service
    provider = _ToolThenAnswer("channel.read_context")
    room_id = await _room_with_agent(svc, provider, "Researcher")
    await svc.send_message(room_id, MessageRole.HUMAN, "owner", "The migration is blocked.")
    executed = svc._execute_tool_request

    async def withdraw_after_the_tool_ran(request: ToolRequest) -> ToolRequest:
        resolved = await executed(request)
        # The withdrawal belongs to a human's own request task; the test injects
        # it inside the turn, so it steps outside the agent-surface boundary the
        # way a concurrent request genuinely would be.
        token = boundary._agent_turn.set(None)
        try:
            await svc.set_member_capabilities(room_id, "owner", [], "owner")
        finally:
            boundary._agent_turn.reset(token)
        return resolved

    monkeypatch.setattr(svc, "_execute_tool_request", withdraw_after_the_tool_ran)

    await svc.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "@Researcher read the channel and tell us",
        invoke_mentioned_agents=True,
    )

    # The tool the owner could still lend ran and was audited; the prompt that would
    # have spent the withdrawn grant never reached the model.
    assert len(provider.prompts) == 1
    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert types.count("tool.call_completed") == 1
    assert [m.role for m in await svc.list_room_messages(room_id)] == [
        MessageRole.HUMAN,
        MessageRole.HUMAN,
    ]
    assert await svc.repos.agent_outputs.list_by_room(room_id) == []

    execution = (await svc.repos.executions.list_by_room(room_id))[0]
    assert execution.status is ExecutionStatus.FAILED
    assert "no effective capability" in execution.error
    run = await _the_run(svc)
    assert run["harness_state"] == HarnessState.SETTLED.value
    # The agent did not fail; the authority it was running under went away.
    assert run["settlement"] == RunSettlement.AUTHORITY_REVOKED.value


@pytest.mark.asyncio
async def test_a_tool_awaiting_approval_suspends_the_turn_and_the_grant_resumes_it(
    service: MultiplayerService,
) -> None:
    """The reviewer holds the turn, and releasing it produces the answer."""
    svc = service
    provider = _ToolThenAnswer("task.create", {"title": "Cut the auth migration"})
    room_id = await _room_with_agent(svc, provider, "Synthesizer")

    mention = await svc.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "@Synthesizer open a task for it",
        invoke_mentioned_agents=True,
    )

    # Suspended, not spinning: the model is not prompted again while she thinks, and
    # the run is holding the approval state rather than a streaming one.
    assert len(provider.prompts) == 1
    waiting = await _the_run(svc)
    assert waiting["harness_state"] == HarnessState.AWAITING_APPROVAL.value
    assert waiting["settlement"] is None
    assert await svc.repos.tasks.list_by_room(room_id) == []
    assert [m.role for m in await svc.list_room_messages(room_id)] == [MessageRole.HUMAN]
    approval = (await svc.list_pending_approvals(room_id))[0]

    await svc.approve_action(approval.approval_id, "owner", require_member=True)

    # The grant runs the tool and the turn goes on from there to the answer.
    assert [task.title for task in await svc.repos.tasks.list_by_room(room_id)] == [
        "Cut the auth migration"
    ]
    assert len(provider.prompts) == 2
    assert "task.create" in provider.prompts[1]
    replies = await svc.list_thread(mention.message_id)
    answers = [r.message for r in replies if r.message.role is MessageRole.AGENT]
    assert [a.content for a in answers] == ["here is the answer"]
    assert answers[0].parent_message_id == mention.message_id

    execution = (await svc.repos.executions.list_by_room(room_id))[0]
    assert execution.status is ExecutionStatus.COMPLETED
    run = await _the_run(svc)
    assert run["harness_state"] == HarnessState.SETTLED.value
    assert run["settlement"] == RunSettlement.END_TURN.value
    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert types.count("approval.granted") == 1
    assert types.count("tool.call_completed") == 1
    assert "agent.run.orphaned" not in types
