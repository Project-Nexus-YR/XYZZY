"""Acceptance proof for bounded, governed, evidence-backed Meta answers."""

from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from multiplayer.api import routes
from multiplayer.db.connection import Database
from multiplayer.domain.meta import (
    ACCEPTED_QUESTIONS,
    REFUSAL_PREFIX,
    MetaQuestionKind,
    _bears_surveillance_marker,
    classify_meta_question,
    normalize_question,
)
from multiplayer.domain.models import (
    DomainError,
    MessageRole,
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
        ("What decisions are pending?", MetaQuestionKind.DECISIONS),
        ("What do we need to decide?", MetaQuestionKind.DECISIONS),
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


async def _seed_assertion_room(service: MultiplayerService) -> str:
    """A room carrying one governed assertion for each of the five new kinds."""
    org = await service.create_organization("Meta org", "meta-kinds-org", "owner")
    workspace = await service.create_workspace(org.org_id, "Engineering", "meta-kinds", "owner")
    room = await service.create_room(workspace.workspace_id, "Kinds", "owner")
    await service.invite_room_member(room.room_id, "viewer", "viewer", "owner")
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
        ("what decisions require attention", "DECISIONS"),
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


def test_browser_meta_contract_exposes_scope_freshness_and_drilldown() -> None:
    ui = (Path(__file__).parents[2] / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="meta-panel"' in ui
    assert 'id="meta-question"' in ui
    assert 'id="meta-scope"' in ui
    assert 'id="meta-answer"' in ui
    assert 'id="meta-evidence"' in ui
    assert 'onclick="askMeta(' in ui
    assert "rooms/${roomId}/meta" in ui
    assert "authorized_head" in ui
    assert "retrieval_counts" in ui
    assert "exact_source_evidence" in ui
    assert "provider_response_id" in ui
    assert "review_status" in ui
