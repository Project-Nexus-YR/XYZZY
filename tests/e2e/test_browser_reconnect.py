"""Browser reload contract: discover authorized durable state before setup writes."""

import asyncio
import hashlib
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

import multiplayer.api.routes as routes_module
from multiplayer.server import create_app

from ._client_source import client_source_text

OWNER = {"Authorization": "Bearer owner-token"}
COLLABORATOR = {"Authorization": "Bearer collaborator-token"}
OUTSIDER = {"Authorization": "Bearer outsider-token"}


def _enter_workspace(
    client: TestClient,
    headers: dict[str, str],
    room_name: str,
    stored_room_id: str = "",
) -> str:
    """Mirror the browser's discovery-first entry without browser storage state."""
    context = client.get("/api/v1/me/context", headers=headers)
    assert context.status_code == 200
    discovered = context.json()
    named_rooms = [room for room in discovered["rooms"] if room["name"] == room_name]
    room = next(
        (room for room in discovered["rooms"] if room["room_id"] == stored_room_id),
        named_rooms[0] if named_rooms else None,
    )
    if room is not None:
        return str(room["room_id"])

    bootstrap = client.post(
        "/api/v1/me/bootstrap",
        headers=headers,
        json={"display_name": "Owner", "room_name": room_name},
    )
    assert bootstrap.status_code == 200, bootstrap.text
    return str(bootstrap.json()["room"]["room_id"])


def _create_output(client: TestClient, room_id: str, template_id: str, prompt: str) -> str:
    agent = client.post(
        f"/api/v1/rooms/{room_id}/agents",
        headers=OWNER,
        json={"template_id": template_id},
    ).json()
    session = client.post(
        f"/api/v1/rooms/{room_id}/agents/{agent['agent_id']}/sessions",
        headers=OWNER,
    ).json()
    execution = client.post(
        f"/api/v1/sessions/{session['session_id']}/execute",
        headers=OWNER,
    ).json()
    response = client.post(
        f"/api/v1/executions/{execution['execution_id']}/step",
        headers=OWNER,
        json={"prompt": prompt},
    )
    assert response.status_code == 200
    return str(response.json()["output_id"])


def _event_signature(state: dict[str, Any]) -> list[tuple[str, int, str]]:
    return [
        (event["event_id"], event["sequence"], event["event_type"])
        for event in state["events_since"]
    ]


