"""S5: an omitted decision-brief title derives from the branch's own prompt

instead of stamping every brief with the dev-leftover "Authentication migration
decision" default, which belongs to nobody's actual question.
"""

from fastapi.testclient import TestClient

from multiplayer.server import create_app

OWNER = {"Authorization": "Bearer owner-token"}


def test_untitled_pricing_brief_does_not_say_authentication_migration() -> None:
    app = create_app(":memory:", auth_tokens={"owner-token": "owner"})
    with TestClient(app) as client:
        org = client.post(
            "/api/v1/organizations", headers=OWNER, json={"name": "Acme", "slug": "acme-pricing"}
        ).json()
        workspace = client.post(
            f"/api/v1/organizations/{org['org_id']}/workspaces",
            headers=OWNER,
            json={"name": "Main", "slug": "main"},
        ).json()
        room_id = client.post(
            f"/api/v1/workspaces/{workspace['workspace_id']}/rooms",
            headers=OWNER,
            json={"name": "Pricing"},
        ).json()["room_id"]

        templates = client.get("/api/v1/agent-templates", headers=OWNER).json()
        agent_ids = [
            client.post(
                f"/api/v1/rooms/{room_id}/agents",
                headers=OWNER,
                json={"template_id": template["template_id"]},
            ).json()["agent_id"]
            for template in templates[:2]
        ]
        prompt = "Should we launch with usage-based pricing or a flat monthly fee?"
        started = client.post(
            f"/api/v1/rooms/{room_id}/branches",
            headers=OWNER,
            json={"mode": "PARALLEL", "prompt": prompt, "agent_ids": agent_ids},
        ).json()
        branch_id = started["branch"]["branch_id"]
        for run in started["runs"]:
            response = client.post(
                f"/api/v1/branches/{branch_id}/runs/{run['execution_id']}/execute", headers=OWNER
            )
            assert response.status_code == 200, response.text

        outputs = client.get(f"/api/v1/rooms/{room_id}/state", headers=OWNER).json()["outputs"]
        for output in outputs:
            selected = client.put(
                f"/api/v1/rooms/{room_id}/output-selections/{output['output_id']}",
                headers=OWNER,
                json={"disposition": "INCLUDED"},
            )
            assert selected.status_code == 200, selected.text

        # No title supplied at all: the service must derive one, not fall back to
        # a hardcoded, unrelated decision.
        brief = client.post(
            f"/api/v1/branches/{branch_id}/syntheses/decision-brief", headers=OWNER, json={}
        )
        assert brief.status_code == 200, brief.text
        content = brief.json()["content"]
        assert "Authentication migration" not in content
        assert content.startswith(f"# {prompt}")
