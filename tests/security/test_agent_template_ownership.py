"""A workspace can write its own specialist, and the write is bounded the same
way every other workspace-tier write is: membership to read or create, the
creator or a workspace admin to retire it, and never a template from a
workspace the room does not belong to.
"""

from fastapi.testclient import TestClient

from multiplayer.server import create_app

OWNER_HEADERS = {"Authorization": "Bearer owner-token"}
MEMBER_HEADERS = {"Authorization": "Bearer member-token"}
OUTSIDER_HEADERS = {"Authorization": "Bearer outsider-token"}


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
    # Inviting a non-admin into the room grants them workspace membership too
    # (mirrors bootstrap), which is how a plain workspace member is seeded here.
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


def test_a_workspace_member_creates_a_custom_template_the_workspace_can_list() -> None:
    with TestClient(_app()) as client:
        seeded = _seed(client)
        workspace_id = seeded["workspace_id"]

        created = client.post(
            f"/api/v1/workspaces/{workspace_id}/agent-templates",
            headers=OWNER_HEADERS,
            json={
                "name": "Release Notes Writer",
                "role": "writer",
                "system_prompt": "Draft notes.",
            },
        )
        assert created.status_code == 200
        body = created.json()
        assert body["builtin"] is False
        assert body["created_by"] == "user-owner"

        listing = client.get(
            f"/api/v1/workspaces/{workspace_id}/agent-templates", headers=MEMBER_HEADERS
        ).json()
        names = {t["name"]: t for t in listing}
        assert "Release Notes Writer" in names
        assert names["Release Notes Writer"]["builtin"] is False
        assert any(t["builtin"] is True for t in listing)


def test_creation_refuses_a_duplicate_name_case_insensitively() -> None:
    with TestClient(_app()) as client:
        seeded = _seed(client)
        workspace_id = seeded["workspace_id"]
        client.post(
            f"/api/v1/workspaces/{workspace_id}/agent-templates",
            headers=OWNER_HEADERS,
            json={"name": "Scribe", "role": "writer", "system_prompt": "p"},
        )
        dup = client.post(
            f"/api/v1/workspaces/{workspace_id}/agent-templates",
            headers=OWNER_HEADERS,
            json={"name": "SCRIBE", "role": "writer", "system_prompt": "p"},
        )
        assert dup.status_code == 400


def test_a_non_member_cannot_create_a_workspace_template() -> None:
    with TestClient(_app()) as client:
        seeded = _seed(client)
        workspace_id = seeded["workspace_id"]
        response = client.post(
            f"/api/v1/workspaces/{workspace_id}/agent-templates",
            headers=OUTSIDER_HEADERS,
            json={"name": "Scribe", "role": "writer", "system_prompt": "p"},
        )
        assert response.status_code == 403


def test_built_in_templates_refuse_deletion() -> None:
    with TestClient(_app()) as client:
        seeded = _seed(client)
        workspace_id = seeded["workspace_id"]
        builtins = client.get(
            f"/api/v1/workspaces/{workspace_id}/agent-templates", headers=OWNER_HEADERS
        ).json()
        builtin_id = next(t["template_id"] for t in builtins if t["builtin"])
        response = client.delete(
            f"/api/v1/workspaces/{workspace_id}/agent-templates/{builtin_id}", headers=OWNER_HEADERS
        )
        assert response.status_code == 400


def test_only_the_creator_or_a_workspace_admin_may_delete_a_custom_template() -> None:
    with TestClient(_app()) as client:
        seeded = _seed(client)
        workspace_id = seeded["workspace_id"]
        created = client.post(
            f"/api/v1/workspaces/{workspace_id}/agent-templates",
            headers=MEMBER_HEADERS,
            json={"name": "Member's Own", "role": "writer", "system_prompt": "p"},
        ).json()
        template_id = created["template_id"]

        # Not the creator and not an admin.
        other_member = client.post(
            f"/api/v1/rooms/{seeded['room_id']}/members/invitations",
            headers=OWNER_HEADERS,
            json={"user_id": "user-outsider", "role": "viewer"},
        )
        assert other_member.status_code == 200
        denied = client.delete(
            f"/api/v1/workspaces/{workspace_id}/agent-templates/{template_id}",
            headers=OUTSIDER_HEADERS,
        )
        assert denied.status_code == 403

        # The workspace admin (the owner) may still delete a member's template.
        deleted = client.delete(
            f"/api/v1/workspaces/{workspace_id}/agent-templates/{template_id}",
            headers=OWNER_HEADERS,
        )
        assert deleted.status_code == 200

        listing = client.get(
            f"/api/v1/workspaces/{workspace_id}/agent-templates", headers=OWNER_HEADERS
        ).json()
        assert all(t["template_id"] != template_id for t in listing)


