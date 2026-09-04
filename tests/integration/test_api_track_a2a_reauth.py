"""Findings 18 and 20: the A2A SSE stream's periodic recheck.

Finding 18: the recheck re-validated the credential but never re-checked room
membership, so a lost cross-process revoke (a missed Redis publish, or any
path that skips `hub.revoke_room_access`) left a removed member subscribed to
one task's status transitions forever. The fix mirrors websocket.py's own
reauth beat: a membership check on the same tick, closing the stream with
``access-revoked`` on an `AuthorizationError`.

Finding 20: the recheck re-read the Authorization header instead of the
credential `_current_user` actually resolved, so a cookie-authenticated
browser client's stream failed closed every 30 seconds even though its
credential (a cookie, never a header) stayed perfectly valid. The fix has
`_current_user` stash the effective ``Bearer ...`` string on `request.state`
for `_stream` to reuse.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

import multiplayer.api.a2a as a2a_module
from multiplayer.api import routes
from multiplayer.server import create_app
from multiplayer.services.service import MultiplayerService

from ..security.test_sso_session_lifecycle import FakeProvider
from .test_cookie_auth import TOKENS, _configured, _sign_in_by_cookie

AUTH = {"Authorization": "Bearer owner-token"}


def _hold_the_task_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        MultiplayerService, "dispatch_agent_task_in_background", lambda self, task: None
    )


async def _room_with_agent(client: AsyncClient, *, headers: dict[str, str]) -> tuple[str, str]:
    org = (
        await client.post(
            "/api/v1/organizations", json={"name": "Acme", "slug": "acme"}, headers=headers
        )
    ).json()
    workspace = (
        await client.post(
            f"/api/v1/organizations/{org['org_id']}/workspaces",
            json={"name": "Main", "slug": "main"},
            headers=headers,
        )
    ).json()
    room = (
        await client.post(
            f"/api/v1/workspaces/{workspace['workspace_id']}/rooms",
            json={"name": "Auth Migration"},
            headers=headers,
        )
    ).json()
    await client.post(f"/api/v1/rooms/{room['room_id']}/join", headers=headers)
    templates = (await client.get("/api/v1/agent-templates", headers=headers)).json()
    agent = (
        await client.post(
            f"/api/v1/rooms/{room['room_id']}/agents",
            json={"template_id": templates[0]["template_id"], "name": "Forge"},
            headers=headers,
        )
    ).json()
    return room["room_id"], agent["agent_id"]


def _send_params(room_id: str, agent_id: str) -> dict[str, Any]:
    return {
        "message": {
            "kind": "message",
            "messageId": "msg-1",
            "role": "user",
            "parts": [{"kind": "text", "text": "port the auth service"}],
            "metadata": {"roomId": room_id, "targetAgentId": agent_id},
        }
    }


async def _call(
    client: AsyncClient,
    method: str,
    params: dict[str, Any] | None,
    call_id: Any,
    headers: dict[str, str],
) -> Any:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": call_id, "method": method}
    if params is not None:
        body["params"] = params
    return await client.post("/a2a/v1", json=body, headers=headers)


@pytest.mark.asyncio
async def test_a_lost_revoke_ends_the_stream_on_the_next_reauth_beat(monkeypatch):
    monkeypatch.setattr(a2a_module, "REAUTH_SECONDS", 0.05)
    _hold_the_task_open(monkeypatch)
    app = create_app(":memory:", auth_tokens=TOKENS)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            room_id, agent_id = await _room_with_agent(client, headers=AUTH)
            opened = (
                await _call(
                    client, "message/send", _send_params(room_id, agent_id), 1, headers=AUTH
                )
            ).json()["result"]

            svc = routes._svc
            assert svc is not None

            async def revoke_without_telling_the_hub() -> None:
                # Exactly the gap the finding names: membership disappears
                # through a path that never calls `hub.revoke_room_access`.
                while await svc.hub.room_subscriber_count(room_id) == 0:
                    await asyncio.sleep(0.01)
                await svc.repos.room_members.remove(room_id, "user_1")

            revoker = asyncio.create_task(revoke_without_telling_the_hub())
            answer = await asyncio.wait_for(
                _call(client, "tasks/resubscribe", {"id": opened["id"]}, "s-1", headers=AUTH),
                10,
            )
            await revoker

    events = [line for line in answer.text.splitlines() if line.startswith("data: ")]
    assert '"closedBecause": "access-revoked"' in events[-1]
    assert '"final": true' in events[-1]


@pytest.mark.asyncio
async def test_a_cookie_authenticated_stream_survives_the_reauth_beat(monkeypatch):
    monkeypatch.setattr(a2a_module, "REAUTH_SECONDS", 0.05)
    _hold_the_task_open(monkeypatch)
    app = create_app(":memory:", auth_tokens=TOKENS)
    idp = FakeProvider()
    async with app.router.lifespan_context(app):
        routes.set_sessions(_configured(idp, https=False, host="test"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            session_cookie = await _sign_in_by_cookie(client, idp)
            cookie_headers = {
                "Cookie": f"xyzzy_session={session_cookie}",
                "X-XYZZY-Client": "web",
            }
            room_id, agent_id = await _room_with_agent(client, headers=cookie_headers)
            opened = (
                await _call(
                    client,
                    "message/send",
                    _send_params(room_id, agent_id),
                    1,
                    headers=cookie_headers,
                )
            ).json()["result"]

            svc = routes._svc
            assert svc is not None

            async def cancel_once_the_stream_is_listening() -> None:
                # Long enough to outlast several reauth beats (0.05s each)
                # before the task itself ends the stream.
                for _ in range(20):
                    if await svc.hub.room_subscriber_count(room_id) > 0:
                        break
                    await asyncio.sleep(0.01)
                await asyncio.sleep(0.3)
                await _call(
                    client, "tasks/cancel", {"id": opened["id"]}, "c-1", headers=cookie_headers
                )

            canceller = asyncio.create_task(cancel_once_the_stream_is_listening())
            answer = await asyncio.wait_for(
                _call(
                    client,
                    "tasks/resubscribe",
                    {"id": opened["id"]},
                    "s-2",
                    headers=cookie_headers,
                ),
                10,
            )
            await canceller

    events = [line for line in answer.text.splitlines() if line.startswith("data: ")]
    assert not any('"closedBecause": "credential-ended"' in event for event in events)
    assert '"final": true' in events[-1]
