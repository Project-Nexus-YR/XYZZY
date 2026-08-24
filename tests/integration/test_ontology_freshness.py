"""Extraction freshness: cursors resume, currency is derived, and reads never write.

A cursor is a resume hint. Nothing a reader sees is decided by it, so an assertion is
current only when no event since it was written falls in its invalidation class — which
is asked at read time, per assertion, against one snapshotted head. The tests below drive
the drain past an event type it does not read and still expect the assertion to report as
of its own sequence, because that is exactly the defect a global cursor allowed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from multiplayer.db.connection import Database
from multiplayer.domain.events import EventType
from multiplayer.domain.models import MessageRole, OntologyExtractor
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService

MIGRATIONS = Path("src/multiplayer/migrations")
KNOWN = frozenset({"owner", "viewer"})


async def _service(db: Database) -> MultiplayerService:
    service = MultiplayerService(db, RealtimeHub(), known_users=KNOWN)
    await service.initialize()
    return service


async def _seed_room(service: MultiplayerService) -> str:
    org = await service.create_organization("Freshness", "freshness-org", "owner")
    workspace = await service.create_workspace(org.org_id, "Engineering", "freshness", "owner")
    room = await service.create_room(workspace.workspace_id, "Freshness", "owner")
    await service.invite_room_member(room.room_id, "viewer", "viewer", "owner")
    await service.create_task(room.room_id, "Ship the gateway", created_by="owner")
    return room.room_id


async def _counts(db: Database, room_id: str) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for name, table in (
        ("entities", "ontology_entities"),
        ("relationships", "ontology_relationships"),
    ):
        row = await db.fetch_one(
            f"SELECT COUNT(*) AS count FROM {table} WHERE room_id = ?", (room_id,)
        )
        rows[name] = int(row["count"]) if row else 0
    head = await db.fetch_one(
        "SELECT COALESCE(MAX(sequence), 0) AS head FROM room_events WHERE room_id = ?",
        (room_id,),
    )
    rows["head"] = int(head["head"]) if head else 0
    cursors = await db.fetch_all(
        "SELECT extractor, last_sequence FROM ontology_extraction_cursors WHERE room_id = ?",
        (room_id,),
    )
    rows["cursors"] = {str(row["extractor"]): int(row["last_sequence"]) for row in cursors}
    return rows


@pytest.mark.asyncio
async def test_a_second_pass_over_the_same_log_writes_nothing_and_emits_nothing() -> None:
    db = Database(":memory:")
    await db.connect()
    try:
        service = await _service(db)
        room_id = await _seed_room(service)
        first = await service.run_ontology_extraction(room_id, OntologyExtractor.IMMEDIATE)
        assert first["entities_written"] > 0
        # Drain the events the first pass emitted, so the log is quiet.
        await service.run_ontology_extraction(room_id, OntologyExtractor.IMMEDIATE)
        before = await _counts(db, room_id)
        events_before = await service.get_room_events(room_id)

        second = await service.run_ontology_extraction(room_id, OntologyExtractor.IMMEDIATE)
        assert second["entities_written"] == 0
        assert second["relationships_written"] == 0
        assert await _counts(db, room_id) == before
        assert await service.get_room_events(room_id) == events_before

        # New events, and every assertion they produce is positioned at the head the
        # pass snapshotted.
        await service.create_task(room_id, "Rotate the keys", created_by="owner")
        head = await service.repos.events.get_latest_sequence(room_id)
        third = await service.run_ontology_extraction(room_id, OntologyExtractor.IMMEDIATE)
        assert third["entities_written"] == 1
        fresh = [
            entity
            for entity in await service.repos.ontology.list_entities(room_id)
            if entity.label == "Rotate the keys"
        ]
        assert [entity.asserted_at_sequence for entity in fresh] == [head]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_failed_pass_rolls_back_its_cursor_with_its_assertions() -> None:
    db = Database(":memory:")
    await db.connect()
    try:
        service = await _service(db)
        room_id = await _seed_room(service)
        await service.run_ontology_extraction(room_id, OntologyExtractor.IMMEDIATE)
        before = await _counts(db, room_id)
        assert before["cursors"]["IMMEDIATE"] > 0

        await service.create_task(room_id, "Rotate the keys", created_by="owner")
        await db.execute_script(
            "CREATE TRIGGER reject_test_extraction BEFORE INSERT ON ontology_entities "
            "BEGIN SELECT RAISE(ABORT, 'injected extraction failure'); END;"
        )
        with pytest.raises(sqlite3.IntegrityError, match="injected extraction failure"):
            await service.run_ontology_extraction(room_id, OntologyExtractor.IMMEDIATE)
        after = await _counts(db, room_id)
        assert after["entities"] == before["entities"]
        assert after["cursors"] == before["cursors"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_currency_is_derived_from_the_log_not_from_the_cursor() -> None:
    db = Database(":memory:")
    await db.connect()
    try:
        service = await _service(db)
        room_id = await _seed_room(service)
        await service.run_ontology_extraction(room_id, OntologyExtractor.IMMEDIATE)
        task = next(
            entity
            for entity in await service.repos.ontology.list_entities(room_id)
            if entity.label == "Ship the gateway"
        )

        def status_of(answer: dict[str, Any]) -> dict[str, Any]:
            return next(
                claim for claim in answer["claims"] if claim["assertion_id"] == task.entity_id
            )

        answer = await service.answer_decision_meta(room_id, "what is the status", user_id="viewer")
        assert status_of(answer)["current"] is True

        # An event outside every invalidation class leaves the assertion current.
        await service.send_message(room_id, MessageRole.HUMAN, "owner", "unrelated chatter")
        cursors = (await _counts(db, room_id))["cursors"]
        answer = await service.answer_decision_meta(room_id, "what is the status", user_id="viewer")
        assert status_of(answer)["current"] is True

        # One event of the assertion's own class makes it not current, and the cursor
        # has not moved: the derivation, not a stamp, decides.
        tasks = await service.list_room_tasks(room_id)
        await service.assign_task(tasks[0].task_id, "agent-1", requested_by="owner")
        assert (await _counts(db, room_id))["cursors"] == cursors
        answer = await service.answer_decision_meta(room_id, "what is the status", user_id="viewer")
        stale = status_of(answer)
        assert stale["current"] is False
        assert stale["invalidating_events"] >= 1

        # Driving the drain past an event type it does not read advances its cursor to
        # head, and the assertion still reports as of its own sequence.
        await service.run_ontology_extraction(room_id, OntologyExtractor.ASYNC)
        drained = (await _counts(db, room_id))["cursors"]["ASYNC"]
        assert drained >= (await _counts(db, room_id))["head"] - 1
        answer = await service.answer_decision_meta(room_id, "what is the status", user_id="viewer")
        assert status_of(answer)["current"] is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_the_ontology_route_discloses_the_same_currency_as_the_meta_path() -> None:
    """One reader, one room, one account of a fact — including the copy in room state.

    The ontology route returned a superseded assertion byte-identical to a live one
    while `/meta` reported it not current, and the silent copy is the one a
    reconnecting client believes.
    """
    db = Database(":memory:")
    await db.connect()
    try:
        service = await _service(db)
        room_id = await _seed_room(service)
        await service.run_ontology_extraction(room_id, OntologyExtractor.IMMEDIATE)
        # One event of the task's own invalidation class.
        tasks = await service.list_room_tasks(room_id)
        await service.assign_task(tasks[0].task_id, "agent-1", requested_by="owner")

        answer = await service.answer_decision_meta(room_id, "what is the status", user_id="viewer")
        claims = {str(claim["assertion_id"]): claim for claim in answer["claims"]}
        assert claims, "the Meta path answered with nothing to compare against"

        ontology = await service.get_room_ontology(room_id)
        records = {str(entity["entity_id"]): entity for entity in ontology["entities"]}
        disclosed = ["current", "invalidating_events", "stale_at_sequence", "asserted_at_sequence"]
        compared = 0
        for assertion_id, claim in claims.items():
            record = records.get(assertion_id)
            if record is None:
                continue
            assert [record[field] for field in disclosed] == [claim[field] for field in disclosed]
            compared += 1
        assert compared, "no assertion appeared on both paths"
        stale = [entity for entity in ontology["entities"] if not entity["current"]]
        assert stale, "the assigned task is not current and the route did not say so"
        assert all(entity["invalidating_events"] >= 1 for entity in stale)
        current = [entity for entity in ontology["entities"] if entity["current"]]
        assert current, "every assertion reported stale, so the field decides nothing"

        # The copy embedded in room state is the same account, not a quieter one.
        state = await service.get_room_state(room_id, user_id="viewer")
        assert state["ontology"] == ontology
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_meta_read_over_a_backlog_changes_nothing_and_still_reports_the_lag() -> None:
    db = Database(":memory:")
    await db.connect()
    try:
        service = await _service(db)
        room_id = await _seed_room(service)
        await service.run_ontology_extraction(room_id, OntologyExtractor.IMMEDIATE)
        # A backlog: real events nothing has drained.
        for title in ("Rotate the keys", "Draft the runbook", "Review the schema"):
            await service.create_task(room_id, title, created_by="owner")
        before = await _counts(db, room_id)
        events_before = await service.get_room_events(room_id)

        for question in ("what is the status", "what changed", "what is blocking"):
            answer = await service.answer_decision_meta(room_id, question, user_id="viewer")
            assert answer["freshness"]["drain_lag_events"] > 0
            assert answer["freshness"]["authorized_head"] == before["head"]
            for claim in answer["claims"]:
                assert isinstance(claim["current"], bool)

        assert await _counts(db, room_id) == before
        assert await service.get_room_events(room_id) == events_before
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_an_extractor_with_no_cursor_row_reports_the_whole_log_as_lag() -> None:
    """An extractor that has never run has drained nothing, so it is the furthest behind.

    Nothing wakes the asynchronous drain, so a room whose structured pass has caught
    up with head still has every message undrained. Taking the minimum over only the
    cursor rows that happen to exist made that backlog invisible and reported the
    room as entirely current — the one disclosure the deferred drain depends on.
    """
    db = Database(":memory:")
    await db.connect()
    try:
        service = await _service(db)
        room_id = await _seed_room(service)
        # Twice, so the structured pass also drains the events it emitted itself.
        await service.run_ontology_extraction(room_id, OntologyExtractor.IMMEDIATE)
        await service.run_ontology_extraction(room_id, OntologyExtractor.IMMEDIATE)
        for text in (
            "Ship the gateway is blocked by Rotate the keys",
            "second",
            "third",
            "fourth",
            "fifth",
        ):
            await service.send_message(room_id, MessageRole.HUMAN, "owner", text)
        await service.run_ontology_extraction(room_id, OntologyExtractor.IMMEDIATE)

        counts = await _counts(db, room_id)
        assert counts["cursors"] == {"IMMEDIATE": counts["head"]}
        assert not [
            item
            for item in await service.repos.ontology.list_relationships(room_id)
            if item.extractor is OntologyExtractor.ASYNC
        ]
        answer = await service.answer_decision_meta(room_id, "what is the status", user_id="viewer")
        assert answer["freshness"]["drain_lag_events"] == counts["head"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migration_017_guards_its_cursor_and_backfills_legacy_assertions() -> None:
    db = Database(":memory:")
    await db.connect()
    try:
        timestamp = "2026-01-01T00:00:00+00:00"
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            if migration.name >= "017":
                continue
            await db.execute_script(migration.read_text())
        await db.execute(
            "INSERT INTO organizations(org_id, name, slug, created_at) VALUES (?, ?, ?, ?)",
            ("org_legacy", "Legacy", "legacy", timestamp),
        )
        await db.execute(
            "INSERT INTO workspaces(workspace_id, org_id, name, slug, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("ws_legacy", "org_legacy", "Legacy", "legacy", timestamp),
        )
        await db.execute(
            "INSERT INTO rooms(room_id, workspace_id, name, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("room_legacy", "ws_legacy", "Legacy", "owner", timestamp),
        )
        await db.execute(
            "INSERT INTO ontology_entities(entity_id, room_id, kind, source_object_id, label, "
            "properties, derivation_kind, confidence, evidence_ids, source_ids, review_status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "ont_legacy",
                "room_legacy",
                "Task",
                "task_legacy",
                "Legacy task",
                "{}",
                "SYSTEM_MATERIALIZED",
                1.0,
                '["task_legacy"]',
                '["task_legacy"]',
                "UNCONFIRMED",
                timestamp,
                timestamp,
            ),
        )
        await db.execute(
            "INSERT INTO ontology_relationships(relationship_id, room_id, kind, from_entity_id, "
            "to_entity_id, derivation_kind, confidence, evidence_ids, source_ids, review_status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "rel_legacy",
                "room_legacy",
                "OWNS",
                "ont_legacy",
                "ont_legacy",
                "SYSTEM_MATERIALIZED",
                1.0,
                '["task_legacy"]',
                '["task_legacy"]',
                "UNCONFIRMED",
                timestamp,
                timestamp,
            ),
        )
        await db.execute(
            "INSERT INTO room_events(event_id, room_id, sequence, event_type, payload, actor_id, "
            "actor_type, timestamp, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "evt_legacy",
                "room_legacy",
                7,
                EventType.ONTOLOGY_MATERIALIZED.value,
                '{"entity_ids": ["ont_legacy"], "relationship_ids": ["rel_legacy"]}',
                "owner",
                "user",
                timestamp,
                1,
            ),
        )

        await db.execute_script((MIGRATIONS / "017_ontology_freshness_and_meta.sql").read_text())

        integrity = await db.fetch_one("PRAGMA integrity_check")
        assert integrity is not None and next(iter(integrity.values())) == "ok"
        entity = await db.fetch_one(
            "SELECT * FROM ontology_entities WHERE entity_id = 'ont_legacy'"
        )
        assert entity is not None
        assert entity["extractor"] == "IMMEDIATE"
        assert entity["asserted_at_sequence"] == 7
        assert entity["stale_at_sequence"] is None
        relationship = await db.fetch_one(
            "SELECT * FROM ontology_relationships WHERE relationship_id = 'rel_legacy'"
        )
        assert relationship is not None
        assert relationship["asserted_at_sequence"] == 7
        # Backfilled from the edge's own from_entity, because '' would assert a chain
        # that does not exist.
        assert relationship["source_object_kind"] == "Task"
        assert relationship["source_object_id"] == "task_legacy"
        cursor = await db.fetch_one(
            "SELECT * FROM ontology_extraction_cursors WHERE room_id = 'room_legacy'"
        )
        assert cursor is not None
        assert cursor["extractor"] == "IMMEDIATE"
        assert cursor["last_sequence"] == 7

        with pytest.raises(sqlite3.IntegrityError):
            await db.execute(
                "INSERT INTO ontology_extraction_cursors(room_id, extractor, last_sequence, "
                "last_run_at) VALUES ('room_legacy', 'QUERY_TIME', 0, '')"
            )
        with pytest.raises(sqlite3.IntegrityError, match="must not rewind"):
            await db.execute(
                "UPDATE ontology_extraction_cursors SET last_sequence = 3 "
                "WHERE room_id = 'room_legacy' AND extractor = 'IMMEDIATE'"
            )
        await db.execute(
            "UPDATE ontology_extraction_cursors SET last_sequence = 9 "
            "WHERE room_id = 'room_legacy' AND extractor = 'IMMEDIATE'"
        )
        advanced = await db.fetch_one(
            "SELECT last_sequence FROM ontology_extraction_cursors WHERE room_id = 'room_legacy'"
        )
        assert advanced is not None and advanced["last_sequence"] == 9
    finally:
        await db.close()
