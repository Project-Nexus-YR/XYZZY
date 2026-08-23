"""Every indexed kind is authorized by the same join, not just messages.

Widening the index is where a search feature usually leaks: a new kind arrives with
its own query, and the membership check that guarded the old one is not on it. So
each kind is proven twice over, against the database rather than against a response
body — a non-member and a member of a sibling room in the same workspace each
retrieve zero rows for an object that is demonstrably in the index.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from multiplayer.db.connection import Database
from multiplayer.domain.models import ArtifactType, MessageRole, SearchObjectKind
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService

OWNER = "user-owner"
SIBLING = "user-sibling"
OUTSIDER = "user-outsider"

# One rare word per kind, so a hit can only have come from that kind's object.
TERMS = {
    SearchObjectKind.MESSAGE: "harbourmaster",
    SearchObjectKind.ARTIFACT_VERSION: "cataphract",
    SearchObjectKind.TASK: "vellichor",
    SearchObjectKind.DECISION: "sundial",
    # The workflow-only provider writes a fixed payload; only room A runs an agent.
    SearchObjectKind.AGENT_OUTPUT: "provenance",
}


@dataclass(frozen=True)
class Seeded:
    room_a: str
    room_b: str
    object_ids: dict[SearchObjectKind, str]


@pytest.fixture
async def service():
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({OWNER, SIBLING, OUTSIDER}))
    await svc.initialize()
    yield svc
    await db.close()


async def _seed(svc: MultiplayerService) -> Seeded:
    """One object of every indexed kind in room A, and a sibling room B beside it."""
    org = await svc.create_organization("Org", "org", OWNER)
    workspace = await svc.create_workspace(org.org_id, "Main", "main", OWNER)
    room_a = await svc.create_room(workspace.workspace_id, "Private decision", OWNER)
    room_b = await svc.create_room(workspace.workspace_id, "Sibling channel", OWNER)
    await svc.invite_room_member(room_b.room_id, SIBLING, "editor", OWNER)

    message = await svc.send_message(
        room_a.room_id, MessageRole.HUMAN, OWNER, "harbourmaster ledger reconciliation"
    )
    task = await svc.create_task(
        room_a.room_id, "vellichor rollout", "kestrel budget overrun", created_by=OWNER
    )
    decision = await svc.create_decision(
        room_a.room_id,
        "sundial cutover",
        "moonstone is the chosen path",
        reason="marlinspike dissent recorded",
        created_by=OWNER,
    )
    artifact = await svc.create_artifact(
        room_a.room_id,
        "Runbook",
        ArtifactType.DOCUMENT,
        created_by=OWNER,
        content="cataphract staging plan",
    )
    version = (await svc.repos.artifacts.list_versions(artifact.artifact_id))[0]

    template = (await svc.list_agent_templates())[0]
    agent = await svc.spawn_agent(room_a.room_id, template.template_id)
    session = await svc.start_agent_session(room_a.room_id, agent.agent_id)
    execution = await svc.start_execution(session.session_id, OWNER)
    step = await svc.execute_agent_step(execution.execution_id, "zeppelin cadence audit")

    return Seeded(
        room_a=room_a.room_id,
        room_b=room_b.room_id,
        object_ids={
            SearchObjectKind.MESSAGE: message.message_id,
            SearchObjectKind.ARTIFACT_VERSION: version.version_id,
            SearchObjectKind.TASK: task.task_id,
            SearchObjectKind.DECISION: decision.decision_id,
            SearchObjectKind.AGENT_OUTPUT: str(step["output_id"]),
        },
    )


@pytest.mark.parametrize("kind", list(SearchObjectKind))
@pytest.mark.asyncio
async def test_a_non_member_and_a_sibling_room_member_retrieve_zero_rows(service, kind) -> None:
    seeded = await _seed(service)
    term = TERMS[kind]

    # The object really is in the index and really is in room A, so an empty result
    # below can only be the authorizing join and never a gap in indexing.
    documents = await service.db.fetch_all(
        "SELECT object_id, room_id FROM search_documents WHERE object_kind = ?", (kind.value,)
    )
    assert [row["object_id"] for row in documents] == [seeded.object_ids[kind]]
    assert documents[0]["room_id"] == seeded.room_a

    assert await service.search(OUTSIDER, term) == []
    assert await service.search(OUTSIDER, term, seeded.room_a) == []
    # A member of a sibling channel in the same workspace is still a non-member here.
    assert await service.search(SIBLING, term) == []
    assert await service.search(SIBLING, term, seeded.room_a) == []

    owner_hits = await service.search(OWNER, term)
    assert [(hit.object_kind, hit.object_id) for hit in owner_hits] == [
        (kind, seeded.object_ids[kind])
    ]


@pytest.mark.asyncio
async def test_membership_is_the_only_thing_the_sibling_was_missing(service) -> None:
    """The positive control: the zero rows above are authorization, not absence."""
    seeded = await _seed(service)
    await service.invite_room_member(seeded.room_a, SIBLING, "viewer", OWNER)

    found = {
        kind: [hit.object_id for hit in await service.search(SIBLING, term)]
        for kind, term in TERMS.items()
    }
    assert found == {kind: [seeded.object_ids[kind]] for kind in SearchObjectKind}


@pytest.mark.asyncio
async def test_losing_membership_stops_every_kind_matching(service) -> None:
    seeded = await _seed(service)
    await service.invite_room_member(seeded.room_a, SIBLING, "viewer", OWNER)
    await service.remove_room_member(seeded.room_a, SIBLING, OWNER)

    for term in TERMS.values():
        assert await service.search(SIBLING, term) == []
    # And the index still holds every object; only the reader changed.
    remaining = await service.db.fetch_all(
        "SELECT DISTINCT object_kind FROM search_documents ORDER BY object_kind"
    )
    assert [row["object_kind"] for row in remaining] == sorted(
        kind.value for kind in SearchObjectKind
    )
