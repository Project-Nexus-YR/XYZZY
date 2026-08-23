"""Concurrency acceptance for derived thread reply counts.

A stored counter is what desyncs under concurrent replies. This layer stores none:
every count is a COUNT() over the durable reply rows, so the only way the number can
be wrong is for a reply row to be missing, and the ordered log shows whether it is.
"""

from __future__ import annotations

import asyncio

import pytest

from multiplayer.db.connection import Database
from multiplayer.domain.models import Message, MessageRole
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService

CONCURRENT_REPLIES = 12


@pytest.mark.asyncio
async def test_concurrent_replies_keep_the_derived_count_exact() -> None:
    db = Database(":memory:")
    await db.connect()
    service = MultiplayerService(db, RealtimeHub())
    await service.initialize()
    try:
        org = await service.create_organization("Thread org", "thread-org", "owner")
        workspace = await service.create_workspace(org.org_id, "Main", "main", "owner")
        room = await service.create_room(workspace.workspace_id, "Thread", "owner")
        root = await service.send_message(
            room.room_id, MessageRole.HUMAN, "owner", "Should we migrate this quarter?"
        )
        gate = asyncio.Event()

        async def reply(index: int) -> Message:
            await gate.wait()
            return await service.send_message(
                room.room_id,
                MessageRole.HUMAN,
                "owner",
                f"Reply {index}",
                parent_message_id=root.message_id,
            )

        tasks = [asyncio.create_task(reply(i)) for i in range(CONCURRENT_REPLIES)]
        gate.set()
        replies = await asyncio.gather(*tasks)

        assert await service.repos.messages.count_replies(root.message_id) == CONCURRENT_REPLIES

        thread = await service.list_thread(root.message_id)
        assert thread[0].message.message_id == root.message_id
        assert thread[0].reply_count == CONCURRENT_REPLIES
        assert len(thread) == CONCURRENT_REPLIES + 1
        assert all(entry.reply_count == 0 for entry in thread[1:])

        # Every reply owns a distinct room sequence, and the thread reads in that order.
        sequences = [message.event_sequence for message in replies]
        assert len(set(sequences)) == CONCURRENT_REPLIES
        assert [entry.message.event_sequence for entry in thread] == sorted(
            [root.event_sequence, *sequences]
        )

        events = await service.get_room_events(room.room_id, root.event_sequence)
        created = [e for e in events if e.event_type.value == "message.created"]
        assert len(created) == CONCURRENT_REPLIES
        assert {e.payload["parent_message_id"] for e in created} == {root.message_id}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_concurrent_replies_to_different_depths_keep_their_own_counts() -> None:
    db = Database(":memory:")
    await db.connect()
    service = MultiplayerService(db, RealtimeHub())
    await service.initialize()
    try:
        org = await service.create_organization("Depth org", "depth-org", "owner")
        workspace = await service.create_workspace(org.org_id, "Main", "main", "owner")
        room = await service.create_room(workspace.workspace_id, "Depth", "owner")
        root = await service.send_message(room.room_id, MessageRole.HUMAN, "owner", "Root")
        child = await service.send_message(
            room.room_id,
            MessageRole.HUMAN,
            "owner",
            "Child",
            parent_message_id=root.message_id,
        )
        gate = asyncio.Event()

        async def reply(parent_id: str, index: int) -> Message:
            await gate.wait()
            return await service.send_message(
                room.room_id,
                MessageRole.HUMAN,
                "owner",
                f"Reply {index} to {parent_id}",
                parent_message_id=parent_id,
            )

        tasks = [asyncio.create_task(reply(root.message_id, i)) for i in range(4)]
        tasks += [asyncio.create_task(reply(child.message_id, i)) for i in range(6)]
        gate.set()
        await asyncio.gather(*tasks)

        counts = {
            entry.message.message_id: entry.reply_count
            for entry in await service.list_thread(root.message_id)
        }
        assert counts[root.message_id] == 5
        assert counts[child.message_id] == 6
        assert sum(counts.values()) == 11
    finally:
        await db.close()
