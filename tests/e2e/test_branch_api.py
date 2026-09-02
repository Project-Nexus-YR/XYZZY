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


def test_room_state_runs_carry_their_branch_id() -> None:
    """The client filters state["runs"] by branch_id, so a run the state
    payload leaves it off of never renders in the branch panel it belongs to.
    """
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
            json={"template_id": template_id, "name": "Architect"},
        ).json()

        # A direct run: an agent invoked by mention, no branch involved.
        mentioned = client.post(
            f"/api/v1/rooms/{room_id}/messages",
            headers=OWNER,
            json={"content": "@Architect go", "invoke_mentioned_agents": True},
        )
        assert mentioned.status_code == 200, mentioned.text
        direct_execution_id = next(
            m["invoked_execution_id"]
            for m in mentioned.json()["mentions"]
            if m["target_type"] == "AGENT"
        )

        # A branch run: a turn-locked branch started against the same agent.
        started = client.post(
            f"/api/v1/rooms/{room_id}/branches",
            headers=OWNER,
            json={
                "mode": "TURN_LOCKED_SINGLE",
                "prompt": "Choose the migration sequence.",
                "agent_ids": [agent["agent_id"]],
            },
        )
        assert started.status_code == 200, started.text
        branch_id = started.json()["branch"]["branch_id"]

        state = client.get(f"/api/v1/rooms/{room_id}/state", headers=OWNER).json()
        branch = next(item for item in state["branches"] if item["branch_id"] == branch_id)
        assert branch["execution_ids"], "branch must have started at least one run"

        runs_by_id = {run["execution_id"]: run for run in state["runs"]}
        for branch_execution_id in branch["execution_ids"]:
            assert runs_by_id[branch_execution_id]["branch_id"] == branch_id
        # The mention run answers through its own (legacy, single-agent) branch,
        # distinct from the branch started above, and that branch also carries it.
        direct_branch = next(
            item for item in state["branches"] if direct_execution_id in item["execution_ids"]
        )
        assert direct_branch["branch_id"] != branch_id
        assert runs_by_id[direct_execution_id]["branch_id"] == direct_branch["branch_id"]
