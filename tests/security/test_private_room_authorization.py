"""Acceptance coverage for private-room authentication and authorization."""

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from multiplayer.server import create_app

OWNER_HEADERS = {"Authorization": "Bearer owner-token"}
OUTSIDER_HEADERS = {"Authorization": "Bearer outsider-token"}


def _seed_private_room(client: TestClient) -> dict[str, str]:
    org = client.post(
        "/api/v1/organizations",
        headers=OWNER_HEADERS,
        json={"name": "Private org", "slug": "private-org"},
    ).json()
    workspace = client.post(
        f"/api/v1/organizations/{org['org_id']}/workspaces",
        headers=OWNER_HEADERS,
        json={"name": "Private workspace", "slug": "private-workspace"},
    ).json()
    room = client.post(
        f"/api/v1/workspaces/{workspace['workspace_id']}/rooms",
        headers=OWNER_HEADERS,
        json={"name": "Private decision"},
    ).json()
    templates = client.get("/api/v1/agent-templates", headers=OWNER_HEADERS).json()
    agent = client.post(
        f"/api/v1/rooms/{room['room_id']}/agents",
        headers=OWNER_HEADERS,
        json={"template_id": templates[0]["template_id"]},
    ).json()
    session = client.post(
        f"/api/v1/rooms/{room['room_id']}/agents/{agent['agent_id']}/sessions",
        headers=OWNER_HEADERS,
    ).json()
    execution = client.post(
        f"/api/v1/sessions/{session['session_id']}/execute",
        headers=OWNER_HEADERS,
    ).json()
    output = client.post(
        f"/api/v1/executions/{execution['execution_id']}/step",
        headers=OWNER_HEADERS,
        json={"prompt": "secret output prompt"},
    ).json()
    message = client.post(
        f"/api/v1/rooms/{room['room_id']}/messages",
        headers=OWNER_HEADERS,
        json={"content": "secret room message"},
    )
    assert message.status_code == 200
    return {
        "room_id": room["room_id"],
        "agent_id": agent["agent_id"],
        "session_id": session["session_id"],
        "execution_id": execution["execution_id"],
        "output_id": output["output_id"],
    }


def test_private_room_denies_outsider_and_viewer_mutations_without_side_effects() -> None:
    app = create_app(
        ":memory:",
        auth_tokens={"owner-token": "user-a", "outsider-token": "user-b"},
    )
    with TestClient(app) as client:
        seeded = _seed_private_room(client)
        room_id = seeded["room_id"]

        unauthenticated = client.get(f"/api/v1/rooms/{room_id}")
        assert unauthenticated.status_code == 401

        owner_state_before = client.get(
            f"/api/v1/rooms/{room_id}/state", headers=OWNER_HEADERS
        ).json()

        forbidden_reads = [
            f"/api/v1/rooms/{room_id}",
            f"/api/v1/rooms/{room_id}/state",
            f"/api/v1/rooms/{room_id}/events?after=0",
            f"/api/v1/rooms/{room_id}/members",
            f"/api/v1/rooms/{room_id}/messages",
            f"/api/v1/rooms/{room_id}/agents",
            f"/api/v1/rooms/{room_id}/outputs",
        ]
        for path in forbidden_reads:
            assert client.get(path, headers=OUTSIDER_HEADERS).status_code == 403

        forbidden_mutations: list[tuple[str, dict[str, object] | None]] = [
            (f"/api/v1/rooms/{room_id}/join", None),
            (f"/api/v1/rooms/{room_id}/messages", {"content": "steal"}),
            (
                f"/api/v1/rooms/{room_id}/agents",
                {"template_id": "not-even-looked-up"},
            ),
            (
                f"/api/v1/rooms/{room_id}/agents/{seeded['agent_id']}/sessions",
                None,
            ),
            (f"/api/v1/sessions/{seeded['session_id']}/execute", None),
            (
                f"/api/v1/executions/{seeded['execution_id']}/step",
                {"prompt": "mutate"},
            ),
            (f"/api/v1/executions/{seeded['execution_id']}/pause", None),
            (f"/api/v1/executions/{seeded['execution_id']}/resume", None),
            (f"/api/v1/executions/{seeded['execution_id']}/cancel", None),
        ]
        for path, body in forbidden_mutations:
            response = client.post(path, headers=OUTSIDER_HEADERS, json=body)
            assert response.status_code == 403, path

        try:
            with client.websocket_connect(f"/ws?room_id={room_id}", headers=OUTSIDER_HEADERS) as ws:
                # Accepted, then closed with the code (see realtime/websocket.py):
                # the rejection now only surfaces on the first receive, not at
                # connect time, since a close code cannot exist before an accept.
                ws.receive_json()
                raise AssertionError("outsider websocket unexpectedly connected")
        except WebSocketDisconnect as exc:
            assert exc.code == 4403

        owner_state_after_denials = client.get(
            f"/api/v1/rooms/{room_id}/state", headers=OWNER_HEADERS
        ).json()
        for key in ("members", "agents", "runs", "outputs", "messages", "events_since"):
            assert owner_state_after_denials[key] == owner_state_before[key]

        invitation = client.post(
            f"/api/v1/rooms/{room_id}/members/invitations",
            headers=OWNER_HEADERS,
            json={"user_id": "user-b", "role": "viewer"},
        )
        assert invitation.status_code == 200

        viewer_state = client.get(f"/api/v1/rooms/{room_id}/state", headers=OUTSIDER_HEADERS)
        assert viewer_state.status_code == 200
        assert viewer_state.json()["messages"][0]["content"] == "secret room message"
        assert viewer_state.json()["outputs"][0]["output_id"] == seeded["output_id"]

        viewer_events_before = viewer_state.json()["events_since"]
        # join is READ-gated (an invited viewer may join their own room), so it
        # is asserted separately below rather than folded into these still-403
        # mutations, which stay MUTATE-gated even for a viewer; it is checked
        # after, not before, the no-side-effects comparison, since a real join
        # does record presence and a USER_JOINED_ROOM event.
        for path, body in forbidden_mutations:
            if path == f"/api/v1/rooms/{room_id}/join":
                continue
            response = client.post(path, headers=OUTSIDER_HEADERS, json=body)
            assert response.status_code == 403, path
        viewer_events_after = client.get(
            f"/api/v1/rooms/{room_id}/events", headers=OUTSIDER_HEADERS
        ).json()
        assert viewer_events_after == viewer_events_before

        joined = client.post(f"/api/v1/rooms/{room_id}/join", headers=OUTSIDER_HEADERS)
        assert joined.status_code == 200, joined.text

        with client.websocket_connect(
            f"/ws?room_id={room_id}", headers=OUTSIDER_HEADERS
        ) as websocket:
            assert websocket.receive_json()["type"] == "connected"
