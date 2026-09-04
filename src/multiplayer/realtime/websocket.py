"""WebSocket endpoint for realtime room updates."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Sequence
from contextlib import suppress
from typing import Protocol

from starlette.websockets import WebSocket, WebSocketDisconnect

from ..domain.events import RoomEvent
from ..realtime.hub import RealtimeHub, RealtimeSubscription, room_event_payload
from ..security import (
    AuthenticationError,
    AuthorizationError,
    RoomCapability,
    RoomPolicy,
    TokenAuthenticator,
)

log = logging.getLogger(__name__)


class EventSource(Protocol):
    """What the socket needs to replay history on a subscribe cursor: the
    same event log `GET /rooms/{id}/state` reads from
    (`MultiplayerService.get_room_events`), so a fresh socket's cursor and
    the HTTP reconnect path can never disagree about what "since" means.
    """

    async def get_room_events(
        self, room_id: str, after_sequence: int = 0
    ) -> Sequence[RoomEvent]: ...


# How long a revoked credential can keep an already-open socket: the send loop
# re-reads the token row on this cadence, so an operator's revocation reaches a
# live connection without any channel into the server process.
REAUTH_SECONDS = 30.0


def _websocket_authorization(
    websocket: WebSocket, allowed_origins: Sequence[str], session_cookie: str | None
) -> tuple[str | None, str | None]:
    """Read a browser-compatible bearer credential from the handshake.

    Browser WebSocket APIs cannot set Authorization headers. The UI therefore sends
    ``xyzzy.v1`` plus a base64url encoded ``bearer.<token>`` protocol value. This
    keeps credentials out of URLs, query logs, and reconnect history.

    Cookie mode has no subprotocol to send either, and the browser attaches the
    cookie itself. What stands in for the header-gate CSRF check HTTP cookie auth
    uses is the Origin header, which a WebSocket handshake does carry and a script
    cannot forge: it must equal one of `configured_origins()` exactly.

    ``session_cookie`` names exactly the one cookie this deployment's scheme
    sets — never both names. Accepting the plain name as a fallback on an
    HTTPS deployment is the same hole a related-subdomain attacker would use
    against the HTTP cookie path, so there is no fallback here either.
    """
    authorization = websocket.headers.get("authorization")
    if authorization:
        return authorization, None
    protocols = [
        value.strip()
        for value in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if value.strip()
    ]
    if "xyzzy.v1" in protocols:
        encoded = next(
            (value.removeprefix("bearer.") for value in protocols if value.startswith("bearer.")),
            "",
        )
        if encoded:
            try:
                padded = encoded + "=" * (-len(encoded) % 4)
                token = base64.urlsafe_b64decode(padded.encode()).decode()
            except (ValueError, UnicodeDecodeError):
                return None, None
            return f"Bearer {token}", "xyzzy.v1"
    cookie = websocket.cookies.get(session_cookie) if session_cookie else None
    if cookie and websocket.headers.get("origin", "") in allowed_origins:
        return f"Bearer {cookie}", None
    return None, None


async def websocket_endpoint(
    websocket: WebSocket,
    hub: RealtimeHub,
    authenticator: TokenAuthenticator,
    authorization: RoomPolicy,
    events: EventSource,
    allowed_origins: Sequence[str] = (),
    session_cookie: str | None = None,
) -> None:
    """Handle a WebSocket connection for realtime room updates.

    Query params:
        room_id: Room to subscribe to
        last_sequence: Optional. The sequence the caller's own snapshot
            (`GET /rooms/{id}/state`) ended at. When present (any
            non-negative integer, including 0), every room event with a
            greater sequence is replayed over the socket, in order, before
            live delivery begins, so a client that fetched a snapshot and
            then opens this socket sees no gap and no duplicate between the
            two. Absent (the default): no replay, exactly today's behavior.
    """
    room_id = websocket.query_params.get("room_id", "").strip()

    if not room_id:
        await websocket.close(code=4400, reason="room_id required")
        return

    raw_cursor = websocket.query_params.get("last_sequence")
    replay_cursor: int | None = None
    if raw_cursor is not None and raw_cursor.strip().isdigit():
        replay_cursor = int(raw_cursor.strip())

    websocket_authorization, accepted_protocol = _websocket_authorization(
        websocket, allowed_origins, session_cookie
    )
    try:
        principal = await authenticator.authenticate(websocket_authorization)
    except AuthenticationError:
        await websocket.close(code=4401, reason="authentication required")
        return

    try:
        await authorization.require(room_id, principal.user_id, RoomCapability.READ)
    except AuthorizationError:
        await websocket.close(code=4403, reason="room access forbidden")
        return

    user_id = principal.user_id
    await websocket.accept(subprotocol=accepted_protocol)

    sub = await hub.subscribe(room_id, user_id)
    # Every subscription this socket holds, keyed by room. The primary and
    # every extra room share `sub.queue`, so one send_loop below delivers
    # events from all of them, and every exit path can release exactly the
    # subscriptions this socket created without touching anyone else's.
    subs_by_room: dict[str, RealtimeSubscription] = {room_id: sub}

    # Send connection confirmation
    await websocket.send_json(
        {
            "type": "connected",
            "subscription_id": sub.subscription_id,
            "room_id": room_id,
        }
    )

    # Every event this backfill already put on the wire, tracked by its
    # globally unique event_id rather than its (per-room) sequence: this
    # socket may end up sharing its queue with another room's subscription,
    # whose sequence numbers live in their own, unrelated namespace.
    replayed_event_ids: set[str] = set()
    if replay_cursor is not None:
        # Subscribed above, before this read: nothing committed from here
        # on can be missed by both the backfill and live delivery at once,
        # only double caught by both, which replayed_event_ids dedupes.
        for room_event in await events.get_room_events(room_id, replay_cursor):
            payload = room_event_payload(room_event)
            replayed_event_ids.add(payload["event_id"])
            await websocket.send_json(payload)

    async def send_loop() -> None:
        """Read from subscription queue and send to WebSocket.

        Authentication happened once at the handshake, but a credential can be
        revoked while the socket lives, from a process this one never hears
        from. The loop therefore re-authenticates on its own heartbeat: a
        revoked token closes the connection within about two beats, busy or
        quiet.

        Membership rides the same heartbeat. The Redis revoke message is
        lossy by contract (see fanout.py), so a socket on another process
        must not depend on it ever arriving: after the credential check, this
        recheck's own room membership for every room it is subscribed to
        (one read per room via the authorization policy) and revokes locally
        for any room membership no longer covers. That caps exposure to one
        reauth period regardless of whether the pub/sub message ever landed.
        """
        loop = asyncio.get_running_loop()
        next_reauth = loop.time() + REAUTH_SECONDS
        while True:
            if loop.time() >= next_reauth:
                try:
                    await authenticator.authenticate(websocket_authorization)
                except AuthenticationError:
                    await websocket.close(code=4401, reason="authentication revoked")
                    return
                except Exception:
                    # Cannot re-check (e.g. shutdown closed the database): close
                    # rather than leave the client on a silent, pingless socket.
                    with suppress(Exception):
                        await websocket.close(code=1011, reason="authentication check failed")
                    return
                for subscribed_room in await hub.get_user_rooms(user_id):
                    try:
                        await authorization.require(subscribed_room, user_id, RoomCapability.READ)
                    except AuthorizationError:
                        await hub.revoke_room_access(user_id, subscribed_room)
                    except Exception:
                        log.exception(
                            "Membership recheck failed for user %s in room %s",
                            user_id,
                            subscribed_room,
                        )
                next_reauth = loop.time() + REAUTH_SECONDS
            try:
                event = await asyncio.wait_for(sub.queue.get(), timeout=REAUTH_SECONDS)
                if event.get("type") == "access_revoked":
                    # Membership was removed; the hub already dropped the subscription.
                    await websocket.close(code=4403, reason="room access revoked")
                    return
                if event.get("type") == "resync":
                    # The hub's queue for this subscription overflowed: this
                    # socket has a hole it cannot see. Close it so the
                    # client's existing reconnect and loadState() path
                    # fetches a fresh snapshot instead of diverging silently.
                    await websocket.close(code=4408, reason="resync required")
                    return
                event_id = event.get("event_id")
                if event_id is not None and event_id in replayed_event_ids:
                    # Already sent during the subscribe backfill above.
                    continue
                await websocket.send_json(event)
            except TimeoutError:
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    return
            except Exception:
                return

    send_task = asyncio.create_task(send_loop())

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "invalid json"})
                continue

            msg_type = msg.get("type", "")

            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
            elif msg_type == "subscribe":
                extra_room = msg.get("room_id", "").strip()
                if extra_room and extra_room != room_id:
                    try:
                        await authorization.require(extra_room, user_id, RoomCapability.READ)
                    except AuthorizationError:
                        await websocket.send_json(
                            {
                                "type": "error",
                                "message": "room access forbidden",
                            }
                        )
                    else:
                        # A repeated subscribe is answered, not re-registered: a
                        # second hub subscription on the same queue would double
                        # every event and orphan the first one from cleanup.
                        if extra_room not in subs_by_room:
                            subs_by_room[extra_room] = await hub.subscribe(
                                extra_room, user_id, queue=sub.queue
                            )
                        await websocket.send_json(
                            {
                                "type": "subscribed",
                                "room_id": extra_room,
                            }
                        )
            elif msg_type == "unsubscribe":
                extra_room = msg.get("room_id", "").strip()
                if extra_room:
                    own_sub = subs_by_room.pop(extra_room, None)
                    if own_sub:
                        await hub.unsubscribe(own_sub.subscription_id)
                    await websocket.send_json(
                        {
                            "type": "unsubscribed",
                            "room_id": extra_room,
                        }
                    )
            else:
                await websocket.send_json(
                    {
                        "type": "error",
                        "message": f"unknown message type: {msg_type}",
                    }
                )

    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("WebSocket error for user %s in room %s", user_id, room_id)
        # Tell the client the server gave up, rather than vanishing: a handler
        # that returns without a close frame leaves the peer waiting on a
        # socket nobody will write to again.
        with suppress(Exception):
            await websocket.close(code=1011, reason="internal error")
    finally:
        send_task.cancel()
        try:
            await send_task
        except asyncio.CancelledError:
            pass
        for held_sub in subs_by_room.values():
            await hub.unsubscribe(held_sub.subscription_id)
