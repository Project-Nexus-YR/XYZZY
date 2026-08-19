"""Concurrency tests for RealtimeHub: subscribe, unsubscribe, broadcast races."""

import asyncio
import pytest
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.domain.events import EventType, RoomEvent


@pytest.fixture
def hub():
    return RealtimeHub()


@pytest.mark.asyncio
async def test_concurrent_subscribe_unsubscribe(hub):
    """50 concurrent subscribes followed by 50 unsubscribes must not leak."""
    room_id = "race_room"
    sub_ids = []

    async def do_subscribe(i: int):
        sub = await hub.subscribe(room_id, f"user_{i}")
        return sub.subscription_id

    sub_ids = await asyncio.gather(*(do_subscribe(i) for i in range(50)))
    assert len(sub_ids) == 50
    assert await hub.room_subscriber_count(room_id) == 50

    await asyncio.gather(*(hub.unsubscribe(sid) for sid in sub_ids))
    assert await hub.room_subscriber_count(room_id) == 0


@pytest.mark.asyncio
async def test_concurrent_broadcast(hub):
    """Broadcasting to 100 subscribers must deliver to all without blocking."""
    room_id = "broadcast_room"
    subs = []
    for i in range(100):
        sub = await hub.subscribe(room_id, f"user_{i}")
        subs.append(sub)

    event = {"type": "test", "data": "hello"}
    delivered = await hub.broadcast_to_room(room_id, event)
    assert len(delivered) == 100

    for sub in subs:
        msg = sub.queue.get_nowait()
        assert msg == event


@pytest.mark.asyncio
async def test_broadcast_during_concurrent_unsubscribe(hub):
    """Broadcast should handle subscribers being removed mid-iteration."""
    room_id = "race_broadcast"
    subs = []
    for i in range(20):
        sub = await hub.subscribe(room_id, f"user_{i}")
        subs.append(sub)

    async def broadcast_and_unsub():
        return await hub.broadcast_to_room(room_id, {"type": "test"})

    async def unsub_some():
        for sub in subs[:10]:
            await hub.unsubscribe(sub.subscription_id)

    delivered_results = await asyncio.gather(
        broadcast_and_unsub(),
        unsub_some(),
    )

    broadcast_result = delivered_results[0]
    assert broadcast_result is not None
    assert len(broadcast_result) >= 10


@pytest.mark.asyncio
async def test_send_to_user_concurrent(hub):
    """send_to_user must find all subscriptions across rooms."""
    for room in ["r1", "r2", "r3"]:
        await hub.subscribe(room, "shared_user")

    delivered = await hub.send_to_user("shared_user", {"msg": "hi"})
    assert delivered is True

    for room in ["r1", "r2", "r3"]:
        sub_ids = await hub.get_subscriptions_for_user_room("shared_user", room)
        assert len(sub_ids) == 1
