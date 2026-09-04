"""A full subscriber queue used to drop a broadcast event with no log above
DEBUG and no counter (findings 28 and 40): a healthy socket silently
diverged from the room until its next reconnect or loadState.

Now the overflow is counted every time, logged once per connection, and
turned into a `resync` marker enqueued in place of the oldest entry, so the
websocket layer can close the socket and force the client's existing
reconnect and loadState path to run.
"""

from __future__ import annotations

import asyncio
import logging

from multiplayer.realtime.hub import RealtimeHub


class _FakeMetrics:
    def __init__(self) -> None:
        self.overflows = 0

    def record_subscriber_queue_overflow(self) -> None:
        self.overflows += 1


async def _fill_queue(sub) -> None:
    while not sub.queue.full():
        sub.queue.put_nowait({"type": "room_event", "sequence": sub.queue.qsize()})


async def test_full_queue_is_counted_and_signals_resync() -> None:
    metrics = _FakeMetrics()
    hub = RealtimeHub(metrics=metrics)
    sub = await hub.subscribe("room1", "user1")
    await _fill_queue(sub)

    delivered = await hub.broadcast_to_room("room1", {"type": "room_event", "sequence": 999})

    assert delivered == []  # the overflowing subscriber did not get this event
    assert metrics.overflows == 1
    # The oldest entry was evicted to make room for a resync marker: the
    # queue is still full, but its last slot is now the marker, not silence.
    drained = []
    while not sub.queue.empty():
        drained.append(sub.queue.get_nowait())
    assert drained[-1] == {"type": "resync"}


async def test_overflow_warning_logs_once_per_connection(caplog) -> None:
    hub = RealtimeHub()
    sub = await hub.subscribe("room1", "user1")
    await _fill_queue(sub)

    with caplog.at_level(logging.WARNING, logger="multiplayer.realtime.hub"):
        await hub.broadcast_to_room("room1", {"type": "room_event", "sequence": 1})
        # Re-fill (the first overflow left a resync marker plus a gap) and
        # overflow again: the warning must not repeat for this connection.
        await _fill_queue(sub)
        await hub.broadcast_to_room("room1", {"type": "room_event", "sequence": 2})

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


async def test_no_metrics_wired_does_not_raise() -> None:
    """The default hub (no metrics) still handles overflow; metrics are
    optional instrumentation, not a precondition for correct delivery.
    """
    hub = RealtimeHub()
    sub = await hub.subscribe("room1", "user1")
    await _fill_queue(sub)

    delivered = await hub.broadcast_to_room("room1", {"type": "room_event"})
    assert delivered == []


async def test_healthy_queue_still_delivers() -> None:
    hub = RealtimeHub()
    sub = await hub.subscribe("room1", "user1")

    delivered = await hub.broadcast_to_room("room1", {"type": "room_event", "sequence": 1})

    assert delivered == [sub.subscription_id]
    assert sub.queue.get_nowait() == {"type": "room_event", "sequence": 1}


async def test_asyncio_queue_full_never_races_in_single_task() -> None:
    """Sanity check for the fixture used above: a queue this test fills to
    maxsize really does report full, so the overflow branch above is the one
    under test rather than the ordinary delivery branch.
    """
    hub = RealtimeHub()
    sub = await hub.subscribe("room1", "user1")
    await _fill_queue(sub)
    assert sub.queue.full()
    await asyncio.sleep(0)  # no other task runs between fill and assert
    assert sub.queue.full()
