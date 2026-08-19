"""Presence service: ephemeral user/agent presence in rooms."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from ..domain.models import Presence, UserStatus, utcnow


class PresenceService:
    """Manages ephemeral presence state for users and agents in rooms."""

    def __init__(self) -> None:
        self._presence: dict[tuple[str, str], Presence] = {}  # (user_id, room_id) -> Presence
        self._heartbeat_interval = timedelta(seconds=30)
        self._stale_threshold = timedelta(minutes=5)
        self._lock = asyncio.Lock()

    async def user_joined(self, user_id: str, room_id: str) -> Presence:
        key = (user_id, room_id)
        presence = Presence(
            user_id=user_id,
            room_id=room_id,
            status=UserStatus.ONLINE,
            last_seen=utcnow(),
        )
        async with self._lock:
            self._presence[key] = presence
        return presence

    async def user_left(self, user_id: str, room_id: str) -> None:
        key = (user_id, room_id)
        async with self._lock:
            self._presence.pop(key, None)

    async def heartbeat(self, user_id: str, room_id: str) -> None:
        key = (user_id, room_id)
        async with self._lock:
            if key in self._presence:
                old = self._presence[key]
                self._presence[key] = Presence(
                    user_id=user_id,
                    room_id=room_id,
                    status=UserStatus.ONLINE,
                    last_seen=utcnow(),
                )

    async def set_away(self, user_id: str, room_id: str) -> None:
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
        result: list[Presence] = []
        async with self._lock:
            for key, pres in self._presence.items():
                if pres.room_id == room_id:
                    result.append(pres)
        return result

    async def get_user_rooms(self, user_id: str) -> list[str]:
        rooms: list[str] = []
        async with self._lock:
            for key, pres in self._presence.items():
                if pres.user_id == user_id:
                    rooms.append(pres.room_id)
        return rooms

    async def cleanup_stale(self) -> list[Presence]:
        """Remove presence entries that haven't been updated recently."""
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
        key = (user_id, room_id)
        async with self._lock:
            return key in self._presence
