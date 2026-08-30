"""S4: create_room refuses a name that case-insensitively duplicates an existing,

non-archived room's name in the same workspace, so the sidebar never shows two
identical entries. The same name in a different workspace is unaffected.
"""

from fastapi.testclient import TestClient

from multiplayer.server import create_app

TOKENS = {"owner-token": "owner"}
OWNER = {"Authorization": "Bearer owner-token"}


def test_duplicate_channel_name_refused_case_insensitively_per_workspace() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        bootstrap = client.post(
            "/api/v1/me/bootstrap",
            headers=OWNER,
            json={"display_name": "Owner", "room_name": "General"},
        ).json()
        workspace_id = bootstrap["room"]["workspace_id"]

        first = client.post(
            f"/api/v1/workspaces/{workspace_id}/rooms",
            headers=OWNER,
            json={"name": "Engineering"},
        )
        assert first.status_code == 200, first.text

        duplicate = client.post(
            f"/api/v1/workspaces/{workspace_id}/rooms",
            headers=OWNER,
            json={"name": "Engineering"},
        )
        assert duplicate.status_code == 400, duplicate.text
        assert "a channel with that name already exists" in duplicate.json()["detail"]

        different_case = client.post(
            f"/api/v1/workspaces/{workspace_id}/rooms",
            headers=OWNER,
            json={"name": "ENGINEERING"},
        )
        assert different_case.status_code == 400, different_case.text

        rooms = client.get(f"/api/v1/workspaces/{workspace_id}/rooms", headers=OWNER).json()
        assert [r["name"] for r in rooms].count("Engineering") == 1

        other_org = client.post(
            "/api/v1/organizations", headers=OWNER, json={"name": "Other", "slug": "other-org"}
        ).json()
        other_ws = client.post(
            f"/api/v1/organizations/{other_org['org_id']}/workspaces",
            headers=OWNER,
            json={"name": "Main", "slug": "main"},
        ).json()
        elsewhere = client.post(
            f"/api/v1/workspaces/{other_ws['workspace_id']}/rooms",
            headers=OWNER,
            json={"name": "Engineering"},
        )
        assert elsewhere.status_code == 200, elsewhere.text
