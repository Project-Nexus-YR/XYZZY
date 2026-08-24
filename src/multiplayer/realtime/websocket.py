"""WebSocket endpoint for realtime room updates."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from contextlib import suppress

from starlette.websockets import WebSocket, WebSocketDisconnect

from ..realtime.hub import RealtimeHub
from ..security import (
    AuthenticationError,
    AuthorizationError,
    RoomCapability,
    RoomPolicy,
    TokenAuthenticator,
)

log = logging.getLogger(__name__)

# How long a revoked credential can keep an already-open socket: the send loop
# re-reads the token row on this cadence, so an operator's revocation reaches a
# live connection without any channel into the server process.
REAUTH_SECONDS = 30.0


def _websocket_authorization(websocket: WebSocket) -> tuple[str | None, str | None]:
    """Read a browser-compatible bearer credential from negotiated subprotocols.

    Browser WebSocket APIs cannot set Authorization headers. The UI therefore sends
    ``xyzzy.v1`` plus a base64url encoded ``bearer.<token>`` protocol value. This
    keeps credentials out of URLs, query logs, and reconnect history.
    """
    authorization = websocket.headers.get("authorization")
    if authorization:
        return authorization, None
    protocols = [
        value.strip()
        for value in websocket.headers.get("sec-websocket-protocol", "").split(",")
        if value.strip()
    ]
    if "xyzzy.v1" not in protocols:
        return None, None
    encoded = next(
        (value.removeprefix("bearer.") for value in protocols if value.startswith("bearer.")), ""
    )
    if not encoded:
        return None, None
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        token = base64.urlsafe_b64decode(padded.encode()).decode()
    except (ValueError, UnicodeDecodeError):
        return None, None
    return f"Bearer {token}", "xyzzy.v1"


async def websocket_endpoint(
    websocket: WebSocket,
    hub: RealtimeHub,
    authenticator: TokenAuthenticator,
    authorization: RoomPolicy,
) -> None:
    """Handle a WebSocket connection for realtime room updates.

    Query params:
        room_id: Room to subscribe to
        last_sequence: Last known sequence for catch-up (informational)
    """
    room_id = websocket.query_params.get("room_id", "").strip()

    if not room_id:
        await websocket.close(code=4400, reason="room_id required")
        return

    websocket_authorization, accepted_protocol = _websocket_authorization(websocket)
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

    # Send connection confirmation
    await websocket.send_json(
        {
            "type": "connected",
            "subscription_id": sub.subscription_id,
            "room_id": room_id,
        }
    )

    async def send_loop() -> None:
        """Read from subscription queue and send to WebSocket.

        Authentication happened once at the handshake, but a credential can be
        revoked while the socket lives, from a process this one never hears
        from. The loop therefore re-authenticates on its own heartbeat: a
        revoked token closes the connection within about two beats, busy or
        quiet.
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
                next_reauth = loop.time() + REAUTH_SECONDS
            try:
                event = await asyncio.wait_for(sub.queue.get(), timeout=REAUTH_SECONDS)
                if event.get("type") == "access_revoked":
                    # Membership was removed; the hub already dropped the subscription.
                    await websocket.close(code=4403, reason="room access revoked")
                    return
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
                        await hub.subscribe(extra_room, user_id)
                        await websocket.send_json(
                            {
                                "type": "subscribed",
                                "room_id": extra_room,
                            }
                        )
            elif msg_type == "unsubscribe":
                extra_room = msg.get("room_id", "").strip()
                if extra_room:
                    sub_ids = await hub.get_subscriptions_for_user_room(user_id, extra_room)
                    for sid in sub_ids:
                        await hub.unsubscribe(sid)
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
    finally:
        send_task.cancel()
        try:
            await send_task
        except asyncio.CancelledError:
            pass
        await hub.unsubscribe(sub.subscription_id)
