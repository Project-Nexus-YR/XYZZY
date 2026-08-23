"""Acceptance proof for the bounded, evidence-backed decision ontology."""

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from multiplayer.db.connection import Database
from multiplayer.domain.events import EventType
from multiplayer.domain.models import OntologyReviewAction, OutputDisposition
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.server import create_app
from multiplayer.services.service import MultiplayerService

OWNER = {"Authorization": "Bearer owner-token"}
EDITOR = {"Authorization": "Bearer editor-token"}
VIEWER = {"Authorization": "Bearer viewer-token"}
OUTSIDER = {"Authorization": "Bearer outsider-token"}


def _seed_output(client: TestClient, room_id: str, template_id: str, prompt: str) -> str:
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
    output = client.post(
        f"/api/v1/executions/{execution['execution_id']}/step",
        headers=OWNER,
        json={"prompt": prompt},
    ).json()
    return str(output["output_id"])


def _invite(client: TestClient, room_id: str, user_id: str, role: str) -> None:
    response = client.post(
        f"/api/v1/rooms/{room_id}/members/invitations",
        headers=OWNER,
        json={"user_id": user_id, "role": role},
    )
    assert response.status_code == 200


def _events(client: TestClient, room_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/v1/rooms/{room_id}/events", headers=OWNER)
    assert response.status_code == 200
    return response.json()


def test_browser_ontology_contract_uses_public_reconnect_state_and_review_routes() -> None:
    """The visible panel is wired only to room state and capability-checked APIs."""
    ui = (Path(__file__).parents[2] / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="ontology-panel"' in ui
    assert 'id="ontology-tree"' in ui
    assert 'id="ontology-history"' in ui
    assert "roomOntology = state.ontology" in ui
    assert "renderOntology(roomOntology)" in ui
    assert 'data-ontology-kind="Decision"' in ui
    assert 'data-ontology-kind="Claim"' in ui
    assert 'data-ontology-kind="AgentOutput"' in ui
    assert "Exact provider/source evidence" in ui
    assert "provider_response_id" in ui
    assert "JSON.stringify(review.before)" in ui
    assert "JSON.stringify(review.after)" in ui
    assert "currentRoomRole" in ui
    assert "canGovernOntology()" in ui
    assert "ontology/entities/${entityId}/reviews" in ui
    assert "ontology/relationships/${relationshipId}/reviews" in ui
    assert "routes_module._svc" not in ui


def test_decision_publication_materializes_selected_evidence_and_review_history() -> None:
    app = create_app(
        ":memory:",
        auth_tokens={
            "owner-token": "owner",
            "editor-token": "editor",
            "viewer-token": "viewer",
            "outsider-token": "outsider",
        },
    )
    with TestClient(app) as client:
        org = client.post(
            "/api/v1/organizations",
            headers=OWNER,
            json={"name": "Ontology org", "slug": "ontology-org"},
        ).json()
        workspace = client.post(
            f"/api/v1/organizations/{org['org_id']}/workspaces",
            headers=OWNER,
            json={"name": "Engineering", "slug": "engineering"},
        ).json()
        room_id = client.post(
            f"/api/v1/workspaces/{workspace['workspace_id']}/rooms",
            headers=OWNER,
            json={"name": "Identity provider decision"},
        ).json()["room_id"]
        _invite(client, room_id, "editor", "editor")
        _invite(client, room_id, "viewer", "viewer")

        templates = client.get("/api/v1/agent-templates", headers=OWNER).json()
        output_ids = [
            _seed_output(client, room_id, template["template_id"], prompt)
            for template, prompt in zip(
                templates[:3],
                ("engineering evidence", "security evidence", "excluded evidence"),
                strict=True,
            )
        ]
        selected_ids = set(output_ids[:2])
        for output_id, disposition in zip(
            output_ids,
            ("INCLUDED", "INCLUDED", "EXCLUDED"),
            strict=True,
        ):
            assert (
                client.put(
                    f"/api/v1/rooms/{room_id}/output-selections/{output_id}",
                    headers=OWNER,
                    json={"disposition": disposition},
                ).status_code
                == 200
            )

        publication = client.post(
            f"/api/v1/rooms/{room_id}/syntheses/decision-brief",
            headers=EDITOR,
            json={"title": "Choose the managed identity provider"},
        )
        assert publication.status_code == 200, publication.text

        response = client.get(f"/api/v1/rooms/{room_id}/ontology", headers=VIEWER)
        assert response.status_code == 200
        ontology = response.json()
        entities = ontology["entities"]
        relationships = ontology["relationships"]
        assert {entity["kind"] for entity in entities} == {
            "Person",
            "Project",
            "Decision",
            "Artifact",
            "Claim",
            "AgentOutput",
        }
        assert {
            entity["source_object_id"] for entity in entities if entity["kind"] == "AgentOutput"
        } == selected_ids
        assert output_ids[2] not in str(ontology)

        entity_by_id = {entity["entity_id"]: entity for entity in entities}
        claim_entities = [entity for entity in entities if entity["kind"] == "Claim"]
        output_entities = [entity for entity in entities if entity["kind"] == "AgentOutput"]
        assert len(claim_entities) == len(output_entities) == 2
        for record in [*entities, *relationships]:
            assert record["derivation_kind"] in {"SYSTEM_MATERIALIZED", "AI_DERIVED"}
            assert 0.0 <= record["confidence"] <= 1.0
            assert record["evidence_ids"]
            assert record["source_ids"]
            assert record["created_at"]
            assert record["updated_at"]
            if record["derivation_kind"] == "AI_DERIVED":
                assert set(record["evidence_ids"]).issubset(selected_ids)

        derivations = [
            relationship
            for relationship in relationships
            if relationship["kind"] == "DERIVED_FROM"
            and entity_by_id[relationship["from_entity_id"]]["kind"] == "Claim"
        ]
        assert len(derivations) == 2
        for relationship in derivations:
            output_entity = entity_by_id[relationship["to_entity_id"]]
            assert output_entity["kind"] == "AgentOutput"
            assert relationship["evidence_ids"] == [output_entity["source_object_id"]]

        # Reconnect returns the identical evidence graph, not a re-extraction.
        state = client.get(f"/api/v1/rooms/{room_id}/state", headers=EDITOR).json()
        assert state["ontology"] == ontology

        events_before_denials = _events(client, room_id)
        assert client.get(f"/api/v1/rooms/{room_id}/ontology", headers=OUTSIDER).status_code == 403
        decision = next(entity for entity in entities if entity["kind"] == "Decision")
        denied_review = client.post(
            f"/api/v1/rooms/{room_id}/ontology/entities/{decision['entity_id']}/reviews",
            headers=OUTSIDER,
            json={"action": "CORRECT", "corrected_label": "tampered", "reason": "attack"},
        )
        assert denied_review.status_code == 403
        viewer_review = client.post(
            f"/api/v1/rooms/{room_id}/ontology/entities/{decision['entity_id']}/reviews",
            headers=VIEWER,
            json={"action": "CONFIRM"},
        )
        assert viewer_review.status_code == 403
        viewer_link_review = client.post(
            f"/api/v1/rooms/{room_id}/ontology/relationships/"
            f"{derivations[0]['relationship_id']}/reviews",
            headers=VIEWER,
            json={"action": "CONFIRM", "reason": "viewer must not govern"},
        )
        assert viewer_link_review.status_code == 403
        assert _events(client, room_id) == events_before_denials

        correction = client.post(
            f"/api/v1/rooms/{room_id}/ontology/entities/{decision['entity_id']}/reviews",
            headers=EDITOR,
            json={
                "action": "CORRECT",
                "corrected_label": "Choose provider after a staged rollout",
                "corrected_confidence": 0.8,
                "reason": "The selected evidence requires a staged rollout.",
            },
        )
        assert correction.status_code == 200, correction.text
        correction_body = correction.json()
        assert correction_body["entity"]["review_status"] == "CORRECTED"
        assert correction_body["entity"]["evidence_ids"] == decision["evidence_ids"]
        assert correction_body["review"]["before"]["label"] == decision["label"]
        assert correction_body["review"]["after"]["confidence"] == 0.8
        assert correction_body["review"]["reviewed_by"] == "editor"

        claim = claim_entities[0]
        confirmation = client.post(
            f"/api/v1/rooms/{room_id}/ontology/entities/{claim['entity_id']}/reviews",
            headers=EDITOR,
            json={"action": "CONFIRM", "reason": "Checked against the frozen output."},
        )
        assert confirmation.status_code == 200
        assert confirmation.json()["entity"]["review_status"] == "CONFIRMED"

        relationship_correction = client.post(
            f"/api/v1/rooms/{room_id}/ontology/relationships/"
            f"{derivations[0]['relationship_id']}/reviews",
            headers=EDITOR,
            json={
                "action": "CORRECT",
                "corrected_confidence": 0.9,
                "reason": "Human review reduced the extraction confidence.",
            },
        )
        assert relationship_correction.status_code == 200
        relationship_body = relationship_correction.json()
        assert relationship_body["relationship"]["review_status"] == "CORRECTED"
        assert relationship_body["relationship"]["confidence"] == 0.9
        assert relationship_body["relationship"]["evidence_ids"] == derivations[0]["evidence_ids"]
        assert relationship_body["review"]["target_type"] == "RELATIONSHIP"

        reconnected = client.get(f"/api/v1/rooms/{room_id}/state", headers=VIEWER).json()
        reviews = reconnected["ontology"]["reviews"]
        assert [review["action"] for review in reviews] == [
            "CORRECT",
            "CONFIRM",
            "CORRECT",
        ]
        event_types = [event["event_type"] for event in reconnected["events_since"]]
        assert "ontology.materialized" in event_types
        assert "ontology.assertion_corrected" in event_types
        assert "ontology.assertion_confirmed" in event_types
        sequences = [event["sequence"] for event in reconnected["events_since"]]
        assert sequences == sorted(sequences)

        # The browser contract consumes the authorized reconnect snapshot and
        # exposes the same public review routes; it does not use a hidden store.
        ui = client.get("/").text
        assert 'id="ontology-panel"' in ui
        assert 'id="ontology-tree"' in ui
        assert 'id="ontology-history"' in ui
        assert "roomOntology = state.ontology" in ui
        assert "renderOntology(roomOntology)" in ui
        assert 'data-ontology-kind="Decision"' in ui
        assert 'data-ontology-kind="Claim"' in ui
        assert 'data-ontology-kind="AgentOutput"' in ui
        assert "Exact provider/source evidence" in ui
        assert "provider_response_id" in ui
        assert "reviewed_by" in ui
        assert "JSON.stringify(review.before)" in ui
        assert "JSON.stringify(review.after)" in ui
        assert "currentRoomRole" in ui
        assert "canGovernOntology()" in ui
        assert "ontology/entities/${entityId}/reviews" in ui
        assert "ontology/relationships/${relationshipId}/reviews" in ui


@pytest.mark.asyncio
async def test_ontology_failure_rolls_back_artifact_provenance_and_events() -> None:
    db = Database(":memory:")
    await db.connect()
    service = MultiplayerService(db, RealtimeHub())
    try:
        await service.initialize()
        org = await service.create_organization("Atomic org", "atomic-org", "owner")
        workspace = await service.create_workspace(
            org.org_id, "Engineering", "engineering", "owner"
        )
        room = await service.create_room(workspace.workspace_id, "Atomic ontology", "owner")
        templates = await service.list_agent_templates()
        output_ids: list[str] = []
        for template, prompt in zip(
            templates[:3],
            ("first selected", "second selected", "excluded"),
            strict=True,
        ):
            agent = await service.spawn_agent(room.room_id, template.template_id)
            session = await service.start_agent_session(room.room_id, agent.agent_id)
            execution = await service.start_execution(session.session_id, "owner")
            result = await service.execute_agent_step(execution.execution_id, prompt)
            output_ids.append(str(result["output_id"]))
        for output_id, disposition in zip(
            output_ids,
            (
                OutputDisposition.INCLUDED,
                OutputDisposition.INCLUDED,
                OutputDisposition.EXCLUDED,
            ),
            strict=True,
        ):
            await service.select_output(room.room_id, output_id, disposition, "owner")

        events_before = await service.get_room_events(room.room_id)
        await db.execute_script(
            "CREATE TRIGGER reject_test_ontology BEFORE INSERT ON ontology_entities "
            "BEGIN SELECT RAISE(ABORT, 'injected ontology failure'); END;"
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected ontology failure"):
            await service.synthesize_decision_brief(room.room_id, "Atomic publication", "owner")

        assert await service.list_room_artifacts(room.room_id) == []
        assert await service.repos.ontology.list_entities(room.room_id) == []
        assert await service.repos.ontology.list_relationships(room.room_id) == []
        # The result rolls back; the failure itself is recorded, never left running.
        events_after = await service.get_room_events(room.room_id)
        assert events_after[: len(events_before)] == events_before
        assert [event.event_type for event in events_after[len(events_before) :]] == [
            EventType.BRANCH_SYNTHESIS_STARTED,
            EventType.BRANCH_SYNTHESIS_FAILED,
        ]
        assert {
            selection.output_id: selection.disposition
            for selection in await service.list_output_selections(room.room_id)
        } == {
            output_ids[0]: OutputDisposition.INCLUDED,
            output_ids[1]: OutputDisposition.INCLUDED,
            output_ids[2]: OutputDisposition.EXCLUDED,
        }
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_unconfirmed_extractions_never_enter_the_confirmed_claim_set() -> None:
    """AI-derived claims are a second result set, and merging them would take new code."""
    db = Database(":memory:")
    await db.connect()
    service = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner", "viewer"}))
    try:
        await service.initialize()
        org = await service.create_organization("Assurance org", "assurance-org", "owner")
        workspace = await service.create_workspace(org.org_id, "Engineering", "assurance", "owner")
        room = await service.create_room(workspace.workspace_id, "Assurance", "owner")
        await service.invite_room_member(room.room_id, "viewer", "viewer", "owner")
        templates = await service.list_agent_templates()
        for template, prompt in zip(
            templates[:2], ("first evidence", "second evidence"), strict=True
        ):
            agent = await service.spawn_agent(room.room_id, template.template_id)
            session = await service.start_agent_session(room.room_id, agent.agent_id)
            execution = await service.start_execution(session.session_id, "owner")
            result = await service.execute_agent_step(execution.execution_id, prompt)
            await service.select_output(
                room.room_id, str(result["output_id"]), OutputDisposition.INCLUDED, "owner"
            )
        await service.synthesize_decision_brief(room.room_id, "Adopt the provider", "owner")

        answer = await service.answer_decision_meta(room.room_id, "why", user_id="viewer")
        assert answer["unconfirmed"]
        for claim in answer["claims"]:
            assert not (
                claim["derivation_kind"] == "AI_DERIVED" and claim["review_status"] == "UNCONFIRMED"
            )
        # Sole support is unconfirmed, so the status says so and claims[] stays empty.
        assert answer["status"] == "ANSWERED_UNCONFIRMED_ONLY"
        assert answer["claims"] == []
        assert answer["counts"]["claims"] == 0
        assert answer["counts"]["unconfirmed"] == len(answer["unconfirmed"])
        for record in answer["unconfirmed"]:
            assert record["assurance"] == "UNCONFIRMED_AI"
            assert record["text"] == f"an unreviewed extraction suggests: {record['label']}"

        # Human review is the only promotion, and it moves the claim across the two sets.
        decision_id = answer["unconfirmed"][0]["assertion_id"]
        await service.review_ontology_entity(
            room.room_id,
            decision_id,
            OntologyReviewAction.CONFIRM,
            "owner",
            "Read against the frozen provenance.",
        )
        promoted = await service.answer_decision_meta(room.room_id, "why", user_id="viewer")
        assert [claim["assertion_id"] for claim in promoted["claims"]] == [decision_id]
        assert promoted["status"] == "ANSWERED"
        assert decision_id not in {record["assertion_id"] for record in promoted["unconfirmed"]}
        for record in promoted["unconfirmed"]:
            assert record["label"] not in promoted["summary"]
    finally:
        await db.close()
