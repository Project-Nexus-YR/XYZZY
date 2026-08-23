"""What the cross-object index holds, what it deliberately leaves out, and how it
stops holding an object that is gone.

The authorization half of this feature lives in
tests/security/test_cross_object_search_authorization.py. This file is about
coverage and hygiene: that a room member finds the objects they would expect to
find, that a hit carries enough to open the object, that a field excluded from a
body cannot be reached through a snippet, and that a deleted or superseded object
leaves the index with it.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from multiplayer.db.connection import Database
from multiplayer.domain.models import ArtifactType, MessageRole, SearchObjectKind
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService

OWNER = "user-owner"


@dataclass(frozen=True)
class Seeded:
    room_id: str
    artifact_id: str
    version_id: str
    message_id: str
    task_id: str
    decision_id: str
    output_id: str


@pytest.fixture
async def service():
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({OWNER}))
    await svc.initialize()
    yield svc
    await db.close()


async def _seed(svc: MultiplayerService) -> Seeded:
    org = await svc.create_organization("Org", "org", OWNER)
    workspace = await svc.create_workspace(org.org_id, "Main", "main", OWNER)
    room = await svc.create_room(workspace.workspace_id, "Cutover", OWNER)

    message = await svc.send_message(
        room.room_id, MessageRole.HUMAN, OWNER, "harbourmaster ledger reconciliation"
    )
    task = await svc.create_task(
        room.room_id, "vellichor rollout", "kestrel budget overrun", created_by=OWNER
    )
    decision = await svc.create_decision(
        room.room_id,
        "sundial cutover",
        "moonstone is the chosen path",
        reason="marlinspike dissent recorded",
        created_by=OWNER,
    )
    artifact = await svc.create_artifact(
        room.room_id,
        "Runbook",
        ArtifactType.DOCUMENT,
        created_by=OWNER,
        content="cataphract staging plan",
    )
    version = (await svc.repos.artifacts.list_versions(artifact.artifact_id))[0]

    template = (await svc.list_agent_templates())[0]
    agent = await svc.spawn_agent(room.room_id, template.template_id)
    session = await svc.start_agent_session(room.room_id, agent.agent_id)
    execution = await svc.start_execution(session.session_id, OWNER)
    step = await svc.execute_agent_step(execution.execution_id, "zeppelin cadence audit")

    return Seeded(
        room_id=room.room_id,
        artifact_id=artifact.artifact_id,
        version_id=version.version_id,
        message_id=message.message_id,
        task_id=task.task_id,
        decision_id=decision.decision_id,
        output_id=str(step["output_id"]),
    )


async def _documents(svc: MultiplayerService) -> list[dict[str, object]]:
    return await svc.db.fetch_all(
        "SELECT object_kind, object_id, container_id, room_id, author_id, content, created_at "
        "FROM search_documents ORDER BY object_kind, object_id"
    )


@pytest.mark.asyncio
async def test_one_index_spans_chat_artifacts_tasks_outputs_and_decisions(service) -> None:
    seeded = await _seed(service)

    found = {
        term: [(hit.object_kind, hit.object_id) for hit in await service.search(OWNER, term)]
        for term in ("harbourmaster", "cataphract", "vellichor", "provenance", "moonstone")
    }
    assert found == {
        "harbourmaster": [(SearchObjectKind.MESSAGE, seeded.message_id)],
        "cataphract": [(SearchObjectKind.ARTIFACT_VERSION, seeded.version_id)],
        "vellichor": [(SearchObjectKind.TASK, seeded.task_id)],
        "provenance": [(SearchObjectKind.AGENT_OUTPUT, seeded.output_id)],
        "moonstone": [(SearchObjectKind.DECISION, seeded.decision_id)],
    }


@pytest.mark.asyncio
async def test_a_hit_carries_what_a_client_needs_to_open_the_object(service) -> None:
    seeded = await _seed(service)

    version_hit = (await service.search(OWNER, "cataphract"))[0]
    assert version_hit.object_kind is SearchObjectKind.ARTIFACT_VERSION
    assert version_hit.object_id == seeded.version_id
    # GET /artifacts/{artifact_id}/versions is the read path, so the version id
    # alone would strand the reader in the room.
    assert version_hit.container_id == seeded.artifact_id
    assert version_hit.room_id == seeded.room_id

    for term in ("harbourmaster", "vellichor", "provenance", "moonstone"):
        hit = (await service.search(OWNER, term))[0]
        assert hit.container_id == "", term
        assert hit.room_id == seeded.room_id


@pytest.mark.asyncio
async def test_a_snippet_cannot_reach_a_field_the_index_leaves_out(service) -> None:
    """Each excluded field is one no read path returns, or one that is not the
    object's own text at all."""
    seeded = await _seed(service)

    task = await service.repos.tasks.get(seeded.task_id)
    assert task is not None and task.description == "kestrel budget overrun"
    assert await service.search(OWNER, "kestrel") == []

    decision = await service.repos.decisions.get(seeded.decision_id)
    assert decision is not None and decision.reason == "marlinspike dissent recorded"
    assert await service.search(OWNER, "marlinspike") == []

    output = await service.repos.agent_outputs.get(seeded.output_id)
    assert output is not None
    # The rendered provider request holds the run's prompt and the specialist's
    # instructions: text assembled for the model, not text this output said.
    assert "zeppelin" in output.provider_input
    assert output.source_prompt == "zeppelin cadence audit"
    assert await service.search(OWNER, "zeppelin") == []

    stored = await service.db.fetch_all(
        "SELECT content FROM search_documents WHERE object_kind = 'AGENT_OUTPUT'"
    )
    assert [row["content"] for row in stored] == [output.content]


