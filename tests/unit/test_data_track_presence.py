"""A stale in-memory presence entry must not be served, even before a sweep
ever runs."""

from datetime import timedelta

import pytest

from multiplayer.domain.models import Presence, UserStatus, utcnow
from multiplayer.services.presence import PresenceService


@pytest.mark.asyncio
async def test_get_room_presence_hides_a_stale_entry_before_any_sweep():
    service = PresenceService()
    await service.user_joined("u1", "room1")
    # Directly age the entry past the stale threshold, the way a crashed or
    # network-dropped client would without ever calling user_left.
    stale_since = utcnow() - service._stale_threshold - timedelta(seconds=1)
    service._presence[("u1", "room1")] = Presence(
        user_id="u1", room_id="room1", status=UserStatus.ONLINE, last_seen=stale_since
    )

    roster = await service.get_room_presence("room1")

    assert roster == []
