"""Finding (final critic, seam 2, medium): `GET /ws?last_sequence=N` with N
beyond the room's head used to set `sub.backfilled_through[room_id]` to N
after an empty first backfill page, then dedupe swallowed every live event
at or below N forever. A cursor beyond the head means the client's state
came from somewhere this log does not know, so the honest answer is a
resync: accept, then close with the existing 4408 code (the same one the
queue-overflow path already uses), naming the cursor and the head.

A cursor equal to the head is caught up, not stale, and must still connect
and receive the next live event normally.
"""

from __future__ import annotations

import asyncio
from datetime import UTC

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from multiplayer.api import routes as routes_mod
from multiplayer.server import create_app

TOKENS = {"owner-token": "owner"}
OWNER = {"Authorization": "Bearer owner-token"}


def _bootstrap(client: TestClient, headers: dict[str, str], room_name: str) -> str:
    response = client.post(
        "/api/v1/me/bootstrap",
        headers=headers,
        json={"display_name": "Owner", "room_name": room_name},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["room"]["room_id"])


def _post_message(client: TestClient, room_id: str, content: str) -> None:
    """Send through the app's own loop. `asyncio.run` from the test thread
    would drive the service on a second loop, and the wake-up it queues for
    the socket's subscription never reaches the app loop on Linux: the
    socket then waits forever for a frame that was already published."""
    from multiplayer.domain.models import MessageRole

    svc = routes_mod._svc
    assert svc is not None
    client.portal.call(svc.send_message, room_id, MessageRole.HUMAN, "owner", content)


def test_cursor_beyond_head_gets_4408_naming_cursor_and_head() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, OWNER, "Room")
        head = routes_mod._svc.repos.events.get_latest_sequence  # type: ignore[union-attr]
        current_head = client.portal.call(head, room_id)
        stale_cursor = current_head + 1000

        with client.websocket_connect(
            f"/ws?room_id={room_id}&last_sequence={stale_cursor}", headers=OWNER
        ) as websocket:
            # Round 4: the head is read, and a stale cursor closed, before
            # ever subscribing or sending "connected" (see finding 2 in
            # round 3's verdict), so a stale cursor gets 4408 as the very
            # first frame, not after a "connected" that no longer precedes it.
            with pytest.raises(WebSocketDisconnect) as excinfo:
                websocket.receive_json()

        assert excinfo.value.code == 4408
        assert str(stale_cursor) in (excinfo.value.reason or "")
        assert str(current_head) in (excinfo.value.reason or "")


def test_cursor_equal_to_head_stays_connected_and_gets_the_next_live_event() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, OWNER, "Room")
        head = client.portal.call(routes_mod._svc.repos.events.get_latest_sequence, room_id)

        with client.websocket_connect(
            f"/ws?room_id={room_id}&last_sequence={head}", headers=OWNER
        ) as websocket:
            assert websocket.receive_json()["type"] == "connected"
            _post_message(client, room_id, "hello")
            frame = websocket.receive_json()
            assert frame["type"] == "room_event"
            assert frame["sequence"] == head + 1


def test_oversized_cursor_gets_4400_and_leaves_no_subscription_behind() -> None:
    """Round 2, finding 2 (high): int() has no length limit, but a
    WebSocket close reason does (123 bytes). A cursor of about 90 digits
    or more used to make the stale-cursor close itself raise from inside
    the reason it built, before the subscription and presence release
    ever ran. Bounded at parse time now: a cursor over 19 digits (past
    2**63 - 1, past any sequence this system will ever reach) is malformed
    input and gets the same 4400 a non-integer cursor already gets, before
    a subscription is even created.
    """
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, OWNER, "Room")
        baseline = client.portal.call(routes_mod._svc.hub.subscriber_count)

        huge_cursor = "9" * 100
        with client.websocket_connect(
            f"/ws?room_id={room_id}&last_sequence={huge_cursor}", headers=OWNER
        ) as websocket:
            with pytest.raises(WebSocketDisconnect) as excinfo:
                websocket.receive_json()

        assert excinfo.value.code == 4400
        assert client.portal.call(routes_mod._svc.hub.subscriber_count) == baseline


