"""Meta authorization: the query decides, and every aggregate is inside the same scope.

Three invariants are proven here. A reader outside the room's reading roles gets zero
claims out of SQLite rather than rows some later Python filter is trusted to drop — and
the room really does hold assertions, asserted against the database, so the emptiness is
the filter and not an empty room. A refusal carries no head, no drain lag and no other
figure, because a count of content the asker may not read leaks that content's existence
and rate. And every statement the Meta path executes carries the membership join and the
role predicate, with no exemption list.
"""

from __future__ import annotations

from typing import Any

import pytest

from multiplayer.db.connection import Database
from multiplayer.db.repositories import MetaRepo
from multiplayer.domain.meta import ACCEPTED_QUESTIONS, MetaQuestionKind
from multiplayer.domain.models import (
    MessageRole,
    OntologyEntityKind,
    OntologyExtractor,
    OntologyRelationshipKind,
    OutputDisposition,
    RoomMember,
)
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.authorization import RoomCapability, roles_with_capability
from multiplayer.security.capabilities import (
    CAPABILITIES,
    CapabilityTerms,
    allowed_tools,
    decide,
    policy_capabilities,
    user_capabilities,
)
from multiplayer.services.service import MultiplayerService

KNOWN = frozenset({"owner", "viewer", "sibling", "observer", "outsider"})


