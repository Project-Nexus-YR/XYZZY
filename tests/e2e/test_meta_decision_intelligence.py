"""Acceptance proof for bounded, governed, evidence-backed Meta answers."""

from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from multiplayer.api import routes
from multiplayer.db.connection import Database
from multiplayer.domain.events import EventType
from multiplayer.domain.meta import (
    ACCEPTED_QUESTIONS,
    DECISION_KINDS,
    REFUSAL_PREFIX,
    MetaQuestionKind,
    _bears_surveillance_marker,
    classify_meta_question,
    normalize_question,
)
from multiplayer.domain.models import (
    DecisionStatus,
    DomainError,
    MessageRole,
    OntologyEntityKind,
    OntologyExtractor,
    OntologyReviewAction,
)
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.server import create_app
from multiplayer.services.service import MultiplayerService

OWNER = {"Authorization": "Bearer owner-token"}
VIEWER = {"Authorization": "Bearer viewer-token"}
OUTSIDER = {"Authorization": "Bearer outsider-token"}


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("why", "WHY_DECISION"),
        ("why_decision", "WHY_DECISION"),
        ("Why was this decision made?", "WHY_DECISION"),
        ("What is the reason for this decision?", "WHY_DECISION"),
        ("evidence", "DECISION_EVIDENCE"),
        ("decision_evidence", "DECISION_EVIDENCE"),
        ("What evidence supports this decision?", "DECISION_EVIDENCE"),
        ("Show supporting evidence", "DECISION_EVIDENCE"),
    ],
)
def test_meta_accepts_only_explicit_decision_query_grammar(question: str, expected: str) -> None:
    assert MultiplayerService._meta_question_kind(question) == expected


@pytest.mark.parametrize(
    "question",
    [
        "Who made the most commits?",
        "Show source code productivity rankings",
        "Rank employees by activity",
        "Who worked hardest?",
        "What source code changed?",
        "Give me evidence about team performance",
        "Why did Alice make fewer commits?",
        "Show proof of individual productivity",
        "What supports the ranking?",
        "Tell me the reason",
        "Show all sources",
    ],
)
def test_meta_rejects_productivity_and_ambiguous_adjacent_queries(question: str) -> None:
    with pytest.raises(DomainError, match="unsupported Meta question"):
        MultiplayerService._meta_question_kind(question)


def test_every_kind_has_at_least_one_accepted_form() -> None:
    assert set(ACCEPTED_QUESTIONS.values()) == set(MetaQuestionKind)


@pytest.mark.parametrize("form", sorted(ACCEPTED_QUESTIONS))
def test_every_accepted_form_survives_the_refusal_pass(form: str) -> None:
    """The two layers agree, and markers match whole words rather than substrings."""
    assert not _bears_surveillance_marker(form)
    assert classify_meta_question(form) is ACCEPTED_QUESTIONS[form]


def test_no_accepted_form_of_one_kind_resolves_to_another() -> None:
    """A cross-product, so a new form cannot silently widen a neighbouring kind."""
    by_kind: dict[MetaQuestionKind, set[str]] = {}
    for form, kind in ACCEPTED_QUESTIONS.items():
        by_kind.setdefault(kind, set()).add(form)
    for kind, forms in by_kind.items():
        for other_kind, other_forms in by_kind.items():
            if other_kind is kind:
                continue
            assert not forms & other_forms
            for form in other_forms:
                assert classify_meta_question(form) is not kind