class _Principal:
    def __init__(self, user_id: str) -> None:
        self.user_id = user_id


class _FakeAuthenticator:
    """A real TestClient handshake cannot make the server's own close()
    call raise mid-flight without the surrounding transport also tearing
    the task down before it gets there (Starlette's WebSocket test session
    only advances the server coroutine as far as the client keeps reading).
    Driving `websocket_endpoint` directly with a fake transport sidesteps
    that: the fake's `close()` raises synchronously, same shape as
    starlette.websockets.WebSocket.close does against an already-gone peer,
    with nothing about the transport's own scheduling in the way.
    """

    def __init__(self, user_id: str) -> None:
        self.user_id = user_id

    async def authenticate(self, header: str | None) -> _Principal:
        return _Principal(self.user_id)


class _FakeAuthorization:
    async def require(self, room_id: str, user_id: str, capability: object) -> None:
        return None


class _FakePresence:
    """Records every join and leave, so a test can assert a doomed socket
    (one closed before it ever subscribes) never flaps presence at all,
    rather than merely joining and leaving once.
    """

    def __init__(self) -> None:
        self.joined: list[tuple[str, str]] = []
        self.left: list[tuple[str, str]] = []

    async def heartbeat(self, user_id: str, room_id: str) -> None:
        return None

    async def user_joined(self, user_id: str, room_id: str) -> object:
        self.joined.append((user_id, room_id))
        return None

    async def user_left(self, user_id: str, room_id: str) -> None:
        self.left.append((user_id, room_id))


class _FakeEvents:
    def __init__(self, head: int) -> None:
        self.head = head

    async def get_room_events(
        self, room_id: str, after_sequence: int = 0, limit: int = 500
    ) -> list[object]:
        return []

    async def get_latest_sequence(self, room_id: str) -> int:
        return self.head


class _RecordingWebSocket:
    """A normal, cooperative transport: `accept` and `send_json` succeed,
    `close` records the code and reason instead of raising or actually
    closing anything, and `receive_text` never resolves, standing in for a
    peer that stays connected but sends nothing further (irrelevant to the
    tests using this fake, which all close or return before reaching it).
    """

    def __init__(self, query_params: dict[str, str]) -> None:
        self.query_params = query_params
        self.headers: dict[str, str] = {"authorization": "Bearer owner-token"}
        self.cookies: dict[str, str] = {}
        self.sent: list[dict[str, object]] = []
        self.closed: tuple[int, str | None] | None = None

    async def accept(self, subprotocol: str | None = None) -> None:
        return None

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = (code, reason)

    async def send_json(self, data: dict[str, object]) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        await asyncio.sleep(3600)
        raise AssertionError("receive_text should never be reached in these tests")


class _VanishingAfterNSendsWebSocket:
    """Stands in for a peer whose connection is already gone by the Nth
    `send_json` call: the first `n` sends succeed (recorded in `sent`), the
    one after raises `WebSocketDisconnect`, the same shape a dropped TCP
    connection's write raises. `n = 0` is a peer gone before the connected
    frame ever lands; `n > 0` is a peer gone partway through the backfill.
    """

    def __init__(self, query_params: dict[str, str], fail_after_sends: int) -> None:
        self.query_params = query_params
        self.headers: dict[str, str] = {"authorization": "Bearer owner-token"}
        self.cookies: dict[str, str] = {}
        self.sent: list[dict[str, object]] = []
        self._fail_after = fail_after_sends

    async def accept(self, subprotocol: str | None = None) -> None:
        return None

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        raise WebSocketDisconnect(code=1006)

    async def send_json(self, data: dict[str, object]) -> None:
        if len(self.sent) >= self._fail_after:
            raise WebSocketDisconnect(code=1006)
        self.sent.append(data)

    async def receive_text(self) -> str:
        raise WebSocketDisconnect(code=1006)


