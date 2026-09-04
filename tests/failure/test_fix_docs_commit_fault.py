"""Drive send_message and artifact creation through a storage failure on the
transaction's own COMMIT, not on a mid-statement execute.

Finding 74: ``FaultInjectingDatabase`` could only fault ``execute``, and
``Database.transaction()`` issues ``BEGIN``/``COMMIT``/``ROLLBACK`` straight
against the raw connection, so the COMMIT-then-ROLLBACK path in
``transaction()`` had no test at all: every existing fault-injection test
proves an error raised *before* COMMIT rolls back, which SQLite guarantees
regardless of the service code above it. This file proves the same durable
all-or-nothing outcome when the failure is COMMIT itself.
"""

import sqlite3

import pytest

from multiplayer.db.connection import Database
from multiplayer.domain.models import ArtifactType, MessageRole
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService
from tests.failure.fault_injection import FaultInjectingDatabase


async def _new_room(db: Database) -> tuple[MultiplayerService, str]:
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset())
    await svc.initialize()
    org = await svc.create_organization("Org", "org", "u1")
    ws = await svc.create_workspace(org.org_id, "Workspace", "ws", "u1")
    room = await svc.create_room(ws.workspace_id, "Room", "u1")
    return svc, room.room_id


@pytest.mark.asyncio
async def test_send_message_leaves_no_partial_row_when_commit_fails():
    db = FaultInjectingDatabase(":memory:")
    svc, room_id = await _new_room(db)
    db.fail_on_commit = db.transaction_count + 1  # the send_message transaction

    with pytest.raises(sqlite3.OperationalError):
        await svc.send_message(room_id, MessageRole.HUMAN, "u1", "hello")

    messages = await db.fetch_all("SELECT * FROM messages WHERE room_id = ?", (room_id,))
    events = await db.fetch_all(
        "SELECT * FROM room_events WHERE room_id = ? AND event_type = 'message.created'",
        (room_id,),
    )
    assert messages == []
    assert events == []
    await db.close()


@pytest.mark.asyncio
async def test_create_artifact_leaves_no_partial_row_when_commit_fails():
    db = FaultInjectingDatabase(":memory:")
    svc, room_id = await _new_room(db)
    db.fail_on_commit = db.transaction_count + 1  # the create_artifact transaction

    with pytest.raises(sqlite3.OperationalError):
        await svc.create_artifact(
            room_id, "Doc", ArtifactType.DOCUMENT, created_by="u1", content="v1"
        )

    artifacts = await db.fetch_all("SELECT * FROM artifacts WHERE room_id = ?", (room_id,))
    events = await db.fetch_all(
        "SELECT * FROM room_events WHERE room_id = ? AND event_type = 'artifact.created'",
        (room_id,),
    )
    assert artifacts == []
    assert events == []
    await db.close()


@pytest.mark.asyncio
async def test_send_message_persists_normally_when_commit_does_not_fail():
    """The fault-injecting subclass with ``fail_on_commit`` unset behaves
    exactly like a plain ``Database``, so the fault above is the only reason
    the two tests fail."""
    db = FaultInjectingDatabase(":memory:")
    svc, room_id = await _new_room(db)

    msg = await svc.send_message(room_id, MessageRole.HUMAN, "u1", "hello")

    messages = await db.fetch_all("SELECT * FROM messages WHERE message_id = ?", (msg.message_id,))
    assert len(messages) == 1
    await db.close()
