"""Concurrency acceptance: a turn that uses a tool still has to reach an end.

Commit f0a5e65 made a model able to request a tool for the first time, and the branch
that served it was terminal. ``execute_agent_step`` returned the gateway's answer and
nothing prompted the harness again: the tool ran and was audited, the run kept a lease
it was no longer working under, zero agent messages reached the thread, and the sweep
later stamped the run ORPHANED with "lease expired after 1 attempt(s)" — a false
account of a dispatcher that had returned normally.

The invariants held here. A tool result goes back to the harness and the turn runs on
until the model answers. A turn that will not converge spends the run's own attempts
and settles PARKED, which is terminal and which a reader can name, instead of sitting
RUNNING. And every prompt of a continuing turn renews the lease, so a long tool-using
turn cannot be swept while it is legitimately working.
"""

from __future__ import annotations

from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import (
    DomainError,
    ExecutionStatus,
    HarnessState,
    MessageRole,
    RunSettlement,
    ToolRequest,
)
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService

_EXPIRED = "2000-01-01T00:00:00+00:00"


class _ReadThenAnswer:
    """One tool call, then the answer the tool result makes possible."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del schema
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            return {
                "action": "tool",
                "tool": "channel.read_context",
                "input": {},
                "output": {"content": "reading the channel first"},
            }
        return {"action": "finish", "output": {"content": "The auth migration is blocking it."}}


class _NeverAnswers:
    """A model that asks for the same tool forever, which is what a bound is for."""

    def __init__(self) -> None:
        self.calls = 0

    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, schema
        self.calls += 1
        return {
            "action": "tool",
            "tool": "channel.read_context",
            "input": {},
            "output": {"content": "reading the channel again"},
        }


class _SweepsAfterTheToolCall:
    """Runs the lease sweep from inside the prompt that follows a long tool call.

    The tool call is the long part of a tool-using turn, and it happens between the
    last thing that renewed the lease and the prompt that reads its result. That gap
    is the window the sweep could take a healthy run in, so the prompt on the far side
    of it has to be what renews the lease — and the sweep here has to find nothing.
    """

    def __init__(self, svc: MultiplayerService) -> None:
        self.svc = svc
        self.calls = 0
        self.swept: int | None = None

    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, schema
        self.calls += 1
        if self.calls == 1:
            return {
                "action": "tool",
                "tool": "channel.read_context",
                "input": {},
                "output": {"content": "a tool call that takes a long time"},
            }
        self.swept = await self.svc.sweep_expired_run_leases()
        return {"action": "finish", "output": {"content": "answered after the long tool call"}}


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner"}))
    await svc.initialize()
    yield svc
    await db.close()


async def _room_with_agent(svc: MultiplayerService, provider: Any) -> str:
    org = await svc.create_organization("Turn org", "turn-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    svc.nexus = NexusAgentBridge(model_provider=provider)
    templates = await svc.list_agent_templates()
    await svc.spawn_agent(
        room.room_id,
        next(t.template_id for t in templates if t.name == "Researcher"),
        name="Researcher",
        requested_by="owner",
    )
    return room.room_id


async def _the_run(svc: MultiplayerService) -> dict[str, Any]:
    rows = await svc.db.fetch_all("SELECT * FROM agent_runs ORDER BY created_at, run_id")
    assert len(rows) == 1, rows
    return rows[0]


@pytest.mark.asyncio
async def test_a_mentioned_agent_that_calls_a_tool_answers_in_the_thread_that_asked(
    service: MultiplayerService,
) -> None:
    """The whole defect in one run: the tool executes and the agent still speaks."""
    svc = service
    provider = _ReadThenAnswer()
    room_id = await _room_with_agent(svc, provider)
    root = await svc.send_message(room_id, MessageRole.HUMAN, "owner", "Why is the deploy stuck?")

    mention = await svc.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "@Researcher read the channel and tell us",
        parent_message_id=root.message_id,
        invoke_mentioned_agents=True,
    )

    # The tool result reached the second prompt, so the answer is not a guess.
    assert len(provider.prompts) == 2
    assert "channel.read_context" in provider.prompts[1]
    assert "Why is the deploy stuck?" in provider.prompts[1]

    replies = await svc.list_thread(root.message_id)
    agent_messages = [reply for reply in replies if reply.message.role is MessageRole.AGENT]
    assert len(agent_messages) == 1
    answer = agent_messages[0].message
    assert answer.content == "The auth migration is blocking it."
    assert answer.parent_message_id == mention.message_id
    assert answer.root_message_id == root.message_id
    assert answer.sender_id == (await svc.list_room_agents(room_id))[0].agent_id
    outputs = await svc.repos.agent_outputs.list_by_room(room_id)
    assert [output.output_id for output in outputs] == [answer.metadata["output_id"]]

    execution = (await svc.repos.executions.list_by_room(room_id))[0]
    assert execution.status is ExecutionStatus.COMPLETED
    run = await _the_run(svc)
    assert run["harness_state"] == HarnessState.SETTLED.value
    assert run["settlement"] == RunSettlement.END_TURN.value
    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert types.count("tool.call_completed") == 1
    assert types.count("agent.run.completed") == 1
    assert "agent.run.orphaned" not in types


@pytest.mark.asyncio
async def test_a_turn_that_never_answers_settles_parked_at_its_bound(
    service: MultiplayerService,
) -> None:
    """The bound is the run's own attempts, and the end of it has a name."""
    svc = service
    provider = _NeverAnswers()
    room_id = await _room_with_agent(svc, provider)

    await svc.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "@Researcher assess the deploy",
        invoke_mentioned_agents=True,
    )

    run = await _the_run(svc)
    assert provider.calls == run["max_attempts"]
    assert run["attempts"] == run["max_attempts"]
    assert run["harness_state"] == HarnessState.SETTLED.value
    assert run["settlement"] == RunSettlement.PARKED.value
    execution = (await svc.repos.executions.list_by_room(room_id))[0]
    assert execution.status is ExecutionStatus.FAILED
    assert "without an answer" in execution.error
    # Nothing was left RUNNING for the sweep to invent an account of.
    assert await svc.sweep_expired_run_leases() == 0
    assert [m.role for m in await svc.list_room_messages(room_id)] == [MessageRole.HUMAN]
    settled = [
        event.payload["settlement"]
        for event in await svc.get_room_events(room_id)
        if event.event_type.value == "agent.run.settled"
    ]
    assert settled == [RunSettlement.PARKED.value]
    # A run that used every attempt it had is not resumed; that is what parking means.
    with pytest.raises(DomainError, match="parked"):
        await svc.resume_agent_run(str(run["run_id"]), "owner")