class _MultiEventEvents:
    """A valid (non-stale), populated backfill: `count` real events past
    sequence 0, enough that a peer dropped partway through sending them is a
    real mid-backfill vanish, not one that happens to land on the last one.
    """

    def __init__(self, count: int) -> None:
        from datetime import datetime

        from multiplayer.domain.events import EventType, RoomEvent

        self.head = count
        self._events = [
            RoomEvent(
                event_id=f"e{seq}",
                room_id="room1",
                sequence=seq,
                event_type=EventType("message.created"),
                payload={
                    "message_id": f"m{seq}",
                    "content": f"c{seq}",
                    "role": "human",
                    "sender_id": "owner",
                },
                actor_id="owner",
                actor_type="user",
                timestamp=datetime.now(UTC),
                schema_version=1,
            )
            for seq in range(1, count + 1)
        ]

    async def get_room_events(
        self, room_id: str, after_sequence: int = 0, limit: int = 500
    ) -> list[object]:
        return [e for e in self._events if e.sequence > after_sequence][:limit]

    async def get_latest_sequence(self, room_id: str) -> int:
        return self.head


class _HeadRaceEvents:
    """A `get_latest_sequence` read that returns `head`, then immediately
    raises the room's true head by one, as if a new event's own commit
    landed the instant after this read returned. `get_room_events` records
    every `after_sequence` it is called with, so a test can assert the
    backfill page was never read at all when the head read alone should
    have settled things.
    """

    def __init__(self, head: int) -> None:
        self.head = head
        self.get_room_events_calls: list[int] = []

    async def get_latest_sequence(self, room_id: str) -> int:
        current = self.head
        self.head += 1
        return current

    async def get_room_events(
        self, room_id: str, after_sequence: int = 0, limit: int = 500
    ) -> list[object]:
        self.get_room_events_calls.append(after_sequence)
        return []


class _VanishingWebSocket:
    """Stands in for a peer already gone by the time the server tries to
    close: `accept` and `send_json` succeed (the handshake and the
    "connected" frame did reach the peer), `close` raises, the same shape
    `starlette.websockets.WebSocket.close` takes once the underlying
    connection is no longer there.
    """

    def __init__(self, query_params: dict[str, str]) -> None:
        self.query_params = query_params
        self.headers: dict[str, str] = {"authorization": "Bearer owner-token"}
        self.cookies: dict[str, str] = {}
        self.sent: list[dict[str, object]] = []

    async def accept(self, subprotocol: str | None = None) -> None:
        return None

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        raise WebSocketDisconnect(code=1006)

    async def send_json(self, data: dict[str, object]) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        raise WebSocketDisconnect(code=1006)


def test_peer_vanishing_on_a_stale_cursor_close_leaves_nothing_to_release() -> None:
    """Round 2, finding 3 (medium), updated for round 4 and round 5: the
    stale-cursor close used to sit outside any try/finally, so a peer
    already gone by the time the server tried to close (`websocket.close`
    itself raising `WebSocketDisconnect`) skipped the subscription and
    presence release that followed it. Round 4 (round 3's verdict, finding
    2) moved the head read, and a stale cursor's close, to before
    `hub.subscribe` and the presence join, so this scenario no longer has a
    subscription or presence row to leak in the first place. That same
    round 4 left the close itself unguarded, the same shape as the
    room_id/cursor_error/auth checks above it, which meant a peer already
    gone here raised `WebSocketDisconnect` straight out of the handler: no
    leak, but an ERROR-level traceback in the server log for every drop, and
    a member could trigger one on demand by opening a poisoned-cursor socket
    and dropping it (round 4 critic's vanish_probe.py: 30 of 30). Round 5
    wraps this close, and the room_id/cursor_error/auth ones above it, in
    `suppress(WebSocketDisconnect)`, so this now returns cleanly instead of
    raising.
    """
    from multiplayer.realtime.hub import RealtimeHub
    from multiplayer.realtime.websocket import websocket_endpoint

    hub = RealtimeHub()
    head = 5
    stale_cursor = head + 1000
    websocket = _VanishingWebSocket({"room_id": "room1", "last_sequence": str(stale_cursor)})

    asyncio.run(
        websocket_endpoint(
            websocket,
            hub,
            _FakeAuthenticator("owner"),
            _FakeAuthorization(),
            _FakeEvents(head),
        )
    )

    assert websocket.sent == []
    assert asyncio.run(hub.subscriber_count()) == 0


