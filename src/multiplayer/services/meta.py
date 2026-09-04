"""Meta: answering a decision or assertion question against the ontology's currency."""

from __future__ import annotations

import logging
from typing import Any

from ..domain.meta import (
    DECISION_KINDS,
    REFUSAL_PREFIX,
    MetaAnswerStatus,
    MetaQuestionKind,
    MetaRefusalReason,
    OntologyAssurance,
    classify_meta_question,
    invalidation_class,
)
from ..domain.models import (
    DecisionStatus,
    DomainError,
    OntologyDerivationKind,
    OntologyEntity,
    OntologyEntityKind,
    OntologyExtractor,
    OntologyRelationship,
    OntologyRelationshipKind,
    OntologyReview,
    OntologyReviewStatus,
)
from ._shared import (
    _DISAGREEMENT_TEMPLATE,
    _MAX_AUDITED_QUESTION,
    _UNCONFIRMED_TEMPLATE,
    _SharedMixin,
)

log = logging.getLogger(__name__)


class _MetaMixin(_SharedMixin):
    """Mixin providing the meta surface of MultiplayerService."""

    _META_ENTITY_KINDS: dict[MetaQuestionKind, tuple[OntologyEntityKind, ...]] = {
        MetaQuestionKind.STATUS: (OntologyEntityKind.TASK, OntologyEntityKind.DECISION),
        # Scoped to work objects, never actors, so this query shape cannot become a
        # monitoring feed.
        MetaQuestionKind.CHANGES: (
            OntologyEntityKind.TASK,
            OntologyEntityKind.DECISION,
            OntologyEntityKind.ARTIFACT,
            OntologyEntityKind.CLAIM,
        ),
        MetaQuestionKind.DECISIONS_OPEN: (OntologyEntityKind.DECISION,),
        MetaQuestionKind.DECISIONS_MADE: (OntologyEntityKind.DECISION,),
    }
    # The two decision kinds ask the same entity kind opposite questions, so the
    # query, not the prose, is what separates them: a decision is open while it is
    # still proposed and made once it has been taken, superseded or rejected.
    _META_ENTITY_STATUSES: dict[MetaQuestionKind, tuple[str, ...]] = {
        MetaQuestionKind.DECISIONS_OPEN: (DecisionStatus.PROPOSED.value,),
        MetaQuestionKind.DECISIONS_MADE: (
            DecisionStatus.ACTIVE.value,
            DecisionStatus.SUPERSEDED.value,
            DecisionStatus.REJECTED.value,
        ),
    }
    # STATUS asked for OWNS and got `owner OWNS <task>` for every task in the answer,
    # which is one person's work list — the shape the free-text pass refuses in
    # aggregate. A kind may not reach what a phrasing cannot, so it is not asked for
    # here and `_meta_edge_in_scope` refuses it however it is asked for.
    _META_RELATIONSHIP_KINDS: dict[MetaQuestionKind, tuple[OntologyRelationshipKind, ...]] = {
        MetaQuestionKind.BLOCKERS: (OntologyRelationshipKind.BLOCKS,),
        MetaQuestionKind.DECISIONS_OPEN: (OntologyRelationshipKind.SUPPORTS,),
        MetaQuestionKind.DECISIONS_MADE: (OntologyRelationshipKind.SUPPORTS,),
        MetaQuestionKind.DISAGREEMENT: (OntologyRelationshipKind.CONTRADICTS,),
    }
    _DECISION_SCOPED_KINDS = frozenset(
        {
            MetaQuestionKind.STATUS,
            MetaQuestionKind.DECISIONS_OPEN,
            MetaQuestionKind.DECISIONS_MADE,
        }
    )
    _DISAGREEMENT_ENDPOINTS = frozenset({OntologyEntityKind.CLAIM, OntologyEntityKind.AGENT_OUTPUT})

    @staticmethod
    def _meta_question_kind(question: str) -> MetaQuestionKind:
        """Refuse first, match exactly second, refuse again otherwise."""
        return classify_meta_question(question)

    @staticmethod
    def _resolve_meta_kind(question: str | None, kind: MetaQuestionKind | None) -> MetaQuestionKind:
        """A named kind is taken as given; free text is matched exactly or refused.

        The enum is the closed set of things this workspace answers, so naming a
        kind cannot reach an activity, ranking or productivity figure — there is no
        such kind to name. Free text supplied alongside a kind is recorded, never
        parsed: it decides nothing, so it cannot decide wrongly.
        """
        if kind is not None:
            return kind
        if question is None:
            raise DomainError(
                f"{REFUSAL_PREFIX}; name a question kind or ask a question, and this asked neither"
            )
        return classify_meta_question(question)

    @staticmethod
    def _audit_question(question: str | None) -> str | None:
        """The copy of the free text that lands in the durable audit record.

        It decides nothing, but it is attacker-chosen and it is kept, so it is
        bounded to what the route already accepts and carries no character that
        could rewrite a line of whatever reads the record back.
        """
        if question is None:
            return None
        return "".join(character for character in question if character.isprintable())[
            :_MAX_AUDITED_QUESTION
        ]

    @staticmethod
    def _meta_assurance(
        derivation_kind: OntologyDerivationKind, review_status: OntologyReviewStatus
    ) -> OntologyAssurance:
        """What a reader is entitled to treat this assertion as."""
        if review_status is not OntologyReviewStatus.UNCONFIRMED:
            return OntologyAssurance.CONFIRMED
        if derivation_kind is OntologyDerivationKind.SYSTEM_MATERIALIZED:
            return OntologyAssurance.SYSTEM_MATERIALIZED
        return OntologyAssurance.UNCONFIRMED_AI

    @staticmethod
    def _source_disagreement(
        label: str,
        properties: dict[str, Any],
        review_status: OntologyReviewStatus,
        source_account: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """The row's account, disclosed only while it currently contradicts a reviewed one.

        A reviewed assertion is a person's account and no later pass rewrites it, so
        the two can come apart. Whether they are apart *now* is a question about the
        row as it stands, asked here, when the answer is built. Recorded instead, it
        outlived what it described: the pass in which the row converged back onto the
        person's account changed nothing, so it wrote nothing, and the marker stood.
        """
        if source_account is None or review_status is OntologyReviewStatus.UNCONFIRMED:
            return None
        if source_account["label"] == label and source_account["properties"] == properties:
            return None
        return source_account

    def _meta_claim_record(
        self,
        *,
        assertion_id: str,
        assertion_type: str,
        kind: str,
        label: str,
        properties: dict[str, Any],
        derivation_kind: OntologyDerivationKind,
        confidence: float,
        review_status: OntologyReviewStatus,
        evidence_ids: tuple[str, ...],
        source_object_kind: str,
        source_object_id: str,
        asserted_at_sequence: int,
        evidence_event_sequences: tuple[int, ...],
        stale_at_sequence: int | None,
        source_account: dict[str, Any] | None,
        currency: tuple[bool, int],
        review: OntologyReview | None,
    ) -> dict[str, Any]:
        assurance = self._meta_assurance(derivation_kind, review_status)
        current, invalidating = currency
        source_disagreement = self._source_disagreement(
            label, properties, review_status, source_account
        )
        # The status a reader is shown is one some source actually holds: the row's
        # while a row still states it, the assertion's when none does — named either
        # way, and never a third value assembled from a marker. Resolved once here,
        # so the prose, the counts and the payload cannot answer differently.
        held = properties if source_account is None else source_account["properties"]
        status = held.get("status")
        record: dict[str, Any] = {
            "assertion_id": assertion_id,
            "assertion_type": assertion_type,
            "kind": kind,
            "label": label,
            # An unreviewed extraction is never rendered as a plain statement, and
            # neither is a reviewed one the source row has since contradicted.
            "text": f"{_UNCONFIRMED_TEMPLATE}: {label}"
            if assurance is OntologyAssurance.UNCONFIRMED_AI
            else f"{label} ({_DISAGREEMENT_TEMPLATE})"
            if source_disagreement is not None
            else label,
            "properties": properties,
            # Compared as this answer was built: null while the assertion and its row
            # agree, otherwise the row's own account beside the person's.
            "source_disagreement": source_disagreement,
            "status": None if status is None else str(status),
            "status_source": "ASSERTION" if source_account is None else "SOURCE_ROW",
            "assurance": assurance.value,
            "derivation_kind": derivation_kind.value,
            "confidence": confidence,
            "review_status": review_status.value,
            "evidence_ids": list(evidence_ids),
            "source_object_kind": source_object_kind,
            "source_object_id": source_object_id,
            "asserted_at_sequence": asserted_at_sequence,
            "evidence_event_sequences": list(evidence_event_sequences),
            "stale_at_sequence": stale_at_sequence,
            "current": current,
            "invalidating_events": invalidating,
        }
        if assurance is OntologyAssurance.CONFIRMED and review is not None:
            record["review_id"] = review.review_id
            record["reviewed_by"] = review.reviewed_by
        return record

    async def _meta_currency(
        self,
        room_id: str,
        user_id: str,
        head: int,
        positions: list[tuple[str, int, tuple[str, ...]]],
    ) -> dict[str, tuple[bool, int]]:
        """Derive currency per assertion, one grouped read per class, never per claim."""
        return await self._currency(
            positions,
            lambda event_class, floor: self.repos.meta.invalidating_sequences(
                room_id, user_id, event_class, floor, head
            ),
        )

    async def _meta_freshness(
        self,
        room_id: str,
        user_id: str,
        head: int,
        claims: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Freshness, computed inside the authorized scope like every other aggregate."""
        cursors = await self.repos.meta.extraction_cursors(room_id, user_id)
        # An extractor with no cursor row has drained nothing, so it is the furthest
        # behind, not absent. Reading only the rows that exist made a room whose
        # asynchronous drain had never run report that everything was current — and
        # nothing wakes that drain today, so it is the ordinary case, not an edge.
        drained_to = min(cursors.get(extractor.value, 0) for extractor in OntologyExtractor)
        positions = [int(claim["asserted_at_sequence"]) for claim in claims]
        return {
            "authorized_head": head,
            # Pending work a reader can see; it decides nothing they are shown.
            "drain_lag_events": max(0, head - drained_to),
            "claims_as_of": min(positions) if positions else None,
        }

    @staticmethod
    def _meta_summary(
        kind: MetaQuestionKind, claims: list[dict[str, Any]], distinct_sources: int
    ) -> str:
        """Prose over an already-authorized claim set; unconfirmed labels never enter it."""
        if not claims:
            return "no confirmed assertions in this room answer that question"
        labels = "; ".join(str(claim["label"]) for claim in claims)
        # A confirmed assertion whose row has moved is counted by the row's account,
        # and the prose says so, because a caveat only the payload carries is a
        # caveat a reader of the sentence never gets.
        disputed = sum(1 for claim in claims if claim["source_disagreement"] is not None)
        caveat = (
            f" ({disputed} confirmed by a person and since contradicted by the source record)"
            if disputed
            else ""
        )
        if kind is MetaQuestionKind.STATUS:
            counts: dict[str, int] = {}
            for claim in claims:
                if claim["assertion_type"] != "ENTITY":
                    continue
                status = _MetaMixin._claim_status(claim)
                counts[status] = counts.get(status, 0) + 1
            grouped = ", ".join(f"{status} {count}" for status, count in sorted(counts.items()))
            return (
                f"{len(claims)} governed assertions describe where things stand ({grouped}){caveat}"
            )
        if kind is MetaQuestionKind.BLOCKERS:
            return f"{len(claims)} blocking relationships: {labels}"
        if kind is MetaQuestionKind.CHANGES:
            latest = max(int(claim["asserted_at_sequence"]) for claim in claims)
            return (
                f"{len(claims)} work objects changed, latest at sequence {latest}{caveat}: {labels}"
            )
        if kind is MetaQuestionKind.DECISIONS_OPEN:
            return f"{len(claims)} decisions are still open{caveat}: {labels}"
        if kind is MetaQuestionKind.DECISIONS_MADE:
            return f"{len(claims)} decisions have been made{caveat}: {labels}"
        return f"{len(claims)} contradictions from {distinct_sources} distinct sources: {labels}"

    @staticmethod
    def _claim_status(claim: dict[str, Any]) -> str:
        """The status a reader is entitled to, resolved from a source when the record was built."""
        return str(claim["status"] or "UNKNOWN")

    def _meta_envelope(
        self,
        *,
        question: str | None,
        kind: MetaQuestionKind,
        room_id: str,
        limit: int,
        claims: list[dict[str, Any]],
        unconfirmed: list[dict[str, Any]],
        freshness: dict[str, Any],
        summary: str,
        refusal_reason: MetaRefusalReason,
    ) -> dict[str, Any]:
        """The shared answer envelope. "We do not know" is a real answer at HTTP 200."""
        if claims:
            status = MetaAnswerStatus.ANSWERED
        elif unconfirmed:
            status = MetaAnswerStatus.ANSWERED_UNCONFIRMED_ONLY
        else:
            status = MetaAnswerStatus.REFUSED
        return {
            "query": {
                "question": question,
                "kind": kind.value,
                "supported_kinds": [member.value for member in MetaQuestionKind],
            },
            "status": status.value,
            "refusal_reason": (
                refusal_reason.value if status is MetaAnswerStatus.REFUSED else None
            ),  # named only when the answer is a refusal
            "summary": summary,
            # Two result sets, never merged: merging them would require code that
            # does not exist, which is a stronger guarantee than a naming convention.
            "claims": claims,
            "unconfirmed": unconfirmed,
            "counts": {
                # Unconfirmed extractions are excluded from every figure presented
                # as fact, and counted separately.
                "claims": len(claims),
                "unconfirmed": len(unconfirmed),
                "current_claims": sum(1 for claim in claims if claim["current"]),
                "max_claims": limit,
            },
            "freshness": freshness,
            "scope": {"room_id": room_id, "max_claims": limit},
        }

    async def answer_decision_meta(
        self,
        room_id: str,
        question: str | None = None,
        *,
        kind: MetaQuestionKind | None = None,
        user_id: str,
        version_id: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Answer one bounded Meta question from current governed assertions.

        The kind is the parameter; the question is free text, kept in the answer for
        audit. A caller that names its kind reaches every supported question, so no
        capability depends on a phrasing this workspace happens to recognize.
        """
        question_kind = self._resolve_meta_kind(question, kind)
        # Classify the question as asked, record a bounded copy: shortening it first
        # would let padding push a surveillance clause past the cut and match a form.
        question = self._audit_question(question)
        if not 1 <= limit <= 10:
            raise DomainError("Meta evidence limit must be between 1 and 10")
        await self.get_room(room_id)
        if question_kind in DECISION_KINDS:
            return await self._answer_decision_meta(
                room_id, user_id, question, question_kind, version_id, limit
            )
        return await self._answer_assertion_meta(room_id, user_id, question, question_kind, limit)

    async def _answer_assertion_meta(
        self,
        room_id: str,
        user_id: str,
        question: str | None,
        kind: MetaQuestionKind,
        limit: int,
    ) -> dict[str, Any]:
        head = await self.repos.meta.head(room_id, user_id)
        if head is None:
            # Nothing this reader may see, so no head, no counts and no other
            # aggregate — a consequence of the query, not a special case.
            return self._meta_envelope(
                question=question,
                kind=kind,
                room_id=room_id,
                limit=limit,
                claims=[],
                unconfirmed=[],
                freshness={},
                summary="no authorized evidence in this room answers that question",
                refusal_reason=MetaRefusalReason.NO_AUTHORIZED_EVIDENCE,
            )
        entities = await self.repos.meta.entities(
            room_id,
            user_id,
            self._META_ENTITY_KINDS.get(kind, ()),
            since_sequence=0 if kind is MetaQuestionKind.CHANGES else None,
            statuses=self._META_ENTITY_STATUSES.get(kind, ()),
            limit=limit,
        )
        relationships = await self.repos.meta.relationships(
            room_id, user_id, self._META_RELATIONSHIP_KINDS.get(kind, ()), limit=limit
        )
        endpoint_ids = sorted(
            {
                entity_id
                for item in relationships
                for entity_id in (item.from_entity_id, item.to_entity_id)
            }
        )
        endpoints = {
            entity.entity_id: entity
            for entity in await self.repos.meta.entities_by_ids(room_id, user_id, endpoint_ids)
        }
        entity_ids = {entity.entity_id for entity in entities}
        relationships = [
            item
            for item in relationships
            if self._meta_edge_in_scope(kind, item, entity_ids, endpoints)
        ]
        positions: list[tuple[str, int, tuple[str, ...]]] = [
            (entity.entity_id, entity.asserted_at_sequence, invalidation_class(entity.kind))
            for entity in entities
        ]
        positions.extend(
            (
                item.relationship_id,
                item.asserted_at_sequence,
                invalidation_class(
                    endpoints[item.from_entity_id].kind, endpoints[item.to_entity_id].kind
                ),
            )
            for item in relationships
        )
        currency = await self._meta_currency(room_id, user_id, head, positions)

        records: list[dict[str, Any]] = []
        for entity in entities:
            records.append(
                self._meta_claim_record(
                    assertion_id=entity.entity_id,
                    assertion_type="ENTITY",
                    kind=entity.kind.value,
                    label=entity.label,
                    properties=entity.properties,
                    derivation_kind=entity.derivation_kind,
                    confidence=entity.confidence,
                    review_status=entity.review_status,
                    evidence_ids=entity.evidence_ids,
                    source_object_kind=entity.kind.value,
                    source_object_id=entity.source_object_id,
                    asserted_at_sequence=entity.asserted_at_sequence,
                    evidence_event_sequences=entity.evidence_event_sequences,
                    stale_at_sequence=entity.stale_at_sequence,
                    source_account=await self._source_account(entity),
                    currency=currency[entity.entity_id],
                    review=await self.repos.meta.latest_review(room_id, user_id, entity.entity_id),
                )
            )
        for item in relationships:
            source = endpoints[item.from_entity_id]
            target = endpoints[item.to_entity_id]
            records.append(
                self._meta_claim_record(
                    assertion_id=item.relationship_id,
                    assertion_type="RELATIONSHIP",
                    kind=item.kind.value,
                    label=f"{source.label} {item.kind.value} {target.label}",
                    properties={},
                    derivation_kind=item.derivation_kind,
                    confidence=item.confidence,
                    review_status=item.review_status,
                    evidence_ids=item.evidence_ids,
                    source_object_kind=item.source_object_kind,
                    source_object_id=item.source_object_id,
                    asserted_at_sequence=item.asserted_at_sequence,
                    evidence_event_sequences=item.evidence_event_sequences,
                    stale_at_sequence=item.stale_at_sequence,
                    source_account=None,
                    currency=currency[item.relationship_id],
                    review=await self.repos.meta.latest_review(
                        room_id, user_id, item.relationship_id
                    ),
                )
            )
        claims = [
            record
            for record in records
            if record["assurance"] != OntologyAssurance.UNCONFIRMED_AI.value
        ]
        unconfirmed = [
            record
            for record in records
            if record["assurance"] == OntologyAssurance.UNCONFIRMED_AI.value
        ]
        distinct_sources = len(
            {
                str(endpoints[item.from_entity_id].properties.get("agent_id", ""))
                for item in relationships
            }
            - {""}
        )
        return self._meta_envelope(
            question=question,
            kind=kind,
            room_id=room_id,
            limit=limit,
            claims=claims,
            unconfirmed=unconfirmed,
            freshness=await self._meta_freshness(room_id, user_id, head, records),
            summary=self._meta_summary(kind, claims, distinct_sources),
            refusal_reason=MetaRefusalReason.NO_ASSERTIONS_IN_SCOPE,
        )

    @staticmethod
    def _meta_edge_in_scope(
        kind: MetaQuestionKind,
        relationship: OntologyRelationship,
        entity_ids: set[str],
        endpoints: dict[str, OntologyEntity],
    ) -> bool:
        """An edge whose endpoints this reader may not see is not part of the answer.

        Nor is an edge that names a person and the work attributed to them: a page
        of those is a per-person work list whatever kind asked for it, and the
        refusal pass already declines that shape in free text. Enforced over what
        an answer may carry rather than over one table entry, so no kind can reach
        it by being pointed at another relationship.
        """
        if (
            relationship.from_entity_id not in endpoints
            or relationship.to_entity_id not in endpoints
        ):
            return False
        if OntologyEntityKind.PERSON in (
            endpoints[relationship.from_entity_id].kind,
            endpoints[relationship.to_entity_id].kind,
        ):
            return False
        if kind in _MetaMixin._DECISION_SCOPED_KINDS:
            return relationship.to_entity_id in entity_ids
        if kind is MetaQuestionKind.DISAGREEMENT:
            return (
                endpoints[relationship.from_entity_id].kind in _MetaMixin._DISAGREEMENT_ENDPOINTS
                and endpoints[relationship.to_entity_id].kind in _MetaMixin._DISAGREEMENT_ENDPOINTS
            )
        return True

    async def _answer_decision_meta(
        self,
        room_id: str,
        user_id: str,
        question: str | None,
        question_kind: MetaQuestionKind,
        version_id: str | None,
        limit: int,
    ) -> dict[str, Any]:
        """The frozen-provenance chain, unchanged, inside the authorized scope."""
        head = await self.repos.meta.head(room_id, user_id)
        if head is None:
            return self._meta_envelope(
                question=question,
                kind=question_kind,
                room_id=room_id,
                limit=limit,
                claims=[],
                unconfirmed=[],
                freshness={},
                summary="no authorized evidence in this room answers that question",
                refusal_reason=MetaRefusalReason.NO_AUTHORIZED_EVIDENCE,
            )
        resolved = await self.repos.artifacts.resolve_decision_version(room_id, version_id)
        if resolved is None:
            raise DomainError("decision artifact version not found in room")
        artifact, version = resolved
        provenance, available_claims = await self.repos.artifacts.get_version_provenance_bounded(
            version.version_id, limit
        )
        decision = await self.repos.meta.entity_by_source(
            room_id, user_id, OntologyEntityKind.DECISION, version.version_id
        )
        if decision is None:
            raise DomainError("decision ontology is not available for artifact version")
        decision_review = await self.repos.meta.latest_review(room_id, user_id, decision.entity_id)

        chains: list[dict[str, Any]] = []
        for source in provenance:
            claim = await self.repos.meta.entity_by_source(
                room_id, user_id, OntologyEntityKind.CLAIM, str(source["claim_id"])
            )
            output = await self.repos.meta.entity_by_source(
                room_id, user_id, OntologyEntityKind.AGENT_OUTPUT, str(source["output_id"])
            )
            if claim is None or output is None:
                raise DomainError("decision evidence chain is incomplete")
            claim_to_decision = await self.repos.meta.relationship_between(
                room_id, user_id, claim.entity_id, decision.entity_id
            )
            claim_to_output = await self.repos.meta.relationship_between(
                room_id, user_id, claim.entity_id, output.entity_id
            )
            if claim_to_decision is None or claim_to_output is None:
                raise DomainError("decision evidence relationship is incomplete")
            claim_review = await self.repos.meta.latest_review(room_id, user_id, claim.entity_id)
            output_review = await self.repos.meta.latest_review(room_id, user_id, output.entity_id)
            decision_link_review = await self.repos.meta.latest_review(
                room_id, user_id, claim_to_decision.relationship_id
            )
            output_link_review = await self.repos.meta.latest_review(
                room_id, user_id, claim_to_output.relationship_id
            )
            chains.append(
                {
                    "claim": {
                        **await self._ontology_entity_record(claim),
                        "published_text": source["text"],
                        "latest_review": (
                            self._ontology_review_record(claim_review)
                            if claim_review is not None
                            else None
                        ),
                    },
                    "agent_output": {
                        **await self._ontology_entity_record(output),
                        "latest_review": (
                            self._ontology_review_record(output_review)
                            if output_review is not None
                            else None
                        ),
                    },
                    "relationships": {
                        "claim_to_decision": {
                            **self._ontology_relationship_record(claim_to_decision),
                            "latest_review": (
                                self._ontology_review_record(decision_link_review)
                                if decision_link_review is not None
                                else None
                            ),
                        },
                        "claim_to_agent_output": {
                            **self._ontology_relationship_record(claim_to_output),
                            "latest_review": (
                                self._ontology_review_record(output_link_review)
                                if output_link_review is not None
                                else None
                            ),
                        },
                    },
                    "exact_source_evidence": {
                        "output_id": source["output_id"],
                        "evidence": source["evidence"],
                        "agent_id": source["agent_id"],
                        "execution_id": source["execution_id"],
                        "source_prompt": source["source_prompt"],
                        "provider_input": source["provider_input"],
                        "provider_name": source["provider_name"],
                        "provider_model": source["provider_model"],
                        "provider_response_id": source["provider_response_id"],
                        "provider_interventions": source["provider_interventions"],
                        "provider_evidence": source["provider_evidence"],
                    },
                    "_assertions": (claim, output, claim_to_decision, claim_to_output),
                    "_reviews": (claim_review, decision_link_review),
                }
            )

        # Only reviewed claims are named as fact; an unreviewed extraction reaches the
        # reader through unconfirmed[] and its hedged template, never through prose.
        current_claims = [
            str(chain["claim"]["label"])
            for chain in chains
            if self._meta_assurance(
                chain["_assertions"][0].derivation_kind,
                chain["_assertions"][0].review_status,
            )
            is not OntologyAssurance.UNCONFIRMED_AI
        ]
        relationship_counts: dict[str, int] = {}
        for chain in chains:
            kind = str(chain["relationships"]["claim_to_decision"]["kind"])
            relationship_counts[kind] = relationship_counts.get(kind, 0) + 1
        relationship_summary = ", ".join(
            f"{kind} {count}" for kind, count in sorted(relationship_counts.items())
        )
        if question_kind is MetaQuestionKind.WHY_DECISION:
            summary = (
                f"{decision.label} has {len(chains)} deliberately selected "
                f"claim{'s' if len(chains) != 1 else ''} ({relationship_summary}): "
                + ("; ".join(current_claims) or "none of them reviewed yet")
            )
        else:
            summary = (
                f"{len(chains)} selected AgentOutput"
                f"{'s' if len(chains) != 1 else ''} are linked to {decision.label} "
                f"through governed claims ({relationship_summary})."
            )

        positions: list[tuple[str, int, tuple[str, ...]]] = [
            (
                decision.entity_id,
                decision.asserted_at_sequence,
                invalidation_class(decision.kind),
            )
        ]
        for chain in chains:
            claim_entity, output_entity, link, output_link = chain["_assertions"]
            positions.append(
                (
                    claim_entity.entity_id,
                    claim_entity.asserted_at_sequence,
                    invalidation_class(claim_entity.kind),
                )
            )
            positions.append(
                (
                    output_entity.entity_id,
                    output_entity.asserted_at_sequence,
                    invalidation_class(output_entity.kind),
                )
            )
            positions.append(
                (
                    link.relationship_id,
                    link.asserted_at_sequence,
                    invalidation_class(claim_entity.kind, decision.kind),
                )
            )
            positions.append(
                (
                    output_link.relationship_id,
                    output_link.asserted_at_sequence,
                    invalidation_class(claim_entity.kind, output_entity.kind),
                )
            )
        currency = await self._meta_currency(room_id, user_id, head, positions)
        # Currency is derived once and every record describing an assertion carries
        # that one answer. A chain record left without it reported the same assertion
        # as still current inside the same response that called it stale.
        for chain in chains:
            claim_entity, output_entity, link, output_link = chain["_assertions"]
            links = chain["relationships"]
            chain["claim"] = self._with_currency(chain["claim"], currency[claim_entity.entity_id])
            chain["agent_output"] = self._with_currency(
                chain["agent_output"], currency[output_entity.entity_id]
            )
            links["claim_to_decision"] = self._with_currency(
                links["claim_to_decision"], currency[link.relationship_id]
            )
            links["claim_to_agent_output"] = self._with_currency(
                links["claim_to_agent_output"], currency[output_link.relationship_id]
            )
        # Retrieval is bounded, so the answer names only the evidence it retrieved.
        bounded_evidence = tuple(
            str(chain["exact_source_evidence"]["output_id"]) for chain in chains
        )
        records = [
            self._meta_claim_record(
                assertion_id=decision.entity_id,
                assertion_type="ENTITY",
                kind=decision.kind.value,
                label=decision.label,
                properties=decision.properties,
                derivation_kind=decision.derivation_kind,
                confidence=decision.confidence,
                review_status=decision.review_status,
                evidence_ids=bounded_evidence,
                source_object_kind=decision.kind.value,
                source_object_id=decision.source_object_id,
                asserted_at_sequence=decision.asserted_at_sequence,
                evidence_event_sequences=decision.evidence_event_sequences,
                stale_at_sequence=decision.stale_at_sequence,
                source_account=await self._source_account(decision),
                currency=currency[decision.entity_id],
                review=decision_review,
            )
        ]
        for chain in chains:
            claim_entity, _output_entity, link, _output_link = chain["_assertions"]
            claim_review, link_review = chain.pop("_reviews")
            del chain["_assertions"]
            records.append(
                self._meta_claim_record(
                    assertion_id=claim_entity.entity_id,
                    assertion_type="ENTITY",
                    kind=claim_entity.kind.value,
                    label=claim_entity.label,
                    properties=claim_entity.properties,
                    derivation_kind=claim_entity.derivation_kind,
                    confidence=claim_entity.confidence,
                    review_status=claim_entity.review_status,
                    evidence_ids=claim_entity.evidence_ids,
                    source_object_kind=claim_entity.kind.value,
                    source_object_id=claim_entity.source_object_id,
                    asserted_at_sequence=claim_entity.asserted_at_sequence,
                    evidence_event_sequences=claim_entity.evidence_event_sequences,
                    stale_at_sequence=claim_entity.stale_at_sequence,
                    source_account=await self._source_account(claim_entity),
                    currency=currency[claim_entity.entity_id],
                    review=claim_review,
                )
            )
            records.append(
                self._meta_claim_record(
                    assertion_id=link.relationship_id,
                    assertion_type="RELATIONSHIP",
                    kind=link.kind.value,
                    label=f"{claim_entity.label} {link.kind.value} {decision.label}",
                    properties={},
                    derivation_kind=link.derivation_kind,
                    confidence=link.confidence,
                    review_status=link.review_status,
                    evidence_ids=link.evidence_ids,
                    source_object_kind=link.source_object_kind,
                    source_object_id=link.source_object_id,
                    asserted_at_sequence=link.asserted_at_sequence,
                    evidence_event_sequences=link.evidence_event_sequences,
                    stale_at_sequence=link.stale_at_sequence,
                    source_account=None,
                    currency=currency[link.relationship_id],
                    review=link_review,
                )
            )
        claims = [
            record
            for record in records
            if record["assurance"] != OntologyAssurance.UNCONFIRMED_AI.value
        ]
        unconfirmed = [
            record
            for record in records
            if record["assurance"] == OntologyAssurance.UNCONFIRMED_AI.value
        ]
        envelope = self._meta_envelope(
            question=question,
            kind=question_kind,
            room_id=room_id,
            limit=limit,
            claims=claims,
            unconfirmed=unconfirmed,
            freshness=await self._meta_freshness(room_id, user_id, head, records),
            summary=summary,
            refusal_reason=MetaRefusalReason.NO_ASSERTIONS_IN_SCOPE,
        )
        envelope["scope"] = {
            "room_id": room_id,
            "artifact_id": artifact.artifact_id,
            "version_id": version.version_id,
            "version_number": version.version_number,
            "max_claims": limit,
        }
        envelope["decision"] = {
            **self._with_currency(
                await self._ontology_entity_record(decision), currency[decision.entity_id]
            ),
            "evidence_ids": [chain["exact_source_evidence"]["output_id"] for chain in chains],
            "source_ids": [
                version.version_id,
                *(chain["claim"]["source_object_id"] for chain in chains),
            ],
            "artifact_name": artifact.name,
            "version_id": version.version_id,
            "latest_review": (
                self._ontology_review_record(decision_review)
                if decision_review is not None
                else None
            ),
        }
        envelope["evidence_chains"] = chains
        envelope["freshness"] = {
            **envelope["freshness"],
            "artifact_created_at": version.created_at.isoformat(),
            "decision_updated_at": decision.updated_at.isoformat(),
        }
        envelope["retrieval_counts"] = {
            "available_claims": available_claims,
            "returned_claims": len(chains),
            "returned_outputs": len(chains),
            "truncated": available_claims > len(chains),
        }
        envelope["provenance"] = {
            "content_hash": version.content_hash,
            "provenance_hash": version.provenance_hash,
            "verified": self.verify_artifact_provenance_hash(version, provenance)
            if available_claims == len(provenance)
            else None,
            "verification_note": (
                "verified against all frozen claims"
                if available_claims == len(provenance)
                else "not recomputed because bounded retrieval omitted claims"
            ),
        }
        return envelope
