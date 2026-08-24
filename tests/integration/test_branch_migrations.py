"""Upgrade proof for migrations 007-009 and 027."""

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


@pytest.mark.asyncio
async def test_027_rebuilds_agent_runs_and_suspended_turns_without_losing_a_row() -> None:
    """027 widens a CHECK and drops a column, and SQLite can do neither in place.

    Both tables are therefore rebuilt, copied and renamed, which is a migration that
    can silently lose the history it exists to protect. A run and the turn it was
    holding at a reviewer are written at the 026 schema here and read back after.
    """
    db = Database(":memory:")
    await db.connect()
    try:
        migrations = sorted(Path("src/multiplayer/migrations").glob("*.sql"))
        await db.execute(
            "CREATE TABLE schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        timestamp = "2026-01-01T00:00:00+00:00"
        for migration in migrations:
            if migration.name.startswith("027"):
                continue
            await db.execute_script(migration.read_text())
            await db.execute(
                "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                (migration.name, timestamp),
            )
        await db.execute(
            "INSERT INTO organizations(org_id, name, slug, created_at) VALUES (?, ?, ?, ?)",
            ("org_pre", "Pre", "pre", timestamp),
        )
        await db.execute(
            "INSERT INTO workspaces(workspace_id, org_id, name, slug, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("ws_pre", "org_pre", "Pre", "pre", timestamp),
        )
        await db.execute(
            "INSERT INTO rooms(room_id, workspace_id, name, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("room_pre", "ws_pre", "Pre", "owner", timestamp),
        )
        await db.execute(
            "INSERT INTO agent_templates(template_id, name, role, created_at) VALUES (?, ?, ?, ?)",
            ("template_pre", "Pre", "Pre", timestamp),
        )
        await db.execute(
            "INSERT INTO agent_instances(agent_id, template_id, room_id, name, role, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("agent_pre", "template_pre", "room_pre", "Pre", "Pre", timestamp),
        )
        await db.execute(
            "INSERT INTO agent_identities(identity_id, created_at, proof_mode, agent_id) "
            "VALUES (?, ?, ?, ?)",
            ("identity_pre", timestamp, "IN_PROCESS", "agent_pre"),
        )
        await db.execute(
            "INSERT INTO agent_room_memberships(membership_id, agent_id, room_id, joined_at) "
            "VALUES (?, ?, ?, ?)",
            ("member_pre", "agent_pre", "room_pre", timestamp),
        )
        await db.execute(
            "INSERT INTO sessions(session_id, room_id, agent_id, status, started_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("session_pre", "room_pre", "agent_pre", "ACTIVE", timestamp),
        )
        await db.execute(
            "INSERT INTO branches(branch_id, room_id, mode, status, initiated_by, "
            "initiating_prompt, context_event_sequence, context_hash, lifecycle_managed, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "branch_pre",
                "room_pre",
                "PARALLEL",
                "RUNNING",
                "owner",
                "LEGACY_LOW_LEVEL_WORKFLOW",
                0,
                "hash",
                0,
                timestamp,
                timestamp,
            ),
        )
        await db.execute(
            "INSERT INTO executions(execution_id, session_id, agent_id, status, started_at, "
            "authorized_by, branch_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "execution_pre",
                "session_pre",
                "agent_pre",
                "RUNNING",
                timestamp,
                "owner",
                "branch_pre",
            ),
        )
        await db.execute(
            "INSERT INTO agent_runs(run_id, execution_id, agent_id, identity_id, room_id, "
            "authorized_by, acting_user_id, harness_id, credential_hash, harness_state, "
            "lease_expires_at, created_at, attempts, max_attempts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "arun_pre",
                "execution_pre",
                "agent_pre",
                "identity_pre",
                "room_pre",
                "owner",
                "owner",
                "nexus",
                "hash",
                "AWAITING_APPROVAL",
                "2099-01-01T00:00:00+00:00",
                timestamp,
                2,
                3,
            ),
        )
        await db.execute(
            "INSERT INTO suspended_turns(execution_id, prompt, acting_as, observations, "
            "steerers, suspended_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("execution_pre", "Assess it.", "owner", '["a tool ran"]', '["steerer"]', timestamp),
        )

        service = MultiplayerService(db, RealtimeHub())
        await service.initialize()

        run = await service.repos.agent_runs.get("arun_pre")
        assert run is not None
        assert run.execution_id == "execution_pre"
        assert run.identity_id == "identity_pre"
        assert run.harness_state.value == "AWAITING_APPROVAL"
        assert (run.attempts, run.max_attempts) == (2, 3)
        parked = await service.repos.suspended_turns.claim("execution_pre")
        assert parked is not None
        assert parked["prompt"] == "Assess it."
        assert parked["observations"] == ["a tool ran"]
        assert await db.fetch_all("PRAGMA foreign_key_check") == []
    finally:
        await db.close()
