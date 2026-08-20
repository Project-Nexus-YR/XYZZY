"""Acceptance proof for durable, human-selected Decision Brief synthesis."""

from fastapi.testclient import TestClient

from multiplayer.server import create_app

OWNER = {"Authorization": "Bearer owner-token"}
EDITOR = {"Authorization": "Bearer editor-token"}


def _create_output(
    client: TestClient, room_id: str, template_id: str, prompt: str
) -> dict[str, str]:
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
        f"/api/v1/sessions/{session['session_id']}/execute", headers=OWNER
    ).json()
    result = client.post(
        f"/api/v1/executions/{execution['execution_id']}/step",
        headers=OWNER,
        json={"prompt": prompt},
    ).json()
    return {"output_id": result["output_id"], "prompt": prompt}


def test_selective_synthesis_persists_choices_and_exact_provenance_on_reconnect() -> None:
    app = create_app(
        ":memory:",
        auth_tokens={"owner-token": "owner", "editor-token": "editor"},
    )
    with TestClient(app) as client:
        org = client.post(
            "/api/v1/organizations",
            headers=OWNER,
            json={"name": "Acme", "slug": "acme-synthesis"},
        ).json()
        workspace = client.post(
            f"/api/v1/organizations/{org['org_id']}/workspaces",
            headers=OWNER,
            json={"name": "Main", "slug": "main"},
        ).json()
        room = client.post(
            f"/api/v1/workspaces/{workspace['workspace_id']}/rooms",
            headers=OWNER,
            json={"name": "Authentication migration"},
        ).json()
        room_id = room["room_id"]
        invitation = client.post(
            f"/api/v1/rooms/{room_id}/members/invitations",
            headers=OWNER,
            json={"user_id": "editor", "role": "editor"},
        )
        assert invitation.status_code == 200

        templates = client.get("/api/v1/agent-templates", headers=OWNER).json()
        seeded = [
            _create_output(client, room_id, template["template_id"], prompt)
            for template, prompt in zip(
                templates[:3],
                ("engineering evidence", "security evidence", "product evidence"),
                strict=True,
            )
        ]
        included_ids = {seeded[0]["output_id"], seeded[1]["output_id"]}
        excluded_id = seeded[2]["output_id"]

        for output in seeded[:2]:
            response = client.put(
                f"/api/v1/rooms/{room_id}/output-selections/{output['output_id']}",
                headers=OWNER,
                json={"disposition": "INCLUDED"},
            )
            assert response.status_code == 200
        response = client.put(
            f"/api/v1/rooms/{room_id}/output-selections/{excluded_id}",
            headers=OWNER,
            json={"disposition": "EXCLUDED"},
        )
        assert response.status_code == 200

        # A different authorized member reloads the exact shared choices.
        editor_state_before = client.get(f"/api/v1/rooms/{room_id}/state", headers=EDITOR).json()
        choices_before = {
            item["output_id"]: item["disposition"]
            for item in editor_state_before["output_selections"]
        }
        assert choices_before == {
            seeded[0]["output_id"]: "INCLUDED",
            seeded[1]["output_id"]: "INCLUDED",
            excluded_id: "EXCLUDED",
        }

        synthesis = client.post(
            f"/api/v1/rooms/{room_id}/syntheses/decision-brief",
            headers=EDITOR,
            json={"title": "Managed identity provider decision"},
        )
        assert synthesis.status_code == 200
        brief = synthesis.json()
        assert brief["version_number"] == 1
        assert {claim["output_id"] for claim in brief["claims"]} == included_ids
        assert excluded_id not in brief["content"]

        outputs = {item["output_id"]: item for item in editor_state_before["outputs"]}
        for claim in brief["claims"]:
            source = outputs[claim["output_id"]]
            assert claim["evidence"] == source["content"]
            assert claim["source_prompt"] == source["source_prompt"]
            assert claim["text"] == source["content"]
            assert claim["is_ai_derived"] == 1

        provenance = client.get(
            f"/api/v1/artifact-versions/{brief['version_id']}/provenance",
            headers=EDITOR,
        ).json()
        assert provenance["claims"] == brief["claims"]
        assert excluded_id not in {claim["output_id"] for claim in provenance["claims"]}

        # Reconnect/state rehydration yields identical choices and artifact identity.
        editor_state_after = client.get(
            f"/api/v1/rooms/{room_id}/state?last_sequence=0", headers=EDITOR
        ).json()
        assert editor_state_after["output_selections"] == editor_state_before["output_selections"]
        artifact = next(
            item
            for item in editor_state_after["artifacts"]
            if item["artifact_id"] == brief["artifact_id"]
        )
        assert artifact["version_id"] == brief["version_id"]
        assert artifact["version"] == 1
        assert artifact["content"] == brief["content"]

        # The shipped browser flow uses Bearer fetches and a negotiated WS
        # subprotocol credential, never a query-string identity or token.
        ui = client.get("/").text
        assert 'id="setup-token"' in ui
        assert "'Authorization': `Bearer ${accessToken}`" in ui
        assert "['multiai.v1', `bearer.${encodedToken}`]" in ui
        assert "output-selections/${outputId}" in ui
        assert "syntheses/decision-brief" in ui
        assert "user_id=${userId}" not in ui

        with client.websocket_connect(
            f"/ws?room_id={room_id}",
            subprotocols=["multiai.v1", "bearer.ZWRpdG9yLXRva2Vu"],
        ) as websocket:
            assert websocket.accepted_subprotocol == "multiai.v1"
            assert websocket.receive_json()["type"] == "connected"
