"""An invalidation class is checked against the emitter, not against a written spec.

The Decision and Artifact classes named `artifact.synthesis_published`, but the
publication path emits `artifact.decision_brief_synthesized` when the synthesis type
is a Decision Brief — so publishing a second brief left the superseded Decision
reporting `current`, while a progress report over the same room invalidated it. The
table was right against the specification and wrong against the code, and a table
maintained by hand against a specification drifts that way again.

So nothing below is written down a second time. Each case runs a real write path
twice over one room and derives, from that room's own event log and assertion set,
which entity kinds gained a second assertion and which event types the path emitted
while doing it. Repeating a path that re-asserts a kind must emit an event inside
that kind's invalidation class; otherwise the first run's assertion is still reported
current after the second run has superseded it. A new event type, a new synthesis
type, or a changed emitter fails here rather than being read back as fresh.

The first run of a path is exempt by construction: it has nothing earlier of its own
to invalidate, and it materializes the Person and Project context entities that no
path ever restates.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest

from multiplayer.db.connection import Database
from multiplayer.domain.meta import invalidation_class
from multiplayer.domain.models import (
    OntologyEntityKind,
    OntologyExtractor,
    OutputDisposition,
)
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService

KNOWN = frozenset({"owner"})

WritePath = Callable[[MultiplayerService, str, int], Awaitable[None]]


@dataclass(frozen=True)
class _Observed:
    """What one run of a write path emitted, and what it newly asserted."""

    emitted: frozenset[str]
    asserted_kinds: frozenset[OntologyEntityKind]


async def _observe(
    service: MultiplayerService, room_id: str, path: WritePath, run: int
) -> _Observed:
    head = await service.repos.events.get_latest_sequence(room_id)
    before = {entity.entity_id for entity in await service.repos.ontology.list_entities(room_id)}
    await path(service, room_id, run)
    events = [event for event in await service.get_room_events(room_id) if event.sequence > head]
    written = [
        entity
        for entity in await service.repos.ontology.list_entities(room_id)
        if entity.entity_id not in before
    ]
    return _Observed(
        emitted=frozenset(event.event_type.value for event in events),
        asserted_kinds=frozenset(entity.kind for entity in written),
    )


async def _create_task(service: MultiplayerService, room_id: str, run: int) -> None:
    await service.create_task(room_id, f"Ship the gateway {run}", created_by="owner")
    await service.run_ontology_extraction(room_id, OntologyExtractor.IMMEDIATE)


async def _create_decision(service: MultiplayerService, room_id: str, run: int) -> None:
    await service.create_decision(room_id, f"Adopt the gateway {run}", "content", "owner")
    await service.run_ontology_extraction(room_id, OntologyExtractor.IMMEDIATE)


async def _publish_decision_brief(service: MultiplayerService, room_id: str, run: int) -> None:
    await service.synthesize_decision_brief(room_id, f"Adopt the provider {run}", "owner")


WRITE_PATHS: dict[str, WritePath] = {
    "task": _create_task,
    "decision": _create_decision,
    "decision brief": _publish_decision_brief,
}


async def _seed(service: MultiplayerService) -> str:
    org = await service.create_organization("Invalidation", "invalidation-org", "owner")
    workspace = await service.create_workspace(org.org_id, "Engineering", "invalidation", "owner")
    room = await service.create_room(workspace.workspace_id, "Invalidation", "owner")
    templates = await service.list_agent_templates()
    for template, prompt in zip(templates[:2], ("first evidence", "second evidence"), strict=True):
        agent = await service.spawn_agent(room.room_id, template.template_id)
        session = await service.start_agent_session(room.room_id, agent.agent_id)
        execution = await service.start_execution(session.session_id, "owner")
        result = await service.execute_agent_step(execution.execution_id, prompt)
        await service.select_output(
            room.room_id, str(result["output_id"]), OutputDisposition.INCLUDED, "owner"
        )
    return room.room_id


@pytest.mark.asyncio
async def test_repeating_a_write_path_emits_its_own_invalidation_events() -> None:
    db = Database(":memory:")
    await db.connect()
    try:
        service = MultiplayerService(db, RealtimeHub(), known_users=KNOWN)
        await service.initialize()
        room_id = await _seed(service)

        covered: set[OntologyEntityKind] = set()
        for name, path in WRITE_PATHS.items():
            await _observe(service, room_id, path, 1)
            repeat = await _observe(service, room_id, path, 2)
            assert repeat.asserted_kinds, f"{name} asserted nothing on its second run"
            for kind in repeat.asserted_kinds:
                assert set(invalidation_class(kind)) & repeat.emitted, (
                    f"{name} re-asserted {kind.value} while emitting "
                    f"{sorted(repeat.emitted)}, none of which invalidates it"
                )
                covered.add(kind)
        # The derivation is worth only the kinds it actually reached.
        assert {
            OntologyEntityKind.TASK,
            OntologyEntityKind.DECISION,
            OntologyEntityKind.ARTIFACT,
            OntologyEntityKind.CLAIM,
        } <= covered
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_second_decision_brief_leaves_the_first_decision_not_current() -> None:
    """The instance the derived case above generalizes."""
    db = Database(":memory:")
    await db.connect()
    try:
        service = MultiplayerService(db, RealtimeHub(), known_users=KNOWN)
        await service.initialize()
        room_id = await _seed(service)
        await service.synthesize_decision_brief(room_id, "Adopt the provider", "owner")
        await service.synthesize_decision_brief(room_id, "Stay self-managed", "owner")

        answer = await service.answer_decision_meta(
            room_id, "what decisions require attention", user_id="owner"
        )
        decisions = [
            claim
            for claim in [*answer["claims"], *answer["unconfirmed"]]
            if claim["kind"] == OntologyEntityKind.DECISION.value
        ]
        assert len(decisions) == 2
        superseded, latest = sorted(decisions, key=lambda claim: claim["asserted_at_sequence"])
        assert superseded["label"] == "Adopt the provider"
        assert superseded["current"] is False
        assert superseded["invalidating_events"] >= 1
        assert latest["label"] == "Stay self-managed"
        assert latest["current"] is True
    finally:
        await db.close()
