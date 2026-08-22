"""Upgrade proof for migrations 007-009."""

from pathlib import Path

import pytest

from multiplayer.db.connection import Database
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService


@pytest.mark.asyncio
async def test_legacy_execution_and_selection_backfill_to_honest_branch() -> None:
    db = Database(":memory:")
    await db.connect()
    try:
        await db.execute_script(Path("src/multiplayer/migrations/001_initial.sql").read_text())
        await db.execute(
            "CREATE TABLE schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        await db.execute(
            "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
            ("001_initial.sql", "2026-01-01T00:00:00+00:00"),
        )
        timestamp = "2026-01-01T00:00:00+00:00"
        await db.execute(
            "INSERT INTO organizations(org_id, name, slug, created_at) VALUES (?, ?, ?, ?)",
            ("org_old", "Old", "old", timestamp),
        )
        await db.execute(
            "INSERT INTO workspaces(workspace_id, org_id, name, slug, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("ws_old", "org_old", "Old", "old", timestamp),
        )
        await db.execute(
            "INSERT INTO rooms(room_id, workspace_id, name, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("room_old", "ws_old", "Old", "owner", timestamp),
        )
        await db.execute(
            "INSERT INTO agent_templates(template_id, name, role, created_at) VALUES (?, ?, ?, ?)",
            ("template_old", "Old", "Old", timestamp),
        )
        await db.execute(
            "INSERT INTO agent_instances(agent_id, template_id, room_id, name, role, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("agent_old", "template_old", "room_old", "Old", "Old", timestamp),
        )
        await db.execute(
            "INSERT INTO sessions(session_id, room_id, agent_id, status, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("session_old", "room_old", "agent_old", "COMPLETED", timestamp),
        )
        await db.execute(
            "INSERT INTO executions(execution_id, session_id, agent_id, status, started_at, "
            "completed_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("execution_old", "session_old", "agent_old", "COMPLETED", timestamp, timestamp),
        )
        await db.execute(
            "INSERT INTO agent_outputs(output_id, room_id, session_id, execution_id, agent_id, "
            "content, source_prompt, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "output_old",
                "room_old",
                "session_old",
                "execution_old",
                "agent_old",
                "Historical output",
                "Historical prompt",
                timestamp,
            ),
        )
        await db.execute(
            "INSERT INTO output_selections(room_id, output_id, disposition, decided_by, "
            "updated_at) VALUES (?, ?, ?, ?, ?)",
            ("room_old", "output_old", "INCLUDED", "owner", timestamp),
        )

        service = MultiplayerService(db, RealtimeHub())
        await service.initialize()

        execution = await service.repos.executions.get("execution_old")
        assert execution is not None and execution.branch_id == "branch_legacy_execution_old"
        branch = await service.get_branch(execution.branch_id)
        assert branch.initiating_prompt == "LEGACY_UNAVAILABLE"
        assert branch.context_hash == "LEGACY_UNAVAILABLE"
        assert branch.context_snapshot == {"boundary": "LEGACY_UNAVAILABLE"}
        selection = (await service.repos.output_selections.list_by_room("room_old"))[0]
        assert selection.branch_id == execution.branch_id
        tables = {
            row["name"]
            for row in await db.fetch_all(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
                "('branches', 'branch_syntheses', 'branch_synthesis_inputs', 'turn_locks')"
            )
        }
        assert tables == {
            "branches",
            "branch_syntheses",
            "branch_synthesis_inputs",
            "turn_locks",
        }
    finally:
        await db.close()
