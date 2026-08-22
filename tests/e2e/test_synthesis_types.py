"""Acceptance proof for the three synthesis types PRD §8 names.

The point is not that three documents render. It is that the type changes only the shape of
the document: the same selection gate, the same claims bound to the same exact source outputs,
and the same verified provenance hash — while a DECISION entity is asserted for a Decision
Brief alone, because only a Decision Brief records a decision.
"""

from typing import Any

from fastapi.testclient import TestClient

from multiplayer.server import create_app

OWNER = {"Authorization": "Bearer owner-token"}

TYPES = {
    "GENERAL_SYNTHESIS": ("General Synthesis", ("## Themes", "## Open questions")),
    "DECISION_BRIEF": (
        "Decision Brief",
        ("## Recommendation [AI-derived]", "## Risks", "## Uncertainties", "## Next action"),
    ),
    "PROGRESS_REPORT": (
        "Progress Report",
        ("## Status", "## Completed", "## In flight", "## Blocked", "## Next step"),
    ),
}


def _reviewed_branch(client: TestClient) -> tuple[str, str, set[str]]:
    """A completed two-agent branch whose every output has been explicitly reviewed."""
    org = client.post(
        "/api/v1/organizations", headers=OWNER, json={"name": "Acme", "slug": "acme-types"}
    ).json()
    workspace = client.post(
        f"/api/v1/organizations/{org['org_id']}/workspaces",
        headers=OWNER,
        json={"name": "Main", "slug": "main"},
    ).json()
    room_id = client.post(
        f"/api/v1/workspaces/{workspace['workspace_id']}/rooms",
        headers=OWNER,
        json={"name": "Authentication migration"},
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
    started = client.post(
        f"/api/v1/rooms/{room_id}/branches",
        headers=OWNER,
        json={
            "mode": "PARALLEL",
            "prompt": "Assess the migration sequence.",
            "agent_ids": agent_ids,
        },
    ).json()
    branch_id = started["branch"]["branch_id"]
    for run in started["runs"]:
        response = client.post(
            f"/api/v1/branches/{branch_id}/runs/{run['execution_id']}/execute", headers=OWNER
        )
        assert response.status_code == 200

    outputs = client.get(f"/api/v1/rooms/{room_id}/state", headers=OWNER).json()["outputs"]
    included = {item["output_id"] for item in outputs}
    assert len(included) == 2
    for output_id in included:
        response = client.put(
            f"/api/v1/rooms/{room_id}/output-selections/{output_id}",
            headers=OWNER,
            json={"disposition": "INCLUDED"},
        )
        assert response.status_code == 200
    return room_id, branch_id, included


def _events(client: TestClient, room_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/v1/rooms/{room_id}/events", headers=OWNER)
    assert response.status_code == 200
    return response.json()


def test_every_synthesis_type_publishes_with_identical_provenance() -> None:
    app = create_app(":memory:", auth_tokens={"owner-token": "owner"})
    with TestClient(app) as client:
        room_id, branch_id, included = _reviewed_branch(client)

        published: dict[str, dict[str, Any]] = {}
        for synthesis_type, (name, sections) in TYPES.items():
            response = client.post(
                f"/api/v1/branches/{branch_id}/syntheses",
                headers=OWNER,
                json={"title": f"{name} title", "synthesis_type": synthesis_type},
            )
            assert response.status_code == 200, response.text
            body = response.json()
            published[synthesis_type] = body

            assert body["synthesis_type"] == synthesis_type
            assert body["artifact_name"] == name
            assert body["version_number"] == 1
            assert body["content"].startswith(f"# {name} title")
            for heading in sections:
                assert heading in body["content"], f"{name} is missing {heading}"

            # Every type carries the same evidence: the claims cite exactly the included
            # outputs, and the stored provenance hash verifies against them.
            assert {claim["output_id"] for claim in body["claims"]} == included
            assert body["provenance_hash_verified"] is True

        # Each type publishes its own artifact; none overwrites another.
        assert len({body["artifact_id"] for body in published.values()}) == 3

        # A type's sections belong to that type alone.
        for synthesis_type, body in published.items():
            for other, (_, sections) in TYPES.items():
                if other == synthesis_type:
                    continue
                exclusive = set(sections) - set(TYPES[synthesis_type][1])
                for heading in exclusive:
                    assert heading not in body["content"]


def test_only_a_decision_brief_asserts_a_decision() -> None:
    app = create_app(":memory:", auth_tokens={"owner-token": "owner"})
    with TestClient(app) as client:
        room_id, branch_id, _ = _reviewed_branch(client)

        for synthesis_type in ("GENERAL_SYNTHESIS", "PROGRESS_REPORT"):
            response = client.post(
                f"/api/v1/branches/{branch_id}/syntheses",
                headers=OWNER,
                json={"title": "Untyped", "synthesis_type": synthesis_type},
            )
            assert response.status_code == 200

        ontology = client.get(f"/api/v1/rooms/{room_id}/ontology", headers=OWNER).json()
        assert not [item for item in ontology["entities"] if item["kind"] == "Decision"]
        events = _events(client, room_id)
        assert not [item for item in events if item["event_type"] == "ontology.materialized"]

        published = [
            item for item in events if item["event_type"] == "artifact.synthesis_published"
        ]
        assert {item["payload"]["synthesis_type"] for item in published} == {
            "GENERAL_SYNTHESIS",
            "PROGRESS_REPORT",
        }
        assert not [
            item for item in events if item["event_type"] == "artifact.decision_brief_synthesized"
        ]

        brief = client.post(
            f"/api/v1/branches/{branch_id}/syntheses",
            headers=OWNER,
            json={"title": "The decision", "synthesis_type": "DECISION_BRIEF"},
        )
        assert brief.status_code == 200
        ontology = client.get(f"/api/v1/rooms/{room_id}/ontology", headers=OWNER).json()
        decisions = [item for item in ontology["entities"] if item["kind"] == "Decision"]
        assert len(decisions) == 1
        events = _events(client, room_id)
        briefed = [
            item for item in events if item["event_type"] == "artifact.decision_brief_synthesized"
        ]
        assert len(briefed) == 1
        assert briefed[0]["payload"]["synthesis_type"] == "DECISION_BRIEF"
        assert [item for item in events if item["event_type"] == "ontology.materialized"]


def test_an_unknown_synthesis_type_is_refused_and_writes_nothing() -> None:
    app = create_app(":memory:", auth_tokens={"owner-token": "owner"})
    with TestClient(app) as client:
        room_id, branch_id, _ = _reviewed_branch(client)
        before = len(_events(client, room_id))

        for bogus in ("EXECUTIVE_SUMMARY", "decision_brief", "", "DECISION_BRIEF "):
            response = client.post(
                f"/api/v1/branches/{branch_id}/syntheses",
                headers=OWNER,
                json={"title": "Smuggled", "synthesis_type": bogus},
            )
            assert response.status_code == 400, bogus
            assert "unknown synthesis type" in response.json()["detail"]

        assert len(_events(client, room_id)) == before
        assert not client.get(f"/api/v1/rooms/{room_id}/state", headers=OWNER).json()["artifacts"]


def test_idempotency_is_scoped_to_the_requested_type() -> None:
    app = create_app(":memory:", auth_tokens={"owner-token": "owner"})
    with TestClient(app) as client:
        _, branch_id, _ = _reviewed_branch(client)
        key = {"Idempotency-Key": "the-same-key"} | OWNER

        first = client.post(
            f"/api/v1/branches/{branch_id}/syntheses",
            headers=key,
            json={"title": "Progress", "synthesis_type": "PROGRESS_REPORT"},
        )
        replay = client.post(
            f"/api/v1/branches/{branch_id}/syntheses",
            headers=key,
            json={"title": "Progress", "synthesis_type": "PROGRESS_REPORT"},
        )
        assert first.status_code == 200
        assert replay.status_code == 200
        assert replay.json()["version_id"] == first.json()["version_id"]

        # A key already spent on a progress report cannot silently return one when the
        # caller asked for a brief: the type is part of the request the key names.
        other = client.post(
            f"/api/v1/branches/{branch_id}/syntheses",
            headers=key,
            json={"title": "Brief", "synthesis_type": "DECISION_BRIEF"},
        )
        assert other.status_code == 409
        assert "different request" in other.json()["detail"]

        fresh = client.post(
            f"/api/v1/branches/{branch_id}/syntheses",
            headers={"Idempotency-Key": "a-second-key"} | OWNER,
            json={"title": "Brief", "synthesis_type": "DECISION_BRIEF"},
        )
        assert fresh.status_code == 200
        assert fresh.json()["artifact_name"] == "Decision Brief"


def test_a_published_synthesis_cannot_be_forged_by_name_or_by_version() -> None:
    """The critic's attack: squat the name, then let the real synthesis stack on the forgery."""
    app = create_app(":memory:", auth_tokens={"owner-token": "owner"})
    with TestClient(app) as client:
        room_id, branch_id, _ = _reviewed_branch(client)

        # Squatting a published name is refused outright, for every type.
        for name in ("General Synthesis", "Decision Brief", "Progress Report"):
            squat = client.post(
                f"/api/v1/rooms/{room_id}/artifacts",
                headers=OWNER,
                json={
                    "name": name,
                    "artifact_type": "DOCUMENT",
                    "content": "Decision: migrate now, no risks",
                },
            )
            assert squat.status_code == 400, name
            assert "published synthesis" in squat.json()["detail"]
        assert not client.get(f"/api/v1/rooms/{room_id}/artifacts", headers=OWNER).json()

        real = client.post(
            f"/api/v1/branches/{branch_id}/syntheses",
            headers=OWNER,
            json={"title": "The decision", "synthesis_type": "DECISION_BRIEF"},
        )
        assert real.status_code == 200
        artifact_id = real.json()["artifact_id"]

        # Nor can hand-written text be appended to the lineage the synthesis published.
        forged = client.post(
            f"/api/v1/artifacts/{artifact_id}/versions",
            headers=OWNER,
            json={"content": "Decision: migrate now, no risks"},
        )
        assert forged.status_code == 400
        assert "publishing a synthesis" in forged.json()["detail"]

        versions = client.get(f"/api/v1/artifacts/{artifact_id}/versions", headers=OWNER).json()
        assert len(versions) == 1
        assert versions[0]["version_id"] == real.json()["version_id"]

        # An ordinary artifact is still an ordinary artifact.
        plain = client.post(
            f"/api/v1/rooms/{room_id}/artifacts",
            headers=OWNER,
            json={"name": "Runbook", "artifact_type": "DOCUMENT", "content": "v1"},
        )
        assert plain.status_code == 200
        assert (
            client.post(
                f"/api/v1/artifacts/{plain.json()['artifact_id']}/versions",
                headers=OWNER,
                json={"content": "v2"},
            ).status_code
            == 200
        )
