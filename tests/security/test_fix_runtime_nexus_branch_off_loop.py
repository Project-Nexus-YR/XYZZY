"""Finding 6: the NEXUS branch must not block the event loop, and must surface
the token usage and provider response id the native branch already does.

Before the fix, ``NexusAgentBridge.execute_step`` called ``self._executor.reason``
directly on the event loop: with a model call that takes real wall time, every
other room, every WebSocket and the lease sweep waited behind it. Its return
dict also carried no ``token_usage`` and its provenance had no
``provider_response_id``, so a run answered through that branch persisted zero
spend and lost the evidence a synthesis claim needs to be drilled back to its
source.
"""

from __future__ import annotations

import asyncio
import time
from enum import StrEnum
from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
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
    def __init__(self, policy: Any) -> None:
        self.policy = policy

    def execute(
        self, principal_id: str, name: str, tool_input: dict[str, Any], idempotency_key: str | None
    ) -> dict[str, Any]:
        del principal_id, name, tool_input, idempotency_key
        return {"decision": "ALLOW", "output": {}}


class _BlockingExecutor:
    """Enough of ``nexus_runtime.agent.AgentExecutor`` to prove the call is
    off the event loop: ``reason`` sleeps with the real, blocking ``time.sleep``,
    which only a worker thread can absorb without stalling the loop."""

    last: _BlockingExecutor | None = None

    def __init__(self, **kwargs: Any) -> None:
        self._model = kwargs["model"]
        self.tools: _Registry = kwargs["tools"]
        self.runs: dict[str, _Run] = {}
        _BlockingExecutor.last = self

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
        time.sleep(0.3)
        return self._model.complete(prompt, schema)

    def choose_action(self, run_id: str, response: dict[str, Any]) -> dict[str, Any]:
        del run_id
        return response

    def execute_action(self, run_id: str, action: dict[str, Any]) -> dict[str, Any] | None:
        run = self.runs[run_id]
        run.outputs = dict(action.get("output", {}))
        run.state = _AgentRunState.COMPLETED
        return run.outputs

    def update_state(self, run_id: str) -> _Run:
        return self.runs[run_id]


class _AnswersWithUsage:
    def complete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del schema
        return {
            "action": "finish",
            "output": {"content": f"answering: {prompt[-20:]}"},
            "token_usage": 321,
            "provider_response_id": "resp_nexus_branch",
            "provider_name": "stub-nexus",
            "provider_model": "stub-model",
        }


@pytest.fixture
def nexus_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in (
        ("_HAS_NEXUS", True),
        ("AgentExecutor", _BlockingExecutor),
        ("AgentRunState", _AgentRunState),
        ("ToolRegistry", _Registry),
        ("PolicyEngine", lambda grants: grants),
        ("InMemoryEventBus", lambda: None),
        ("SQLiteStateStore", lambda path: None),
        ("Agent", lambda **kwargs: type("Agent", (), kwargs)),
        ("Budget", lambda **kwargs: kwargs),
    ):
        monkeypatch.setattr(bridge_module, name, value, raising=False)
    _BlockingExecutor.last = None


@pytest.fixture
async def service(nexus_runtime: None) -> MultiplayerService:
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner"}))
    await svc.initialize()
    yield svc
    await db.close()


@pytest.mark.asyncio
async def test_the_nexus_branchs_blocking_call_does_not_stall_a_concurrent_task(
    service: MultiplayerService,
) -> None:
    """A concurrent tick advances well before the blocking call returns."""
    svc = service
    svc.nexus = NexusAgentBridge(model_provider=_AnswersWithUsage())
    assert bridge_module._HAS_NEXUS is True

    org = await svc.create_organization("Bridge org", "bridge-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(
        room.room_id,
        next(t.template_id for t in templates if t.name == "Researcher"),
        requested_by="owner",
        harness_id=NEXUS_HARNESS_ID,
    )
    session = await svc.start_agent_session(room.room_id, agent.agent_id)
    run = await svc.start_execution(session.session_id, "owner")

    ticked = asyncio.Event()

    async def tick_soon() -> float:
        started = time.monotonic()
        await asyncio.sleep(0.02)
        ticked.set()
        return time.monotonic() - started

    step_task = asyncio.create_task(
        svc.execute_agent_step(run.execution_id, "Assess the deploy.", "owner")
    )
    tick_task = asyncio.create_task(tick_soon())

    tick_elapsed = await tick_task
    result = await step_task

    # The 20 ms tick landed well inside the blocking call's 300 ms sleep,
    # which it could only do off the event loop that call is holding.
    assert ticked.is_set()
    assert tick_elapsed < 0.15
    assert result["action"] == "finish"
    assert result["token_usage"] == 321
    assert result["provenance"]["provider_response_id"] == "resp_nexus_branch"
    assert result["provenance"]["provider_name"] == "stub-nexus"
