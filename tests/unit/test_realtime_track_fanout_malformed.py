"""Finding 43: `_handle_message` used to index `event`/`user_id`/`room_id`
directly for each `kind`, so a well-formed-JSON message missing one of those
keys raised `KeyError` out of `_handle_message`. That exception was not
caught locally, so it propagated through the `async for raw in
pubsub.listen()` loop into `_subscribe_forever`'s outer `except Exception`,
tearing down and reconnecting the whole subscribe loop over one bad message
on the shared, unnamespaced `xyzzy:events` channel.

`_handle_message` now reads every field with `.get` and drops (returns)
instead of raising when a required one is missing.
"""

from __future__ import annotations

from multiplayer.realtime.fanout import RedisFanout
from multiplayer.realtime.hub import RealtimeHub


async def test_room_event_missing_event_key_is_dropped_not_raised() -> None:
    hub = RealtimeHub()
    await hub.subscribe("room1", "user1")
    fanout = RedisFanout(object(), hub)  # redis client unused by _handle_message

    # Previously: message["event"] -> KeyError.
    await fanout._handle_message('{"kind": "room_event", "room_id": "room1"}')


async def test_revoke_missing_room_id_is_dropped_not_raised() -> None:
    hub = RealtimeHub()
    fanout = RedisFanout(object(), hub)

    # Previously: message["room_id"] -> KeyError.
    await fanout._handle_message('{"kind": "revoke", "user_id": "user1"}')


async def test_send_to_user_missing_event_is_dropped_not_raised() -> None:
    hub = RealtimeHub()
    # This user must have a live subscription, or the original code already
    # short-circuited before touching the missing key.
    await hub.subscribe("room1", "user1")
    fanout = RedisFanout(object(), hub)

    # Previously: message["event"] -> KeyError.
    await fanout._handle_message('{"kind": "send_to_user", "user_id": "user1"}')


async def test_unrecognised_kind_is_dropped_quietly() -> None:
    hub = RealtimeHub()
    fanout = RedisFanout(object(), hub)

    await fanout._handle_message('{"kind": "something_else"}')


async def test_non_object_json_is_dropped_quietly() -> None:
    hub = RealtimeHub()
    fanout = RedisFanout(object(), hub)

    # Valid JSON, but not the object every `kind` branch assumes.
    await fanout._handle_message("42")
    await fanout._handle_message("[1, 2, 3]")


async def test_well_formed_message_after_malformed_one_still_delivers() -> None:
    """The point of dropping rather than raising: the connection survives to
    handle the next, well-formed message.
    """
    hub = RealtimeHub()
    sub = await hub.subscribe("room1", "user1")
    fanout = RedisFanout(object(), hub)

    await fanout._handle_message('{"kind": "room_event", "room_id": "room1"}')  # dropped
    await fanout._handle_message(
        '{"kind": "room_event", "room_id": "room1", "event": {"type": "room_event", "sequence": 1}}'
    )

    assert sub.queue.get_nowait() == {"type": "room_event", "sequence": 1}