def test_refusal_pass_is_not_shadowed_by_the_exact_match_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A question that is both marker-bearing and an exact accepted form still refuses."""
    laundered = "who closed the most blockers"
    monkeypatch.setitem(ACCEPTED_QUESTIONS, laundered, MetaQuestionKind.BLOCKERS)
    assert ACCEPTED_QUESTIONS[laundered] is MetaQuestionKind.BLOCKERS
    assert _bears_surveillance_marker(laundered)
    with pytest.raises(DomainError, match="unsupported Meta question"):
        classify_meta_question(laundered)


@pytest.mark.parametrize(
    "written",
    [
        "  WHY   was   this DECISION   made ??? ",
        "Why Was This Decision Made.",
        "why was this decision made!",
        "\twhy was this decision made\n",
    ],
)
def test_normalization_is_stable_across_whitespace_case_and_punctuation(written: str) -> None:
    assert normalize_question(written) == "why was this decision made"
    assert classify_meta_question(written) is MetaQuestionKind.WHY_DECISION


@pytest.mark.parametrize(
    ("written", "normalized"),
    [
        ("What's the status?", "what is the status"),
        ("What’s the status", "what is the status"),
        ("Where's the disagreement", "where is the disagreement"),
        ("WHAT-IS-THE-STATUS", "what is the status"),
        ("what is the team's status", "what is the teams status"),
        ("Aren't there any blockers?", "are not there any blockers"),
    ],
)
def test_normalization_folds_contractions_apostrophes_and_punctuation(
    written: str, normalized: str
) -> None:
    """One question written two ways is one key; two questions never become one."""
    assert normalize_question(written) == normalized


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What's the status?", MetaQuestionKind.STATUS),
        ("How are things going?", MetaQuestionKind.STATUS),
        ("Give me a status update", MetaQuestionKind.STATUS),
        ("What's blocking us?", MetaQuestionKind.BLOCKERS),
        ("Are there any blockers?", MetaQuestionKind.BLOCKERS),
        ("What's in the way?", MetaQuestionKind.BLOCKERS),
        ("What's new?", MetaQuestionKind.CHANGES),
        ("What's changed lately?", MetaQuestionKind.CHANGES),
        ("Any updates?", MetaQuestionKind.CHANGES),
        ("What decisions are pending?", MetaQuestionKind.DECISIONS_OPEN),
        ("What do we need to decide?", MetaQuestionKind.DECISIONS_OPEN),
        ("What has been decided?", MetaQuestionKind.DECISIONS_MADE),
        ("What are we disagreeing about?", MetaQuestionKind.DISAGREEMENT),
        ("Where do we disagree?", MetaQuestionKind.DISAGREEMENT),
    ],
)
def test_meta_resolves_the_ordinary_phrasing_of_each_supported_kind(
    question: str, expected: MetaQuestionKind
) -> None:
    """Thirteen phrasings a person actually types, every one of them refused before."""
    assert classify_meta_question(question) is expected


def test_every_accepted_form_is_already_normalized() -> None:
    """A key normalization would rewrite is a key no question can ever reach."""
    assert [form for form in ACCEPTED_QUESTIONS if normalize_question(form) != form] == []


def test_no_accepted_form_survives_a_surveillance_marker() -> None:
    """Widening the corpus cannot launder a productivity question into an answer."""
    for form in ACCEPTED_QUESTIONS:
        for marker in ("ranked by person", "by productivity", "for the top 5"):
            with pytest.raises(DomainError, match="unsupported Meta question"):
                classify_meta_question(f"{form} {marker}")


def test_an_off_corpus_refusal_says_what_meta_can_answer() -> None:
    """Useful enough to rephrase from, without publishing the corpus or the question."""
    messages = set()
    for question in ("what is the weather", "give me the payroll numbers", "read the codebase"):
        with pytest.raises(DomainError) as raised:
            classify_meta_question(question)
        assert question not in str(raised.value)
        messages.add(str(raised.value))
    # One constant refusal, so it cannot leak which form a question nearly matched.
    assert len(messages) == 1
    message = messages.pop()
    assert message.startswith(REFUSAL_PREFIX)
    for subject in ("stand", "blocked", "changed", "decisions", "disagreement", "evidence"):
        assert subject in message


@pytest.mark.parametrize(
    "question",
    [
        # "status, who worked the most?" — the trailing clause is the whole question,
        # and stripping it left the accepted key `status`.
        "status 誰が一番多く働いたか",
        "статус кто больше всех работал",
        "الحالة من عمل أكثر",
        # A refusal is not conditional on the surveillance clause: anything the
        # normalizer cannot read refuses whole.
        "what is the status 状況",
        "où en sommes-nous",
    ],
)
def test_a_question_the_normalizer_cannot_read_refuses_whole(question: str) -> None:
    """Deleting the unreadable part answers a question nobody asked."""
    with pytest.raises(DomainError, match=REFUSAL_PREFIX):
        normalize_question(question)
    with pytest.raises(DomainError, match=REFUSAL_PREFIX):
        classify_meta_question(question)


def test_naming_a_kind_reaches_every_supported_question() -> None:
    """The corpus is a convenience; the enum is the interface, and it is closed."""
    for kind in MetaQuestionKind:
        assert MultiplayerService._resolve_meta_kind(None, kind) is kind
        # Free text alongside a named kind is recorded, never parsed, so it cannot
        # redirect the answer — including free text this workspace would refuse.
        assert MultiplayerService._resolve_meta_kind("who worked hardest", kind) is kind
    with pytest.raises(DomainError, match=REFUSAL_PREFIX):
        MultiplayerService._resolve_meta_kind(None, None)


def test_free_text_kept_for_audit_is_bounded_and_stripped() -> None:
    """It decides nothing, but it is attacker-chosen and it lands in a durable record."""
    assert MultiplayerService._audit_question(None) is None
    assert MultiplayerService._audit_question("what\x00 is\r\n the status") == "what is the status"
    assert len(MultiplayerService._audit_question("x" * 5000) or "") == 500


async def _seed_assertion_room(service: MultiplayerService) -> str:
    """A room carrying one governed assertion for each of the five new kinds."""
    org = await service.create_organization("Meta org", "meta-kinds-org", "owner")
    workspace = await service.create_workspace(org.org_id, "Engineering", "meta-kinds", "owner")
    room = await service.create_room(workspace.workspace_id, "Kinds", "owner")
    await service.invite_room_member(room.room_id, "viewer", "viewer", "owner")
    await service.create_task(room.room_id, "Ship the gateway", created_by="owner")
    await service.create_task(room.room_id, "Rotate the keys", created_by="owner")
    await service.create_decision(room.room_id, "Adopt the gateway", "content", created_by="owner")
    # One decision still open and one already taken, so the two decision kinds are
    # answered from different rows rather than from one shared payload.
    settled = await service.create_decision(
        room.room_id, "Keep the current gateway", "content", created_by="owner"
    )
    # Through the service verb a room actually has, not the repository beneath it:
    # staging a made decision by hand proved a lifecycle the product did not ship.
    await service.update_decision_status(
        settled.decision_id, DecisionStatus.ACTIVE, reviewed_by="owner", require_member=True
    )
    await service.run_ontology_extraction(room.room_id, OntologyExtractor.IMMEDIATE)
    await service.send_message(
        room.room_id,
        MessageRole.HUMAN,
        "owner",
        "Ship the gateway is blocked by Rotate the keys",
    )
    await service.run_ontology_extraction(room.room_id, OntologyExtractor.ASYNC)
    blocks = [
        item
        for item in await service.repos.ontology.list_relationships(room.room_id)
        if item.kind.value == "BLOCKS"
    ]
    assert len(blocks) == 1
    await service.review_ontology_relationship(
        room.room_id,
        blocks[0].relationship_id,
        OntologyReviewAction.CONFIRM,
        "owner",
        "Confirmed against the message that reported it.",
    )
    return room.room_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("question", "kind"),
    [
        ("what is the status", "STATUS"),
        ("what is blocking", "BLOCKERS"),
        ("what changed", "CHANGES"),
        ("what decisions are pending", "DECISIONS_OPEN"),
        ("what has been decided", "DECISIONS_MADE"),
    ],
)
async def test_each_new_kind_answers_from_governed_assertions(question: str, kind: str) -> None:
    db = Database(":memory:")
    await db.connect()
    service = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner", "viewer"}))
    try:
        await service.initialize()
        room_id = await _seed_assertion_room(service)
        answer = await service.answer_decision_meta(room_id, question, user_id="viewer")
        assert answer["query"]["kind"] == kind
        assert answer["status"] == "ANSWERED"
        assert answer["refusal_reason"] is None
        assert answer["claims"]
        assert answer["counts"]["claims"] == len(answer["claims"])
        assert answer["freshness"]["authorized_head"] > 0
        for claim in answer["claims"]:
            assert claim["source_object_id"]
            assert not (
                claim["derivation_kind"] == "AI_DERIVED" and claim["review_status"] == "UNCONFIRMED"
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_opposite_decision_questions_never_share_a_payload() -> None:
    """One kind served both, so "what is undecided" answered with what was decided."""
    db = Database(":memory:")
    await db.connect()
    service = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner", "viewer"}))
    try:
        await service.initialize()
        room_id = await _seed_assertion_room(service)
        undecided = await service.answer_decision_meta(
            room_id, "what is undecided", user_id="viewer"
        )
        decided = await service.answer_decision_meta(
            room_id, "what has been decided", user_id="viewer"
        )
        assert undecided["query"]["kind"] == "DECISIONS_OPEN"
        assert decided["query"]["kind"] == "DECISIONS_MADE"
        assert undecided != decided

        def labels(answer: dict[str, Any]) -> set[str]:
            return {
                str(claim["label"])
                for claim in [*answer["claims"], *answer["unconfirmed"]]
                if claim["kind"] == "Decision"
            }

        assert labels(undecided) == {"Adopt the gateway"}
        assert labels(decided) == {"Keep the current gateway"}
        assert not labels(undecided) & labels(decided)
        assert undecided["summary"] != decided["summary"]
        # Naming either kind reaches the same two payloads without a phrasing.
        by_kind = await service.answer_decision_meta(
            room_id, kind=MetaQuestionKind.DECISIONS_MADE, user_id="viewer"
        )
        assert by_kind["claims"] == decided["claims"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_seeded_empty_room_refuses_with_a_reason_and_writes_nothing() -> None:
    db = Database(":memory:")
    await db.connect()
    service = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner"}))
    try:
        await service.initialize()
        org = await service.create_organization("Empty org", "empty-meta-org", "owner")
        workspace = await service.create_workspace(org.org_id, "Engineering", "empty-meta", "owner")
        room = await service.create_room(workspace.workspace_id, "Empty", "owner")
        before = await service.get_room_events(room.room_id)
        answer = await service.answer_decision_meta(
            room.room_id, "what is blocking", user_id="owner"
        )
        assert answer["status"] == "REFUSED"
        assert answer["refusal_reason"] == "NO_ASSERTIONS_IN_SCOPE"
        assert answer["claims"] == [] and answer["unconfirmed"] == []
        # A Meta read has nothing to emit, because reads never write.
        assert await service.get_room_events(room.room_id) == before
    finally:
        await db.close()


async def _seed_proposed_decision(service: MultiplayerService) -> tuple[str, str]:
    """A room whose one decision is still proposed, already projected into the ontology."""
    org = await service.create_organization("Lifecycle org", "lifecycle-org", "owner")
    workspace = await service.create_workspace(org.org_id, "Engineering", "lifecycle", "owner")
    room = await service.create_room(workspace.workspace_id, "Lifecycle", "owner")
    await service.invite_room_member(room.room_id, "viewer", "viewer", "owner")
    decision = await service.create_decision(
        room.room_id, "Adopt the gateway", "content", created_by="owner"
    )
    await service.run_ontology_extraction(room.room_id, OntologyExtractor.IMMEDIATE)
    return room.room_id, decision.decision_id


def _decision_labels(answer: dict[str, Any]) -> set[str]:
    return {
        str(claim["label"])
        for claim in [*answer["claims"], *answer["unconfirmed"]]
        if claim["kind"] == "Decision"
    }


@pytest.mark.asyncio
async def test_a_decided_decision_leaves_the_open_list_and_joins_the_made_one() -> None:
    """The split was real in the query and meaningless in the data: nothing drained it."""
    db = Database(":memory:")
    await db.connect()
    service = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner", "viewer"}))
    try:
        await service.initialize()
        room_id, decision_id = await _seed_proposed_decision(service)

        async def kinds() -> tuple[set[str], set[str]]:
            return (
                _decision_labels(
                    await service.answer_decision_meta(
                        room_id, kind=MetaQuestionKind.DECISIONS_OPEN, user_id="viewer"
                    )
                ),
                _decision_labels(
                    await service.answer_decision_meta(
                        room_id, kind=MetaQuestionKind.DECISIONS_MADE, user_id="viewer"
                    )
                ),
            )

        still_open, already_made = await kinds()
        assert still_open == {"Adopt the gateway"}
        assert already_made == set()

        decided = await service.update_decision_status(
            decision_id, DecisionStatus.ACTIVE, reviewed_by="owner", require_member=True
        )
        assert decided.status is DecisionStatus.ACTIVE
        assert decided.reviewed_by == "owner"
        await service.run_ontology_extraction(room_id, OntologyExtractor.IMMEDIATE)

        still_open, already_made = await kinds()
        assert still_open == set()
        assert already_made == {"Adopt the gateway"}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_an_assertion_follows_the_decision_row_it_describes() -> None:
    """Extraction that cannot update an assertion whose source row moved goes stale."""
    db = Database(":memory:")
    await db.connect()
    service = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner", "viewer"}))
    try:
        await service.initialize()
        room_id, decision_id = await _seed_proposed_decision(service)

        async def assertion() -> dict[str, Any]:
            answer = await service.answer_decision_meta(
                room_id, kind=MetaQuestionKind.STATUS, user_id="viewer"
            )
            return next(claim for claim in answer["claims"] if claim["kind"] == "Decision")

        assert (await assertion())["properties"]["status"] == "PROPOSED"
        assert (await assertion())["current"] is True

        await service.update_decision_status(
            decision_id, DecisionStatus.ACTIVE, reviewed_by="owner", require_member=True
        )
        # The transition alone is enough to stop the old assertion reading as current.
        stale = await assertion()
        assert stale["properties"]["status"] == "PROPOSED"
        assert stale["current"] is False
        assert stale["invalidating_events"] >= 1

        result = await service.run_ontology_extraction(room_id, OntologyExtractor.IMMEDIATE)
        assert result["entities_written"] == 1
        followed = await assertion()
        assert followed["properties"]["status"] == "ACTIVE"
        assert followed["current"] is True
        # Re-asserting replaces the row's account of itself, not the events behind it.
        assert len(followed["evidence_event_sequences"]) == 2

        # A pass over an unmoved row still writes nothing.
        repeat = await service.run_ontology_extraction(room_id, OntologyExtractor.IMMEDIATE)
        assert repeat["entities_written"] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_confirmed_assertion_whose_row_moves_is_reconciled_not_skipped() -> None:
    """Two rules collided, and the collision was resolved by abandoning one of them.

    A reviewed assertion is a person's account and no later machine pass rewrites it.
    An assertion also has to follow the row it describes. Confirming the assertion and
    then moving the decision satisfied the first by dropping the second: the conflict
    clause declined, the cursor advanced past the event, and nothing looked again — so
    the open list said the decision was still open, the made list said no confirmed
    assertion answered, and the decisions route said active, permanently and with no
    caveat in the prose.

    Both rules hold here. The human's label and properties are untouched, the row's
    own account is disclosed beside them, and a status question is answered from the
    row. Nothing about the disagreement is stored: it is compared when the answer is
    built, so the pass that would have to clear it does not need to exist.
    """
    db = Database(":memory:")
    await db.connect()
    service = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner", "viewer"}))
    try:
        await service.initialize()
        room_id, decision_id = await _seed_proposed_decision(service)
        entity = next(
            item
            for item in await service.repos.ontology.list_entities(room_id)
            if item.source_object_id == decision_id
        )
        await service.review_ontology_entity(
            room_id,
            entity.entity_id,
            OntologyReviewAction.CONFIRM,
            "owner",
            "Checked against the thread that proposed it.",
        )
        await service.update_decision_status(
            decision_id, DecisionStatus.ACTIVE, reviewed_by="owner", require_member=True
        )
        result = await service.run_ontology_extraction(room_id, OntologyExtractor.IMMEDIATE)
        # Not rewritten, and not passed over in silence either.
        assert result["entities_written"] == 0
        assert result["reconciled"] == [entity.entity_id]
        assert EventType.ONTOLOGY_ASSERTION_RECONCILED.value in [
            event.event_type.value for event in await service.get_room_events(room_id)
        ]

        preserved = await service.repos.ontology.get_entity(entity.entity_id)
        assert preserved is not None
        assert preserved.label == entity.label
        assert preserved.properties == entity.properties
        assert preserved.properties["status"] == "PROPOSED"

        # Three surfaces, one answer: the row is active, so the decision has been
        # made, it is not on the open list, and the route agrees with both.
        row = await service.repos.decisions.get(decision_id)
        assert row is not None and row.status is DecisionStatus.ACTIVE
        open_answer = await service.answer_decision_meta(
            room_id, kind=MetaQuestionKind.DECISIONS_OPEN, user_id="viewer"
        )
        made_answer = await service.answer_decision_meta(
            room_id, kind=MetaQuestionKind.DECISIONS_MADE, user_id="viewer"
        )
        assert _decision_labels(open_answer) == set()
        assert _decision_labels(made_answer) == {"Adopt the gateway"}

        # The disagreement is visible to a reader, in the record and in the prose.
        claim = next(item for item in made_answer["claims"] if item["kind"] == "Decision")
        assert claim["review_status"] == "CONFIRMED"
        assert claim["properties"]["status"] == "PROPOSED"
        assert claim["source_disagreement"]["properties"]["status"] == "ACTIVE"
        assert claim["current"] is False
        assert "source record does not agree" in claim["text"]
        assert "contradicted by the source record" in made_answer["summary"]
        record = next(
            item
            for item in (await service.get_room_ontology(room_id))["entities"]
            if item["entity_id"] == entity.entity_id
        )
        assert record["source_disagreement"] == {
            "label": "Adopt the gateway",
            "properties": {"status": "ACTIVE", "decision_id": decision_id},
        }

        # A pass over a row that has not moved again reconciles nothing.
        assert (await service.run_ontology_extraction(room_id, OntologyExtractor.IMMEDIATE))[
            "reconciled"
        ] == []

        # And a review that accepts what the row says settles the disagreement,
        # because the two now compare equal — not because anything was cleared.
        corrected, _review = await service.review_ontology_entity(
            room_id,
            entity.entity_id,
            OntologyReviewAction.CORRECT,
            "owner",
            "The decision was taken; adopting the row's account.",
            corrected_properties=record["source_disagreement"]["properties"],
        )
        assert corrected.properties["status"] == "ACTIVE"
        settled = next(
            item
            for item in (await service.get_room_ontology(room_id))["entities"]
            if item["entity_id"] == entity.entity_id
        )
        assert settled["source_disagreement"] is None
        assert settled["properties"]["status"] == "ACTIVE"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_no_kind_returns_an_enumerable_per_person_work_list() -> None:
    """Naming a kind must not reach what phrasing a question cannot.

    `kind=STATUS` returned `owner OWNS <task>` for every task in the answer — a
    per-person work list, the shape the free-text pass refuses in aggregate and the
    shape this repository forbids outright. The rule now holds over what an answer
    may carry, so it cannot be reintroduced by pointing a kind at another edge.
    """
    db = Database(":memory:")
    await db.connect()
    service = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner", "viewer"}))
    try:
        await service.initialize()
        room_id = await _seed_assertion_room(service)
        people = {
            item.entity_id
            for item in await service.repos.ontology.list_entities(room_id)
            if item.kind is OntologyEntityKind.PERSON
        }
        attributions = [
            item
            for item in await service.repos.ontology.list_relationships(room_id)
            if item.from_entity_id in people or item.to_entity_id in people
        ]
        assert attributions, "the room holds no person-to-work edge, so nothing was withheld"

        withheld = {item.relationship_id for item in attributions}
        for kind in MetaQuestionKind:
            if kind in DECISION_KINDS:
                # Those two answer over a published decision artifact this room has
                # none of, and their chains carry no person endpoint to withhold.
                continue
            answer = await service.answer_decision_meta(room_id, kind=kind, user_id="viewer")
            returned = {
                str(item["assertion_id"]) for item in [*answer["claims"], *answer["unconfirmed"]]
            }
            assert not returned & withheld, f"{kind.value} enumerated one person's work"

        # The kind still answers; it is the per-person shape that is gone.
        status = await service.answer_decision_meta(
            room_id, kind=MetaQuestionKind.STATUS, user_id="viewer"
        )
        assert status["claims"], "STATUS answered with nothing, so the assertion proves nothing"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_an_invalid_decision_transition_is_refused() -> None:
    """A decision that could move anywhere is not a state machine."""
    db = Database(":memory:")
    await db.connect()
    service = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner", "viewer"}))
    try:
        await service.initialize()
        room_id, decision_id = await _seed_proposed_decision(service)
        before = await service.get_room_events(room_id)
        with pytest.raises(DomainError, match="invalid decision transition"):
            await service.update_decision_status(
                decision_id, DecisionStatus.SUPERSEDED, reviewed_by="owner", require_member=True
            )
        # A refused transition writes neither the row nor an event.
        decision = await service.repos.decisions.get(decision_id)
        assert decision is not None and decision.status is DecisionStatus.PROPOSED
        assert await service.get_room_events(room_id) == before

        await service.update_decision_status(
            decision_id, DecisionStatus.ACTIVE, reviewed_by="owner", require_member=True
        )
        for refused in (DecisionStatus.PROPOSED, DecisionStatus.REJECTED, DecisionStatus.ACTIVE):
            with pytest.raises(DomainError, match="invalid decision transition"):
                await service.update_decision_status(
                    decision_id, refused, reviewed_by="owner", require_member=True
                )
    finally:
        await db.close()


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
    result = client.post(
        f"/api/v1/executions/{execution['execution_id']}/step",
        headers=OWNER,
        json={"prompt": prompt},
    ).json()
    return str(result["output_id"])


def _seed_decision(client: TestClient) -> tuple[str, str, list[str]]:
    org_id = client.post(
        "/api/v1/organizations",
        headers=OWNER,
        json={"name": "Meta org", "slug": "meta-org"},
    ).json()["org_id"]
    workspace_id = client.post(
        f"/api/v1/organizations/{org_id}/workspaces",
        headers=OWNER,
        json={"name": "Engineering", "slug": "engineering"},
    ).json()["workspace_id"]
    room_id = client.post(
        f"/api/v1/workspaces/{workspace_id}/rooms",
        headers=OWNER,
        json={"name": "Identity decision"},
    ).json()["room_id"]
    client.post(
        f"/api/v1/rooms/{room_id}/members/invitations",
        headers=OWNER,
        json={"user_id": "viewer", "role": "viewer"},
    )
    templates = client.get("/api/v1/agent-templates", headers=OWNER).json()
    output_ids = [
        _seed_output(client, room_id, template["template_id"], prompt)
        for template, prompt in zip(
            templates[:3],
            ("architecture evidence", "security evidence", "excluded evidence"),
            strict=True,
        )
    ]
    for output_id, disposition in zip(
        output_ids, ("INCLUDED", "INCLUDED", "EXCLUDED"), strict=True
    ):
        response = client.put(
            f"/api/v1/rooms/{room_id}/output-selections/{output_id}",
            headers=OWNER,
            json={"disposition": disposition},
        )
        assert response.status_code == 200
    publication = client.post(
        f"/api/v1/rooms/{room_id}/syntheses/decision-brief",
        headers=OWNER,
        json={"title": "Adopt the managed identity provider"},
    )
    assert publication.status_code == 200, publication.text
    return room_id, workspace_id, [publication.json()["version_id"], *output_ids]


def _ask(
    client: TestClient,
    room_id: str,
    question: str,
    *,
    headers: dict[str, str] = VIEWER,
    limit: int = 10,
    version_id: str | None = None,
) -> Any:
    path = f"/api/v1/rooms/{room_id}/meta?question={quote(question)}&limit={limit}"
    if version_id is not None:
        path += f"&version_id={quote(version_id)}"
    return client.get(path, headers=headers)


def test_meta_returns_only_selected_bounded_room_evidence_and_governed_corrections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(
        ":memory:",
        auth_tokens={
            "owner-token": "owner",
            "viewer-token": "viewer",
            "outsider-token": "outsider",
        },
    )
    with TestClient(app) as client:
        room_id, workspace_id, identifiers = _seed_decision(client)
        version_id, included_one, included_two, excluded = identifiers

        why = _ask(client, room_id, "Why was this decision made?", limit=1)
        assert why.status_code == 200, why.text
        answer = why.json()
        assert answer["query"]["kind"] == "WHY_DECISION"
        assert answer["scope"] == {
            "room_id": room_id,
            "artifact_id": answer["scope"]["artifact_id"],
            "version_id": version_id,
            "version_number": 1,
            "max_claims": 1,
        }
        assert answer["retrieval_counts"] == {
            "available_claims": 2,
            "returned_claims": 1,
            "returned_outputs": 1,
            "truncated": True,
        }
        assert len(answer["evidence_chains"]) == 1
        chain = answer["evidence_chains"][0]
        assert chain["exact_source_evidence"]["output_id"] == included_one
        assert chain["exact_source_evidence"]["source_prompt"] == "architecture evidence"
        assert chain["claim"]["derivation_kind"] == "AI_DERIVED"
        assert chain["claim"]["review_status"] == "UNCONFIRMED"
        assert excluded not in str(answer)
        assert included_two not in str(answer)
        assert answer["freshness"]["authorized_head"] > 0
        assert answer["provenance"]["verified"] is None

        evidence = _ask(client, room_id, "What evidence supports this decision?")
        assert evidence.status_code == 200
        evidence_answer = evidence.json()
        assert evidence_answer["query"]["kind"] == "DECISION_EVIDENCE"
        assert {
            item["exact_source_evidence"]["output_id"]
            for item in evidence_answer["evidence_chains"]
        } == {included_one, included_two}
        assert excluded not in str(evidence_answer)
        assert evidence_answer["provenance"]["verified"] is True

        decision_id = evidence_answer["decision"]["entity_id"]
        claim_id = evidence_answer["evidence_chains"][0]["claim"]["entity_id"]
        support_link_id = evidence_answer["evidence_chains"][0]["relationships"][
            "claim_to_decision"
        ]["relationship_id"]
        corrected_decision = client.post(
            f"/api/v1/rooms/{room_id}/ontology/entities/{decision_id}/reviews",
            headers=OWNER,
            json={
                "action": "CORRECT",
                "corrected_label": "Adopt only after a staged rollout",
                "corrected_confidence": 0.75,
                "reason": "Selected evidence requires staged deployment.",
            },
        )
        assert corrected_decision.status_code == 200
        confirmed_claim = client.post(
            f"/api/v1/rooms/{room_id}/ontology/entities/{claim_id}/reviews",
            headers=OWNER,
            json={"action": "CONFIRM", "reason": "Validated against the frozen output."},
        )
        assert confirmed_claim.status_code == 200
        corrected_link = client.post(
            f"/api/v1/rooms/{room_id}/ontology/relationships/{support_link_id}/reviews",
            headers=OWNER,
            json={
                "action": "CORRECT",
                "corrected_kind": "CONTRADICTS",
                "corrected_confidence": 0.65,
                "reason": "The evidence now cuts against the proposed timing.",
            },
        )
        assert corrected_link.status_code == 200

        governed = _ask(client, room_id, "why", version_id=version_id).json()
        assert "Adopt only after a staged rollout" in governed["summary"]
        assert "CONTRADICTS 1" in governed["summary"]
        assert governed["decision"]["review_status"] == "CORRECTED"
        assert governed["decision"]["confidence"] == 0.75
        assert governed["decision"]["latest_review"]["reason"] == (
            "Selected evidence requires staged deployment."
        )
        governed_claim = next(
            item["claim"]
            for item in governed["evidence_chains"]
            if item["claim"]["entity_id"] == claim_id
        )
        assert governed_claim["review_status"] == "CONFIRMED"
        assert governed_claim["latest_review"]["action"] == "CONFIRM"
        governed_link = next(
            item["relationships"]["claim_to_decision"]
            for item in governed["evidence_chains"]
            if item["claim"]["entity_id"] == claim_id
        )
        assert governed_link["kind"] == "CONTRADICTS"
        assert governed_link["review_status"] == "CORRECTED"
        assert governed_link["latest_review"]["reason"] == (
            "The evidence now cuts against the proposed timing."
        )

        other_room = client.post(
            f"/api/v1/workspaces/{workspace_id}/rooms",
            headers=OWNER,
            json={"name": "Other room"},
        ).json()["room_id"]
        assert (
            _ask(client, other_room, "why", headers=OWNER, version_id=version_id).status_code == 404
        )
        assert _ask(client, room_id, "why", headers=OUTSIDER).status_code == 403
        assert client.get(f"/api/v1/rooms/{room_id}/meta?question=why").status_code == 401
        assert _ask(client, room_id, "why", limit=11).status_code == 422

        events_before_rejections = client.get(
            f"/api/v1/rooms/{room_id}/events", headers=OWNER
        ).json()

        async def forbidden_evidence_retrieval(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("invalid Meta query reached evidence retrieval")

        service = routes._svc
        assert service is not None
        monkeypatch.setattr(
            service.repos.artifacts,
            "resolve_decision_version",
            forbidden_evidence_retrieval,
        )
        invalid_queries = (
            "Who made the most commits?",
            "Show source code productivity rankings",
            "Rank employees by activity",
            "Who worked hardest?",
            "Give me evidence about team performance",
            "Why did Alice make fewer commits?",
            "What supports the ranking?",
            "Tell me the reason",
        )
        for invalid_query in invalid_queries:
            rejected = _ask(client, room_id, invalid_query)
            assert rejected.status_code == 400
            assert "evidence_chains" not in rejected.text
        assert (
            client.get(f"/api/v1/rooms/{room_id}/events", headers=OWNER).json()
            == events_before_rejections
        )


def test_every_kind_is_reachable_by_naming_it_over_the_route() -> None:
    """The ordinary phrasings that refuse no longer cost the capability behind them."""
    app = create_app(
        ":memory:",
        auth_tokens={
            "owner-token": "owner",
            "viewer-token": "viewer",
            "outsider-token": "outsider",
        },
    )
    with TestClient(app) as client:
        room_id, _workspace_id, _identifiers = _seed_decision(client)
        for kind in MetaQuestionKind:
            named = client.get(
                f"/api/v1/rooms/{room_id}/meta?kind={kind.value.lower()}&limit=10", headers=VIEWER
            )
            assert named.status_code == 200, named.text
            assert named.json()["query"]["kind"] == kind.value

        # Ordinary phrasings this workspace does not recognize: the free text still
        # refuses, and the kind behind it still answers.
        for refused, kind in (
            ("What's our status?", MetaQuestionKind.STATUS),
            ("Are we blocked on anything?", MetaQuestionKind.BLOCKERS),
            ("Anything new since Tuesday?", MetaQuestionKind.CHANGES),
        ):
            assert _ask(client, room_id, refused).status_code == 400
            answered = client.get(
                f"/api/v1/rooms/{room_id}/meta?kind={kind.value}"
                f"&question={quote(refused)}&limit=10",
                headers=VIEWER,
            )
            assert answered.status_code == 200, answered.text
            # Recorded verbatim for audit, and it decided nothing.
            assert answered.json()["query"] == {
                "question": refused,
                "kind": kind.value,
                "supported_kinds": [member.value for member in MetaQuestionKind],
            }

        # Neither a kind nor a question is not a question, and an invented kind is
        # not a kind.
        assert client.get(f"/api/v1/rooms/{room_id}/meta", headers=VIEWER).status_code == 400
        assert (
            client.get(
                f"/api/v1/rooms/{room_id}/meta?kind=PRODUCTIVITY", headers=VIEWER
            ).status_code
            == 400
        )
        assert client.get(f"/api/v1/rooms/{room_id}/meta?kind=STATUS").status_code == 401
        assert (
            client.get(f"/api/v1/rooms/{room_id}/meta?kind=STATUS", headers=OUTSIDER).status_code
            == 403
        )


def test_one_meta_answer_gives_one_account_of_each_assertion_currency() -> None:
    """One response called an assertion stale in claims[] and current in its own chain."""
    app = create_app(
        ":memory:",
        auth_tokens={"owner-token": "owner", "viewer-token": "viewer"},
    )
    with TestClient(app) as client:
        room_id, _workspace_id, identifiers = _seed_decision(client)
        version_id = identifiers[0]
        # A decision taken after publication invalidates the brief's assertion, so
        # the answer has something to be consistent about.
        proposed = client.post(
            f"/api/v1/rooms/{room_id}/decisions",
            headers=OWNER,
            json={"title": "Revisit the identity provider", "content": "content"},
        )
        assert proposed.status_code == 200, proposed.text
        moved = client.post(
            f"/api/v1/decisions/{proposed.json()['decision_id']}/status",
            headers=OWNER,
            json={"status": "ACTIVE"},
        )
        assert moved.status_code == 200, moved.text

        response = _ask(client, room_id, "Why was this decision made?", version_id=version_id)
        assert response.status_code == 200, response.text
        answer = response.json()

        def currency(record: dict[str, Any]) -> tuple[bool, int]:
            assert isinstance(record["current"], bool), record
            assert isinstance(record["invalidating_events"], int), record
            return record["current"], record["invalidating_events"]

        listed = {
            record["assertion_id"]: (record["current"], record["invalidating_events"])
            for record in [*answer["claims"], *answer["unconfirmed"]]
        }
        decision = answer["decision"]
        assert currency(decision) == listed[decision["entity_id"]]
        assert decision["current"] is False
        assert answer["evidence_chains"]
        for chain in answer["evidence_chains"]:
            claim = chain["claim"]
            link = chain["relationships"]["claim_to_decision"]
            assert currency(claim) == listed[claim["entity_id"]]
            assert currency(link) == listed[link["relationship_id"]]
            # Currency is per assertion, not per answer: the edge into the decision
            # moved with it while the claim behind the edge did not.
            assert link["current"] is False
            assert claim["current"] is True
            # Named only inside the chain, so the chain is where they must carry it.
            currency(chain["agent_output"])
            currency(chain["relationships"]["claim_to_agent_output"])


def test_browser_meta_contract_exposes_scope_freshness_and_drilldown() -> None:
    ui = (Path(__file__).parents[2] / "web" / "index.html").read_text(encoding="utf-8")
    assert 'data-center-view="meta"' in ui
    assert 'id="meta-question"' in ui
    assert 'id="meta-scope"' in ui
    assert 'id="meta-answer"' in ui
    assert 'id="meta-evidence"' in ui
    assert 'onclick="askMeta(' in ui
    assert 'id="meta-kinds"' in ui
    # Every kind offered as a choice, so no supported question needs a phrasing.
    for kind in MetaQuestionKind:
        assert f"askMetaKind('{kind.value}')" in ui
    assert "rooms/${roomId}/meta" in ui
    assert "authorized_head" in ui
    assert "retrieval_counts" in ui
    assert "exact_source_evidence" in ui
    assert "provider_response_id" in ui
    assert "review_status" in ui


def _seed_room(client: TestClient, slug: str, name: str) -> str:
    org_id = client.post(
        "/api/v1/organizations",
        headers=OWNER,
        json={"name": name, "slug": f"{slug}-org"},
    ).json()["org_id"]
    workspace_id = client.post(
        f"/api/v1/organizations/{org_id}/workspaces",
        headers=OWNER,
        json={"name": "Engineering", "slug": slug},
    ).json()["workspace_id"]
    room_id = str(
        client.post(
            f"/api/v1/workspaces/{workspace_id}/rooms",
            headers=OWNER,
            json={"name": name},
        ).json()["room_id"]
    )
    client.post(
        f"/api/v1/rooms/{room_id}/members/invitations",
        headers=OWNER,
        json={"user_id": "viewer", "role": "viewer"},
    )
    return room_id


def _extract(client: TestClient, room_id: str) -> Any:
    response = client.post(
        f"/api/v1/rooms/{room_id}/ontology/extractions",
        headers=OWNER,
        json={"extractor": "IMMEDIATE"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _entity(client: TestClient, room_id: str, kind: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/rooms/{room_id}/ontology", headers=VIEWER)
    assert response.status_code == 200, response.text
    return next(item for item in response.json()["entities"] if item["kind"] == kind)


def _claim(client: TestClient, room_id: str, meta_kind: str, entity_kind: str) -> dict[str, Any]:
    response = client.get(f"/api/v1/rooms/{room_id}/meta?kind={meta_kind}", headers=VIEWER)
    assert response.status_code == 200, response.text
    answer = response.json()
    claim = next(item for item in answer["claims"] if item["kind"] == entity_kind)
    claim["_summary"] = answer["summary"]
    return claim


def _row(client: TestClient, room_id: str, task_id: str) -> dict[str, Any]:
    return next(
        item
        for item in client.get(f"/api/v1/rooms/{room_id}/tasks", headers=VIEWER).json()
        if item["task_id"] == task_id
    )


def _decision_bucket(client: TestClient, room_id: str, meta_kind: str) -> set[str]:
    response = client.get(f"/api/v1/rooms/{room_id}/meta?kind={meta_kind}", headers=VIEWER)
    assert response.status_code == 200, response.text
    answer = response.json()
    return {
        str(item["label"])
        for item in [*answer["claims"], *answer["unconfirmed"]]
        if item["kind"] == "Decision"
    }


def _a_task_the_row_contradicts(client: TestClient) -> tuple[str, str, str, str]:
    """A task a person has reviewed as cancelled, whose row then went to assigned.

    Every step is one of the product's own verbs over HTTP. A previous test in this
    file staged its premise past the service and passed against behaviour the product
    did not have.
    """
    room_id = _seed_room(client, "divergence", "Divergence")
    task_id = str(
        client.post(
            f"/api/v1/rooms/{room_id}/tasks",
            headers=OWNER,
            json={"title": "Ship the gateway"},
        ).json()["task_id"]
    )
    template_id = client.get("/api/v1/agent-templates", headers=OWNER).json()[0]["template_id"]
    agent_id = str(
        client.post(
            f"/api/v1/rooms/{room_id}/agents",
            headers=OWNER,
            json={"template_id": template_id},
        ).json()["agent_id"]
    )
    _extract(client, room_id)
    entity_id = str(_entity(client, room_id, "Task")["entity_id"])
    corrected = client.post(
        f"/api/v1/rooms/{room_id}/ontology/entities/{entity_id}/reviews",
        headers=OWNER,
        json={
            "action": "CORRECT",
            "reason": "Called off in the standup; it was this agent's to do.",
            "corrected_properties": {
                "status": "CANCELLED",
                "priority": "NORMAL",
                "assigned_agent_id": agent_id,
            },
        },
    )
    assert corrected.status_code == 200, corrected.text
    assigned = client.post(
        f"/api/v1/tasks/{task_id}/assign", headers=OWNER, json={"agent_id": agent_id}
    )
    assert assigned.status_code == 200, assigned.text
    # The pass that reads the moved row may not rewrite a person's account, and says
    # so in the log. That observation is all a pass leaves behind.
    assert _extract(client, room_id)["reconciled"] == [entity_id]
    assert _row(client, room_id, task_id)["status"] == "ASSIGNED"
    return room_id, task_id, entity_id, agent_id


def test_a_disagreement_never_dates_a_change_it_did_not_observe() -> None:
    """The disclosure states a comparison, because a comparison is all the code knows.

    It used to read "the source record has since changed". Correct only the
    assertion and the row is untouched - yet that sentence asserted an edit to the
    source that never happened. True disagreement, truthful status, invented
    history: the same family as the stored marker that outlived the disagreement it
    described.
    """
    app = create_app(":memory:", auth_tokens={"owner-token": "owner", "viewer-token": "viewer"})
    with TestClient(app) as client:
        room_id = _seed_room(client, "invented", "Invented history")
        task_id = str(
            client.post(
                f"/api/v1/rooms/{room_id}/tasks",
                headers=OWNER,
                json={"title": "Ship the gateway"},
            ).json()["task_id"]
        )
        _extract(client, room_id)
        entity_id = str(_entity(client, room_id, "Task")["entity_id"])
        before = _row(client, room_id, task_id)

        corrected = client.post(
            f"/api/v1/rooms/{room_id}/ontology/entities/{entity_id}/reviews",
            headers=OWNER,
            json={
                "action": "CORRECT",
                "reason": "The thread calls it the identity gateway.",
                "corrected_label": "Ship the identity gateway",
            },
        )
        assert corrected.status_code == 200, corrected.text

        # Only the assertion moved. The row is the same one we started with.
        assert _row(client, room_id, task_id) == before

        claim = _claim(client, room_id, "STATUS", "Task")
        assert claim["source_disagreement"] is not None, "they do differ, and that is disclosed"
        assert "does not agree" in claim["text"]
        assert "has since changed" not in claim["text"]
        assert "since" not in claim["text"], "no sentence may date a change it did not observe"


def test_a_standing_disagreement_is_disclosed_on_every_surface() -> None:
    """While the two accounts really differ, no surface may quietly pick one."""
    app = create_app(":memory:", auth_tokens={"owner-token": "owner", "viewer-token": "viewer"})
    with TestClient(app) as client:
        room_id, _task_id, entity_id, agent_id = _a_task_the_row_contradicts(client)
        row_account = {
            "label": "Ship the gateway",
            "properties": {
                "status": "ASSIGNED",
                "priority": "NORMAL",
                "assigned_agent_id": agent_id,
            },
        }

        claim = _claim(client, room_id, "STATUS", "Task")
        assert claim["review_status"] == "CORRECTED"
        assert claim["properties"]["status"] == "CANCELLED"
        assert claim["source_disagreement"] == row_account
        assert "source record does not agree" in claim["text"]
        assert "contradicted by the source record" in claim["_summary"]
        # The status it reports is the row's, and it says so rather than leaving a
        # reader to work out which of the two accounts they are looking at.
        assert claim["status"] == "ASSIGNED"
        assert claim["status_source"] == "SOURCE_ROW"

        record = _entity(client, room_id, "Task")
        assert record["entity_id"] == entity_id
        assert record["source_disagreement"] == row_account


def test_a_row_that_converges_back_leaves_no_trace_in_any_answer() -> None:
    """The disagreement must not outlive the disagreement.

    The row diverged from a reviewed assertion and then came back to it. Recorded, the
    marker survived the convergence: the only write ever added it, the pass that would
    have cleared it saw an unchanged row and skipped, and nothing short of another
    human review could take it away. Compared when the answer is built, there is
    nothing to take away.
    """
    app = create_app(":memory:", auth_tokens={"owner-token": "owner", "viewer-token": "viewer"})
    with TestClient(app) as client:
        room_id, task_id, entity_id, agent_id = _a_task_the_row_contradicts(client)
        assert _claim(client, room_id, "STATUS", "Task")["source_disagreement"] is not None

        cancelled = client.post(f"/api/v1/tasks/{task_id}/cancel", headers=OWNER)
        assert cancelled.status_code == 200, cancelled.text
        assert _row(client, room_id, task_id)["status"] == "CANCELLED"

        def assert_no_trace() -> None:
            claim = _claim(client, room_id, "STATUS", "Task")
            assert claim["source_disagreement"] is None
            assert claim["text"] == "Ship the gateway"
            assert "source record does not agree" not in claim["text"]
            assert "contradicted by the source record" not in claim["_summary"]
            assert _entity(client, room_id, "Task")["source_disagreement"] is None

        # No pass is needed to clear it, because there is nothing to clear.
        assert_no_trace()
        for index in range(30):
            client.post(
                f"/api/v1/rooms/{room_id}/messages",
                headers=OWNER,
                json={"content": f"unrelated note {index}"},
            )
        for _ in range(12):
            assert _extract(client, room_id)["reconciled"] == []
        assert_no_trace()
        # The person's account is still theirs, and still the one under review.
        record = _entity(client, room_id, "Task")
        assert record["entity_id"] == entity_id
        assert record["review_status"] == "CORRECTED"
        assert record["properties"]["assigned_agent_id"] == agent_id


def test_no_answer_reports_a_status_that_neither_source_holds() -> None:
    """A third value existed: the marker's, after both sources had left it behind."""
    app = create_app(":memory:", auth_tokens={"owner-token": "owner", "viewer-token": "viewer"})
    with TestClient(app) as client:
        room_id, task_id, _entity_id, _agent_id = _a_task_the_row_contradicts(client)

        def held() -> tuple[str, str, dict[str, Any]]:
            claim = _claim(client, room_id, "STATUS", "Task")
            return (
                str(_row(client, room_id, task_id)["status"]),
                str(claim["properties"]["status"]),
                claim,
            )

        # While they differ, the answer is grouped under one of the two.
        row_status, human_status, claim = held()
        assert (row_status, human_status) == ("ASSIGNED", "CANCELLED")
        assert "ASSIGNED 1" in claim["_summary"]

        cancelled = client.post(f"/api/v1/tasks/{task_id}/cancel", headers=OWNER)
        assert cancelled.status_code == 200, cancelled.text

        # Both sources now say cancelled, so nothing may be reported as assigned.
        row_status, human_status, claim = held()
        assert (row_status, human_status) == ("CANCELLED", "CANCELLED")
        assert "CANCELLED 1" in claim["_summary"]
        assert "ASSIGNED" not in claim["_summary"]
        assert "contradicted by the source record" not in claim["_summary"]

        # And the payload says which source the status it reports comes from.
        assert claim["status"] in {row_status, human_status}
        assert claim["status"] == "CANCELLED"
        assert claim["status_source"] == "SOURCE_ROW"


