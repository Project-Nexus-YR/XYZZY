"""The NEXUS branch of the bridge, exercised rather than switched off.

``NexusAgentBridge`` has two branches, chosen by whether the NEXUS runtime imports.
Every other test in this repository sets ``_HAS_NEXUS`` to False, so the branch that
runs wherever NEXUS is installed — and it is installed beside this repository — was
the one branch nobody had ever checked.

It executed tool calls itself. ``execute_step`` handed the chosen action to
``AgentExecutor.execute_action``, which ran it through NEXUS's own ``ToolRegistry``
under a ``PolicyEngine({})`` the bridge constructs empty and never populates, and
returned a dict with no ``tool`` and no ``input`` key. XYZZY's gateway — the
five-way capability intersection, the approval gate, and the audit event — was not on
the path at all: with the registry empty every agent tool call failed as "unknown
tool", and any tool ever registered on it would have run with no capability check, no
approval and no audit trail.

The invariant here: XYZZY's gateway decides every tool call, whichever runtime is
present. NEXUS may execute a step; it may not decide one. The doubles below implement
the executor contract faithfully — including a registry that would happily run
anything it was handed — so what the tests assert is that it is never handed one.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import ExecutionStatus, HarnessState, MessageRole, RunSettlement
from multiplayer.harness import NEXUS_HARNESS_ID
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService


class _AgentRunState(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class _Run:
    def __init__(self, run_id: str, agent_id: str) -> None:
        self.run_id = run_id
        self.agent_id = agent_id
        self.state = _AgentRunState.CREATED
        self.outputs: dict[str, Any] = {}


class _Registry:
    """NEXUS's own tool registry, deciding under a policy XYZZY never configured.

    It runs whatever it is handed, which is the point: anything reaching it has left
    the workspace gateway behind.
    """

    def __init__(self, policy: Any) -> None:
        self.policy = policy
        self.executed: list[str] = []

    def execute(
        self, principal_id: str, name: str, tool_input: dict[str, Any], idempotency_key: str | None
    ) -> dict[str, Any]:
        del principal_id, tool_input, idempotency_key
        self.executed.append(name)
        return {"decision": "ALLOW", "output": {"ran": name}}


class _Executor:
    """Enough of ``nexus_runtime.agent.AgentExecutor`` for the bridge's NEXUS branch."""

    last: _Executor | None = None

    def __init__(self, **kwargs: Any) -> None:
        self._model = kwargs["model"]
        self.tools: _Registry = kwargs["tools"]
        self.runs: dict[str, _Run] = {}
        _Executor.last = self

    def create_run(self, *, agent: Any, run_id: str | None = None, **kwargs: Any) -> _Run:
        del kwargs
        run = _Run(run_id or "run", agent.agent_id)
        self.runs[run.run_id] = run
        return run

    def get_run(self, run_id: str) -> _Run:
        return self.runs[run_id]

    def transition(self, run_id: str, target: _AgentRunState, reason: str) -> _Run:
        del reason
        run = self.runs[run_id]
        run.state = target
        return run

    def reason(self, run_id: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del run_id
        response = self._model.complete(prompt, schema)
        assert isinstance(response, dict)
        return response

    def choose_action(self, run_id: str, response: dict[str, Any]) -> dict[str, Any]:
        del run_id
        return response

    def execute_action(self, run_id: str, action: dict[str, Any]) -> dict[str, Any] | None:
        run = self.runs[run_id]
        kind = str(action["action"])
        if kind == "finish":
            run.outputs = dict(action.get("output", {}))
            run.state = _AgentRunState.COMPLETED
            return run.outputs
        if kind == "tool":
            self.tools.execute(run.agent_id, str(action["tool"]), action.get("input", {}), None)
            return {"tool": action["tool"]}
        return None

    def update_state(self, run_id: str) -> _Run:
        return self.runs[run_id]


class _ToolThenAnswer:
    """The bridge's NEXUS branch reasons synchronously, so this provider does too."""

    def __init__(self, tool: str, tool_input: dict[str, Any] | None = None) -> None:
        self.tool = tool
        self.tool_input = tool_input or {}
        self.prompts: list[str] = []

    def complete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del schema
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            return {
                "action": "tool",
                "tool": self.tool,
                "input": self.tool_input,
                "output": {"content": f"requesting {self.tool}"},
            }
        return {"action": "finish", "output": {"content": "answered through the gateway"}}


@pytest.fixture
def nexus_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enter the branch the bridge takes when the NEXUS runtime is importable."""
    for name, value in (
        ("_HAS_NEXUS", True),
        ("AgentExecutor", _Executor),
        ("AgentRunState", _AgentRunState),
        ("ToolRegistry", _Registry),
        ("PolicyEngine", lambda grants: grants),
        ("InMemoryEventBus", lambda: None),
        ("SQLiteStateStore", lambda path: None),
        ("Agent", lambda **kwargs: type("Agent", (), kwargs)),
        ("Budget", lambda **kwargs: kwargs),
    ):
        monkeypatch.setattr(bridge_module, name, value, raising=False)
    _Executor.last = None


@pytest.fixture
async def service(nexus_runtime: None) -> MultiplayerService:
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner"}))
    await svc.initialize()
    yield svc
    await db.close()


async def _room_with_agent(svc: MultiplayerService, provider: Any, template: str) -> str:
    org = await svc.create_organization("Bridge org", "bridge-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    svc.nexus = NexusAgentBridge(model_provider=provider)
    assert bridge_module._HAS_NEXUS is True
    assert isinstance(svc.nexus._executor, _Executor)
    templates = await svc.list_agent_templates()
    await svc.spawn_agent(
        room.room_id,
        next(t.template_id for t in templates if t.name == template),
        name=template,
        requested_by="owner",
        harness_id=NEXUS_HARNESS_ID,
    )
    return room.room_id


def _nexus_ran() -> list[str]:
    executor = _Executor.last
    assert executor is not None
    return executor.tools.executed


async def _tool_requests(svc: MultiplayerService) -> list[tuple[str, str]]:
    rows = await svc.db.fetch_all(
        "SELECT tool, status FROM tool_requests ORDER BY created_at, request_id"
    )
    return [(str(row["tool"]), str(row["status"])) for row in rows]


@pytest.mark.asyncio
async def test_a_tool_a_nexus_backed_agent_asks_for_goes_through_the_workspace_gateway(
    service: MultiplayerService,
) -> None:
    svc = service
    provider = _ToolThenAnswer("channel.read_context")
    room_id = await _room_with_agent(svc, provider, "Researcher")
    await svc.send_message(room_id, MessageRole.HUMAN, "owner", "The migration is blocked.")

    await svc.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "@Researcher read the channel and tell us",
        invoke_mentioned_agents=True,
    )

    # The runtime that reasoned did not decide: the request went to the gateway,
    # which checked the capability, executed the tool and audited it.
    assert _nexus_ran() == []
    assert await _tool_requests(svc) == [("channel.read_context", "EXECUTED")]
    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert types.count("tool.call_started") == 1
    assert types.count("tool.call_completed") == 1
    # And the gateway's result went back to the runtime, which answered.
    assert len(provider.prompts) == 2
    assert "The migration is blocked." in provider.prompts[1]
    assert [m.content for m in await svc.list_room_messages(room_id)][-1] == (
        "answered through the gateway"
    )
    execution = (await svc.repos.executions.list_by_room(room_id))[0]
    assert execution.status is ExecutionStatus.COMPLETED


@pytest.mark.asyncio
async def test_a_nexus_backed_agent_is_refused_a_tool_outside_the_effective_set(
    service: MultiplayerService,
) -> None:
    """Deny by default reaches the NEXUS branch too, and NEXUS runs nothing anyway."""
    svc = service
    provider = _ToolThenAnswer("channel.read_context")
    room_id = await _room_with_agent(svc, provider, "Researcher")
    await svc.set_member_capabilities(room_id, "owner", ["analysis"], "owner")

    await svc.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "@Researcher read the channel and tell us",
        invoke_mentioned_agents=True,
    )

    assert _nexus_ran() == []
    assert await _tool_requests(svc) == [("channel.read_context", "REJECTED")]
    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert "tool.call_rejected" in types
    assert "tool.call_completed" not in types


@pytest.mark.asyncio
async def test_a_nexus_backed_tool_that_needs_approval_still_stops_at_the_reviewer(
    service: MultiplayerService,
) -> None:
    """The approval gate is the part a bypass removes most quietly."""
    svc = service
    provider = _ToolThenAnswer("task.create", {"title": "Cut the auth migration"})
    room_id = await _room_with_agent(svc, provider, "Synthesizer")

    await svc.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "@Synthesizer open a task for it",
        invoke_mentioned_agents=True,
    )

    assert _nexus_ran() == []
    assert await _tool_requests(svc) == [("task.create", "PENDING_APPROVAL")]
    assert await svc.repos.tasks.list_by_room(room_id) == []
    approval = (await svc.list_pending_approvals(room_id))[0]

    await svc.approve_action(approval.approval_id, "owner", require_member=True)

    assert _nexus_ran() == []
    assert [task.title for task in await svc.repos.tasks.list_by_room(room_id)] == [
        "Cut the auth migration"
    ]
    rows = await svc.db.fetch_all("SELECT * FROM agent_runs")
    assert rows[0]["harness_state"] == HarnessState.SETTLED.value
    assert rows[0]["settlement"] == RunSettlement.END_TURN.value