def test_cursor_zero_on_a_populated_room_still_replays_gapless() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, OWNER, "Room")
        _post_message(client, room_id, "msg1")
        _post_message(client, room_id, "msg2")
        head = client.portal.call(routes_mod._svc.repos.events.get_latest_sequence, room_id)
        assert head == 3  # room_created + 2 messages

        with client.websocket_connect(
            f"/ws?room_id={room_id}&last_sequence=0", headers=OWNER
        ) as websocket:
            assert websocket.receive_json()["type"] == "connected"
            sequences = []
            for _ in range(head):
                frame = websocket.receive_json()
                assert frame["type"] == "room_event"
                sequences.append(frame["sequence"])
        assert sequences == list(range(1, head + 1))


def test_cursor_head_plus_one_with_event_landing_after_the_head_read_resyncs_not_swallows() -> None:
    """Round 3, finding 2 (medium), ruling reversed from round 2, then
    narrowed further in round 4 (round 3's verdict, finding 2): reading the
    head only after an empty first backfill page let a cursor of exactly
    head+1, whose own event committed during the handshake, look valid by
    the time the (now-late) head read ran: the backfill page read after that
    event's own sequence came back empty (correctly, nothing is newer), the
    head read then returned the event's own sequence, `stale` came out
    False, and `backfilled_through` was set to that same sequence, so the
    send loop's high-water dedupe silently dropped the event's live copy as
    "already sent" (round 2 critic's race_probe.py: 'frames sent:
    [(connected, None)]', the event never delivered by any path).

    Round 3 moved the head read before the backfill loop, closing that
    window; round 4 moves it again, before `hub.subscribe` and the presence
    join, since an event landing between subscribe and a head read taken
    right after it hit the exact same race one step later. Reading it
    before anything else exists means a stale cursor never subscribes,
    never joins presence, and never sends "connected" at all.
    """
    from multiplayer.realtime.hub import RealtimeHub
    from multiplayer.realtime.websocket import websocket_endpoint

    hub = RealtimeHub()
    head = 5
    events = _HeadRaceEvents(head)
    presence = _FakePresence()
    websocket = _RecordingWebSocket({"room_id": "room1", "last_sequence": str(head + 1)})

    asyncio.run(
        websocket_endpoint(
            websocket,
            hub,
            _FakeAuthenticator("owner"),
            _FakeAuthorization(),
            events,
            presence=presence,
        )
    )

    assert websocket.sent == []
    assert events.get_room_events_calls == []
    assert websocket.closed == (
        4408,
        f"cursor {head + 1} is beyond room head {head}; resync required",
    )
    assert asyncio.run(hub.subscriber_count()) == 0
    assert presence.joined == []
    assert presence.left == []


def test_peer_gone_before_the_connected_frame_still_releases_the_subscription() -> None:
    """Round 3, finding 1 (high), case A: only the stale-cursor close ended
    up under a `try/finally` after round 2, so a peer already gone before
    the server even sends the "connected" frame (a closed tab racing its own
    handshake, reproducible by any member on purpose) still raised
    `WebSocketDisconnect` straight out of the function before the
    subscription or presence row was ever released (round 2 critic's
    vanish_probe.py, case A: 30 of 30 leaked). One try/finally now spans the
    whole subscription lifetime, starting before the first send.
    """
    from multiplayer.realtime.hub import RealtimeHub
    from multiplayer.realtime.websocket import websocket_endpoint

    hub = RealtimeHub()
    websocket = _VanishingAfterNSendsWebSocket(
        {"room_id": "room1", "last_sequence": "0"}, fail_after_sends=0
    )

    asyncio.run(
        websocket_endpoint(
            websocket,
            hub,
            _FakeAuthenticator("owner"),
            _FakeAuthorization(),
            _FakeEvents(head=0),
        )
    )

    assert websocket.sent == []
    assert asyncio.run(hub.subscriber_count()) == 0


