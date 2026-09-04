"""Drive send_message and artifact creation through a storage failure on the
last write of their transaction, and confirm the row and its event are
either both present or both absent, never one without the other.

Finding 13: nothing in the suite ever injected a storage-layer failure, so
nobody could tell what a partial write under a real error leaves behind.
The agent task transition path is covered by the service track, not here.
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


async def _count_execute_calls(coro_factory) -> int:
    """How many ``db.execute`` calls one action takes, with no fault armed."""
    db = FaultInjectingDatabase(":memory:", fail_on_execute=0)
    svc, room_id = await _new_room(db)
    baseline = db.execute_count
    await coro_factory(svc, room_id)
    await db.close()
    return db.execute_count - baseline


@pytest.mark.asyncio
async def test_send_message_leaves_no_partial_row_when_storage_fails():
    async def action(svc, room_id):
        await svc.send_message(room_id, MessageRole.HUMAN, "u1", "hello")

    calls = await _count_execute_calls(action)

    db = FaultInjectingDatabase(":memory:", fail_on_execute=0)
    svc, room_id = await _new_room(db)
    db.fail_on_execute = db.execute_count + calls  # fail on the last write

    with pytest.raises(sqlite3.OperationalError):
        await svc.send_message(room_id, MessageRole.HUMAN, "u1", "hello")

    messages = await db.fetch_all("SELECT * FROM messages WHERE room_id = ?", (room_id,))
    events = await db.fetch_all(
        "SELECT * FROM room_events WHERE room_id = ? AND event_type = 'message.created'",
        (room_id,),
    )
    assert (len(messages) == 0) == (len(events) == 0), (
        "a failed send_message must leave the message and its event both "
        "present or both absent, never one without the other"
    )
    assert len(messages) == 0
    assert len(events) == 0
    await db.close()


@pytest.mark.asyncio
async def test_send_message_persists_both_row_and_event_when_storage_does_not_fail():
    db = FaultInjectingDatabase(":memory:", fail_on_execute=0)
    svc, room_id = await _new_room(db)

    msg = await svc.send_message(room_id, MessageRole.HUMAN, "u1", "hello")

    messages = await db.fetch_all("SELECT * FROM messages WHERE message_id = ?", (msg.message_id,))
    events = await db.fetch_all(
        "SELECT * FROM room_events WHERE room_id = ? AND event_type = 'message.created'",
        (room_id,),
    )
    assert len(messages) == 1
    assert len(events) == 1
    await db.close()


@pytest.mark.asyncio
async def test_artifact_publish_leaves_no_partial_row_when_storage_fails():
    async def action(svc, room_id):
        await svc.create_artifact(
            room_id, "Doc", ArtifactType.DOCUMENT, created_by="u1", content="hello"
        )

    calls = await _count_execute_calls(action)

    db = FaultInjectingDatabase(":memory:", fail_on_execute=0)
    svc, room_id = await _new_room(db)
    db.fail_on_execute = db.execute_count + calls  # fail on the last write

    with pytest.raises(sqlite3.OperationalError):
        await svc.create_artifact(
            room_id, "Doc", ArtifactType.DOCUMENT, created_by="u1", content="hello"
        )

    artifacts = await db.fetch_all("SELECT * FROM artifacts WHERE room_id = ?", (room_id,))
    events = await db.fetch_all(
        "SELECT * FROM room_events WHERE room_id = ? AND event_type = 'artifact.created'",
        (room_id,),
    )
    assert len(artifacts) == 0
    assert len(events) == 0
    await db.close()
