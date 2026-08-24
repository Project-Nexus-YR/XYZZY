"""Governance actions are structurally outside the agent surface.

The four-tool registry never offered these actions, but that was a byproduct
of what happened to be registered - one registry entry away from wrong. The
boundary makes it a property: while a model-driven turn is executing, the
ambient context says so, and a fenced method refuses whoever calls it,
through whatever path, including a tool added next year.
"""

from typing import Any

import pytest

from multiplayer.db.connection import Database
from multiplayer.domain.models import ToolRequest
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security import AuthorizationError
from multiplayer.security.boundary import active_agent_turn, agent_turn
from multiplayer.security.capabilities import Posture
from multiplayer.services.service import MultiplayerService, _TurnContinuation


@pytest.fixture
async def service():
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub())
    await svc.initialize()
    yield svc
    await db.close()


def _fenced_calls(svc: MultiplayerService) -> dict[str, Any]:
    """Every fenced action; the fence fires before any argument is validated."""
    return {
        "room.policy": lambda: svc.set_room_policy("r", None, "u"),
        "room.posture": lambda: svc.declare_room_posture("r", Posture.STRICT, "u"),
        "member.capabilities": lambda: svc.set_member_capabilities("r", "u", None, "u"),
        "workspace.policy": lambda: svc.set_workspace_policy("w", None, "u"),
        "member.invite": lambda: svc.invite_room_member("r", "u2", "editor", "u"),
        "member.remove": lambda: svc.remove_room_member("r", "u2", "u"),
        "agent.spawn": lambda: svc.spawn_agent("r", "t"),
        "agent.identity.revoke": lambda: svc.revoke_agent_identity("a", "u"),
        "agent.remove": lambda: svc.remove_agent_from_room("a", "r", "u"),
        "approval.approve": lambda: svc.approve_action("ap", "u"),
        "approval.reject": lambda: svc.reject_action("ap", "u"),
        "agent.interrupt": lambda: svc.interrupt_agent("a", "u"),
    }


async def test_every_governance_action_refuses_inside_an_agent_turn(service):
    for call in _fenced_calls(service).values():
        with agent_turn("exec_test"):
            with pytest.raises(AuthorizationError, match="outside the agent surface"):
                await call()


async def test_the_fence_lifts_when_the_turn_ends(service):
    with agent_turn("exec_test"):
        pass
    assert active_agent_turn() is None
    # Outside a turn the same call proceeds past the fence to ordinary
    # validation - a missing room, not a boundary refusal.
    with pytest.raises(Exception) as excinfo:
        await service.set_room_policy("missing", None, "nobody")
    assert "outside the agent surface" not in str(excinfo.value)


async def test_the_step_driver_sets_the_boundary(service, monkeypatch):
    seen: list[str | None] = []

    async def probe(self, execution_id, continuation):
        seen.append(active_agent_turn())
        return {}

    monkeypatch.setattr(MultiplayerService, "_execute_one_agent_step_inner", probe)
    await service._execute_one_agent_step(
        "exec_probe", _TurnContinuation(prompt="p", acting_as="u")
    )
    assert seen == ["exec_probe"]
    assert active_agent_turn() is None


async def test_the_tool_executor_sets_the_boundary(service, monkeypatch):
    seen: list[str | None] = []

    async def probe(self, request):
        seen.append(active_agent_turn())
        return request

    monkeypatch.setattr(MultiplayerService, "_execute_tool_request_inner", probe)
    request = ToolRequest(
        request_id="req_probe",
        room_id="r",
        execution_id="exec_probe",
        agent_id="a",
        requested_by="a",
        tool="task.create",
    )
    await service._execute_tool_request(request)
    assert seen == ["exec_probe"]
    assert active_agent_turn() is None
