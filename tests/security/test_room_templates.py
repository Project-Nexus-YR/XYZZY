"""A workspace's room recipe: membership to read or create, the creator or a
workspace admin to retire it, and a recipe that names a specialist no longer
spawnable refuses the whole room rather than half of one.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from multiplayer.db.connection import Database
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.server import create_app
from multiplayer.services.service import MultiplayerService

OWNER_HEADERS = {"Authorization": "Bearer owner-token"}
MEMBER_HEADERS = {"Authorization": "Bearer member-token"}
OUTSIDER_HEADERS = {"Authorization": "Bearer outsider-token"}


def _total_agents(client: TestClient, workspace_id: str) -> int:
    rooms = client.get(f"/api/v1/workspaces/{workspace_id}/rooms", headers=OWNER_HEADERS).json()
    return sum(
        len(client.get(f"/api/v1/rooms/{r['room_id']}/agents", headers=OWNER_HEADERS).json())
        for r in rooms
    )


def _seed(client: TestClient) -> dict[str, str]:
    org = client.post(
        "/api/v1/organizations",
        headers=OWNER_HEADERS,
        json={"name": "Acme", "slug": "acme"},
    ).json()
    workspace = client.post(
        f"/api/v1/organizations/{org['org_id']}/workspaces",
        headers=OWNER_HEADERS,
        json={"name": "Main", "slug": "main"},
    ).json()
    room = client.post(
        f"/api/v1/workspaces/{workspace['workspace_id']}/rooms",
        headers=OWNER_HEADERS,
        json={"name": "General"},
    ).json()
    invite = client.post(
        f"/api/v1/rooms/{room['room_id']}/members/invitations",
        headers=OWNER_HEADERS,
        json={"user_id": "user-member", "role": "viewer"},
    )
    assert invite.status_code == 200
    return {"workspace_id": workspace["workspace_id"], "room_id": room["room_id"]}


def _app() -> object:
    return create_app(
        ":memory:",
        auth_tokens={
            "owner-token": "user-owner",
            "member-token": "user-member",
            "outsider-token": "user-outsider",
        },
    )


def _builtin_id(client: TestClient, workspace_id: str) -> str:
    templates = client.get(
        f"/api/v1/workspaces/{workspace_id}/agent-templates", headers=OWNER_HEADERS
    ).json()
    return next(t["template_id"] for t in templates if t["builtin"])


def test_a_workspace_member_creates_and_lists_a_room_template() -> None:
    with TestClient(_app()) as client:
        seeded = _seed(client)
        workspace_id = seeded["workspace_id"]
        builtin_id = _builtin_id(client, workspace_id)

        created = client.post(
            f"/api/v1/workspaces/{workspace_id}/room-templates",
            headers=OWNER_HEADERS,
            json={
                "name": "Standup Room",
                "description": "Daily sync",
                "agent_template_ids": [builtin_id],
            },
        )
        assert created.status_code == 200
        body = created.json()
        assert body["name"] == "Standup Room"
        assert body["agent_template_ids"] == [builtin_id]
        assert body["created_by"] == "user-owner"

        listing = client.get(
            f"/api/v1/workspaces/{workspace_id}/room-templates", headers=MEMBER_HEADERS
        ).json()
        assert any(t["template_id"] == body["template_id"] for t in listing)


def test_creation_refuses_a_duplicate_name_case_insensitively() -> None:
    with TestClient(_app()) as client:
        seeded = _seed(client)
        workspace_id = seeded["workspace_id"]
        client.post(
            f"/api/v1/workspaces/{workspace_id}/room-templates",
            headers=OWNER_HEADERS,
            json={"name": "Recipe", "agent_template_ids": []},
        )
        dup = client.post(
            f"/api/v1/workspaces/{workspace_id}/room-templates",
            headers=OWNER_HEADERS,
            json={"name": "RECIPE", "agent_template_ids": []},
        )
        assert dup.status_code == 400


def test_a_non_member_cannot_create_a_room_template() -> None:
    with TestClient(_app()) as client:
        seeded = _seed(client)
        workspace_id = seeded["workspace_id"]
        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/room-templates",
            headers=OUTSIDER_HEADERS,
            json={"name": "Recipe", "agent_template_ids": []},
        )
        assert response.status_code == 403


def test_only_the_creator_or_a_workspace_admin_may_delete_a_room_template() -> None:
    with TestClient(_app()) as client:
        seeded = _seed(client)
        workspace_id, room_id = seeded["workspace_id"], seeded["room_id"]
        created = client.post(
            f"/api/v1/workspaces/{workspace_id}/room-templates",
            headers=MEMBER_HEADERS,
            json={"name": "Member's Recipe", "agent_template_ids": []},
        ).json()
        template_id = created["template_id"]

        other_member = client.post(
            f"/api/v1/rooms/{room_id}/members/invitations",
            headers=OWNER_HEADERS,
            json={"user_id": "user-outsider", "role": "viewer"},
        )
        assert other_member.status_code == 200
        denied = client.delete(
            f"/api/v1/workspaces/{workspace_id}/room-templates/{template_id}",
            headers=OUTSIDER_HEADERS,
        )
        assert denied.status_code == 403

        deleted = client.delete(
            f"/api/v1/workspaces/{workspace_id}/room-templates/{template_id}",
            headers=OWNER_HEADERS,
        )
        assert deleted.status_code == 200
        listing = client.get(
            f"/api/v1/workspaces/{workspace_id}/room-templates", headers=OWNER_HEADERS
        ).json()
        assert all(t["template_id"] != template_id for t in listing)


def test_room_creation_from_a_template_spawns_the_preselected_specialists_and_records_it() -> None:
    with TestClient(_app()) as client:
        seeded = _seed(client)
        workspace_id = seeded["workspace_id"]
        builtin_id = _builtin_id(client, workspace_id)

        recipe = client.post(
            f"/api/v1/workspaces/{workspace_id}/room-templates",
            headers=OWNER_HEADERS,
            json={"name": "Standup", "agent_template_ids": [builtin_id]},
        ).json()

        created = client.post(
            f"/api/v1/workspaces/{workspace_id}/rooms",
            headers=OWNER_HEADERS,
            json={"name": "Daily", "room_template_id": recipe["template_id"]},
        )
        assert created.status_code == 200
        room_id = created.json()["room_id"]

        agents = client.get(f"/api/v1/rooms/{room_id}/agents", headers=OWNER_HEADERS).json()
        assert any(a["role"] for a in agents) and len(agents) == 1

        events = client.get(f"/api/v1/rooms/{room_id}/events", headers=OWNER_HEADERS).json()
        room_created = next(e for e in events if e["event_type"] == "room.created")
        assert room_created["payload"]["room_template_id"] == recipe["template_id"]


def test_room_creation_refuses_a_room_template_from_another_workspace() -> None:
    with TestClient(_app()) as client:
        seeded = _seed(client)
        workspace_id = seeded["workspace_id"]

        other_org = client.post(
            "/api/v1/organizations", headers=OWNER_HEADERS, json={"name": "Other", "slug": "other"}
        ).json()
        other_ws = client.post(
            f"/api/v1/organizations/{other_org['org_id']}/workspaces",
            headers=OWNER_HEADERS,
            json={"name": "Other Ws", "slug": "other-ws"},
        ).json()
        foreign_recipe = client.post(
            f"/api/v1/workspaces/{other_ws['workspace_id']}/room-templates",
            headers=OWNER_HEADERS,
            json={"name": "Foreign", "agent_template_ids": []},
        ).json()

        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/rooms",
            headers=OWNER_HEADERS,
            json={"name": "Cross", "room_template_id": foreign_recipe["template_id"]},
        )
        assert response.status_code == 400


def test_room_creation_refuses_whole_when_a_preselected_specialist_was_deleted() -> None:
    with TestClient(_app()) as client:
        seeded = _seed(client)
        workspace_id = seeded["workspace_id"]
        builtin_id = _builtin_id(client, workspace_id)

        custom = client.post(
            f"/api/v1/workspaces/{workspace_id}/agent-templates",
            headers=OWNER_HEADERS,
            json={"name": "Doomed Specialist", "role": "writer", "system_prompt": "p"},
        ).json()
        # A live specialist named FIRST, and the doomed one SECOND: if the room
        # were created and specialists spawned one at a time rather than all
        # inside the room's own transaction, the live one would already be a
        # committed agent by the time the doomed one is found missing.
        recipe = client.post(
            f"/api/v1/workspaces/{workspace_id}/room-templates",
            headers=OWNER_HEADERS,
            json={
                "name": "Recipe With Doomed",
                "agent_template_ids": [builtin_id, custom["template_id"]],
            },
        ).json()

        client.delete(
            f"/api/v1/workspaces/{workspace_id}/agent-templates/{custom['template_id']}",
            headers=OWNER_HEADERS,
        )

        before = client.get(
            f"/api/v1/workspaces/{workspace_id}/rooms", headers=OWNER_HEADERS
        ).json()
        agents_before = _total_agents(client, workspace_id)
        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/rooms",
            headers=OWNER_HEADERS,
            json={"name": "Should Not Exist", "room_template_id": recipe["template_id"]},
        )
        assert response.status_code == 400
        assert custom["template_id"] in response.json()["detail"]
        after = client.get(f"/api/v1/workspaces/{workspace_id}/rooms", headers=OWNER_HEADERS).json()
        assert len(after) == len(before)
        # Not half a room: the live specialist named ahead of the doomed one in
        # the recipe was never spawned either.
        assert _total_agents(client, workspace_id) == agents_before


def test_a_room_already_created_keeps_working_when_its_template_is_later_deleted() -> None:
    with TestClient(_app()) as client:
        seeded = _seed(client)
        workspace_id = seeded["workspace_id"]
        builtin_id = _builtin_id(client, workspace_id)

        recipe = client.post(
            f"/api/v1/workspaces/{workspace_id}/room-templates",
            headers=OWNER_HEADERS,
            json={"name": "Ephemeral Recipe", "agent_template_ids": [builtin_id]},
        ).json()
        room = client.post(
            f"/api/v1/workspaces/{workspace_id}/rooms",
            headers=OWNER_HEADERS,
            json={"name": "Survivor", "room_template_id": recipe["template_id"]},
        ).json()
        room_id = room["room_id"]

        deleted = client.delete(
            f"/api/v1/workspaces/{workspace_id}/room-templates/{recipe['template_id']}",
            headers=OWNER_HEADERS,
        )
        assert deleted.status_code == 200

        # Exercise the room, not just its row: invite a member and post a message.
        invite = client.post(
            f"/api/v1/rooms/{room_id}/members/invitations",
            headers=OWNER_HEADERS,
            json={"user_id": "user-member", "role": "editor"},
        )
        assert invite.status_code == 200
        message = client.post(
            f"/api/v1/rooms/{room_id}/messages",
            headers=OWNER_HEADERS,
            json={"content": "still alive"},
        )
        assert message.status_code == 200
        agents = client.get(f"/api/v1/rooms/{room_id}/agents", headers=OWNER_HEADERS).json()
        assert len(agents) == 1


async def test_a_forced_failure_partway_through_spawning_leaves_nothing_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The room row and every preselected specialist commit or roll back together.

    Forces the second specialist's write to raise, in the style of
    test_audit_export.py's truncated-page monkeypatch: wrap the real repo
    method and inject a failure on a specific call rather than faking the
    whole write path. If room creation and the spawns were separate
    transactions, the room and the first specialist would already be
    committed by the time this failure lands.
    """
    import multiplayer.db.repositories as repositories_module

    db = Database(":memory:")
    await db.connect()
    try:
        svc = MultiplayerService(db, RealtimeHub())
        await svc.initialize()
        org = await svc.create_organization("Acme", "acme", "owner")
        workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
        first = await svc.create_agent_template(
            workspace.workspace_id, "First", "writer", "p", "owner"
        )
        second = await svc.create_agent_template(
            workspace.workspace_id, "Second", "writer", "p", "owner"
        )
        recipe = await svc.create_room_template(
            workspace.workspace_id,
            "Two Specialists",
            "",
            [first.template_id, second.template_id],
            "owner",
        )

        real_create_instance = repositories_module.AgentRepo.create_instance
        calls = {"n": 0}

        async def flaky_create_instance(self: object, agent: object) -> object:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("forced failure partway through spawning")
            return await real_create_instance(self, agent)  # type: ignore[arg-type]

        monkeypatch.setattr(repositories_module.AgentRepo, "create_instance", flaky_create_instance)

        with pytest.raises(RuntimeError, match="forced failure partway through spawning"):
            await svc.create_room(
                workspace.workspace_id,
                "Should Roll Back",
                "owner",
                room_template_id=recipe.template_id,
            )

        rooms = await svc.repos.rooms.list_by_workspace(workspace.workspace_id)
        assert rooms == []
        agent_row_count = await db.fetch_one("SELECT COUNT(*) AS c FROM agent_instances")
        assert agent_row_count is not None and agent_row_count["c"] == 0
        room_created_events = await db.fetch_all(
            "SELECT * FROM room_events WHERE event_type = 'room.created'"
        )
        assert room_created_events == []
    finally:
        await db.close()