@pytest.mark.asyncio
async def test_every_prompt_of_a_continuing_turn_renews_the_lease(
    service: MultiplayerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long tool-using turn is working, not orphaned, and the sweep must agree."""
    svc = service
    room_id = await _room_with_agent(svc, None)
    provider = _SweepsAfterTheToolCall(svc)
    svc.nexus = NexusAgentBridge(model_provider=provider)
    executed = svc._execute_tool_request

    async def age_the_lease(request: ToolRequest) -> ToolRequest:
        # The tool took longer than the lease the last prompt left behind.
        resolved = await executed(request)
        await svc.db.execute("UPDATE agent_runs SET lease_expires_at = ?", (_EXPIRED,))
        return resolved

    monkeypatch.setattr(svc, "_execute_tool_request", age_the_lease)

    await svc.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "@Researcher assess the deploy",
        invoke_mentioned_agents=True,
    )

    # The sweep ran from inside the second prompt, with the lease the tool call
    # outlived, and took nothing: that prompt had already renewed it.
    assert provider.calls == 2
    assert provider.swept == 0
    run = await _the_run(svc)
    assert run["settlement"] == RunSettlement.END_TURN.value
    assert run["lease_expires_at"] > _EXPIRED
    assert [m.role for m in await svc.list_room_messages(room_id)] == [
        MessageRole.HUMAN,
        MessageRole.AGENT,
    ]
    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert "agent.run.orphaned" not in types
