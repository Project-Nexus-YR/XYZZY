"""Regression coverage for persistent, inspectable agent outputs."""

import hashlib
import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from multiplayer.db.connection import Database
from multiplayer.domain.events import EventType, RoomEvent
from multiplayer.domain.models import (
    AgentOutput,
    DomainError,
    Execution,
    ExecutionStatus,
    SessionStatus,
)
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService


@pytest.fixture
async def service():
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub())
    await svc.initialize()
    yield svc
    await db.close()


async def _pending_execution(service: MultiplayerService):
    org = await service.create_organization("Acme", "acme", "u1")
    workspace = await service.create_workspace(org.org_id, "Main", "main", "u1")
    room = await service.create_room(workspace.workspace_id, "Architecture", "u1")
    template = (await service.list_agent_templates())[0]
    agent = await service.spawn_agent(room.room_id, template.template_id)
    session = await service.start_agent_session(room.room_id, agent.agent_id)
    execution = await service.start_execution(session.session_id)
    return room, agent, session, execution


@pytest.mark.asyncio
async def test_workflow_only_run_persists_labelled_output_and_reconnect_state(service):
    room, agent, session, execution = await _pending_execution(service)

    result = await service.execute_agent_step(
        execution.execution_id, "Assess PostgreSQL migration risk"
    )

    assert result["status"] == "ok"
    assert result["action"] == "finish"
    output = await service.repos.agent_outputs.get(result["output_id"])
    assert output is not None
    assert output.agent_id == agent.agent_id
    assert output.session_id == session.session_id
    assert output.execution_id == execution.execution_id
    assert output.content.startswith("SIMULATED WORKFLOW OUTPUT")
    assert output.output_data["simulated"] is True
    assert output.output_data["provider"] == "workflow-only"
    assert "stub response" not in output.content
    assert output.source_prompt == "Assess PostgreSQL migration risk"

    state = await service.get_room_state(room.room_id)
    assert state["runs"] == [
        {
            "execution_id": execution.execution_id,
            "session_id": session.session_id,
            "agent_id": agent.agent_id,
            "run_id": f"run_{execution.execution_id}",
            "status": "COMPLETED",
            "started_at": state["runs"][0]["started_at"],
            "completed_at": state["runs"][0]["completed_at"],
        }
    ]
    assert state["outputs"][0]["output_id"] == output.output_id
    assert state["outputs"][0]["content"] == output.content
    assert state["outputs"][0]["source_prompt"] == "Assess PostgreSQL migration risk"

    event_types = [event.event_type for event in await service.get_room_events(room.room_id)]
    assert EventType.AGENT_OUTPUT_CREATED in event_types
    assert EventType.AGENT_RUN_COMPLETED in event_types


