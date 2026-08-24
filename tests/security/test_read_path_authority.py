"""Authority is established before every tool branch, and reads are a tool branch.

``_run_tool`` returned for ``channel.read_context`` before ``_run_authorization`` was
computed, so a read reached no re-check at all: no settled-run check and no membership
check. A removal that settled a run ``AGENT_REMOVED`` and emitted ``agent.left_room``
still let that run's next ``channel.read_context`` execute and hand the room's messages
back to the departed agent. The writers were safe because each re-checks inside its own
transaction, which is exactly why the read was the one that was missed. The continuation
loop then turned a one-shot window into a per-prompt one.

Two more invariants live here. Every steer that shaped any prompt of a turn is
re-derived at each later prompt, so narrowing the person who steered narrows the rest
of the turn rather than leaving a set cached from the prompt that accepted her text.
And ``_execute_tool_request`` really never raises: it caught ``RunAuthorityRevoked``
and ``DomainError`` only, so ``add_agent_reaction``'s membership check — a bare
``AuthorizationError``, which is a ``PermissionError`` — escaped and left a
``tool_requests`` row at ``PENDING_APPROVAL`` under a ``tool.call_started`` event with
no completion and no rejection.
"""

from __future__ import annotations

from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import HarnessState, MessageRole, RunSettlement
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security import boundary
from multiplayer.services.service import MultiplayerService

SECRET = "the rollback key is in the vault"

_TERMINAL_TOOL_EVENTS = {"tool.call_completed", "tool.call_failed", "tool.call_rejected"}


async def _as_human(coro: Any) -> Any:
    """A concurrent human request runs with no turn context.

    The stub model injects these calls mid-turn, so they step outside the
    agent-surface boundary the way a genuinely concurrent request would be.
    """
    token = boundary._agent_turn.set(None)
    try:
        return await coro
    finally:
        boundary._agent_turn.reset(token)


def _read_context() -> dict[str, Any]:
    return {
        "action": "tool",
        "tool": "channel.read_context",
        "input": {},
        "output": {"content": "reading the channel"},
    }


class _ReadsAgainAfterBeingRemoved:
    """Reads once as a member, then again after a removal lands mid-turn."""

    def __init__(self) -> None:
        self.svc: MultiplayerService | None = None
        self.room_id = ""
        self.agent_id = ""
        self.prompts: list[str] = []

    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del schema
        self.prompts.append(prompt)
        assert self.svc is not None
        if len(self.prompts) == 1:
            return _read_context()
        if len(self.prompts) == 2:
            # Said in the room after the agent's first read and before its second.
            await self.svc.send_message(self.room_id, MessageRole.HUMAN, "owner", SECRET)
            await _as_human(self.svc.remove_agent_from_room(self.agent_id, self.room_id, "owner"))
            return _read_context()
        return {"action": "finish", "output": {"content": "answered"}}


class _ReactsAfterBeingRemoved:
    """Asks to react at the moment its membership stops existing."""

    def __init__(self, message_id: str = "") -> None:
        self.svc: MultiplayerService | None = None
        self.room_id = ""
        self.agent_id = ""
        self.message_id = message_id
        self.prompts: list[str] = []

    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del schema
        self.prompts.append(prompt)
        assert self.svc is not None
        if len(self.prompts) == 1:
            await _as_human(self.svc.remove_agent_from_room(self.agent_id, self.room_id, "owner"))
            return {
                "action": "tool",
                "tool": "message.react",
                "input": {"message_id": self.message_id, "emoji": "\N{THUMBS UP SIGN}"},
                "output": {"content": "reacting"},
            }
        return {"action": "finish", "output": {"content": "answered"}}


class _ReadsWhileASteererIsNarrowed:
    """Steers the turn, then narrows the steerer while a later prompt is answered.

    The narrowing lands after that prompt's terms were computed and before its tool
    request is decided, which is precisely the window a cached bound survives.
    """

    def __init__(self, *, narrow: bool) -> None:
        self.narrow = narrow
        self.svc: MultiplayerService | None = None
        self.room_id = ""
        self.execution_id = ""
        self.prompts: list[str] = []
        self.schemas: list[dict[str, Any]] = []

    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.prompts.append(prompt)
        self.schemas.append(schema)
        assert self.svc is not None
        if len(self.prompts) == 1:
            # The steer enters the turn here, so every later prompt is bounded by it.
            await _as_human(
                self.svc.intervene_execution(
                    self.execution_id, "steerer", "check the channel first"
                )
            )
            return _read_context()
        if len(self.prompts) == 2:
            if self.narrow:
                await _as_human(
                    self.svc.set_member_capabilities(
                        self.room_id, "steerer", ["analysis", "research"], "owner"
                    )
                )
            return _read_context()
        return {"action": "finish", "output": {"content": "answered"}}


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner", "steerer"}))
    await svc.initialize()
    yield svc
    await db.close()


