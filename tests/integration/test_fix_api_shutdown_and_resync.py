"""Finding 8: a graceful stop must not wait out an open A2A SSE stream's
30-second reauth beat (or hang forever, pre-fix, since `uvicorn.run` never
passed a `timeout_graceful_shutdown`). Finding 10... no — finding 70: a hub
queue overflow enqueues a `resync` marker the stream used to silently drop,
so an evicted terminal update could leave it open forever; it must now
re-read the task and recover.

Both live in the same generator (`a2a._task_events`), so one file covers both.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from multiplayer.api import routes
from multiplayer.server import create_app
from multiplayer.server import main as server_main

TOKENS = {"owner-token": "user_1"}
AUTH = {"Authorization": "Bearer owner-token"}


@pytest.fixture
async def client():
    app = create_app(":memory:", auth_tokens=TOKENS)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c


def _hold_the_task_open(monkeypatch: pytest.MonkeyPatch) -> None:
    from multiplayer.services.service import MultiplayerService

    monkeypatch.setattr(
        MultiplayerService, "dispatch_agent_task_in_background", lambda self, task: None
    )


async def _room_with_agent(client: AsyncClient) -> tuple[str, str]:
    org = (
        await client.post(
            "/api/v1/organizations", json={"name": "Acme", "slug": "acme"}, headers=AUTH
        )
    ).json()
    workspace = (
        await client.post(
            f"/api/v1/organizations/{org['org_id']}/workspaces",
            json={"name": "Main", "slug": "main"},
            headers=AUTH,
        )
    ).json()
    room = (
        await client.post(
            f"/api/v1/workspaces/{workspace['workspace_id']}/rooms",
            json={"name": "Room"},
            headers=AUTH,
        )
    ).json()
    await client.post(f"/api/v1/rooms/{room['room_id']}/join", headers=AUTH)
    templates = (await client.get("/api/v1/agent-templates", headers=AUTH)).json()
    agent = (
        await client.post(
            f"/api/v1/rooms/{room['room_id']}/agents",
            json={"template_id": templates[0]["template_id"], "name": "Forge"},
            headers=AUTH,
        )
    ).json()
    return room["room_id"], agent["agent_id"]


def _send_params(room_id: str, agent_id: str) -> dict[str, Any]:
    return {
        "message": {
            "kind": "message",
            "messageId": "msg-1",
            "role": "user",
            "parts": [{"kind": "text", "text": "start"}],
            "metadata": {"roomId": room_id, "targetAgentId": agent_id},
        }
    }


async def _call(client: AsyncClient, method: str, params: dict[str, Any], call_id: Any = 1) -> Any:
    return await client.post(
        "/a2a/v1",
        json={"jsonrpc": "2.0", "id": call_id, "method": method, "params": params},
        headers=AUTH,
    )


# ── Finding 8: the shutdown signal ───────────────────────────────────────────


async def test_an_open_stream_closes_itself_on_the_shutdown_signal(client, monkeypatch):
    """Fails before the fix: the loop only ever checked a stale, captured-once
    `authenticator` reference and never learned the process was stopping, so
    it would sit on the hub's queue for a full REAUTH_SECONDS (30s) — this test
    bounds the wait far below that, and would time out on the unfixed tree.
    """
    _hold_the_task_open(monkeypatch)
    room_id, agent_id = await _room_with_agent(client)

    svc = routes._svc
    assert svc is not None

    opened_task = (await _call(client, "message/send", _send_params(room_id, agent_id))).json()[
        "result"
    ]

    async def flip_shutdown_once_listening() -> None:
        while await svc.hub.room_subscriber_count(room_id) == 0:
            await asyncio.sleep(0.01)
        routes.set_shutting_down(True)

    flipper = asyncio.create_task(flip_shutdown_once_listening())
    try:
        answer = await asyncio.wait_for(
            _call(client, "tasks/resubscribe", {"id": opened_task["id"]}, call_id="s-shutdown"), 5
        )
    finally:
        await flipper
        routes.set_shutting_down(False)

    events = [line for line in answer.text.splitlines() if line.startswith("data: ")]
    assert len(events) == 2
    assert '"final": true' in events[-1]
    assert "server-shutting-down" in events[-1]


def test_main_passes_a_graceful_shutdown_timeout_to_uvicorn(monkeypatch):
    """Fails before the fix: `uvicorn.run` was called with no
    `timeout_graceful_shutdown` at all, so this kwarg was simply absent.
    """
    captured: dict[str, Any] = {}

    class _FakeUvicorn:
        @staticmethod
        def run(app: Any, **kwargs: Any) -> None:
            captured.update(kwargs)

    import sys as _sys

    monkeypatch.setitem(_sys.modules, "uvicorn", _FakeUvicorn())
    # A path argument of ":memory:" so this never touches a file on disk.
    monkeypatch.setattr(_sys, "argv", ["xyzzy", ":memory:"])
    monkeypatch.setenv("XYZZY_AUTH_TOKENS", '{"t": "u"}')
    monkeypatch.delenv("XYZZY_SHUTDOWN_GRACE_SECONDS", raising=False)

    server_main()
    assert captured.get("timeout_graceful_shutdown") == 10

    monkeypatch.setenv("XYZZY_SHUTDOWN_GRACE_SECONDS", "3")
    captured.clear()
    server_main()
    assert captured.get("timeout_graceful_shutdown") == 3


# ── Finding 70: the resync marker ────────────────────────────────────────────


async def test_a_resync_marker_is_recovered_by_rereading_the_task(client, monkeypatch):
    """Fails before the fix: the loop only checked for `access_revoked` and
    otherwise dropped any other event type (including `resync`) via
    `_status_update` returning None for it, so the stream never emitted
    anything for the marker and never noticed the task might already be over.
    """
    _hold_the_task_open(monkeypatch)
    room_id, agent_id = await _room_with_agent(client)
    opened = (await _call(client, "message/send", _send_params(room_id, agent_id))).json()["result"]

    svc = routes._svc
    assert svc is not None
    real_subscribe = svc.hub.subscribe

    async def subscribe_then_seed_resync(room_id_: str, user_id_: str, *, queue=None):
        sub = await real_subscribe(room_id_, user_id_, queue=queue)
        # Simulates the hub's own overflow handling (hub.py's
        # `_handle_queue_overflow`) without needing 257 real broadcasts to
        # trigger it: the marker is what matters here, not how it arrived.
        await sub.queue.put({"type": "resync"})
        return sub

    monkeypatch.setattr(svc.hub, "subscribe", subscribe_then_seed_resync)

    async def cancel_once_the_stream_is_listening() -> None:
        while await svc.hub.room_subscriber_count(room_id) == 0:
            await asyncio.sleep(0.01)
        await svc.cancel_agent_task(opened["id"], requested_by="user_1")

    canceller = asyncio.create_task(cancel_once_the_stream_is_listening())
    answer = await asyncio.wait_for(
        _call(client, "tasks/resubscribe", {"id": opened["id"]}, call_id="s-resync"), 10
    )
    await canceller

    events = [line for line in answer.text.splitlines() if line.startswith("data: ")]
    # snapshot, the resync-recovered re-read (still submitted, not final),
    # then the real cancellation.
    assert len(events) == 3
    assert '"submitted"' in events[1]
    assert '"final": false' in events[1]
    assert '"canceled"' in events[2]
    assert '"final": true' in events[2]
