"""WebSocket endpoint for realtime room updates."""

from __future__ import annotations

import asyncio
import json
import logging

from starlette.websockets import WebSocket, WebSocketDisconnect

from ..realtime.hub import RealtimeHub

log = logging.getLogger(__name__)


async def websocket_endpoint(websocket: WebSocket, hub: RealtimeHub) -> None:
    """Handle a WebSocket connection for realtime room updates.

    Query params:
        room_id: Room to subscribe to
        user_id: User making the connection
        last_sequence: Last known sequence for catch-up (informational)
    """
    await websocket.accept()

    room_id = websocket.query_params.get("room_id", "").strip()
    user_id = websocket.query_params.get("user_id", "").strip()

    if not room_id or not user_id:
        await websocket.close(code=4000, reason="room_id and user_id required")
        return

    sub = await hub.subscribe(room_id, user_id)

    # Send connection confirmation
    await websocket.send_json({
        "type": "connected",
        "subscription_id": sub.subscription_id,
        "room_id": room_id,
    })

    async def send_loop() -> None:
        """Read from subscription queue and send to WebSocket."""
        while True:
            try:
                event = await asyncio.wait_for(sub.queue.get(), timeout=30.0)
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
                    await hub.subscribe(extra_room, user_id)
                    await websocket.send_json({
                        "type": "subscribed",
                        "room_id": extra_room,
                    })
            elif msg_type == "unsubscribe":
                extra_room = msg.get("room_id", "").strip()
                if extra_room:
                    sub_ids = await hub.get_subscriptions_for_user_room(user_id, extra_room)
                    for sid in sub_ids:
                        await hub.unsubscribe(sid)
                    await websocket.send_json({
                        "type": "unsubscribed",
                        "room_id": extra_room,
                    })
            else:
                await websocket.send_json({
                    "type": "error",
                    "message": f"unknown message type: {msg_type}",
                })

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