def test_reload_stale_storage_and_second_browser_restore_without_duplicate_writes() -> None:
    app = create_app(
        ":memory:",
        auth_tokens={
            "owner-token": "owner",
            "collaborator-token": "collaborator",
            "outsider-token": "outsider",
        },
    )
    with TestClient(app) as client:
        room_name = "Authentication Migration"
        room_id = _enter_workspace(client, OWNER, room_name)

        invite = client.post(
            f"/api/v1/rooms/{room_id}/members/invitations",
            headers=OWNER,
            json={"user_id": "collaborator", "role": "editor"},
        )
        assert invite.status_code == 200

        templates = client.get("/api/v1/agent-templates", headers=OWNER).json()
        output_ids = [
            _create_output(client, room_id, template["template_id"], prompt)
            for template, prompt in zip(
                templates[:3],
                ("architecture evidence", "security evidence", "delivery evidence"),
                strict=True,
            )
        ]
        for output_id, disposition in zip(
            output_ids,
            ("INCLUDED", "INCLUDED", "EXCLUDED"),
            strict=True,
        ):
            selection = client.put(
                f"/api/v1/rooms/{room_id}/output-selections/{output_id}",
                headers=OWNER,
                json={"disposition": disposition},
            )
            assert selection.status_code == 200
        synthesis = client.post(
            f"/api/v1/rooms/{room_id}/syntheses/decision-brief",
            headers=OWNER,
            json={"title": "Authentication migration decision"},
        )
        assert synthesis.status_code == 200

        context_before = client.get("/api/v1/me/context", headers=OWNER).json()
        state_before = client.get(
            f"/api/v1/rooms/{room_id}/state?last_sequence=0",
            headers=OWNER,
        ).json()
        events_before = _event_signature(state_before)
        assert len(events_before) == len(set(events_before))

        # Same browser reload: the persisted, non-secret room ID is authorized
        # through discovery and setup performs no mutations.
        assert _enter_workspace(client, OWNER, room_name, room_id) == room_id
        assert _enter_workspace(client, OWNER, "Changed setup default", room_id) == room_id

        # Stale browser storage is ignored because it does not appear in the
        # principal-scoped discovery response.
        assert _enter_workspace(client, OWNER, room_name, "room_stale") == room_id

        # A second browser has no storage at all and resolves the same room by
        # its authorized membership and exact name.
        assert _enter_workspace(client, OWNER, room_name) == room_id

        # An invited collaborator can discover the room directly. They are not
        # silently granted org membership, but they are granted membership in the
        # room's own workspace (S1) - otherwise every workspace-scoped route (e.g.
        # creating a channel) 403s despite the room being visible.
        collaborator_context = client.get("/api/v1/me/context", headers=COLLABORATOR).json()
        assert collaborator_context["organizations"] == []
        assert [w["workspace_id"] for w in collaborator_context["workspaces"]] == [
            state_before["room"]["workspace_id"]
        ]
        assert [room["room_id"] for room in collaborator_context["rooms"]] == [room_id]
        assert _enter_workspace(client, COLLABORATOR, room_name) == room_id

        context_after = client.get("/api/v1/me/context", headers=OWNER).json()
        state_after = client.get(
            f"/api/v1/rooms/{room_id}/state?last_sequence=0",
            headers=OWNER,
        ).json()
        assert context_after == context_before
        assert _event_signature(state_after) == events_before
        assert len(state_after["outputs"]) == 3
        assert sorted(
            selection["disposition"] for selection in state_after["output_selections"]
        ) == ["EXCLUDED", "INCLUDED", "INCLUDED"]
        assert len(state_after["artifacts"]) == 1

        # Discovery is deny-by-default and never reveals another user's context.
        assert client.get("/api/v1/me/context").status_code == 401
        assert client.get("/api/v1/me/context", headers=OUTSIDER).json() == {
            "user_id": "outsider",
            "organizations": [],
            "workspaces": [],
            "rooms": [],
        }


def test_web_setup_discovers_before_writes_and_persists_no_credential() -> None:
    html = client_source_text()

    discovery = html.index("const context = await api('GET', '/me/context')")
    bootstrap_write = html.index("const bootstrap = await api('POST', '/me/bootstrap'")
    assert discovery < bootstrap_write
    assert "api('POST', '/organizations'" not in html
    assert "api('POST', `/organizations/" not in html
    assert "api('POST', `/workspaces/" not in html
    assert "localStorage.setItem(ACTIVE_ROOM_STORAGE_KEY, value)" in html
    assert "localStorage.setItem" in html
    assert "localStorage.setItem('accessToken'" not in html
    assert 'localStorage.setItem("accessToken"' not in html
    assert "access_token=" not in html
    assert "`bearer.${encodedToken}`" in html
    assert 'id="setup-token" type="password"' in html
    assert 'autocomplete="current-password"' not in html
    assert "snapshot.events_since.forEach(event => logEvent(event))" in html


