"""Public API proof for Branch ownership and turn-locked message behavior."""

from fastapi.testclient import TestClient

from multiplayer.server import create_app

OWNER = {"Authorization": "Bearer owner-token"}


def test_turn_locked_branch_is_reconnectable_and_releases_room_messages() -> None:
    app = create_app(":memory:", auth_tokens={"owner-token": "owner"})
    with TestClient(app) as client:
        bootstrap = client.post(
            "/api/v1/me/bootstrap",
            headers=OWNER,
            json={"display_name": "Owner", "room_name": "Decision"},
        ).json()
        room_id = bootstrap["room"]["room_id"]
        template_id = client.get("/api/v1/agent-templates", headers=OWNER).json()[0]["template_id"]
        agent = client.post(
            f"/api/v1/rooms/{room_id}/agents",
            headers=OWNER,
            json={"template_id": template_id},
        ).json()
        started = client.post(
            f"/api/v1/rooms/{room_id}/branches",
            headers=OWNER,
            json={
                "mode": "TURN_LOCKED_SINGLE",
                "prompt": "Choose the migration sequence.",
                "agent_ids": [agent["agent_id"]],
            },
        )
        assert started.status_code == 200
        payload = started.json()
        branch_id = payload["branch"]["branch_id"]
        execution_id = payload["runs"][0]["execution_id"]

        locked = client.post(
            f"/api/v1/rooms/{room_id}/messages",
            headers=OWNER,
            json={"content": "This must not slip past the context boundary"},
        )
        assert locked.status_code == 409
        assert (
            client.post(
                f"/api/v1/rooms/{room_id}/messages",
                headers=OWNER,
                json={"content": "Spoofed bypass", "role": "AGENT"},
            ).status_code
            == 403
        )
        state = client.get(f"/api/v1/rooms/{room_id}/state", headers=OWNER).json()
        assert state["turn_lock"]["branch_id"] == branch_id
        assert any(item["branch_id"] == branch_id for item in state["branches"])
        assert all(item["content"] != "Spoofed bypass" for item in state["messages"])

        completed = client.post(
            f"/api/v1/branches/{branch_id}/runs/{execution_id}/execute",
            headers=OWNER,
        )
        assert completed.status_code == 200
        reconnect = client.get(f"/api/v1/branches/{branch_id}", headers=OWNER).json()
        assert reconnect["branch"]["status"] == "COMPLETED"
        assert reconnect["runs"][0]["status"] == "COMPLETED"
        assert (
            client.post(
                f"/api/v1/rooms/{room_id}/messages",
                headers=OWNER,
                json={"content": "Accepted after the turn"},
            ).status_code
            == 200
        )
