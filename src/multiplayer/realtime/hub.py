"""Realtime layer: in-memory pub/sub for WebSocket broadcasting."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..domain.events import RoomEvent
from ..domain.models import new_id

log = logging.getLogger(__name__)


@dataclass
class RealtimeSubscription:
    subscription_id: str
    room_id: str
    user_id: str
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=lambda: asyncio.Queue(maxsize=256))


class RealtimeHub:
    """Manages WebSocket connections and broadcasts room events.

    All mutations to internal state are protected by an asyncio.Lock.
    broadcast_to_room takes a snapshot of subscribers under the lock,
    then delivers outside the lock to avoid holding it during I/O.
    """

    def __init__(self) -> None:
        self._subscriptions: dict[str, RealtimeSubscription] = {}
        self._room_subscriptions: dict[str, set[str]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def subscribe(self, room_id: str, user_id: str) -> RealtimeSubscription:
        # Minted the way every other id here is minted, and deliberately not from
        # a clock. The previous spelling was the loop time to six decimals plus
        # `id(self)`, and `self` is the one hub, so the whole identifier was a
        # timestamp — on a platform whose loop clock advances every 15 ms, two
        # sockets opening in one tick got the same string. The second overwrote
        # the first in `_subscriptions`, which is the dictionary
        # `revoke_room_access` searches, so the overwritten socket became
        # unrevokable and kept receiving the room after its access was withdrawn.
        sub = RealtimeSubscription(
            subscription_id=new_id("sub"),
            room_id=room_id,
            user_id=user_id,
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

    async def revoke_room_access(self, user_id: str, room_id: str) -> int:
        """Drop a user's live subscriptions to a room and tell each socket to close."""
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
                log.debug("Queue full for revoked subscription %s", sub.subscription_id)
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
                    log.debug("Queue full for subscription %s, dropping event", sub.subscription_id)
        return delivered

    async def broadcast_room_event(self, room_event: RoomEvent) -> list[str]:
        """Broadcast a RoomEvent to all room subscribers."""
        payload = {
            "type": "room_event",
            "event_type": room_event.event_type.value,
            "sequence": room_event.sequence,
            "payload": room_event.payload,
            "actor_id": room_event.actor_id,
            "actor_type": room_event.actor_type,
            "timestamp": room_event.timestamp.isoformat(),
            "event_id": room_event.event_id,
        }
        return await self.broadcast_to_room(room_event.room_id, payload)

    async def send_to_user(self, user_id: str, event: dict[str, Any]) -> bool:
        """Send event to all subscriptions belonging to a user. Returns True if delivered."""
        delivered = False
        async with self._lock:
            for sub in list(self._subscriptions.values()):
                if sub.user_id == user_id and not sub.queue.full():
                    try:
                        sub.queue.put_nowait(event)
                        delivered = True
                    except asyncio.QueueFull:
                        pass
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