def test_an_agent_already_spawned_from_a_deleted_template_keeps_working() -> None:
    with TestClient(_app()) as client:
        seeded = _seed(client)
        workspace_id, room_id = seeded["workspace_id"], seeded["room_id"]
        created = client.post(
            f"/api/v1/workspaces/{workspace_id}/agent-templates",
            headers=OWNER_HEADERS,
            json={"name": "Doomed", "role": "writer", "system_prompt": "p"},
        ).json()
        template_id = created["template_id"]

        spawned = client.post(
            f"/api/v1/rooms/{room_id}/agents",
            headers=OWNER_HEADERS,
            json={"template_id": template_id, "name": "Doomed Instance"},
        )
        assert spawned.status_code == 200
        agent_id = spawned.json()["agent_id"]

        deleted = client.delete(
            f"/api/v1/workspaces/{workspace_id}/agent-templates/{template_id}",
            headers=OWNER_HEADERS,
        )
        assert deleted.status_code == 200

        # "Keeps working" means it can still run a turn, not just still be listed:
        # drive it through the real session/execution/step path. No provider key
        # is configured, so this completes offline against the SIMULATED provider.
        session = client.post(
            f"/api/v1/rooms/{room_id}/agents/{agent_id}/sessions", headers=OWNER_HEADERS
        )
        assert session.status_code == 200
        execution = client.post(
            f"/api/v1/sessions/{session.json()['session_id']}/execute", headers=OWNER_HEADERS
        )
        assert execution.status_code == 200
        step = client.post(
            f"/api/v1/executions/{execution.json()['execution_id']}/step",
            headers=OWNER_HEADERS,
            json={"prompt": "Assess the deleted-template regression"},
        )
        assert step.status_code == 200
        output_id = step.json()["output_id"]
        outputs = client.get(f"/api/v1/rooms/{room_id}/outputs", headers=OWNER_HEADERS).json()
        output = next(o for o in outputs if o["output_id"] == output_id)
        assert output["agent_id"] == agent_id
        assert output["content"].startswith("SIMULATED WORKFLOW OUTPUT")
        assert output["source_prompt"] == "Assess the deleted-template regression"

        agents = client.get(f"/api/v1/rooms/{room_id}/agents", headers=OWNER_HEADERS).json()
        assert any(a["agent_id"] == agent_id for a in agents)

        respawn = client.post(
            f"/api/v1/rooms/{room_id}/agents",
            headers=OWNER_HEADERS,
            json={"template_id": template_id},
        )
        assert respawn.status_code == 400


def test_spawning_refuses_a_template_from_a_different_workspace() -> None:
    with TestClient(_app()) as client:
        seeded = _seed(client)
        owner_room_id = seeded["room_id"]

        other_org = client.post(
            "/api/v1/organizations", headers=OWNER_HEADERS, json={"name": "Other", "slug": "other"}
        ).json()
        other_ws = client.post(
            f"/api/v1/organizations/{other_org['org_id']}/workspaces",
            headers=OWNER_HEADERS,
            json={"name": "Other Ws", "slug": "other-ws"},
        ).json()
        foreign_template = client.post(
            f"/api/v1/workspaces/{other_ws['workspace_id']}/agent-templates",
            headers=OWNER_HEADERS,
            json={"name": "Foreign", "role": "writer", "system_prompt": "p"},
        ).json()

        response = client.post(
            f"/api/v1/rooms/{owner_room_id}/agents",
            headers=OWNER_HEADERS,
            json={"template_id": foreign_template["template_id"]},
        )
        assert response.status_code == 400
        assert "different workspace" in response.json()["detail"]
