"""Every claim resolves to a durable row and a position in the log, or it is not returned.

A relationship used to have no hop from the edge to the row whose content states the
relation, so a BLOCKERS or DISAGREEMENT answer terminated early. Migration 017 gives
relationships their own source object and the write path refuses an empty one, so the
chain below can be walked for every claim of every answer without an exemption.
"""

from __future__ import annotations

from typing import Any

import pytest

from multiplayer.db.connection import Database
from multiplayer.domain.meta import ACCEPTED_QUESTIONS
from multiplayer.domain.models import (
    MessageRole,
    OntologyDerivationKind,
    OntologyEntityKind,
    OntologyExtractor,
    OntologyRelationship,
    OntologyRelationshipKind,
    OntologyReviewAction,
    OutputDisposition,
)
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService

KNOWN = frozenset({"owner", "viewer"})

# Where each kind of source object is durable. agent_outputs, artifact_versions,
# artifact_claims and artifact_claim_sources are immutable by trigger (migration 005),
# which is what makes the end of a chain evidence rather than a pointer.
_TERMINALS: dict[str, tuple[str, str]] = {
    "Claim": ("artifact_claims", "claim_id"),
    "AgentOutput": ("agent_outputs", "output_id"),
    "Artifact": ("artifact_versions", "version_id"),
    "Task": ("tasks", "task_id"),
    "Person": ("users", "user_id"),
    "Project": ("rooms", "room_id"),
    "Message": ("messages", "message_id"),
}


async def _resolve(db: Database, kind: str, object_id: str) -> str:
    """One hop to the durable row, asserting it is exactly one row."""
    if kind == "Decision":
        # A Decision is either a published artifact version or a decision record.
        for table, column in (("artifact_versions", "version_id"), ("decisions", "decision_id")):
            rows = await db.fetch_all(
                f"SELECT {column} AS id FROM {table} WHERE {column} = ?", (object_id,)
            )
            if rows:
                assert len(rows) == 1
                return str(rows[0]["id"])
        raise AssertionError(f"Decision source {object_id} resolves to no durable row")
    table, column = _TERMINALS[kind]
    rows = await db.fetch_all(
        f"SELECT {column} AS id FROM {table} WHERE {column} = ?", (object_id,)
    )
    assert len(rows) == 1, f"{kind} {object_id} resolved to {len(rows)} rows"
    return str(rows[0]["id"])


async def _seed(service: MultiplayerService) -> str:
    org = await service.create_organization("Drilldown", "drilldown-org", "owner")
    workspace = await service.create_workspace(org.org_id, "Engineering", "drilldown", "owner")
    room = await service.create_room(workspace.workspace_id, "Drilldown", "owner")
    room_id = room.room_id
    await service.invite_room_member(room_id, "viewer", "viewer", "owner")
    await service.create_task(room_id, "Ship the gateway", created_by="owner")
    await service.create_task(room_id, "Rotate the keys", created_by="owner")
    await service.create_decision(room_id, "Adopt the gateway", "content", created_by="owner")
    await service.run_ontology_extraction(room_id, OntologyExtractor.IMMEDIATE)
    await service.send_message(
        room_id, MessageRole.HUMAN, "owner", "Ship the gateway is blocked by Rotate the keys"
    )
    await service.run_ontology_extraction(room_id, OntologyExtractor.ASYNC)
    blocks = next(
        item
        for item in await service.repos.ontology.list_relationships(room_id)
        if item.kind is OntologyRelationshipKind.BLOCKS
    )
    await service.review_ontology_relationship(
        room_id, blocks.relationship_id, OntologyReviewAction.CONFIRM, "owner", "reported"
    )

    templates = await service.list_agent_templates()
    for template, prompt in zip(templates[:2], ("first evidence", "second evidence"), strict=True):
        agent = await service.spawn_agent(room_id, template.template_id)
        session = await service.start_agent_session(room_id, agent.agent_id)
        execution = await service.start_execution(session.session_id, "owner")
        result = await service.execute_agent_step(execution.execution_id, prompt)
        await service.select_output(
            room_id, str(result["output_id"]), OutputDisposition.INCLUDED, "owner"
        )
    await service.synthesize_decision_brief(room_id, "Adopt the managed gateway", "owner")

    # A human correction makes one published claim the negation of another, which is
    # what the consolidation pass detects.
    claims = [
        entity
        for entity in await service.repos.ontology.list_entities(room_id)
        if entity.kind is OntologyEntityKind.CLAIM
    ]
    assert len(claims) == 2
    await service.review_ontology_entity(
        room_id,
        claims[0].entity_id,
        OntologyReviewAction.CORRECT,
        "owner",
        "Review found the opposite.",
        corrected_label=f"not {claims[1].label}",
    )
    await service.run_ontology_extraction(room_id, OntologyExtractor.SCHEDULED)
    contradicts = next(
        item
        for item in await service.repos.ontology.list_relationships(room_id)
        if item.kind is OntologyRelationshipKind.CONTRADICTS
    )
    await service.review_ontology_relationship(
        room_id,
        contradicts.relationship_id,
        OntologyReviewAction.CONFIRM,
        "owner",
        "Both claims were read.",
    )
    return room_id


@pytest.mark.asyncio
async def test_every_claim_of_every_answer_walks_to_an_immutable_row_and_a_sequence() -> None:
    db = Database(":memory:")
    await db.connect()
    try:
        service = MultiplayerService(db, RealtimeHub(), known_users=KNOWN)
        await service.initialize()
        room_id = await _seed(service)

        walked = 0
        kinds_seen: set[str] = set()
        relationship_kinds: set[str] = set()
        for question in ACCEPTED_QUESTIONS:
            answer = await service.answer_decision_meta(room_id, question, user_id="viewer")
            records: list[dict[str, Any]] = [*answer["claims"], *answer["unconfirmed"]]
            for record in records:
                assert record["source_object_id"], record
                resolved = await _resolve(
                    db, str(record["source_object_kind"]), str(record["source_object_id"])
                )
                assert resolved == record["source_object_id"]
                for sequence in record["evidence_event_sequences"]:
                    row = await db.fetch_one(
                        "SELECT sequence FROM room_events WHERE room_id = ? AND sequence = ?",
                        (room_id, sequence),
                    )
                    assert row is not None
                kinds_seen.add(str(record["source_object_kind"]))
                if record["assertion_type"] == "RELATIONSHIP":
                    relationship_kinds.add(str(record["kind"]))
                walked += 1
        assert walked > 0
        # The two relationship-centric answers the old schema could not terminate.
        assert {"BLOCKS", "CONTRADICTS"} <= relationship_kinds
        assert "Message" in kinds_seen
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_an_edge_without_a_source_object_cannot_be_written() -> None:
    db = Database(":memory:")
    await db.connect()
    try:
        service = MultiplayerService(db, RealtimeHub(), known_users=KNOWN)
        await service.initialize()
        room_id = await _seed(service)
        entities = await service.repos.ontology.list_entities(room_id)
        broken = OntologyRelationship(
            relationship_id="rel_broken",
            room_id=room_id,
            kind=OntologyRelationshipKind.REFERENCES,
            from_entity_id=entities[0].entity_id,
            to_entity_id=entities[1].entity_id,
            derivation_kind=OntologyDerivationKind.SYSTEM_MATERIALIZED,
            evidence_ids=("x",),
            source_ids=("x",),
        )
        with pytest.raises(ValueError, match="requires a source object"):
            async with db.transaction():
                await service.repos.ontology.materialize_in_transaction([], [broken])
        assert await service.repos.ontology.get_relationship("rel_broken") is None
    finally:
        await db.close()