def test_the_bucket_a_decision_falls_into_follows_a_source_not_a_marker() -> None:
    """Open or made is a question about the decision row, asked when it is answered."""
    app = create_app(":memory:", auth_tokens={"owner-token": "owner", "viewer-token": "viewer"})
    with TestClient(app) as client:
        room_id = _seed_room(client, "buckets", "Buckets")
        decision_id = str(
            client.post(
                f"/api/v1/rooms/{room_id}/decisions",
                headers=OWNER,
                json={"title": "Adopt the gateway", "content": "content"},
            ).json()["decision_id"]
        )
        _extract(client, room_id)
        entity_id = str(_entity(client, room_id, "Decision")["entity_id"])
        # A person gets ahead of the row: they say it was taken, the row says proposed.
        corrected = client.post(
            f"/api/v1/rooms/{room_id}/ontology/entities/{entity_id}/reviews",
            headers=OWNER,
            json={
                "action": "CORRECT",
                "reason": "We took this call in the review; the row has not moved yet.",
                "corrected_properties": {"status": "ACTIVE", "decision_id": decision_id},
            },
        )
        assert corrected.status_code == 200, corrected.text

        # The row still says proposed, so the decision is still open — and the
        # person's account is disclosed against it rather than silently binned.
        assert _decision_bucket(client, room_id, "DECISIONS_OPEN") == {"Adopt the gateway"}
        assert _decision_bucket(client, room_id, "DECISIONS_MADE") == set()
        open_claim = _claim(client, room_id, "DECISIONS_OPEN", "Decision")
        assert open_claim["properties"]["status"] == "ACTIVE"
        assert open_claim["status"] == "PROPOSED"
        assert open_claim["status_source"] == "SOURCE_ROW"
        assert open_claim["source_disagreement"]["properties"]["status"] == "PROPOSED"

        # The row catches up. Both accounts now say the same thing, the bucket moves
        # with the row, and there is nothing left over to disclose.
        taken = client.post(
            f"/api/v1/decisions/{decision_id}/status", headers=OWNER, json={"status": "ACTIVE"}
        )
        assert taken.status_code == 200, taken.text
        assert _decision_bucket(client, room_id, "DECISIONS_OPEN") == set()
        assert _decision_bucket(client, room_id, "DECISIONS_MADE") == {"Adopt the gateway"}
        made_claim = _claim(client, room_id, "DECISIONS_MADE", "Decision")
        assert made_claim["status"] == "ACTIVE"
        assert made_claim["source_disagreement"] is None
        assert _entity(client, room_id, "Decision")["source_disagreement"] is None