async def _room_with_agent(
    svc: MultiplayerService, provider: Any, template: str = "Researcher"
) -> tuple[str, str]:
    """A room and one agent. Researcher holds retrieval; Synthesizer holds writing."""
    org = await svc.create_organization("Read org", "read-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    svc.nexus = NexusAgentBridge(model_provider=provider)
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(
        room.room_id,
        next(t.template_id for t in templates if t.name == template),
        name=template,
        requested_by="owner",
    )
    return room.room_id, agent.agent_id


async def _run_a_turn(svc: MultiplayerService, room_id: str, agent_id: str) -> str:
    session = await svc.start_agent_session(room_id, agent_id)
    execution = await svc.start_execution(session.session_id, "owner")
    await svc.execute_agent_step(execution.execution_id, "Assess the deploy.", "owner")
    return execution.execution_id


async def _tool_requests(svc: MultiplayerService) -> list[dict[str, Any]]:
    return await svc.db.fetch_all("SELECT * FROM tool_requests ORDER BY created_at, request_id")


def _assert_every_started_call_ended(events: list[Any]) -> None:
    """No tool call may be started and left without a terminal event.

    The converse is allowed and normal: a request the gateway refuses before it runs
    is rejected without ever having started.
    """
    started = {e.payload["request_id"] for e in events if e.event_type.value == "tool.call_started"}
    ended = {e.payload["request_id"] for e in events if e.event_type.value in _TERMINAL_TOOL_EVENTS}
    assert started, "the turn was supposed to start a tool call"
    assert not started - ended, sorted(started - ended)


# ── The read path ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_removed_agents_read_is_refused_mid_continuation(
    service: MultiplayerService,
) -> None:
    """The leak: "it only reads" was never a reason to skip the gate."""
    svc = service
    provider = _ReadsAgainAfterBeingRemoved()
    room_id, agent_id = await _room_with_agent(svc, provider)
    provider.svc, provider.room_id, provider.agent_id = svc, room_id, agent_id
    await svc.send_message(room_id, MessageRole.HUMAN, "owner", "Why is the deploy stuck?")

    await _run_a_turn(svc, room_id, agent_id)

    requests = await _tool_requests(svc)
    assert len(requests) == 2, requests
    # The member's read ran. The gate has to refuse the departed agent without
    # refusing the room's own.
    assert requests[0]["status"] == "EXECUTED"
    assert "Why is the deploy stuck?" in requests[0]["result_json"]
    # The departed agent's read did not.
    assert requests[1]["status"] == "REJECTED"
    assert requests[1]["result_json"] == "{}"

    # Nothing said in the room after it left reached it, by any route.
    assert all(SECRET not in request["result_json"] for request in requests)
    assert all(SECRET not in prompt for prompt in provider.prompts)
    assert len(provider.prompts) == 2, "the turn kept prompting after it was settled"

    events = await svc.get_room_events(room_id)
    types = [event.event_type.value for event in events]
    assert "agent.left_room" in types
    assert "agent.run.authority_revoked" in types
    assert types.index("agent.left_room") < types.index("tool.call_rejected")
    _assert_every_started_call_ended(events)

    run = (await svc.db.fetch_all("SELECT * FROM agent_runs"))[0]
    assert run["harness_state"] == HarnessState.SETTLED.value


@pytest.mark.asyncio
async def test_a_members_read_still_runs(service: MultiplayerService) -> None:
    """The gate is a gate, not a wall: nothing changes for a run still authorized."""
    svc = service

    class _ReadThenAnswer:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
            del schema
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                return _read_context()
            return {"action": "finish", "output": {"content": "answered"}}

    provider = _ReadThenAnswer()
    room_id, agent_id = await _room_with_agent(svc, provider)
    await svc.send_message(room_id, MessageRole.HUMAN, "owner", "Why is the deploy stuck?")

    await _run_a_turn(svc, room_id, agent_id)

    requests = await _tool_requests(svc)
    assert [r["status"] for r in requests] == ["EXECUTED"]
    assert "Why is the deploy stuck?" in requests[0]["result_json"]
    _assert_every_started_call_ended(await svc.get_room_events(room_id))