@pytest.mark.asyncio
async def test_two_cold_browser_flows_share_one_atomic_bootstrap() -> None:
    app = create_app(
        ":memory:",
        auth_tokens={"owner-token": "owner", "attacker-token": "attacker"},
    )
    headers = {"Authorization": "Bearer owner-token"}
    attacker_headers = {"Authorization": "Bearer attacker-token"}
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers=headers,
        ) as client:
            # The globally writable legacy namespace is already occupied. It is
            # irrelevant because bootstrap identity is keyed by the principal in
            # a dedicated mapping, not by a predictable organization slug.
            old_owner_key = hashlib.sha256(b"owner").hexdigest()[:24]
            reserved = await client.post(
                "/api/v1/organizations",
                headers=attacker_headers,
                json={"name": "Forged bootstrap", "slug": "bootstrap-forged"},
            )
            assert reserved.status_code == 400
            assert reserved.json()["detail"] == "organization slug namespace is reserved"
            occupied = await client.post(
                "/api/v1/organizations",
                headers=attacker_headers,
                json={"name": "Occupied", "slug": f"personal-{old_owner_key}"},
            )
            assert occupied.status_code == 200

            both_observed_empty = asyncio.Barrier(2)

            async def cold_browser_flow() -> dict[str, Any]:
                context = await client.get("/api/v1/me/context")
                assert context.status_code == 200
                assert context.json()["organizations"] == []
                assert context.json()["workspaces"] == []
                assert context.json()["rooms"] == []
                await both_observed_empty.wait()
                response = await client.post(
                    "/api/v1/me/bootstrap",
                    json={
                        "display_name": "Owner",
                        "room_name": "Authentication Migration",
                    },
                )
                assert response.status_code == 200, response.text
                payload: dict[str, Any] = response.json()
                return payload

            first, second = await asyncio.gather(cold_browser_flow(), cold_browser_flow())

            assert first == second
            stable_retry = await client.post(
                "/api/v1/me/bootstrap",
                json={
                    "display_name": "Changed display name",
                    "room_name": "Changed room name",
                },
            )
            assert stable_retry.status_code == 200
            assert stable_retry.json() == first

            context = (await client.get("/api/v1/me/context")).json()
            assert len(context["organizations"]) == 1
            assert len(context["workspaces"]) == 1
            assert len(context["rooms"]) == 1
            assert context["organizations"][0]["org_id"] == first["organization"]["org_id"]
            assert context["workspaces"][0]["workspace_id"] == first["workspace"]["workspace_id"]
            assert context["rooms"][0]["room_id"] == first["room"]["room_id"]
            assert first["organization"]["slug"] != f"personal-{old_owner_key}"
            assert old_owner_key not in first["organization"]["slug"]

            attacker_context = (
                await client.get("/api/v1/me/context", headers=attacker_headers)
            ).json()
            assert [org["org_id"] for org in attacker_context["organizations"]] == [
                occupied.json()["org_id"]
            ]
            attacker_access = await client.get(
                f"/api/v1/organizations/{first['organization']['org_id']}/workspaces",
                headers=attacker_headers,
            )
            assert attacker_access.status_code == 403

            room_id = first["room"]["room_id"]
            state = (await client.get(f"/api/v1/rooms/{room_id}/state")).json()
            room_created_events = [
                event for event in state["events_since"] if event["event_type"] == "room.created"
            ]
            assert len(room_created_events) == 1
            assert [event["sequence"] for event in state["events_since"]] == [1]

            svc = routes_module._svc
            assert svc is not None
            assert await svc.repos.orgs.list_members(first["organization"]["org_id"]) == [
                await svc.repos.orgs.get_member(first["organization"]["org_id"], "owner")
            ]
            membership_rows = await svc.db.fetch_all(
                "SELECT 'org' AS scope, user_id, role FROM organization_members "
                "WHERE org_id = ? UNION ALL "
                "SELECT 'workspace', user_id, role FROM workspace_members "
                "WHERE workspace_id = ? UNION ALL "
                "SELECT 'room', user_id, role FROM room_members WHERE room_id = ?",
                (
                    first["organization"]["org_id"],
                    first["workspace"]["workspace_id"],
                    first["room"]["room_id"],
                ),
            )
            assert membership_rows == [
                {"scope": "org", "user_id": "owner", "role": "admin"},
                {"scope": "workspace", "user_id": "owner", "role": "admin"},
                {"scope": "room", "user_id": "owner", "role": "admin"},
            ]
            mappings = await svc.db.fetch_all("SELECT * FROM user_bootstrap_contexts")
            assert len(mappings) == 1