def test_peer_gone_mid_backfill_still_releases_the_subscription() -> None:
    """Round 3, finding 1 (high), case B: a peer gone partway through a
    valid cursor's backfill (round 2 critic's vanish_probe.py, case B: 20 of
    20 leaked over a 1501-event room) also raised past
    `release_subscriptions()`, since the backfill's own `send_json` calls
    sat outside the one `try/finally` that existed. The connected frame and
    two room_event sends succeed here; the third raises.
    """
    from multiplayer.realtime.hub import RealtimeHub
    from multiplayer.realtime.websocket import websocket_endpoint

    hub = RealtimeHub()
    websocket = _VanishingAfterNSendsWebSocket(
        {"room_id": "room1", "last_sequence": "0"}, fail_after_sends=3
    )

    asyncio.run(
        websocket_endpoint(
            websocket,
            hub,
            _FakeAuthenticator("owner"),
            _FakeAuthorization(),
            _MultiEventEvents(count=10),
        )
    )

    assert len(websocket.sent) == 3
    assert websocket.sent[0]["type"] == "connected"
    assert [frame["type"] for frame in websocket.sent[1:]] == ["room_event", "room_event"]
    assert asyncio.run(hub.subscriber_count()) == 0


def test_latest_sequence_is_read_before_events_so_a_mid_snapshot_commit_is_included() -> None:
    """Round 5, finding 1 (medium): get_room_state used to read
    latest_sequence last, after events_since, messages and presence, so an
    event whose commit landed inside that read window had a sequence at or
    below latest_sequence while being absent from both events_since and
    messages. A socket that then subscribed at last_sequence=latest_sequence
    read it as caught up and never replayed it (round 4 critic's
    window_probe.py: event 36 replayed False at cursor 36, True at the
    pre-fix cursor 35). Reading latest_sequence first, before any other read
    in the method, is what closes this: everything at or below it is
    guaranteed to already be committed by the time the later reads run.

    Hooks the repository's own list_since (the read get_room_events pages
    through) to send a message the instant it is first called, simulating a
    commit landing exactly between the head read and the events read, then
    opens a real socket at the returned cursor and confirms the injected
    message's own event is replayed rather than silently missing.
    """
    from multiplayer.domain.models import MessageRole

    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, OWNER, "Room")
        svc = routes_mod._svc
        assert svc is not None

        original_list_since = svc.repos.events.list_since
        injected = {"done": False}

        async def hooked_list_since(room_id_arg: str, after_sequence: int, limit: int = 500):
            if not injected["done"]:
                injected["done"] = True
                await svc.send_message(room_id_arg, MessageRole.HUMAN, "owner", "mid-snapshot")
            return await original_list_since(room_id_arg, after_sequence, limit=limit)

        svc.repos.events.list_since = hooked_list_since
        try:
            state = client.portal.call(lambda: svc.get_room_state(room_id, last_sequence=0))
        finally:
            svc.repos.events.list_since = original_list_since

        cursor = state["latest_sequence"]
        # The injection landed after the head read this fix moved first, so
        # the returned cursor must be strictly behind the injected event: a
        # cursor equal to or past it would mean the race this test drives
        # was not actually hit.
        injected_sequence = client.portal.call(svc.repos.events.get_latest_sequence, room_id)
        assert cursor < injected_sequence

        with client.websocket_connect(
            f"/ws?room_id={room_id}&last_sequence={cursor}", headers=OWNER
        ) as websocket:
            assert websocket.receive_json()["type"] == "connected"
            frame = websocket.receive_json()
            assert frame["type"] == "room_event"
            assert frame["sequence"] == injected_sequence
            assert frame["event_type"] == "message.created"
