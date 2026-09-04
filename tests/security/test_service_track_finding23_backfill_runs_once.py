"""Finding 23: the startup event chain backfill only runs the boot that
applies the migration adding prev_hash/event_hash, never again after that.

``_backfill_event_chain`` used to re-hash any NULL event_hash on every
startup, with no way to tell a legacy row (never hashed, from before the
migration ran) from a tampered one (its stored hash cleared by an attacker
with database write access after the migration had long since run). Both
looked the same to it, and both got a fresh hash computed and written, so an
edited row's hash break vanished on the very next restart. This proves a
second boot against the same database, the realistic shape of a restart,
leaves a since cleared hash alone rather than silently repairing it.
"""

from __future__ import annotations

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService

OWNER = "owner"


@pytest.fixture
async def db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    database = Database(":memory:")
    await database.connect()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_a_second_boot_does_not_repair_a_hash_cleared_after_the_first(
    db: Database,
) -> None:
    first_boot = MultiplayerService(db, RealtimeHub(), known_users=frozenset({OWNER}))
    await first_boot.initialize()
    assert first_boot._event_chain_migration_is_new, (
        "the boot that applies the migration must be told it is the first"
    )

    org = await first_boot.create_organization("Finding23 org", "finding23-org", OWNER)
    workspace = await first_boot.create_workspace(org.org_id, "Main", "main", OWNER)
    room = await first_boot.create_room(workspace.workspace_id, "Decision", OWNER)
    events = await first_boot.get_room_events(room.room_id)
    assert events, "room creation should have appended at least one event"
    event_id = events[0].event_id

    # Simulate an attacker with database write access erasing a stored hash,
    # after the fact, to make an edited event pass as one the chain never
    # covered, then a restart against the same, now tampered, database.
    # Round 2 (crypto track) added an append-only trigger on room_events
    # (migration 050) that refuses this exact rewrite from anyone still
    # holding this connection; a file-level attacker drops it first, the
    # same as here.
    await db.execute("DROP TRIGGER IF EXISTS room_events_reject_hash_rewrite")
    await db.execute("UPDATE room_events SET event_hash = NULL WHERE event_id = ?", (event_id,))
    await db.commit()

    second_boot = MultiplayerService(db, RealtimeHub(), known_users=frozenset({OWNER}))
    await second_boot.initialize()
    assert not second_boot._event_chain_migration_is_new, (
        "a restart against a database that already has the migration is not the first boot"
    )

    row = await db.fetch_one("SELECT event_hash FROM room_events WHERE event_id = ?", (event_id,))
    assert row is not None
    assert row["event_hash"] is None, "a NULLed hash must not be silently repaired on restart"
