"""Finding 41: `attempt = 0` used to sit above the message-type filter in
`_subscribe_forever`, so a subscribe ACK (a `{"type": "subscribe"}` message
Redis sends on every (re)subscribe, never a `"message"`) reset the backoff
counter every cycle. Against the exact failure the backoff guards (Redis
ACKs the SUBSCRIBE then drops the connection), the delay stayed pinned at
roughly one second forever instead of escalating toward the 30 second cap.

`_subscribe_forever` loops forever by design, so this drives it directly and
stops it after a fixed number of reconnect cycles by making the mocked sleep
raise `CancelledError`, which is not caught by the pubsub try/except above
it (that block only wraps the pubsub work, not the trailing sleep).
"""

from __future__ import annotations

import asyncio

import pytest

from multiplayer.realtime import fanout as fanout_module
from multiplayer.realtime.fanout import RedisFanout
from multiplayer.realtime.hub import RealtimeHub


class _FakePubSub:
    async def subscribe(self, _channel: str) -> None:
        return None

    async def listen(self):
        # Every reconnect: Redis ACKs the subscribe, then the connection
        # drops (the exact failure finding 41 is about).
        yield {"type": "subscribe"}
        raise ConnectionError("dropped after ACK")

    async def aclose(self) -> None:
        return None


class _FakeRedis:
    def pubsub(self) -> _FakePubSub:
        return _FakePubSub()


async def test_backoff_escalates_across_ack_then_drop_cycles(monkeypatch) -> None:
    delays: list[float] = []

    def fake_uniform(_low: float, high: float) -> float:
        delays.append(high)
        return 0.0

    monkeypatch.setattr(fanout_module.random, "uniform", fake_uniform)

    calls = {"n": 0}
    real_sleep = asyncio.sleep

    async def fast_sleep(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] >= 4:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(fanout_module.asyncio, "sleep", fast_sleep)

    fanout = RedisFanout(_FakeRedis(), RealtimeHub())

    with pytest.raises(asyncio.CancelledError):
        await fanout._subscribe_forever()

    # 0.5 * 2**attempt for attempt = 1, 2, 3, 4: strictly escalating, not
    # pinned at the first step the way the unfixed code left it.
    assert delays == [1.0, 2.0, 4.0, 8.0]
