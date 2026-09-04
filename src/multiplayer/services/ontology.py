"""Ontology: extraction, consolidation, review, and the room's derived structure."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from ..domain.events import EventType, RoomEvent
from ..domain.meta import (
    invalidation_class,
)
from ..domain.models import (
    Decision,
    DomainError,
    OntologyDerivationKind,
    OntologyEntity,
    OntologyEntityKind,
    OntologyExtractor,
    OntologyRelationship,
    OntologyRelationshipKind,
    OntologyReview,
    OntologyReviewAction,
    OntologyReviewStatus,
    Task,
    new_id,
    utcnow,
)
from ._shared import (
    _ASYNC_PASS_LIMIT,
    _INFERRED_CONFIDENCE,
    _SharedMixin,
)

log = logging.getLogger(__name__)


class _OntologyMixin(_SharedMixin):
    """Mixin providing the ontology surface of MultiplayerService."""

    _IMMEDIATE_EVENTS: frozenset[EventType] = frozenset(
        {
            EventType.TASK_CREATED,
            EventType.TASK_ASSIGNED,
            EventType.TASK_UNASSIGNED,
            EventType.TASK_STARTED,
            EventType.TASK_COMPLETED,
            EventType.TASK_FAILED,
            EventType.TASK_CANCELLED,
            EventType.TASK_DELEGATED,
            EventType.DECISION_CREATED,
            # A decision that moves state changes the row the assertion describes,
            # so the pass that would otherwise never look again re-reads it here.
            EventType.DECISION_UPDATED,
            EventType.DECISION_SUPERSEDED,
            # An artifact version and a published synthesis are projected inside
            # their own committing transaction, by create_synthesis_in_transaction.
            # They stay in this allowlist so the cursor means "every structured
            # action up to here is handled", not "every one this pass looked at".
            EventType.ARTIFACT_VERSION_CREATED,
            EventType.SYNTHESIS_PUBLISHED,
        }
    )
    _ASYNC_EVENTS: frozenset[EventType] = frozenset(
        {
            EventType.MESSAGE_CREATED,
            EventType.AGENT_OUTPUT_CREATED,
            EventType.BRANCH_SYNTHESIS_COMPLETED,
        }
    )
    _DECISION_EVENTS: frozenset[EventType] = frozenset(
        {
            EventType.DECISION_CREATED,
            EventType.DECISION_UPDATED,
            EventType.DECISION_SUPERSEDED,
        }
    )
    _TASK_ID_KEYS = ("task_id", "child_task_id", "parent_task_id")
    _BLOCKED_BY = " is blocked by "

    async def get_room_ontology(self, room_id: str) -> dict[str, Any]:
        """This room's assertions, each told with the currency the Meta path derives.

        Without it a superseded assertion left here byte-identical to a live one,
        and this is the account embedded in room state — the one a reconnecting
        client believes.
        """
        await self.get_room(room_id)
        entities = await self.repos.ontology.list_entities(room_id)
        relationships = await self.repos.ontology.list_relationships(room_id)
        reviews = await self.repos.ontology.list_reviews(room_id)
        currency = await self._ontology_currency(room_id, entities, relationships)
        return {
            "entities": [
                self._with_currency(
                    await self._ontology_entity_record(entity), currency[entity.entity_id]
                )
                for entity in entities
            ],
            "relationships": [
                self._with_currency(
                    self._ontology_relationship_record(relationship),
                    currency[relationship.relationship_id],
                )
                for relationship in relationships
            ],
            "reviews": [self._ontology_review_record(review) for review in reviews],
        }

    async def _ontology_currency(
        self,
        room_id: str,
        entities: list[OntologyEntity],
        relationships: list[OntologyRelationship],
    ) -> dict[str, tuple[bool, int]]:
        """Currency for a whole room, on the rule and the read shape Meta already uses.

        Both surfaces now ask the log for the events that can invalidate an
        assertion. Counting a fetched page of the room's own ordered events instead
        made every assertion past that page report itself current for ever, and a
        page is wrong again at whatever the next limit turns out to be.
        """
        kinds = {entity.entity_id: entity.kind for entity in entities}
        positions: list[tuple[str, int, tuple[str, ...]]] = [
            (entity.entity_id, entity.asserted_at_sequence, invalidation_class(entity.kind))
            for entity in entities
        ]
        positions.extend(
            (
                relationship.relationship_id,
                relationship.asserted_at_sequence,
                invalidation_class(
                    kinds[relationship.from_entity_id], kinds[relationship.to_entity_id]
                ),
            )
            for relationship in relationships
        )
        return await self._currency(
            positions,
            lambda event_class, floor: self.repos.ontology.invalidating_sequences(
                room_id, event_class, floor
            ),
        )

    @staticmethod
    async def _currency(
        positions: list[tuple[str, int, tuple[str, ...]]],
        invalidating: Callable[[tuple[str, ...], int], Awaitable[list[int]]],
    ) -> dict[str, tuple[bool, int]]:
        """Group by invalidation class, one read per class, then count per assertion.

        The one derivation every surface goes through, because two of them written
        separately is how the ontology route and the Meta path came to disagree.
        """
        grouped: dict[tuple[str, ...], list[tuple[str, int]]] = {}
        for assertion_id, sequence, event_class in positions:
            grouped.setdefault(event_class, []).append((assertion_id, sequence))
        currency: dict[str, tuple[bool, int]] = {}
        for event_class, members in grouped.items():
            floor = min(sequence for _assertion_id, sequence in members)
            sequences = await invalidating(event_class, floor)
            for assertion_id, sequence in members:
                count = sum(1 for item in sequences if item > sequence)
                currency[assertion_id] = (count == 0, count)
        return currency

    @staticmethod
    def _with_currency(record: dict[str, Any], currency: tuple[bool, int]) -> dict[str, Any]:
        """The two derived fields the Meta path reports, named the same way."""
        current, invalidating = currency
        return {**record, "current": current, "invalidating_events": invalidating}

    async def run_ontology_extraction(
        self,
        room_id: str,
        extractor: OntologyExtractor,
        *,
        actor_id: str = "system",
        limit: int = _ASYNC_PASS_LIMIT,
    ) -> dict[str, Any]:
        """One bounded extraction pass. No read path calls this: reads never write.

        The pass snapshots head, reads only what its cursor has not seen, and writes
        the assertions, their events and the cursor advance in one transaction, so a
        crash rolls the cursor back with the work. Assertions carry deterministic IDs
        and land ON CONFLICT DO NOTHING, which makes at-least-once delivery over
        idempotent writes exactly-once in effect.
        """
        persisted: list[RoomEvent] = []
        result: dict[str, Any] = {}
        async with self.db.transaction():
            head = await self.repos.events.get_latest_sequence(room_id)
            cursor = await self.repos.ontology.get_cursor(room_id, extractor)
            last = cursor.last_sequence if cursor is not None else 0
            entities, relationships, stale_ids, to_sequence = await self._extract(
                room_id, extractor, last, head, limit
            )
            (
                entities_written,
                relationships_written,
                reconciled,
            ) = await self.repos.ontology.materialize_in_transaction(entities, relationships)
            marked = await self.repos.ontology.mark_stale_in_transaction(
                room_id, stale_ids, to_sequence
            )
            events: list[RoomEvent] = []
            if entities_written or relationships_written:
                events.append(
                    RoomEvent(
                        room_id=room_id,
                        sequence=0,
                        event_type=EventType.ONTOLOGY_MATERIALIZED,
                        payload={
                            "extractor": extractor.value,
                            "entity_ids": [entity.entity_id for entity in entities],
                            "relationship_ids": [item.relationship_id for item in relationships],
                        },
                        actor_id=actor_id,
                        actor_type="system",
                    )
                )
            if marked:
                events.append(
                    RoomEvent(
                        room_id=room_id,
                        sequence=0,
                        event_type=EventType.ONTOLOGY_ASSERTION_SUPERSEDED,
                        payload={"assertion_ids": marked, "stale_at_sequence": to_sequence},
                        actor_id=actor_id,
                        actor_type="system",
                    )
                )
            if reconciled:
                # A reviewed assertion the pass may not rewrite is still an
                # assertion whose row moved, so the log says so rather than the
                # pass passing over it in silence.
                events.append(
                    RoomEvent(
                        room_id=room_id,
                        sequence=0,
                        event_type=EventType.ONTOLOGY_ASSERTION_RECONCILED,
                        payload={"assertion_ids": reconciled, "at_sequence": to_sequence},
                        actor_id=actor_id,
                        actor_type="system",
                    )
                )
            if events:
                events.append(
                    RoomEvent(
                        room_id=room_id,
                        sequence=0,
                        event_type=EventType.ONTOLOGY_EXTRACTION_ADVANCED,
                        payload={
                            "extractor": extractor.value,
                            "from_sequence": last,
                            "to_sequence": to_sequence,
                            "entities_written": entities_written,
                            "relationships_written": relationships_written,
                        },
                        actor_id=actor_id,
                        actor_type="system",
                    )
                )
            for event in events:
                persisted.append(
                    await self.repos.events.append_with_next_sequence_in_transaction(event)
                )
            if to_sequence > last:
                await self.repos.ontology.advance_cursor_in_transaction(
                    room_id, extractor, last, to_sequence, utcnow()
                )
            result = {
                "extractor": extractor.value,
                "from_sequence": last,
                "to_sequence": to_sequence,
                "entities_written": entities_written,
                "relationships_written": relationships_written,
                "superseded": marked,
                "reconciled": reconciled,
            }
        await self._broadcast_persisted_events(persisted)
        return result

    async def _extract(
        self,
        room_id: str,
        extractor: OntologyExtractor,
        last: int,
        head: int,
        limit: int,
    ) -> tuple[list[OntologyEntity], list[OntologyRelationship], list[str], int]:
        if extractor is OntologyExtractor.SCHEDULED:
            relationships, stale_ids = await self._consolidate(room_id, head)
            return [], relationships, stale_ids, head
        allowed = (
            self._IMMEDIATE_EVENTS
            if extractor is OntologyExtractor.IMMEDIATE
            else self._ASYNC_EVENTS
        )
        read = [
            event
            for event in await self.repos.events.list_since(room_id, last, limit)
            if event.sequence <= head
        ]
        # A capped pass advances only as far as it actually read, or the next pass
        # would skip the events this one never saw.
        to_sequence = read[-1].sequence if len(read) >= limit and read else head
        relevant = [event for event in read if event.event_type in allowed]
        if extractor is OntologyExtractor.IMMEDIATE:
            entities, relationships = await self._project_structured(room_id, relevant, to_sequence)
        else:
            entities, relationships = await self._project_inferred(room_id, relevant, to_sequence)
        return entities, relationships, [], to_sequence

    @staticmethod
    def _task_account(task: Task) -> dict[str, Any]:
        """What a task row says about itself. One definition, projected and compared."""
        return {
            "label": task.title,
            "properties": {
                "status": task.status.value,
                "priority": task.priority.value,
                "assigned_agent_id": task.assigned_agent_id or "",
            },
        }

    @staticmethod
    def _decision_account(decision: Decision) -> dict[str, Any]:
        """What a decision row says about itself."""
        return {
            "label": decision.title,
            "properties": {
                "status": decision.status.value,
                "decision_id": decision.decision_id,
            },
        }

    async def _source_account(self, entity: OntologyEntity) -> dict[str, Any] | None:
        """The source row's own account of itself, read now; None when no row states it.

        A pass projects this into an assertion. A read compares against it, so the two
        are the same function: a shape that drifted between them would invent a
        disagreement out of its own formatting. An assertion whose source is frozen —
        a published version, an agent output — has no row that can move, and gets None.
        """
        if entity.kind is OntologyEntityKind.TASK:
            task = await self.repos.tasks.get(entity.source_object_id)
            if task is None or task.room_id != entity.room_id:
                return None
            return self._task_account(task)
        if entity.kind is OntologyEntityKind.DECISION:
            decision = await self.repos.decisions.get(entity.source_object_id)
            if decision is None or decision.room_id != entity.room_id:
                return None
            return self._decision_account(decision)
        return None

    async def _project_structured(
        self, room_id: str, events: list[RoomEvent], at_sequence: int
    ) -> tuple[list[OntologyEntity], list[OntologyRelationship]]:
        """Project structured records. A structured record needs no inference."""
        task_events: dict[str, list[int]] = {}
        decision_events: dict[str, list[int]] = {}
        for event in events:
            if event.event_type in self._DECISION_EVENTS:
                decision_id = str(event.payload.get("decision_id") or "")
                if decision_id:
                    decision_events.setdefault(decision_id, []).append(event.sequence)
                continue
            for key in self._TASK_ID_KEYS:
                task_id = str(event.payload.get(key) or "")
                if task_id:
                    task_events.setdefault(task_id, []).append(event.sequence)
        timestamp = utcnow()
        entities: list[OntologyEntity] = []
        relationships: list[OntologyRelationship] = []
        owners: dict[str, str] = {}
        for task_id, sequences in sorted(task_events.items()):
            task = await self.repos.tasks.get(task_id)
            if task is None or task.room_id != room_id:
                continue
            entity_id = self._ontology_id("ont", room_id, "Task", task_id)
            account = self._task_account(task)
            entities.append(
                OntologyEntity(
                    entity_id=entity_id,
                    room_id=room_id,
                    kind=OntologyEntityKind.TASK,
                    source_object_id=task_id,
                    label=account["label"],
                    properties=account["properties"],
                    derivation_kind=OntologyDerivationKind.SYSTEM_MATERIALIZED,
                    confidence=1.0,
                    evidence_ids=(task_id,),
                    source_ids=(task_id,),
                    extractor=OntologyExtractor.IMMEDIATE,
                    asserted_at_sequence=at_sequence,
                    evidence_event_sequences=tuple(sorted(set(sequences))),
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
            member = await self.repos.room_members.get(room_id, task.created_by)
            if member is None:
                continue
            person_id = self._ontology_id("ont", room_id, "Person", task.created_by)
            if task.created_by not in owners:
                owners[task.created_by] = person_id
                user = await self.repos.users.get(task.created_by)
                entities.append(
                    OntologyEntity(
                        entity_id=person_id,
                        room_id=room_id,
                        kind=OntologyEntityKind.PERSON,
                        source_object_id=task.created_by,
                        label=user.display_name if user is not None else task.created_by,
                        properties={"user_id": task.created_by},
                        derivation_kind=OntologyDerivationKind.SYSTEM_MATERIALIZED,
                        confidence=1.0,
                        evidence_ids=(task.created_by,),
                        source_ids=(task.created_by,),
                        extractor=OntologyExtractor.IMMEDIATE,
                        asserted_at_sequence=at_sequence,
                        evidence_event_sequences=tuple(sorted(set(sequences))),
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                )
            relationships.append(
                OntologyRelationship(
                    relationship_id=self._ontology_id("rel", room_id, "OWNS", person_id, entity_id),
                    room_id=room_id,
                    kind=OntologyRelationshipKind.OWNS,
                    from_entity_id=person_id,
                    to_entity_id=entity_id,
                    derivation_kind=OntologyDerivationKind.SYSTEM_MATERIALIZED,
                    confidence=1.0,
                    evidence_ids=(task_id,),
                    source_ids=(task.created_by, task_id),
                    source_object_kind=OntologyEntityKind.TASK.value,
                    source_object_id=task_id,
                    extractor=OntologyExtractor.IMMEDIATE,
                    asserted_at_sequence=at_sequence,
                    evidence_event_sequences=tuple(sorted(set(sequences))),
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
        for decision_id, sequences in sorted(decision_events.items()):
            decision = await self.repos.decisions.get(decision_id)
            if decision is None or decision.room_id != room_id:
                continue
            # A re-assertion replaces the row's account of itself, not its history:
            # the events that produced the earlier assertion still evidence this one.
            asserted = await self.repos.ontology.get_entity_by_source(
                room_id, OntologyEntityKind.DECISION, decision_id
            )
            if asserted is not None:
                sequences = [*sequences, *asserted.evidence_event_sequences]
            account = self._decision_account(decision)
            entities.append(
                OntologyEntity(
                    entity_id=self._ontology_id("ont", room_id, "Decision", decision_id),
                    room_id=room_id,
                    kind=OntologyEntityKind.DECISION,
                    source_object_id=decision_id,
                    label=account["label"],
                    properties=account["properties"],
                    derivation_kind=OntologyDerivationKind.SYSTEM_MATERIALIZED,
                    confidence=1.0,
                    evidence_ids=(decision_id,),
                    source_ids=(decision_id,),
                    extractor=OntologyExtractor.IMMEDIATE,
                    asserted_at_sequence=at_sequence,
                    evidence_event_sequences=tuple(sorted(set(sequences))),
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
        return entities, relationships

    async def _project_inferred(
        self, room_id: str, events: list[RoomEvent], at_sequence: int
    ) -> tuple[list[OntologyEntity], list[OntologyRelationship]]:
        """Read the fixed allowlist and label everything it produces unconfirmed."""
        timestamp = utcnow()
        entities: list[OntologyEntity] = []
        relationships: list[OntologyRelationship] = []
        tasks_by_label = {
            entity.label.strip().lower().rstrip("."): entity
            for entity in await self.repos.ontology.list_entities(room_id)
            if entity.kind is OntologyEntityKind.TASK
        }
        for event in events:
            if event.event_type is EventType.AGENT_OUTPUT_CREATED:
                output_id = str(event.payload.get("output_id") or "")
                output = await self.repos.agent_outputs.get(output_id) if output_id else None
                if output is None or output.room_id != room_id:
                    continue
                entities.append(
                    OntologyEntity(
                        entity_id=self._ontology_id("ont", room_id, "AgentOutput", output_id),
                        room_id=room_id,
                        kind=OntologyEntityKind.AGENT_OUTPUT,
                        source_object_id=output_id,
                        label=f"Agent output {output_id}",
                        properties={
                            "agent_id": output.agent_id,
                            "execution_id": output.execution_id,
                            "provider_name": output.provider_name,
                            "provider_model": output.provider_model,
                        },
                        derivation_kind=OntologyDerivationKind.AI_DERIVED,
                        confidence=_INFERRED_CONFIDENCE,
                        evidence_ids=(output_id,),
                        source_ids=(output_id, output.execution_id),
                        review_status=OntologyReviewStatus.UNCONFIRMED,
                        extractor=OntologyExtractor.ASYNC,
                        asserted_at_sequence=at_sequence,
                        evidence_event_sequences=(event.sequence,),
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                )
                continue
            if event.event_type is not EventType.MESSAGE_CREATED:
                continue
            message_id = str(event.payload.get("message_id") or "")
            message = await self.repos.messages.get(message_id) if message_id else None
            if message is None or message.room_id != room_id:
                continue
            edge = self._blocking_edge(message.content, tasks_by_label)
            if edge is None:
                continue
            blocker, blocked = edge
            relationships.append(
                OntologyRelationship(
                    relationship_id=self._ontology_id(
                        "rel", room_id, "BLOCKS", blocker.entity_id, blocked.entity_id
                    ),
                    room_id=room_id,
                    kind=OntologyRelationshipKind.BLOCKS,
                    from_entity_id=blocker.entity_id,
                    to_entity_id=blocked.entity_id,
                    derivation_kind=OntologyDerivationKind.AI_DERIVED,
                    confidence=_INFERRED_CONFIDENCE,
                    evidence_ids=(message_id,),
                    source_ids=(message_id,),
                    review_status=OntologyReviewStatus.UNCONFIRMED,
                    # The durable row whose content states the blockage, not an
                    # endpoint: the message is what reported it.
                    source_object_kind="Message",
                    source_object_id=message_id,
                    extractor=OntologyExtractor.ASYNC,
                    asserted_at_sequence=at_sequence,
                    evidence_event_sequences=(event.sequence,),
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
        return entities, relationships

    @classmethod
    def _blocking_edge(
        cls, content: str, tasks_by_label: dict[str, OntologyEntity]
    ) -> tuple[OntologyEntity, OntologyEntity] | None:
        """One fixed form over already-materialized tasks; there is no open-ended read."""
        normalized = " ".join(content.strip().lower().split()).rstrip(".!?")
        if cls._BLOCKED_BY not in normalized:
            return None
        blocked_label, _, blocker_label = normalized.partition(cls._BLOCKED_BY)
        blocked = tasks_by_label.get(blocked_label.strip())
        blocker = tasks_by_label.get(blocker_label.strip())
        if blocked is None or blocker is None or blocked.entity_id == blocker.entity_id:
            return None
        return blocker, blocked

    async def _consolidate(
        self, room_id: str, head: int
    ) -> tuple[list[OntologyRelationship], list[str]]:
        """Relate and supersede existing assertions. It never reads raw evidence.

        Deduplication has nothing to remove: assertions carry deterministic IDs under
        a UNIQUE(room_id, kind, source_object_id) index, so a duplicate cannot be
        written in the first place. What is left is contradiction detection and the
        staleness marking that follows from it. Nothing here deletes: a removed
        assertion cannot be audited.
        """
        entities = await self.repos.ontology.list_entities(room_id)
        claims = [entity for entity in entities if entity.kind is OntologyEntityKind.CLAIM]
        by_label = {claim.label.strip().lower().rstrip("."): claim for claim in claims}
        timestamp = utcnow()
        relationships: list[OntologyRelationship] = []
        stale_ids: list[str] = []
        for claim in claims:
            label = claim.label.strip().lower().rstrip(".")
            if not label.startswith("not "):
                continue
            target = by_label.get(label[4:].strip())
            if target is None or target.entity_id == claim.entity_id:
                continue
            relationships.append(
                OntologyRelationship(
                    relationship_id=self._ontology_id(
                        "rel", room_id, "CONTRADICTS", claim.entity_id, target.entity_id
                    ),
                    room_id=room_id,
                    kind=OntologyRelationshipKind.CONTRADICTS,
                    from_entity_id=claim.entity_id,
                    to_entity_id=target.entity_id,
                    # What this pass thinks its own detection is worth. The shared
                    # repository method lowers it to the weakest of the two claims it
                    # relates, which is why a consolidation edge over two unconfirmed
                    # entities cannot reach a reader as confirmed truth.
                    derivation_kind=OntologyDerivationKind.SYSTEM_MATERIALIZED,
                    confidence=1.0,
                    evidence_ids=claim.evidence_ids,
                    source_ids=(claim.source_object_id, target.source_object_id),
                    source_object_kind=OntologyEntityKind.CLAIM.value,
                    source_object_id=claim.source_object_id,
                    extractor=OntologyExtractor.SCHEDULED,
                    asserted_at_sequence=head,
                    evidence_event_sequences=claim.evidence_event_sequences,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
            if target.asserted_at_sequence < claim.asserted_at_sequence:
                stale_ids.append(target.entity_id)
        return relationships, stale_ids

    async def review_ontology_entity(
        self,
        room_id: str,
        entity_id: str,
        action: OntologyReviewAction,
        reviewed_by: str,
        reason: str,
        *,
        require_member: bool = False,
        corrected_label: str | None = None,
        corrected_properties: dict[str, Any] | None = None,
        corrected_confidence: float | None = None,
    ) -> tuple[OntologyEntity, OntologyReview]:
        reason = reason.strip()
        if len(reason) > 2000:
            raise DomainError("ontology review reason must not exceed 2000 characters")
        if corrected_label is not None:
            corrected_label = self._validate_non_empty(corrected_label, "corrected label")
        if corrected_confidence is not None and not 0.0 <= corrected_confidence <= 1.0:
            raise DomainError("corrected confidence must be between 0 and 1")
        corrections = (corrected_label, corrected_properties, corrected_confidence)
        if action == OntologyReviewAction.CONFIRM and any(
            correction is not None for correction in corrections
        ):
            raise DomainError("confirmation cannot change an ontology fact")
        if action == OntologyReviewAction.CORRECT and all(
            correction is None for correction in corrections
        ):
            raise DomainError("correction must provide a changed value")
        if action == OntologyReviewAction.CORRECT and not reason:
            raise DomainError("correction reason must not be empty")

        reviewed_at = utcnow()
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(room_id, reviewed_by)
            entity = await self.repos.ontology.get_entity(entity_id)
            if entity is None or entity.room_id != room_id:
                raise DomainError("ontology entity not found in room")
            if action == OntologyReviewAction.CORRECT and all(
                (
                    corrected_label is None or corrected_label == entity.label,
                    corrected_properties is None or corrected_properties == entity.properties,
                    corrected_confidence is None or corrected_confidence == entity.confidence,
                )
            ):
                raise DomainError("correction must change an ontology fact")
            updated, review = await self.repos.ontology.review_entity_in_transaction(
                entity,
                new_id("orev"),
                action,
                reviewed_by,
                reason,
                corrected_label=corrected_label,
                corrected_properties=corrected_properties,
                corrected_confidence=corrected_confidence,
                reviewed_at=reviewed_at,
            )
            event_type = (
                EventType.ONTOLOGY_ASSERTION_CONFIRMED
                if action == OntologyReviewAction.CONFIRM
                else EventType.ONTOLOGY_ASSERTION_CORRECTED
            )
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=event_type,
                    payload={
                        "target_type": "ENTITY",
                        "target_id": entity_id,
                        "review_id": review.review_id,
                        "action": action.value,
                        "before": review.before_value,
                        "after": review.after_value,
                        "reason": reason,
                    },
                    actor_id=reviewed_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return updated, review

    async def review_ontology_relationship(
        self,
        room_id: str,
        relationship_id: str,
        action: OntologyReviewAction,
        reviewed_by: str,
        reason: str,
        *,
        require_member: bool = False,
        corrected_kind: OntologyRelationshipKind | None = None,
        corrected_confidence: float | None = None,
    ) -> tuple[OntologyRelationship, OntologyReview]:
        reason = reason.strip()
        if len(reason) > 2000:
            raise DomainError("ontology review reason must not exceed 2000 characters")
        if corrected_confidence is not None and not 0.0 <= corrected_confidence <= 1.0:
            raise DomainError("corrected confidence must be between 0 and 1")
        corrections = (corrected_kind, corrected_confidence)
        if action == OntologyReviewAction.CONFIRM and any(
            correction is not None for correction in corrections
        ):
            raise DomainError("confirmation cannot change an ontology relationship")
        if action == OntologyReviewAction.CORRECT and all(
            correction is None for correction in corrections
        ):
            raise DomainError("correction must provide a changed value")
        if action == OntologyReviewAction.CORRECT and not reason:
            raise DomainError("correction reason must not be empty")

        reviewed_at = utcnow()
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(room_id, reviewed_by)
            relationship = await self.repos.ontology.get_relationship(relationship_id)
            if relationship is None or relationship.room_id != room_id:
                raise DomainError("ontology relationship not found in room")
            if action == OntologyReviewAction.CORRECT and all(
                (
                    corrected_kind is None or corrected_kind == relationship.kind,
                    corrected_confidence is None or corrected_confidence == relationship.confidence,
                )
            ):
                raise DomainError("correction must change an ontology relationship")
            if corrected_kind is not None:
                room_relationships = await self.repos.ontology.list_relationships(room_id)
                if any(
                    candidate.relationship_id != relationship.relationship_id
                    and candidate.kind == corrected_kind
                    and candidate.from_entity_id == relationship.from_entity_id
                    and candidate.to_entity_id == relationship.to_entity_id
                    for candidate in room_relationships
                ):
                    raise DomainError("corrected ontology relationship already exists")
            updated, review = await self.repos.ontology.review_relationship_in_transaction(
                relationship,
                new_id("orev"),
                action,
                reviewed_by,
                reason,
                corrected_kind=corrected_kind,
                corrected_confidence=corrected_confidence,
                reviewed_at=reviewed_at,
            )
            event_type = (
                EventType.ONTOLOGY_ASSERTION_CONFIRMED
                if action == OntologyReviewAction.CONFIRM
                else EventType.ONTOLOGY_ASSERTION_CORRECTED
            )
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=event_type,
                    payload={
                        "target_type": "RELATIONSHIP",
                        "target_id": relationship_id,
                        "review_id": review.review_id,
                        "action": action.value,
                        "before": review.before_value,
                        "after": review.after_value,
                        "reason": reason,
                    },
                    actor_id=reviewed_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return updated, review

    async def _ontology_entity_record(self, entity: OntologyEntity) -> dict[str, Any]:
        """One assertion, including where in the room's order it stands.

        The sequence fields are not decoration: dropping `stale_at_sequence` was
        what let a superseded assertion read exactly like a live one.

        It reads the source row rather than taking a record of it, so this surface
        cannot report a disagreement the other two have stopped reporting.
        """
        source_account = await self._source_account(entity)
        return {
            "entity_id": entity.entity_id,
            "kind": entity.kind.value,
            "source_object_id": entity.source_object_id,
            "label": entity.label,
            "properties": entity.properties,
            "derivation_kind": entity.derivation_kind.value,
            "confidence": entity.confidence,
            "evidence_ids": list(entity.evidence_ids),
            "source_ids": list(entity.source_ids),
            "review_status": entity.review_status.value,
            "asserted_at_sequence": entity.asserted_at_sequence,
            "evidence_event_sequences": list(entity.evidence_event_sequences),
            "stale_at_sequence": entity.stale_at_sequence,
            # Compared as this record was built: null while the assertion and its row
            # agree, otherwise the row's own account beside the human's.
            "source_disagreement": self._source_disagreement(
                entity.label, entity.properties, entity.review_status, source_account
            ),
            "created_at": entity.created_at.isoformat(),
            "updated_at": entity.updated_at.isoformat(),
        }

    @staticmethod
    def _ontology_relationship_record(
        relationship: OntologyRelationship,
    ) -> dict[str, Any]:
        return {
            "relationship_id": relationship.relationship_id,
            "kind": relationship.kind.value,
            "from_entity_id": relationship.from_entity_id,
            "to_entity_id": relationship.to_entity_id,
            "derivation_kind": relationship.derivation_kind.value,
            "confidence": relationship.confidence,
            "evidence_ids": list(relationship.evidence_ids),
            "source_ids": list(relationship.source_ids),
            "review_status": relationship.review_status.value,
            "asserted_at_sequence": relationship.asserted_at_sequence,
            "evidence_event_sequences": list(relationship.evidence_event_sequences),
            "stale_at_sequence": relationship.stale_at_sequence,
            "created_at": relationship.created_at.isoformat(),
            "updated_at": relationship.updated_at.isoformat(),
        }

    @staticmethod
    def _ontology_review_record(review: OntologyReview) -> dict[str, Any]:
        return {
            "review_id": review.review_id,
            "target_type": review.target_type.value,
            "target_id": review.target_id,
            "action": review.action.value,
            "before": review.before_value,
            "after": review.after_value,
            "reason": review.reason,
            "reviewed_by": review.reviewed_by,
            "created_at": review.created_at.isoformat(),
        }
