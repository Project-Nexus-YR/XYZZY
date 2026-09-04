"""Presence service: ephemeral user/agent presence in rooms.

Single-process default is an in-memory dict, unchanged from before. When a
`redis_client` is supplied (server.py does this only when XYZZY_REDIS_URL is
set), presence instead lives in Redis keys with a TTL refreshed on every
heartbeat: "online" becomes "a key that has not expired," so who-is-online is
correct across every process sharing that Redis, and a process that dies
silently drops out of presence on its own once the TTL lapses — no cleanup
sweep, no cross-process message required. The public interface is unchanged
either way.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any

from ..domain.models import Presence, UserStatus, utcnow

# Comfortably wider than the heartbeat cadence so one missed beat is not
# mistaken for the user having left; three misses in a row is.
_REDIS_PRESENCE_TTL_SECONDS = 90


def _redis_key(room_id: str, user_id: str) -> str:
    return f"presence:{room_id}:{user_id}"


class PresenceService:
    """Manages ephemeral presence state for users and agents in rooms."""

    def __init__(self, redis_client: Any | None = None) -> None:
        self._presence: dict[tuple[str, str], Presence] = {}  # (user_id, room_id) -> Presence
        self._heartbeat_interval = timedelta(seconds=30)
        self._stale_threshold = timedelta(minutes=5)
        self._lock = asyncio.Lock()
        self._redis = redis_client

    async def user_joined(self, user_id: str, room_id: str) -> Presence:
        presence = Presence(
            user_id=user_id,
            room_id=room_id,
            status=UserStatus.ONLINE,
            last_seen=utcnow(),
        )
        if self._redis is not None:
            await self._redis.set(
                _redis_key(room_id, user_id),
                _encode(presence),
                ex=_REDIS_PRESENCE_TTL_SECONDS,
            )
            return presence
        async with self._lock:
            self._presence[(user_id, room_id)] = presence
        return presence

    async def user_left(self, user_id: str, room_id: str) -> None:
        if self._redis is not None:
            await self._redis.delete(_redis_key(room_id, user_id))
            return
        async with self._lock:
            self._presence.pop((user_id, room_id), None)

    async def heartbeat(self, user_id: str, room_id: str) -> None:
        if self._redis is not None:
            presence = Presence(
                user_id=user_id, room_id=room_id, status=UserStatus.ONLINE, last_seen=utcnow()
            )
            # XX: only refresh a presence that already exists — matches the
            # in-memory branch below, which is also a no-op for a user who
            # never joined.
            await self._redis.set(
                _redis_key(room_id, user_id),
                _encode(presence),
                ex=_REDIS_PRESENCE_TTL_SECONDS,
                xx=True,
            )
            return
        key = (user_id, room_id)
        async with self._lock:
            if key in self._presence:
                self._presence[key] = Presence(
                    user_id=user_id,
                    room_id=room_id,
                    status=UserStatus.ONLINE,
                    last_seen=utcnow(),
                )

    async def set_away(self, user_id: str, room_id: str) -> None:
        if self._redis is not None:
            existing = await self._get_redis(room_id, user_id)
            if existing is None:
                return
            presence = Presence(
                user_id=user_id,
                room_id=room_id,
                status=UserStatus.AWAY,
                last_seen=existing.last_seen,
            )
            await self._redis.set(
                _redis_key(room_id, user_id),
                _encode(presence),
                ex=_REDIS_PRESENCE_TTL_SECONDS,
                xx=True,
            )
            return
        key = (user_id, room_id)
        async with self._lock:
            if key in self._presence:
                old = self._presence[key]
                self._presence[key] = Presence(
                    user_id=user_id,
                    room_id=room_id,
                    status=UserStatus.AWAY,
                    last_seen=old.last_seen,
                )

    async def get_room_presence(self, room_id: str) -> list[Presence]:
        if self._redis is not None:
            redis_result: list[Presence] = []
            async for key in self._redis.scan_iter(match=_redis_key(room_id, "*")):
                pres = await self._get_by_key(key)
                if pres is not None:
                    redis_result.append(pres)
            return redis_result
        now = utcnow()
        result: list[Presence] = []
        async with self._lock:
            for pres in self._presence.values():
                if pres.room_id == room_id and now - pres.last_seen <= self._stale_threshold:
                    result.append(pres)
        return result

    async def get_user_rooms(self, user_id: str) -> list[str]:
        if self._redis is not None:
            redis_rooms: list[str] = []
            async for key in self._redis.scan_iter(match=f"presence:*:{user_id}"):
                pres = await self._get_by_key(key)
                if pres is not None:
                    redis_rooms.append(pres.room_id)
            return redis_rooms
        rooms: list[str] = []
        async with self._lock:
            for pres in self._presence.values():
                if pres.user_id == user_id:
                    rooms.append(pres.room_id)
        return rooms

    async def cleanup_stale(self) -> list[Presence]:
        """Remove presence entries that haven't been updated recently.

        A no-op under Redis: the TTL set on join/heartbeat already expires a
        silent process's entries on its own, which is the "expire on
        silence" half of the guarantee — there is nothing here left to sweep.
        """
        if self._redis is not None:
            return []
        now = utcnow()
        stale: list[Presence] = []
        async with self._lock:
            to_remove = []
            for key, pres in self._presence.items():
                if now - pres.last_seen > self._stale_threshold:
                    stale.append(pres)
                    to_remove.append(key)
            for key in to_remove:
                del self._presence[key]
        return stale

    async def is_user_in_room(self, user_id: str, room_id: str) -> bool:
        if self._redis is not None:
            return bool(await self._redis.exists(_redis_key(room_id, user_id)))
        async with self._lock:
            return (user_id, room_id) in self._presence

    async def _get_redis(self, room_id: str, user_id: str) -> Presence | None:
        return await self._get_by_key(_redis_key(room_id, user_id))

    async def _get_by_key(self, key: str | bytes) -> Presence | None:
        assert self._redis is not None
        raw = await self._redis.get(key)
        if raw is None:
            return None
        text = key.decode() if isinstance(key, bytes) else key
        _, room_id, user_id = text.split(":", 2)
        data = json.loads(raw)
        return Presence(
            user_id=user_id,
            room_id=room_id,
            status=UserStatus(data["status"]),
            last_seen=datetime.fromisoformat(data["last_seen"]),
        )


def _encode(presence: Presence) -> str:
    return json.dumps(
        {"status": presence.status.value, "last_seen": presence.last_seen.isoformat()}
    )
