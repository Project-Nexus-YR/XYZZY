"""Redis fan-out for the realtime hub: cross-process delivery, failure modes,
control-broadcast propagation, and presence backed by Redis TTL keys.

Uses fakeredis for determinism, per house style. Two `RealtimeHub` instances
sharing one `fakeredis.FakeServer` stand in for two XYZZY processes.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest
from fakeredis import FakeServer
from fakeredis.aioredis import FakeRedis

from multiplayer.domain.events import EventType, RoomEvent
from multiplayer.realtime.fanout import RedisFanout
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.presence import PresenceService

pytestmark = pytest.mark.asyncio

_SETTLE = 0.3  # generous time for a background subscribe loop to see a publish


def _redis(server: FakeServer) -> FakeRedis:
    return FakeRedis(server=server)


@pytest.fixture
def fake_server() -> FakeServer:
    return FakeServer()


@pytest.fixture
async def two_hubs(fake_server: FakeServer):
    """Two hubs, distinct origin tokens, sharing one fakeredis server."""
    hub_a = RealtimeHub()
    hub_b = RealtimeHub()
    fanout_a = RedisFanout(_redis(fake_server), hub_a)
    fanout_b = RedisFanout(_redis(fake_server), hub_b)
    assert fanout_a.origin != fanout_b.origin
    hub_a.attach_fanout(fanout_a)
    hub_b.attach_fanout(fanout_b)
    fanout_a.start()
    fanout_b.start()
    await asyncio.sleep(_SETTLE)  # let both subscribe loops attach before publishing
    try:
        yield hub_a, hub_b, fanout_a, fanout_b
    finally:
        await fanout_a.stop()
        await fanout_b.stop()


def _event(room_id: str, sequence: int = 1) -> RoomEvent:
    return RoomEvent(
        room_id=room_id,
        sequence=sequence,
        event_type=EventType.ROOM_UPDATED,
        payload={"name": "renamed"},
        actor_id="user_1",
        actor_type="user",
    )


async def test_event_reaches_other_hub_exactly_once_in_order(two_hubs):
    hub_a, hub_b, _fanout_a, _fanout_b = two_hubs
    sub_a = await hub_a.subscribe("room1", "userA")
    sub_b = await hub_b.subscribe("room1", "userB")

    await hub_a.broadcast_room_event(_event("room1", 1))
    await hub_a.broadcast_room_event(_event("room1", 2))
    await asyncio.sleep(_SETTLE)

    # Hub A's own subscriber: exactly the two local broadcasts, no echo duplicate.
    a_messages = []
    while not sub_a.queue.empty():
        a_messages.append(sub_a.queue.get_nowait())
    assert [m["sequence"] for m in a_messages] == [1, 2]

    # Hub B's subscriber: the same two events, once each, in order.
    b_messages = []
    while not sub_b.queue.empty():
        b_messages.append(sub_b.queue.get_nowait())
    assert [m["sequence"] for m in b_messages] == [1, 2]


async def test_no_local_subscribers_drops_cheaply(two_hubs):
    """Room with no local subscribers on hub B: publish must not raise or hang."""
    hub_a, hub_b, _fanout_a, _fanout_b = two_hubs
    await hub_a.broadcast_room_event(_event("empty_room"))
    await asyncio.sleep(_SETTLE)
    assert await hub_b.room_subscriber_count("empty_room") == 0


async def test_redis_down_mid_flight_does_not_break_local_send(fake_server):
    """Publish failing must not affect local delivery, and must count the failure."""
    hub = RealtimeHub()
    fanout = RedisFanout(_redis(fake_server), hub)
    hub.attach_fanout(fanout)

    calls = {"n": 0}

    async def _boom(*_a, **_kw):
        calls["n"] += 1
        raise ConnectionError("redis unreachable")

    fanout._redis.publish = _boom  # simulate Redis being down

    class _Metrics:
        def __init__(self):
            self.failures = 0

        def record_redis_publish_failure(self):
            self.failures += 1

    metrics = _Metrics()
    fanout._metrics = metrics

    sub = await hub.subscribe("room1", "user1")
    delivered = await hub.broadcast_room_event(_event("room1"))

    assert delivered == [sub.subscription_id]  # local send still worked
    assert sub.queue.get_nowait()["sequence"] == 1
    assert calls["n"] == 1
    assert metrics.failures == 1


async def test_session_revocation_propagates_across_hubs(two_hubs):
    hub_a, hub_b, _fanout_a, _fanout_b = two_hubs
    sub_b = await hub_b.subscribe("room1", "userA")

    revoked_locally = await hub_a.revoke_room_access("userA", "room1")
    await asyncio.sleep(_SETTLE)

    assert revoked_locally == 0  # nothing local to hub A
    assert await hub_b.room_subscriber_count("room1") == 0
    message = sub_b.queue.get_nowait()
    assert message == {"type": "access_revoked", "room_id": "room1"}


async def test_presence_visible_across_hubs_and_expires_on_ttl(fake_server, monkeypatch):
    import multiplayer.services.presence as presence_module

    monkeypatch.setattr(presence_module, "_REDIS_PRESENCE_TTL_SECONDS", 1)

    presence_a = PresenceService(redis_client=_redis(fake_server))
    presence_b = PresenceService(redis_client=_redis(fake_server))

    await presence_a.user_joined("user1", "room1")
    assert await presence_b.is_user_in_room("user1", "room1")
    room_presence = await presence_b.get_room_presence("room1")
    assert [p.user_id for p in room_presence] == ["user1"]

    await asyncio.sleep(1.5)  # TTL lapses without process A saying anything
    assert not await presence_b.is_user_in_room("user1", "room1")


async def test_redis_absent_no_import_and_unchanged_behavior():
    """XYZZY_REDIS_URL unset: create_app must never import redis."""
    script = (
        "import sys, os\n"
        "os.environ.pop('XYZZY_REDIS_URL', None)\n"
        "sys.path.insert(0, 'src')\n"
        "from multiplayer.server import create_app\n"
        "create_app(':memory:', auth_tokens={'t': 'user_1'})\n"
        "assert 'redis' not in sys.modules, sorted(k for k in sys.modules if 'redis' in k)\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=None,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