@pytest.mark.asyncio
async def test_provider_provenance_migration_preserves_legacy_output_rows() -> None:
    db = Database(":memory:")
    await db.connect()
    initial = Path("src/multiplayer/migrations/001_initial.sql").read_text()
    await db.execute_script(initial)
    await db.execute("PRAGMA foreign_keys=OFF")
    await db.execute(
        "INSERT INTO agent_outputs(output_id, room_id, session_id, execution_id, agent_id, "
        "content, output_data, source_prompt, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "out_legacy",
            "room_legacy",
            "session_legacy",
            "execution_legacy",
            "agent_legacy",
            "historical evidence",
            "{}",
            "historical prompt",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    await db.execute("PRAGMA foreign_keys=ON")
    svc = MultiplayerService(db, RealtimeHub())
    try:
        await svc.initialize()
        output = await svc.repos.agent_outputs.get("out_legacy")
        assert output is not None
        assert output.source_prompt == "historical prompt"
        assert output.provider_input == ""
        assert output.provider_name == ""
        assert output.provider_model == ""
        assert output.provider_response_id == ""
        assert output.provider_interventions == ()
        assert output.provider_evidence == ""
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_frozen_provenance_migration_backfills_legacy_claim_and_hash() -> None:
    db = Database(":memory:")
    await db.connect()
    migrations = Path("src/multiplayer/migrations")
    await db.execute_script((migrations / "001_initial.sql").read_text())
    await db.execute_script((migrations / "002_agent_output_provider_provenance.sql").read_text())
    await db.execute(
        "CREATE TABLE schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    await db.executemany(
        "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
        [
            ("001_initial.sql", "2026-01-01T00:00:00+00:00"),
            ("002_agent_output_provider_provenance.sql", "2026-01-01T00:00:00+00:00"),
        ],
    )
    await db.execute("PRAGMA foreign_keys=OFF")
    await db.execute(
        "INSERT INTO agent_outputs(output_id, room_id, session_id, execution_id, agent_id, "
        "content, output_data, source_prompt, provider_input, provider_name, provider_model, "
        "provider_response_id, provider_interventions, provider_evidence, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "out_before_snapshot",
            "room_legacy",
            "session_legacy",
            "execution_legacy",
            "agent_legacy",
            "legacy evidence",
            "{}",
            "legacy human prompt",
            "legacy exact provider input",
            "openai",
            "gpt-legacy",
            "resp_legacy",
            '["legacy intervention"]',
            "legacy evidence",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    content = "# Legacy decision\n"
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    await db.execute(
        "INSERT INTO artifact_versions(version_id, artifact_id, version_number, content, "
        "content_hash, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "ver_legacy",
            "art_legacy",
            1,
            content,
            content_hash,
            "owner",
            "2026-01-01T00:00:00+00:00",
        ),
    )
    await db.execute(
        "INSERT INTO artifact_claims(claim_id, version_id, ordinal, text, is_ai_derived, "
        "confidence) VALUES (?, ?, ?, ?, ?, ?)",
        ("claim_legacy", "ver_legacy", 1, "legacy evidence", 1, 1.0),
    )
    await db.execute(
        "INSERT INTO artifact_claim_sources(claim_id, output_id, evidence) VALUES (?, ?, ?)",
        ("claim_legacy", "out_before_snapshot", "legacy evidence"),
    )
    await db.execute("PRAGMA foreign_keys=ON")
    svc = MultiplayerService(db, RealtimeHub())
    try:
        await svc.initialize()
        provenance = await svc.repos.artifacts.get_version_provenance("ver_legacy")
        assert provenance[0]["provider_input"] == "legacy exact provider input"
        assert provenance[0]["provider_interventions"] == ["legacy intervention"]
        version = await svc.repos.artifacts.get_version("ver_legacy")
        assert version is not None
        assert svc.verify_artifact_provenance_hash(version, provenance)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_publication_identity_migration_rehashes_prior_version_deterministically() -> None:
    db = Database(":memory:")
    await db.connect()
    migrations = Path("src/multiplayer/migrations")
    for name in (
        "001_initial.sql",
        "002_agent_output_provider_provenance.sql",
        "003_frozen_artifact_provenance.sql",
    ):
        await db.execute_script((migrations / name).read_text())
    await db.execute(
        "CREATE TABLE schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    await db.executemany(
        "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
        [
            (name, "2026-01-01T00:00:00+00:00")
            for name in (
                "001_initial.sql",
                "002_agent_output_provider_provenance.sql",
                "003_frozen_artifact_provenance.sql",
            )
        ],
    )
    await db.execute("PRAGMA foreign_keys=OFF")
    content = "legacy version without claims"
    await db.execute(
        "INSERT INTO artifact_versions(version_id, artifact_id, version_number, content, "
        "content_hash, provenance_hash, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "ver_old_envelope",
            "art_old_envelope",
            2,
            content,
            hashlib.sha256(content.encode()).hexdigest(),
            "old-envelope-hash",
            "  owner  ",
            "2026-01-01T01:00:00+01:00",
        ),
    )
    await db.execute("PRAGMA foreign_keys=ON")
    svc = MultiplayerService(db, RealtimeHub())
    try:
        await svc.initialize()
        version = await svc.repos.artifacts.get_version("ver_old_envelope")
        assert version is not None
        assert version.provenance_hash != "old-envelope-hash"
        assert svc.verify_artifact_provenance_hash(version, [])
        # Equivalent author/timestamp representations produce the same commitment.
        normalized = replace(
            version,
            created_by="owner",
            created_at=datetime.fromisoformat("2026-01-01T00:00:00+00:00"),
        )
        assert svc._artifact_provenance_hash(version, []) == svc._artifact_provenance_hash(
            normalized, []
        )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_terminal_execution_cannot_create_duplicate_output(service):
    _, _, _, execution = await _pending_execution(service)
    await service.execute_agent_step(execution.execution_id, "First prompt")

    with pytest.raises(DomainError, match="is terminal"):
        await service.execute_agent_step(execution.execution_id, "Retry prompt")

    assert (
        len(
            await service.repos.agent_outputs.list_by_room(
                (await service.repos.sessions.get(execution.session_id)).room_id
            )
        )
        == 1
    )


@pytest.mark.asyncio
async def test_output_state_and_events_roll_back_as_one_unit(service):
    room, agent, session, execution = await _pending_execution(service)
    output = AgentOutput(
        output_id="out_atomic",
        room_id=room.room_id,
        session_id=session.session_id,
        execution_id=execution.execution_id,
        agent_id=agent.agent_id,
        content="candidate",
        output_data={"result": "candidate"},
        source_prompt="question",
    )
    duplicate = RoomEvent(
        event_id="evt_duplicate",
        room_id=room.room_id,
        sequence=0,
        event_type=EventType.AGENT_OUTPUT_CREATED,
        payload={"output_id": output.output_id},
        actor_id=agent.agent_id,
        actor_type="agent",
    )

    with pytest.raises(sqlite3.IntegrityError):
        await service.repos.agent_outputs.complete_execution(output, [duplicate, duplicate])

    assert await service.repos.agent_outputs.get(output.output_id) is None
    persisted_execution = await service.repos.executions.get(execution.execution_id)
    assert persisted_execution is not None
    assert persisted_execution.status == ExecutionStatus.PENDING
    assert all(
        event.event_id != duplicate.event_id
        for event in await service.get_room_events(room.room_id)
    )


@pytest.mark.asyncio
async def test_run_start_state_and_event_roll_back_as_one_unit(service):
    org = await service.create_organization("Acme", "acme", "u1")
    workspace = await service.create_workspace(org.org_id, "Main", "main", "u1")
    room = await service.create_room(workspace.workspace_id, "Architecture", "u1")
    template = (await service.list_agent_templates())[0]
    agent = await service.spawn_agent(room.room_id, template.template_id)
    session = await service.start_agent_session(room.room_id, agent.agent_id)
    execution = Execution(
        execution_id="exec_atomic_start",
        session_id=session.session_id,
        agent_id=agent.agent_id,
    )
    existing_event = (await service.get_room_events(room.room_id))[0]
    colliding_event = RoomEvent(
        event_id=existing_event.event_id,
        room_id=room.room_id,
        sequence=0,
        event_type=EventType.AGENT_RUN_STARTED,
        payload={"execution_id": execution.execution_id},
        actor_id=agent.agent_id,
        actor_type="agent",
    )

    with pytest.raises(sqlite3.IntegrityError):
        await service.repos.executions.start_with_event(execution, colliding_event)

    persisted_session = await service.repos.sessions.get(session.session_id)
    assert persisted_session is not None
    assert persisted_session.status == SessionStatus.CREATED
    assert await service.repos.executions.get(execution.execution_id) is None
