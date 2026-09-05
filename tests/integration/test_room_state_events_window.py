"""get_room_state's events_since window at last_sequence=0 (a fresh connect,
no real cursor yet), per the f4_pageload ruling: on a room past the event
cap, paging events_since from sequence 1 forward (the old behaviour) always
returned the same handful of ancient events and never the one that just
happened, because get_room_events pages ascending from after_sequence.

The fix windows a last_sequence=0 request around whatever message page the
same response already shows: from that page's oldest message's own sequence
(or from 1, when the room has fewer messages than the page, so nothing was
cut off) up to the room's head, capped at event_limit from the head end. A
real cursor keeps its existing meaning unchanged, everything after it.
"""

from __future__ import annotations

import pytest

from multiplayer.db.connection import Database
from multiplayer.domain.models import MessageRole
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.capabilities import Posture
from multiplayer.services.service import MultiplayerService

OWNER = "owner"


@pytest.fixture
async def service():
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({OWNER}))
    await svc.initialize()
    yield svc
    await db.close()


async def _room(svc: MultiplayerService) -> str:
    org = await svc.create_organization("Window org", "window-org", OWNER)
    ws = await svc.create_workspace(org.org_id, "Main", "main", OWNER)
    room = await svc.create_room(ws.workspace_id, "Decision", OWNER)
    return room.room_id


@pytest.mark.asyncio
async def test_small_room_still_returns_everything_from_the_start(
    service: MultiplayerService,
) -> None:
    """Fewer messages than the page: nothing was cut off, so the window
    starts from sequence 1 exactly as it always did. A small demo room's
    earliest events (an invite, say) still render on a fresh connect."""
    room_id = await _room(service)
    for index in range(5):
        await service.send_message(room_id, MessageRole.HUMAN, OWNER, f"msg{index}")

    state = await service.get_room_state(room_id, last_sequence=0)

    assert [e["sequence"] for e in state["events_since"]][0] == 1


@pytest.mark.asyncio
async def test_room_past_the_cap_windows_around_the_recent_message_page(
    service: MultiplayerService,
) -> None:
    """A room with more messages than the page and more events than the
    limit: events_since must carry the recent event (here, the last one)
    and drop the room's own early history, the opposite of paging from
    sequence 1 forward."""
    room_id = await _room(service)
    for index in range(600):
        await service.send_message(room_id, MessageRole.HUMAN, OWNER, f"seed-{index}")
    await service.declare_room_posture(room_id, Posture.STRICT, OWNER)

    state = await service.get_room_state(room_id, last_sequence=0, event_limit=500)

    sequences = [e["sequence"] for e in state["events_since"]]
    assert sequences[-1] == max(sequences)
    assert any(e["event_type"] == "room.posture_declared" for e in state["events_since"])
    # The room's own early history (room creation, the first couple of
    # messages) is well outside a 500-event window this far past it.
    assert 1 not in sequences
    assert 2 not in sequences


@pytest.mark.asyncio
async def test_real_cursor_keeps_todays_meaning(service: MultiplayerService) -> None:
    """A non-zero last_sequence is an actual reconnect cursor, not a fresh
    connect: the window is untouched, everything after it, regardless of
    how many messages exist."""
    room_id = await _room(service)
    for index in range(600):
        await service.send_message(room_id, MessageRole.HUMAN, OWNER, f"seed-{index}")

    counter = await service.repos.events.get_sequence_counter(room_id)
    state = await service.get_room_state(room_id, last_sequence=1, event_limit=5000)

    assert len(state["events_since"]) == counter - 1
    assert state["events_since"][0]["sequence"] == 2


@pytest.mark.asyncio
async def test_a_room_with_exactly_a_page_of_messages_still_shows_its_early_events(
    service: MultiplayerService,
) -> None:
    """A room whose message count exactly matches the page size (50) is not
    "cut off": every one of its messages already fits the page, the same as
    a room with 49. The returned row count at exactly 50 cannot tell those
    two cases apart on its own (a room past the page also returns 50), so
    the window still has to start at sequence 1 here, not at the oldest of
    the 50 messages, or a room this size would drop its own early invite
    the moment a 51st message ever landed and then got deleted, or simply
    never had one."""
    room_id = await _room(service)
    for index in range(50):
        await service.send_message(room_id, MessageRole.HUMAN, OWNER, f"msg{index}")

    state = await service.get_room_state(room_id, last_sequence=0)

    assert [e["sequence"] for e in state["events_since"]][0] == 1


@pytest.mark.asyncio
async def test_a_commit_between_the_head_read_and_the_events_read_never_exceeds_the_head(
    service: MultiplayerService,
) -> None:
    """Round f4_pageload critic, round 3 finding: get_room_state reads the
    head (latest_sequence) first, then messages, then events with no upper
    bound of its own. A message committed in the gap between that head read
    and the events read used to land in events_since with a sequence above
    the latest_sequence this same response reported, while being absent
    from messages (already read before the commit landed): the client
    takes the higher of the two as its subscribe cursor (socket.js), so it
    would subscribe past a message it never actually showed.

    Same hook `test_latest_sequence_is_read_before_events_so_a_mid_snapshot_commit_is_included`
    (test_f3_stale_cursor.py) uses: the repository's own list_since, the
    read get_room_events pages through, sends a message the instant it is
    first called, simulating a commit landing exactly between the head read
    and the events read.
    """
    room_id = await _room(service)
    for index in range(5):
        await service.send_message(room_id, MessageRole.HUMAN, OWNER, f"msg{index}")

    original_list_since = service.repos.events.list_since
    injected = {"done": False}

    async def hooked_list_since(room_id_arg: str, after_sequence: int, limit: int = 500):
        if not injected["done"]:
            injected["done"] = True
            await service.send_message(room_id_arg, MessageRole.HUMAN, OWNER, "mid-snapshot")
        return await original_list_since(room_id_arg, after_sequence, limit=limit)

    service.repos.events.list_since = hooked_list_since  # type: ignore[method-assign]
    try:
        state = await service.get_room_state(room_id, last_sequence=0)
    finally:
        service.repos.events.list_since = original_list_since  # type: ignore[method-assign]

    injected_sequence = await service.repos.events.get_latest_sequence(room_id)
    # The injection landed after the head read this fix reads first, so the
    # snapshot's own reported head must be strictly behind the injected
    # event: a head equal to or past it would mean the race this test
    # drives was not actually hit.
    assert state["latest_sequence"] < injected_sequence

    assert all(event["sequence"] <= state["latest_sequence"] for event in state["events_since"])