@pytest.mark.asyncio
async def test_a_superseded_artifact_version_stops_being_a_hit(service) -> None:
    seeded = await _seed(service)
    newer = await service.update_artifact(seeded.artifact_id, "obsidian rollback plan", OWNER)

    assert await service.search(OWNER, "cataphract") == []
    hits = await service.search(OWNER, "obsidian")
    assert [(hit.object_id, hit.container_id) for hit in hits] == [
        (newer.version_id, seeded.artifact_id)
    ]
    indexed = await service.db.fetch_all(
        "SELECT object_id FROM search_documents WHERE object_kind = 'ARTIFACT_VERSION'"
    )
    assert [row["object_id"] for row in indexed] == [newer.version_id]
    # The superseded version is still durable history, just not a search hit.
    versions = await service.repos.artifacts.list_versions(seeded.artifact_id)
    assert {version.version_id for version in versions} == {
        seeded.version_id,
        newer.version_id,
    }


@pytest.mark.asyncio
async def test_a_deleted_object_does_not_linger_in_the_index(service) -> None:
    """Every deletable indexed object takes its index row with it.

    Artifact versions and agent outputs are absent here because 005 forbids
    deleting either; the version above is how one of those leaves the index.
    """
    seeded = await _seed(service)

    await service.db.execute("DELETE FROM messages WHERE message_id = ?", (seeded.message_id,))
    await service.db.execute("DELETE FROM tasks WHERE task_id = ?", (seeded.task_id,))
    await service.db.execute("DELETE FROM decisions WHERE decision_id = ?", (seeded.decision_id,))

    for term in ("harbourmaster", "vellichor", "moonstone"):
        assert await service.search(OWNER, term) == [], term
    remaining = {row["object_kind"] for row in await _documents(service)}
    assert remaining == {"ARTIFACT_VERSION", "AGENT_OUTPUT"}


@pytest.mark.asyncio
async def test_the_backfill_indexes_what_predates_it_and_repeats_without_effect(
    service,
) -> None:
    await _seed(service)
    before = await _documents(service)

    # Every kind this migration added, as it would look on a database written
    # before the kind was on the allowlist.
    await service.db.execute("DELETE FROM search_documents WHERE object_kind <> 'MESSAGE'")
    await service.repos.search.backfill()
    first = await _documents(service)
    assert first == before

    await service.repos.search.backfill()
    assert await _documents(service) == first

    assert {row["object_kind"] for row in first} == {kind.value for kind in SearchObjectKind}
