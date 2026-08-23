"""Regression: a mention that cannot open a turn says so instead of going quiet.

Members and agents share one handle namespace, which is correct and stays: @finance
has to mean exactly one participant in a room. It also means a person can hold the
handle an agent would otherwise have taken. A user called researcher joins first,
the Researcher agent gets researcher-2, and "@researcher please analyse this" with
invoke_mentioned_agents set opened no run at all and reported nothing: the handle
resolved, so it was not unrecognized, and the author waited on an answer nobody was
writing. The collision is not the bug. The silence was.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.server import create_app

TOKENS = {"owner-token": "owner", "researcher-token": "researcher"}
OWNER = {"Authorization": "Bearer owner-token"}


@pytest.mark.asyncio
async def test_a_mention_that_resolves_to_a_person_cannot_open_a_turn_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    app = create_app(":memory:", auth_tokens=TOKENS)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            room_id = (
                await client.post(
                    "/api/v1/me/bootstrap",
                    headers=OWNER,
                    json={"display_name": "Owner", "room_name": "Decision"},
                )
            ).json()["room"]["room_id"]
            # The person takes @researcher first, so the agent is issued researcher-2.
            assert (
                await client.post(
                    f"/api/v1/rooms/{room_id}/members/invitations",
                    headers=OWNER,
                    json={"user_id": "researcher", "role": "editor"},
                )
            ).status_code == 200
            templates = (await client.get("/api/v1/agent-templates", headers=OWNER)).json()
            researcher = next(t for t in templates if t["name"] == "Researcher")
            assert (
                await client.post(
                    f"/api/v1/rooms/{room_id}/agents",
                    headers=OWNER,
                    json={"template_id": researcher["template_id"]},
                )
            ).status_code == 200
            roster = (await client.get(f"/api/v1/rooms/{room_id}/state", headers=OWNER)).json()
            assert [a["handle"] for a in roster["agents"]] == ["researcher-2"]
            assert "researcher" in [m["handle"] for m in roster["members"]]

            squatted = await client.post(
                f"/api/v1/rooms/{room_id}/messages",
                headers=OWNER,
                json={
                    "content": "@researcher please analyse this",
                    "invoke_mentioned_agents": True,
                },
            )

            assert squatted.status_code == 200, squatted.text
            body = squatted.json()
            assert [m["target_type"] for m in body["mentions"]] == ["USER"]
            assert all(m["invoked_execution_id"] is None for m in body["mentions"])
            # The handle reached somebody, so it was never unrecognized.
            assert body["unrecognized_mentions"] == []
            assert body["uninvocable_mentions"] == ["researcher"]
            started = [
                event
                for event in (
                    await client.get(f"/api/v1/rooms/{room_id}/events", headers=OWNER)
                ).json()
                if event["event_type"] == "agent.run.started"
            ]
            assert started == []

            # And the agent's own handle still opens one, with nothing to report.
            invoked = (
                await client.post(
                    f"/api/v1/rooms/{room_id}/messages",
                    headers=OWNER,
                    json={
                        "content": "@researcher-2 please analyse this",
                        "invoke_mentioned_agents": True,
                    },
                )
            ).json()
            assert [m["target_type"] for m in invoked["mentions"]] == ["AGENT"]
            assert invoked["mentions"][0]["invoked_execution_id"]
            assert invoked["uninvocable_mentions"] == []