# ── The "never raises" contract ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_tool_that_raises_authorization_error_still_ends_its_call(
    service: MultiplayerService,
) -> None:
    """A started call that never ended is a hole in the audit trail, not a detail."""
    svc = service
    provider = _ReactsAfterBeingRemoved()
    room_id, agent_id = await _room_with_agent(svc, provider, "Synthesizer")
    message = await svc.send_message(room_id, MessageRole.HUMAN, "owner", "Ship it?")
    provider.svc, provider.room_id = svc, room_id
    provider.agent_id, provider.message_id = agent_id, message.message_id

    # The contract is that this returns rather than raising.
    await _run_a_turn(svc, room_id, agent_id)

    requests = await _tool_requests(svc)
    assert len(requests) == 1
    assert requests[0]["status"] == "REJECTED", requests[0]
    assert requests[0]["status"] != "PENDING_APPROVAL"

    events = await svc.get_room_events(room_id)
    types = [event.event_type.value for event in events]
    assert types.count("tool.call_started") == 1
    assert types.count("tool.call_rejected") == 1
    _assert_every_started_call_ended(events)
    # And the agent did not sign a reaction on its way out of the room.
    assert await svc.list_reactions(message.message_id) == []


# ── A steer is re-derived at every prompt it bounds ──────────────────────────


async def _steered_turn(svc: MultiplayerService, provider: _ReadsWhileASteererIsNarrowed) -> str:
    room_id, agent_id = await _room_with_agent(svc, provider)
    await svc.invite_room_member(room_id, "steerer", "editor", "owner")
    await svc.send_message(room_id, MessageRole.HUMAN, "owner", "Ship it?")
    provider.svc, provider.room_id = svc, room_id
    session = await svc.start_agent_session(room_id, agent_id)
    execution = await svc.start_execution(session.session_id, "owner")
    provider.execution_id = execution.execution_id
    await svc.execute_agent_step(execution.execution_id, "Assess the deploy.", "owner")
    return room_id


def _offered_tools(schema: dict[str, Any]) -> list[str]:
    tool = schema.get("properties", {}).get("tool", {})
    return list(tool.get("enum", []))


@pytest.mark.asyncio
async def test_narrowing_a_steerer_mid_turn_narrows_the_rest_of_the_turn(
    service: MultiplayerService,
) -> None:
    """A cached bound is an authorization input frozen at the moment it was computed."""
    svc = service
    provider = _ReadsWhileASteererIsNarrowed(narrow=True)
    room_id = await _steered_turn(svc, provider)

    requests = await _tool_requests(svc)
    # The first read ran under a steerer who still held retrieval.
    assert requests[0]["status"] == "EXECUTED"
    # The second was decided after she lost it, and her steer bounds this turn.
    assert requests[1]["status"] == "REJECTED", requests[1]
    assert "retrieval" in requests[1]["reason"]
    assert requests[1]["result_json"] == "{}"

    # And the prompt after it was not offered a tool the run could no longer call:
    # the bound reached the next prompt too, rather than being cached wide.
    assert _offered_tools(provider.schemas[0]) == ["channel.read_context"]
    assert _offered_tools(provider.schemas[2]) == []
    _assert_every_started_call_ended(await svc.get_room_events(room_id))


@pytest.mark.asyncio
async def test_an_unnarrowed_steerer_leaves_the_turn_alone(
    service: MultiplayerService,
) -> None:
    """Re-deriving must not narrow a turn nobody narrowed."""
    svc = service
    provider = _ReadsWhileASteererIsNarrowed(narrow=False)
    await _steered_turn(svc, provider)

    requests = await _tool_requests(svc)
    assert [r["status"] for r in requests] == ["EXECUTED", "EXECUTED"], requests
    assert _offered_tools(provider.schemas[2]) == ["channel.read_context"]
    run = (await svc.db.fetch_all("SELECT * FROM agent_runs"))[0]
    assert run["settlement"] == RunSettlement.END_TURN.value
