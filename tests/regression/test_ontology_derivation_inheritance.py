"""A derived assertion is only as good as its weakest input.

The laundering path: a consolidation pass relates two unconfirmed AI extractions and
declares the edge system-materialized, which would put it in claims[] as confirmed truth.
The one repository method every timing writes through lowers it instead, so the edge lands
where its inputs put it — and no confidence threshold ever promotes it. Only human review
does, and reviewing one input does not review the edge.
"""

from __future__ import annotations

import pytest

from multiplayer.db.connection import Database
from multiplayer.domain.models import (
    OntologyDerivationKind,
    OntologyEntity,
    OntologyEntityKind,
    OntologyExtractor,
    OntologyReviewAction,
    OntologyReviewStatus,
    weakest_derivation_kind,
    weakest_review_status,
)
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService

KNOWN = frozenset({"owner", "viewer"})


def test_the_inheritance_orders_are_the_ones_the_rule_names() -> None:
    assert (
        weakest_derivation_kind(
            [OntologyDerivationKind.SYSTEM_MATERIALIZED, OntologyDerivationKind.AI_DERIVED]
        )
        is OntologyDerivationKind.AI_DERIVED
    )
    assert (
        weakest_review_status(
            [
                OntologyReviewStatus.CONFIRMED,
                OntologyReviewStatus.CORRECTED,
                OntologyReviewStatus.UNCONFIRMED,
            ]
        )
        is OntologyReviewStatus.UNCONFIRMED
    )
    assert (
        weakest_review_status([OntologyReviewStatus.CONFIRMED, OntologyReviewStatus.CORRECTED])
        is OntologyReviewStatus.CORRECTED
    )
    assert weakest_derivation_kind([]) is None
    assert weakest_review_status([]) is None


@pytest.mark.asyncio
async def test_a_consolidation_edge_cannot_launder_two_unconfirmed_inputs() -> None:
    db = Database(":memory:")
    await db.connect()
    try:
        service = MultiplayerService(db, RealtimeHub(), known_users=KNOWN)
        await service.initialize()
        org = await service.create_organization("Laundering", "laundering-org", "owner")
        workspace = await service.create_workspace(org.org_id, "Engineering", "laundering", "owner")
        room = await service.create_room(workspace.workspace_id, "Laundering", "owner")
        room_id = room.room_id
        await service.invite_room_member(room_id, "viewer", "viewer", "owner")

        def unconfirmed(label: str, source_id: str, confidence: float) -> OntologyEntity:
            return OntologyEntity(
                entity_id=service._ontology_id("ont", room_id, "Claim", source_id),
                room_id=room_id,
                kind=OntologyEntityKind.CLAIM,
                source_object_id=source_id,
                label=label,
                properties={"agent_id": f"agent_{source_id}"},
                derivation_kind=OntologyDerivationKind.AI_DERIVED,
                confidence=confidence,
                evidence_ids=(source_id,),
                source_ids=(source_id,),
                review_status=OntologyReviewStatus.UNCONFIRMED,
                extractor=OntologyExtractor.ASYNC,
                asserted_at_sequence=1,
                evidence_event_sequences=(1,),
            )

        positive = unconfirmed("The gateway is ready", "claim_positive", 0.9)
        negative = unconfirmed("not The gateway is ready", "claim_negative", 0.42)
        async with db.transaction():
            await service.repos.ontology.materialize_in_transaction([positive, negative], [])

        await service.run_ontology_extraction(room_id, OntologyExtractor.SCHEDULED)
        edges = await service.repos.ontology.list_relationships(room_id)
        assert len(edges) == 1
        edge = edges[0]
        # The consolidation writer declared SYSTEM_MATERIALIZED at confidence 1.0.
        assert edge.extractor is OntologyExtractor.SCHEDULED
        assert edge.derivation_kind is OntologyDerivationKind.AI_DERIVED
        assert edge.review_status is OntologyReviewStatus.UNCONFIRMED
        assert edge.confidence <= min(positive.confidence, negative.confidence)

        answer = await service.answer_decision_meta(
            room_id, "where is the disagreement", user_id="viewer"
        )
        assert answer["status"] == "ANSWERED_UNCONFIRMED_ONLY"
        assert answer["claims"] == []
        assert [record["assertion_id"] for record in answer["unconfirmed"]] == [
            edge.relationship_id
        ]
        assert answer["unconfirmed"][0]["assurance"] == "UNCONFIRMED_AI"
        assert answer["unconfirmed"][0]["text"].startswith("an unreviewed extraction suggests")
        # The summary never names an unconfirmed claim outside the hedged template.
        assert edge.kind.value not in answer["summary"]

        # Confirming one input does not promote the edge: only reviewing the edge can.
        await service.review_ontology_entity(
            room_id,
            positive.entity_id,
            OntologyReviewAction.CONFIRM,
            "owner",
            "Checked against the output.",
        )
        await service.run_ontology_extraction(room_id, OntologyExtractor.SCHEDULED)
        after = await service.repos.ontology.get_relationship(edge.relationship_id)
        assert after is not None
        assert after.derivation_kind is OntologyDerivationKind.AI_DERIVED
        assert after.review_status is OntologyReviewStatus.UNCONFIRMED
        promoted = await service.answer_decision_meta(
            room_id, "where is the disagreement", user_id="viewer"
        )
        assert promoted["status"] == "ANSWERED_UNCONFIRMED_ONLY"
        assert promoted["claims"] == []
    finally:
        await db.close()
