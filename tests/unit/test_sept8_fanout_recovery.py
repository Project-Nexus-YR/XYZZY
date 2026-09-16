"""A prolonged Redis outage must not exhaust the reconnect loop itself."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from multiplayer.realtime import fanout as fanout_module
from multiplayer.realtime.fanout import RedisFanout
from multiplayer.realtime.hub import RealtimeHub


class _RecoveringRedis:
    def __init__(self) -> None:
        self.attempts = 0

    def pubsub(self):
        return self

    async def subscribe(self, _channel: str) -> None:
        self.attempts += 1
        if self.attempts <= 1100:
            raise ConnectionError("Redis remains unavailable")

    async def listen(self):
        yield {"type": "subscribe"}
        yield {
            "type": "message",
            "data": json.dumps(
                {
                    "origin": "remote-process",
                    "kind": "send_to_user",
                    "user_id": "member",
                    "event": {"type": "recovered"},
                }
            ),
        }
        raise asyncio.CancelledError

    async def aclose(self) -> None:
        return None


async def test_reconnect_recovers_after_more_than_1024_failures(monkeypatch, caplog) -> None:
    caplog.set_level(logging.CRITICAL, logger="multiplayer.realtime.fanout")
    delays: list[float] = []

    async def fast_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(fanout_module.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(fanout_module.random, "uniform", lambda _low, high: high)
    redis = _RecoveringRedis()
    hub = RealtimeHub()
    subscription = await hub.subscribe("room", "member")
    fanout = RedisFanout(redis, hub)

    with pytest.raises(asyncio.CancelledError):
        await fanout._subscribe_forever()

    assert redis.attempts == 1101
    assert len(delays) == 1100
    assert delays[:6] == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]
    assert set(delays[6:]) == {30.0}
    assert subscription.queue.get_nowait() == {"type": "recovered"}
    assert subscription.queue.empty()