class _RecordingDatabase:
    """Passes every read through and keeps the SQL, so the test can inspect it."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self.statements: list[str] = []

    async def fetch_one(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        self.statements.append(sql)
        return await self._db.fetch_one(sql, params)

    async def fetch_all(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        self.statements.append(sql)
        return await self._db.fetch_all(sql, params)


async def _seed(service: MultiplayerService) -> tuple[str, str]:
    """A room with governed assertions, and a sibling room the outsider does belong to."""
    org = await service.create_organization("Meta security", "meta-security", "owner")
    workspace = await service.create_workspace(org.org_id, "Engineering", "meta-sec", "owner")
    room = await service.create_room(workspace.workspace_id, "Guarded", "owner")
    sibling = await service.create_room(workspace.workspace_id, "Sibling", "owner")
    await service.invite_room_member(room.room_id, "viewer", "viewer", "owner")
    await service.invite_room_member(sibling.room_id, "sibling", "viewer", "owner")
    await service.create_task(room.room_id, "Ship the gateway", created_by="owner")
    await service.create_task(room.room_id, "Rotate the keys", created_by="owner")
    await service.create_decision(room.room_id, "Adopt the gateway", "content", created_by="owner")
    await service.run_ontology_extraction(room.room_id, OntologyExtractor.IMMEDIATE)
    await service.send_message(
        room.room_id,
        MessageRole.HUMAN,
        "owner",
        "Ship the gateway is blocked by Rotate the keys",
    )
    await service.run_ontology_extraction(room.room_id, OntologyExtractor.ASYNC)
    templates = await service.list_agent_templates()
    for template, prompt in zip(templates[:2], ("first evidence", "second evidence"), strict=True):
        agent = await service.spawn_agent(room.room_id, template.template_id)
        session = await service.start_agent_session(room.room_id, agent.agent_id)
        execution = await service.start_execution(session.session_id, "owner")
        result = await service.execute_agent_step(execution.execution_id, prompt)
        await service.select_output(
            room.room_id, str(result["output_id"]), OutputDisposition.INCLUDED, "owner"
        )
    await service.synthesize_decision_brief(room.room_id, "Adopt the managed gateway", "owner")
    # A membership row bearing a role the policy grants nothing for. room_members.role
    # has no CHECK, so this row exists and existence-only membership would read the room.
    await service.repos.room_members.add(
        RoomMember(room_id=room.room_id, user_id="observer", role="observer")
    )
    return room.room_id, sibling.room_id


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", ["outsider", "sibling", "observer"])
async def test_unauthorized_readers_receive_zero_claims_from_a_non_empty_room(
    reader: str,
) -> None:
    db = Database(":memory:")
    await db.connect()
    service = MultiplayerService(db, RealtimeHub(), known_users=KNOWN)
    try:
        await service.initialize()
        room_id, _sibling_id = await _seed(service)
        assert "observer" not in roles_with_capability(RoomCapability.READ)
        # The room really does hold assertions; the emptiness below is the filter.
        stored = await db.fetch_one(
            "SELECT COUNT(*) AS count FROM ontology_entities WHERE room_id = ?", (room_id,)
        )
        assert stored is not None and int(stored["count"]) > 0
        member = await service.answer_decision_meta(room_id, "what is the status", user_id="viewer")
        assert member["status"] == "ANSWERED" and member["claims"]

        answer = await service.answer_decision_meta(room_id, "what is the status", user_id=reader)
        assert answer["claims"] == []
        assert answer["unconfirmed"] == []
        assert answer["status"] == "REFUSED"
        assert answer["refusal_reason"] == "NO_AUTHORIZED_EVIDENCE"
        # No head, no drain lag, no unread figure: a refusal carries no aggregate.
        assert answer["freshness"] == {}
        assert answer["counts"] == {
            "claims": 0,
            "unconfirmed": 0,
            "current_claims": 0,
            "max_claims": 10,
        }
        assert "authorized_head" not in str(answer)
        assert "drain_lag_events" not in str(answer)
        # Nor any label out of the room the reader may not see.
        for claim in member["claims"]:
            assert claim["label"] not in str(answer)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_every_meta_query_carries_the_membership_join_and_role_predicate() -> None:
    db = Database(":memory:")
    await db.connect()
    service = MultiplayerService(db, RealtimeHub(), known_users=KNOWN)
    try:
        await service.initialize()
        room_id, _sibling_id = await _seed(service)
        recorder = _RecordingDatabase(db)
        service.repos.meta.db = recorder  # type: ignore[assignment]
        for kind in MetaQuestionKind:
            question = next(form for form, mapped in ACCEPTED_QUESTIONS.items() if mapped is kind)
            await service.answer_decision_meta(room_id, question, user_id="viewer")
        roles = ", ".join("?" for _ in roles_with_capability(RoomCapability.READ))
        assert recorder.statements
        for statement in recorder.statements:
            assert "JOIN room_members m ON m.room_id" in statement
            assert f"m.user_id = ? AND m.role IN ({roles})" in statement
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_no_public_meta_repository_method_returns_rows_to_a_non_member() -> None:
    """Every method on the layer, with no exemption list, and the list cannot go stale."""
    db = Database(":memory:")
    await db.connect()
    service = MultiplayerService(db, RealtimeHub(), known_users=KNOWN)
    try:
        await service.initialize()
        room_id, _sibling_id = await _seed(service)
        meta = service.repos.meta
        entity_ids = [
            entity.entity_id for entity in await service.repos.ontology.list_entities(room_id)
        ]
        assert entity_ids
        exercised = {
            "head": await meta.head(room_id, "outsider"),
            "entities": await meta.entities(
                room_id, "outsider", (OntologyEntityKind.TASK, OntologyEntityKind.DECISION)
            ),
            "relationships": await meta.relationships(
                room_id, "outsider", (OntologyRelationshipKind.BLOCKS,)
            ),
            "entities_by_ids": await meta.entities_by_ids(room_id, "outsider", entity_ids),
            "entity_by_source": await meta.entity_by_source(
                room_id, "outsider", OntologyEntityKind.TASK, "anything"
            ),
            "relationship_between": await meta.relationship_between(
                room_id, "outsider", entity_ids[0], entity_ids[-1]
            ),
            "latest_review": await meta.latest_review(room_id, "outsider", entity_ids[0]),
            "invalidating_sequences": await meta.invalidating_sequences(
                room_id, "outsider", ("task.created",), 0, 1000
            ),
            "extraction_cursors": await meta.extraction_cursors(room_id, "outsider"),
        }
        public = {
            name
            for name in vars(MetaRepo)
            if not name.startswith("_") and callable(getattr(MetaRepo, name))
        }
        assert public == set(exercised)
        for name, result in exercised.items():
            assert not result, name
    finally:
        await db.close()


def test_agent_without_retrieval_may_not_read_context_on_a_members_behalf() -> None:
    """RoomCapability gates the human; the five-way intersection gates the agent."""
    terms = CapabilityTerms(
        user=user_capabilities("editor"),
        agent=CAPABILITIES,
        skill=CAPABILITIES - {"retrieval"},
        channel=policy_capabilities(None),
        workspace=policy_capabilities(None),
    )
    assert "retrieval" not in terms.effective
    assert "channel.read_context" not in allowed_tools(terms.effective)
    decision = decide("channel.read_context", terms.effective)
    assert decision.allowed is False
    assert decision.required_capability == "retrieval"
