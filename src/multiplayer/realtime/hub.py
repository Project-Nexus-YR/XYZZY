"""Realtime layer: in-memory pub/sub for WebSocket broadcasting."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..domain.events import RoomEvent
from ..domain.models import new_id

log = logging.getLogger(__name__)


class FanoutPublisher(Protocol):
    """What the hub needs from a cross-process transport (see realtime/fanout.py).

    Defined here rather than imported so hub.py never imports redis, or
    anything that does, when XYZZY_REDIS_URL is unset.
    """

    async def publish(self, message: dict[str, Any]) -> None: ...


class HubMetrics(Protocol):
    """What the hub needs from Metrics, defined here so hub.py never imports
    the metrics module's Prometheus rendering machinery for the sake of one
    counter.
    """

    def record_subscriber_queue_overflow(self) -> None: ...


@dataclass
class RealtimeSubscription:
    subscription_id: str
    room_id: str
    user_id: str
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=lambda: asyncio.Queue(maxsize=256))
    # Set the first time this subscription's queue overflows, so the warning
    # below fires once per connection instead of once per dropped event.
    overflow_warned: bool = False
    # High-water mark per room this subscription's backfill replayed
    # through (see websocket.py's subscribe-time backfill): a live event at
    # or below the mark for its room is a duplicate the backfill already
    # sent, one above it is not. One int per subscribed room rather than a
    # set of every replayed event_id, so a socket's own state stays O(rooms
    # subscribed) instead of O(events in a room's backlog).
    backfilled_through: dict[str, int] = field(default_factory=dict)


def room_event_payload(room_event: RoomEvent) -> dict[str, Any]:
    """The wire shape for one RoomEvent, shared by live broadcast
    (`RealtimeHub.broadcast_room_event`) and subscribe-time history replay
    (`websocket.py`'s `EventSource` backfill), so the two paths can never
    disagree about what a client-facing room_event message looks like.
    """
    return {
        "type": "room_event",
        "event_type": room_event.event_type.value,
        "sequence": room_event.sequence,
        "room_id": room_event.room_id,
        "payload": room_event.payload,
        "actor_id": room_event.actor_id,
        "actor_type": room_event.actor_type,
        "timestamp": room_event.timestamp.isoformat(),
        "event_id": room_event.event_id,
    }


class RealtimeHub:
    """Manages WebSocket connections and broadcasts room events.

    All mutations to internal state are protected by an asyncio.Lock.
    broadcast_to_room takes a snapshot of subscribers under the lock,
    then delivers outside the lock to avoid holding it during I/O.
    """

    def __init__(
        self, fanout: FanoutPublisher | None = None, *, metrics: HubMetrics | None = None
    ) -> None:
        self._subscriptions: dict[str, RealtimeSubscription] = {}
        self._room_subscriptions: dict[str, set[str]] = defaultdict(set)
        self._lock = asyncio.Lock()
        # None (the default) is single-process mode: no cross-process transport,
        # every method below behaves exactly as it did before this existed.
        self._fanout = fanout
        # None (the default, and every existing test's hub) means overflow is
        # still handled, just not counted: the /metrics gauge is optional
        # instrumentation, not a precondition for correct delivery.
        self._metrics = metrics

    def record_sequence_gap(self) -> None:
        """Count a sequence gap a client detected on a live socket (see
        websocket.py's `resync_request` handling) and asked to resync from.
        `multiplayer.metrics.Metrics.record_sequence_gap` renders it as
        `xyzzy_sequence_gaps_total` on `/metrics`.

        `record_sequence_gap` is deliberately not on `HubMetrics` above:
        adding it there would require every caller's metrics object to
        implement it, including any test double that only ever exercised
        `record_subscriber_queue_overflow`. The `getattr` fallback below is
        the same one `_handle_queue_overflow` already relies on for a hub
        with no metrics wired at all, kept here so a partial metrics stub
        stays a silent no-op instead of an `AttributeError`.
        """
        record = getattr(self._metrics, "record_sequence_gap", None)
        if callable(record):
            record()

    def attach_fanout(self, fanout: FanoutPublisher) -> None:
        """Wire a fan-out layer in after construction.

        Exists because RedisFanout needs the hub to deliver received messages
        into, and the hub needs the fanout to publish to — server.py breaks
        that cycle by constructing the hub first, then the fanout, then
        calling this.
        """
        self._fanout = fanout

    async def subscribe(
        self, room_id: str, user_id: str, *, queue: asyncio.Queue[dict[str, Any]] | None = None
    ) -> RealtimeSubscription:
        # Minted the way every other id here is minted, and deliberately not from
        # a clock. The previous spelling was the loop time to six decimals plus
        # `id(self)`, and `self` is the one hub, so the whole identifier was a
        # timestamp — on a platform whose loop clock advances every 15 ms, two
        # sockets opening in one tick got the same string. The second overwrote
        # the first in `_subscriptions`, which is the dictionary
        # `revoke_room_access` searches, so the overwritten socket became
        # unrevokable and kept receiving the room after its access was withdrawn.
        #
        # `queue` lets a caller give an extra room subscription the same queue
        # as one it already holds, so one socket can drain every room it
        # subscribed to through a single read loop instead of one per room.
        sub = RealtimeSubscription(
            subscription_id=new_id("sub"),
            room_id=room_id,
            user_id=user_id,
            queue=queue if queue is not None else asyncio.Queue(maxsize=256),
        )
        async with self._lock:
            self._subscriptions[sub.subscription_id] = sub
            self._room_subscriptions[room_id].add(sub.subscription_id)
        return sub

    async def unsubscribe(self, subscription_id: str) -> None:
        async with self._lock:
            sub = self._subscriptions.pop(subscription_id, None)
            if sub:
                self._room_subscriptions[sub.room_id].discard(subscription_id)

    async def get_subscription(self, subscription_id: str) -> RealtimeSubscription | None:
        """Look up a live subscription by id, e.g. to inspect its own state
        (`backfilled_through`, `queue`) rather than the hub's aggregate
        counters. The "connected" frame a socket receives on open carries
        exactly this id.
        """
        async with self._lock:
            return self._subscriptions.get(subscription_id)

    async def revoke_room_access(self, user_id: str, room_id: str, *, publish: bool = True) -> int:
        """Drop a user's live subscriptions to a room and tell each socket to close.

        `publish=False` is for the fan-out subscriber replaying another
        process's revocation locally — it must not re-publish, or two
        processes would echo the same revocation at each other forever.
        """
        async with self._lock:
            revoked = [
                sub
                for sub in self._subscriptions.values()
                if sub.user_id == user_id and sub.room_id == room_id
            ]
            for sub in revoked:
                self._subscriptions.pop(sub.subscription_id, None)
                self._room_subscriptions[room_id].discard(sub.subscription_id)
        for sub in revoked:
            try:
                sub.queue.put_nowait({"type": "access_revoked", "room_id": room_id})
            except asyncio.QueueFull:
                # A full queue almost always already carries a resync
                # marker from broadcast_to_room's own overflow handling,
                # which closes the socket anyway — but "almost always" is
                # not "always", and a revoked member's socket must not be
                # the one case left open with nobody ever telling it.
                # Evicting the oldest entry for the same marker
                # `_handle_queue_overflow` already uses is what makes this
                # path force the reconnect that access_revoked itself
                # otherwise would have.
                self._handle_queue_overflow(sub)
        if publish and self._fanout is not None:
            # Published unconditionally, even when nothing was revoked locally:
            # the whole point is telling OTHER processes, which may hold the
            # live socket this one never saw.
            await self._fanout.publish({"kind": "revoke", "room_id": room_id, "user_id": user_id})
        return len(revoked)

    async def broadcast_to_room(self, room_id: str, event: dict[str, Any]) -> list[str]:
        """Send event to all subscribers of a room. Returns list of delivered subscription IDs."""
        delivered: list[str] = []
        async with self._lock:
            sub_ids = list(self._room_subscriptions.get(room_id, set()))
            # Snapshot subscription objects under the lock
            subs = []
            for sid in sub_ids:
                sub = self._subscriptions.get(sid)
                if sub:
                    subs.append(sub)
        # Deliver outside the lock
        for sub in subs:
            if not sub.queue.full():
                try:
                    sub.queue.put_nowait(event)
                    delivered.append(sub.subscription_id)
                except asyncio.QueueFull:
                    self._handle_queue_overflow(sub)
            else:
                self._handle_queue_overflow(sub)
        return delivered

    def _handle_queue_overflow(self, sub: RealtimeSubscription) -> None:
        """A subscriber's queue is full: it fell behind by a whole backlog
        window and this event cannot be delivered.

        Counted every time (the /metrics signal an operator alerts on),
        logged once per connection (so a stalled tab does not spam the log),
        and turned into a `resync` marker enqueued in place of the oldest
        entry, so `websocket.py`'s send loop learns the socket fell behind
        and closes it, which the client's existing reconnect and loadState
        path already heals.
        """
        if self._metrics is not None:
            self._metrics.record_subscriber_queue_overflow()
        if not sub.overflow_warned:
            sub.overflow_warned = True
            log.warning(
                "Subscriber queue full for subscription %s (room %s): forcing resync",
                sub.subscription_id,
                sub.room_id,
            )
        with suppress(asyncio.QueueEmpty):
            sub.queue.get_nowait()
        with suppress(asyncio.QueueFull):
            sub.queue.put_nowait({"type": "resync"})

    async def broadcast_room_event(
        self, room_event: RoomEvent, *, publish: bool = True
    ) -> list[str]:
        """Broadcast a RoomEvent to all room subscribers.

        Local delivery (`broadcast_to_room` below) never waits on the
        publish: it already happened by the time `publish` runs, and a slow
        or dead Redis cannot delay or block it. `publish=False` is for the
        fan-out subscriber replaying another process's event locally.
        """
        payload = room_event_payload(room_event)
        delivered = await self.broadcast_to_room(room_event.room_id, payload)
        if publish and self._fanout is not None:
            await self._fanout.publish(
                {"kind": "room_event", "room_id": room_event.room_id, "event": payload}
            )
        return delivered

    async def send_to_user(
        self, user_id: str, event: dict[str, Any], *, publish: bool = True
    ) -> bool:
        """Send event to all subscriptions belonging to a user. Returns True if delivered."""
        delivered = False
        async with self._lock:
            for sub in list(self._subscriptions.values()):
                if sub.user_id == user_id:
                    if not sub.queue.full():
                        try:
                            sub.queue.put_nowait(event)
                            delivered = True
                            continue
                        except asyncio.QueueFull:
                            pass
                    # Full: silently skipping used to drop room_invited/
                    # room_removed sidebar notifications without a trace.
                    # Forcing the same resync marker every other overflow
                    # path uses is what makes this socket notice it fell
                    # behind instead of just missing this one event.
                    self._handle_queue_overflow(sub)
        if publish and self._fanout is not None:
            await self._fanout.publish({"kind": "send_to_user", "user_id": user_id, "event": event})
        return delivered

    async def room_subscriber_count(self, room_id: str) -> int:
        async with self._lock:
            return len(self._room_subscriptions.get(room_id, set()))

    async def subscriber_count(self) -> int:
        """Live WebSocket subscriptions across every room, for the /metrics gauge."""
        async with self._lock:
            return len(self._subscriptions)

    async def get_user_rooms(self, user_id: str) -> set[str]:
        rooms: set[str] = set()
        async with self._lock:
            for sub in self._subscriptions.values():
                if sub.user_id == user_id:
                    rooms.add(sub.room_id)
        return rooms

    async def get_subscriptions_for_user_room(self, user_id: str, room_id: str) -> list[str]:
        """Get subscription IDs for a specific user in a specific room."""
        async with self._lock:
            return [
                sid
                for sid, sub in self._subscriptions.items()
                if sub.user_id == user_id and sub.room_id == room_id
            ]
