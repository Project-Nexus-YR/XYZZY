"""Finding 72 (low): `revoke_room_access` and `send_to_user` dropped their
payload on a full queue with no resync marker (`log.debug` and a bare
`pass`, respectively) — unlike `broadcast_to_room`'s own overflow path,
which evicts the oldest entry and enqueues a `resync` marker
(`_handle_queue_overflow`) so the send loop notices and closes the socket.
A removed member whose queue happened to be full some other way stayed
subscribed and was never told; a `room_invited`/`room_removed` notification
on a full queue vanished without trace.

Both paths now route a full queue through the same `_handle_queue_overflow`
every other overflow already uses.
"""

from __future__ import annotations

import asyncio

from multiplayer.realtime.hub import RealtimeHub


async def _fill_queue(sub) -> None:
    while not sub.queue.full():
        sub.queue.put_nowait({"type": "room_event", "sequence": sub.queue.qsize()})


async def test_revoke_room_access_forces_a_resync_on_a_full_queue() -> None:
    hub = RealtimeHub()
    sub = await hub.subscribe("room1", "user1")
    await _fill_queue(sub)

    revoked = await hub.revoke_room_access("user1", "room1")

    assert revoked == 1
    drained = []
    while not sub.queue.empty():
        drained.append(sub.queue.get_nowait())
    # The oldest entry was evicted to make room; the marker forces the send
    # loop to close the socket, exactly as the room_removed side effect
    # (access_revoked itself) would have, if it had fit.
    assert drained[-1] == {"type": "resync"}


async def test_send_to_user_forces_a_resync_on_a_full_queue_instead_of_dropping() -> None:
    hub = RealtimeHub()
    sub = await hub.subscribe("room1", "user1")
    await _fill_queue(sub)

    delivered = await hub.send_to_user("user1", {"type": "room_invited", "room_id": "room2"})

    assert delivered is False  # the invite itself still did not fit
    drained = []
    while not sub.queue.empty():
        drained.append(sub.queue.get_nowait())
    assert drained[-1] == {"type": "resync"}


async def test_send_to_user_still_delivers_to_a_healthy_queue() -> None:
    hub = RealtimeHub()
    sub = await hub.subscribe("room1", "user1")

    delivered = await hub.send_to_user("user1", {"type": "room_invited", "room_id": "room2"})

    assert delivered is True
    assert sub.queue.get_nowait() == {"type": "room_invited", "room_id": "room2"}


async def test_asyncio_sanity_full_queue_stays_full() -> None:
    """Sanity check for the fixture above, matching the existing overflow suite."""
    hub = RealtimeHub()
    sub = await hub.subscribe("room1", "user1")
    await _fill_queue(sub)
    assert sub.queue.full()
    await asyncio.sleep(0)
    assert sub.queue.full()
