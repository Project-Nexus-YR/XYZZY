"""Redis fan-out: best-effort cross-process transport for the realtime hub.

Redis pub/sub is at-most-once transport BY CONTRACT. That is acceptable
precisely because XYZZY events carry per-room sequence numbers and the client
already reconciles gaps against the room event log, which is the single
source of truth (see `list_since` / the reconnect path). This module only
shortens latency between processes; it never becomes a delivery guarantee,
and local delivery in RealtimeHub never waits on it.

Nothing in this file imports the `redis` package: the caller (server.py)
builds the async redis client and passes it in, so importing this module
costs nothing when XYZZY_REDIS_URL is unset.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import secrets
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .hub import RealtimeHub

log = logging.getLogger(__name__)

DEFAULT_CHANNEL = "xyzzy:events"
_MIN_BACKOFF_SECONDS = 0.5
_MAX_BACKOFF_SECONDS = 30.0


class FanoutMetrics(Protocol):
    def record_redis_publish_failure(self) -> None: ...


class RedisFanout:
    """Publishes hub broadcasts to one Redis channel and replays other
    processes' broadcasts into the local hub.

    Every message carries an `origin` token, random per process at startup.
    A process ignores messages carrying its own origin (it already delivered
    those locally before publishing), which is what stops an echo loop
    between processes without depending on Redis for anything but latency.
    """

    def __init__(
        self,
        redis_client: Any,
        hub: RealtimeHub,
        *,
        channel: str = DEFAULT_CHANNEL,
        metrics: FanoutMetrics | None = None,
    ) -> None:
        self._redis = redis_client
        self._hub = hub
        self._channel = channel
        self._metrics = metrics
        self.origin = secrets.token_hex(16)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Begin the background subscribe loop. Idempotent no-op if already running."""
        if self._task is None:
            self._task = asyncio.create_task(self._subscribe_forever())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        with suppress(Exception):
            await self._redis.aclose()

    async def publish(self, message: dict[str, Any]) -> None:
        """Fire-and-forget publish. Never raises into the caller's request path.

        Called only after the hub has already delivered locally, so a failure
        here — Redis down, network blip — cannot lose anything this process's
        own subscribers were owed; it only means other processes wait for
        their own reconnect/replay instead of hearing about it early.
        """
        try:
            body = json.dumps({**message, "origin": self.origin})
            await self._redis.publish(self._channel, body)
        except Exception:
            log.warning("Redis publish failed; local delivery already happened", exc_info=True)
            if self._metrics is not None:
                self._metrics.record_redis_publish_failure()

    async def _subscribe_forever(self) -> None:
        """Reconnect with capped exponential backoff and full jitter, forever.

        A reconnect triggers nothing else — no resync request, no replay
        request. The room event log is the resync; a client that missed
        anything during the gap recovers it on its own reconnect, not because
        this loop asked anyone to resend.
        """
        attempt = 0
        while True:
            pubsub = None
            try:
                pubsub = self._redis.pubsub()
                await pubsub.subscribe(self._channel)
                async for raw in pubsub.listen():
                    # A subscribe that is ACKed and then dropped must still
                    # escalate the backoff, so the attempt counter resets only
                    # once the stream has actually yielded a real message, not
                    # on the subscribe ACK itself.
                    if raw.get("type") != "message":
                        continue
                    attempt = 0
                    await self._handle_message(raw.get("data"))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("Redis subscribe loop dropped; reconnecting", exc_info=True)
            finally:
                if pubsub is not None:
                    with suppress(Exception):
                        await pubsub.aclose()
            attempt += 1
            delay = min(_MAX_BACKOFF_SECONDS, _MIN_BACKOFF_SECONDS * (2**attempt))
            await asyncio.sleep(random.uniform(0, delay))

    async def _handle_message(self, raw: bytes | str | None) -> None:
        if raw is None:
            return
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(message, dict) or message.get("origin") == self.origin:
            return  # our own publish, echoed back by Redis — no double delivery
        kind = message.get("kind")
        # Every field below is read with `.get`, and a missing one drops the
        # message instead of raising: this channel is shared and unnamespaced
        # (another XYZZY deployment, a version skew, an operator's manual
        # PUBLISH), so an off-schema message must not tear down the
        # subscribe loop that every other message on this connection rides.
        if kind == "room_event":
            room_id = message.get("room_id")
            event = message.get("event")
            # Cheap drop: nothing to deserialize further, no local subscriber
            # to give the event to.
            if not room_id or event is None or await self._hub.room_subscriber_count(room_id) == 0:
                return
            await self._hub.broadcast_to_room(room_id, event)
        elif kind == "revoke":
            user_id = message.get("user_id")
            room_id = message.get("room_id")
            if not user_id or not room_id:
                return
            await self._hub.revoke_room_access(user_id, room_id, publish=False)
        elif kind == "send_to_user":
            user_id = message.get("user_id")
            event = message.get("event")
            if not user_id or event is None or not await self._hub.get_user_rooms(user_id):
                return
            await self._hub.send_to_user(user_id, event, publish=False)
