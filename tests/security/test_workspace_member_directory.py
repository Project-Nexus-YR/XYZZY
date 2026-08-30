"""S2: GET /workspaces/{workspace_id}/members backs the client's invite picker.

Workspace-membership-gated, shaped like the room member list, with the same
users-row-or-user_id display name fallback.
"""

from fastapi.testclient import TestClient

from multiplayer.server import create_app

TOKENS = {
    "owner-token": "owner",
    "alex-token": "alex",
    "outsider-token": "outsider",
}
OWNER = {"Authorization": "Bearer owner-token"}
ALEX = {"Authorization": "Bearer alex-token"}
OUTSIDER = {"Authorization": "Bearer outsider-token"}


def _bootstrap(client: TestClient) -> tuple[str, str]:
    response = client.post(
        "/api/v1/me/bootstrap",
        headers=OWNER,
        json={"display_name": "Owner Person", "room_name": "Main channel"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return body["room"]["room_id"], body["room"]["workspace_id"]


def test_shape_gating_and_display_name_fallback() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id, workspace_id = _bootstrap(client)

        # A non-member is refused, even one who is a known account elsewhere.
        gated = client.get(f"/api/v1/workspaces/{workspace_id}/members", headers=OUTSIDER)
        assert gated.status_code == 403, gated.text

        invited = client.post(
            f"/api/v1/rooms/{room_id}/members/invitations",
            headers=OWNER,
            json={"user_id": "alex", "role": "editor"},
        )
        assert invited.status_code == 200, invited.text

        listed = client.get(f"/api/v1/workspaces/{workspace_id}/members", headers=ALEX)
        assert listed.status_code == 200, listed.text
        members = listed.json()
        by_user = {m["user_id"]: m for m in members}
        assert set(by_user) == {"owner", "alex"}
        # Bootstrap wrote a users row with the typed display name for owner;
        # alex was only invited into the room, so falls back to their user_id.
        assert by_user["owner"] == {
            "user_id": "owner",
            "display_name": "Owner Person",
            "workspace_role": "admin",
        }
        assert by_user["alex"] == {
            "user_id": "alex",
            "display_name": "alex",
            "workspace_role": "member",
        }
