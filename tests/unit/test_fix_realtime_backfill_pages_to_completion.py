"""Finding 4 (high): the subscribe-time backfill used to hand ``last_sequence``
straight to ``MultiplayerService.get_room_events``, which caps at
``_ROOM_EVENTS_MAX_LIMIT`` (5000). A room with more history than that past
the cursor had the remainder silently dropped: live delivery resumed at head
with no marker of the hole in between, contradicting the socket's own
docstring ("sees no gap and no duplicate").

websocket.py now pages the backfill on the repository's own 500-row page
until a short page proves the caller is caught up, so a room of any size
replays gaplessly. Test with 5,300 appended events, well past the old cap.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from multiplayer.api import routes as routes_mod
from multiplayer.domain.events import EventType, RoomEvent
from multiplayer.realtime import websocket as websocket_module
from multiplayer.server import create_app

TOKENS = {"owner-token": "owner"}
OWNER = {"Authorization": "Bearer owner-token"}

_APPENDED = 5300


def _bootstrap(client: TestClient, headers: dict[str, str], room_name: str) -> str:
    response = client.post(
        "/api/v1/me/bootstrap",
        headers=headers,
        json={"display_name": "Owner", "room_name": room_name},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["room"]["room_id"])


def _append_many_events(room_id: str, count: int = _APPENDED) -> None:
    async def _run() -> None:
        svc = routes_mod._svc
        assert svc is not None
        # One transaction for the whole backlog rather than one per event:
        # at 100k events the per-call BEGIN/COMMIT overhead of
        # append_with_next_sequence dominates otherwise. Correctness is
        # unaffected — append_with_next_sequence_in_transaction is the same
        # insert, just under a transaction the caller owns.
        async with svc.db.transaction():
            for index in range(count):
                await svc.repos.events.append_with_next_sequence_in_transaction(
                    RoomEvent(
                        room_id=room_id,
                        sequence=0,  # allocated by append_with_next_sequence
                        event_type=EventType.MESSAGE_CREATED,
                        payload={"content": f"msg{index}"},
                        actor_id="owner",
                        actor_type="user",
                    )
                )

    asyncio.run(_run())


def _drain_room_event_sequences(websocket, wanted: int) -> list[int]:
    """Read frames until ``wanted`` room_event sequences arrive, or fail.

    A bounded read rather than an unconditional loop: on the unfixed tree
    the backfill silently stops at the old cap and resumes at head with no
    marker, so a caller waiting for events that will never come would hang
    forever instead of failing (the common brief calls a hang here a defect
    in its own right). One extra frame of slack covers the live event a
    resumed-at-head socket would otherwise deliver out of order.
    """
    sequences: list[int] = []
    for _ in range(wanted + 5):
        if len(sequences) >= wanted:
            break
        frame = websocket.receive_json()
        if frame.get("type") == "room_event":
            sequences.append(frame["sequence"])
    assert len(sequences) >= wanted, (
        f"only {len(sequences)} of {wanted} room_events arrived before the frame budget ran out"
    )
    return sequences


def test_backfill_delivers_every_event_past_the_old_5000_cap(monkeypatch) -> None:
    # A fast ping cadence bounds how long an unfixed tree's hang (backfill
    # silently stops at the old cap, then nothing more ever arrives) takes
    # to surface as a clean assertion failure instead of a multi-second wait.
    monkeypatch.setattr(websocket_module, "REAUTH_SECONDS", 0.2)
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, OWNER, "Big room")
        _append_many_events(room_id)

        with client.websocket_connect(
            f"/ws?room_id={room_id}&last_sequence=0", headers=OWNER
        ) as websocket:
            assert websocket.receive_json()["type"] == "connected"
            head_sequence = 1 + _APPENDED  # room_created, then every appended event
            sequences = _drain_room_event_sequences(websocket, head_sequence)

    assert len(sequences) == head_sequence
    assert sequences == list(range(1, head_sequence + 1))


def test_backfill_from_a_mid_range_cursor_past_the_cap_still_reaches_head(monkeypatch) -> None:
    """A cursor already past the point where the old single-call cap would
    have started truncating (a client reconnecting after its own snapshot
    already read most of a huge room) must still page to completion.
    """
    monkeypatch.setattr(websocket_module, "REAUTH_SECONDS", 0.2)
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, OWNER, "Big room")
        _append_many_events(room_id)
        cursor = 4000

        with client.websocket_connect(
            f"/ws?room_id={room_id}&last_sequence={cursor}", headers=OWNER
        ) as websocket:
            assert websocket.receive_json()["type"] == "connected"
            head_sequence = 1 + _APPENDED
            expected = head_sequence - cursor
            sequences = _drain_room_event_sequences(websocket, expected)

    assert sequences == list(range(cursor + 1, head_sequence + 1))


def test_backfill_state_is_o1_in_the_backlog_size(monkeypatch) -> None:
    """Round 2 critic finding: delivery was gapless and exactly-once at 5.3k
    and 12k events, but the dedup structure backing it — a `set[str]` of
    every replayed event_id — grew with the backlog (0.93 MB at 5.3k, up to
    11.9 MB at 100k, per socket, for the socket's whole lifetime). Replaced
    with `RealtimeSubscription.backfilled_through`, a high-water mark per
    room: one int, however large the backlog. Asserted on the subscription
    object's own attributes, not on process memory, at a 100k-event
    backlog well past every size tested before.
    """
    monkeypatch.setattr(websocket_module, "REAUTH_SECONDS", 0.2)
    backlog = 100_000
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, OWNER, "Huge room")
        _append_many_events(room_id, backlog)
        head_sequence = 1 + backlog  # room_created, then every appended event

        with client.websocket_connect(
            f"/ws?room_id={room_id}&last_sequence=0", headers=OWNER
        ) as websocket:
            connected = websocket.receive_json()
            assert connected["type"] == "connected"
            subscription_id = connected["subscription_id"]
            sequences = _drain_room_event_sequences(websocket, head_sequence)

            async def _inspect() -> dict:
                svc = routes_mod._svc
                assert svc is not None
                sub = await svc.hub.get_subscription(subscription_id)
                assert sub is not None
                return dict(sub.backfilled_through)

            backfilled_through = asyncio.run(_inspect())

    assert len(sequences) == head_sequence
    # One entry, for the one room this socket backfilled — not one per
    # event, regardless of how many events that room ever had.
    assert backfilled_through == {room_id: head_sequence}
