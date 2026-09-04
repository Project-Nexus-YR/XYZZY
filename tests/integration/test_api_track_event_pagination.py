"""Finding 36: GET /rooms/{room_id}/events and /rooms/{room_id}/state used to
page a room's entire event log into memory with no limit, unlike every
sibling list route. Both now accept a limit and hand back at most that many
events, so a busy room's log cannot be pulled whole in one request.

Events are seeded straight through the events repository, not through
`send_message` or the HTTP route: this needs hundreds of rows in one room,
and going through the full message pipeline (or the route's own rate limit)
for each one would make the test itself the slow, memory-heavy thing it is
testing against.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

import multiplayer.api.routes as routes
from multiplayer.domain.events import EventType, RoomEvent
from multiplayer.server import create_app

OWNER_HEADERS = {"Authorization": "Bearer owner-token"}


async def _room_with_events(client: AsyncClient, count: int) -> str:
    bootstrap = await client.post(
        "/api/v1/me/bootstrap",
        headers=OWNER_HEADERS,
        json={"display_name": "Owner", "room_name": "Busy"},
    )
    assert bootstrap.status_code == 200, bootstrap.text
    room_id = str(bootstrap.json()["room"]["room_id"])
    svc = routes._svc
    assert svc is not None
    for i in range(count):
        event = RoomEvent(
            room_id=room_id,
            sequence=0,
            event_type=EventType.SESSION_STARTED,
            payload={"i": i},
            actor_id="user-owner",
            actor_type="user",
        )
        await svc.repos.events.append_with_next_sequence(event)
    return room_id


@pytest.mark.asyncio
async def test_events_route_caps_the_page_at_the_default_limit() -> None:
    app = create_app(":memory:", auth_tokens={"owner-token": "user-owner"})
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            room_id = await _room_with_events(c, 501)
            response = await c.get(f"/api/v1/rooms/{room_id}/events", headers=OWNER_HEADERS)
        assert response.status_code == 200
        assert len(response.json()) == 500


@pytest.mark.asyncio
async def test_events_route_honors_a_smaller_explicit_limit() -> None:
    app = create_app(":memory:", auth_tokens={"owner-token": "user-owner"})
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            room_id = await _room_with_events(c, 10)
            response = await c.get(
                f"/api/v1/rooms/{room_id}/events",
                headers=OWNER_HEADERS,
                params={"limit": 3},
            )
        assert response.status_code == 200
        assert len(response.json()) == 3


@pytest.mark.asyncio
async def test_state_route_caps_events_since_at_the_default_limit() -> None:
    app = create_app(":memory:", auth_tokens={"owner-token": "user-owner"})
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            room_id = await _room_with_events(c, 501)
            response = await c.get(f"/api/v1/rooms/{room_id}/state", headers=OWNER_HEADERS)
        assert response.status_code == 200
        assert len(response.json()["events_since"]) == 500


@pytest.mark.asyncio
async def test_state_route_honors_a_smaller_explicit_events_limit() -> None:
    app = create_app(":memory:", auth_tokens={"owner-token": "user-owner"})
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            room_id = await _room_with_events(c, 10)
            response = await c.get(
                f"/api/v1/rooms/{room_id}/state",
                headers=OWNER_HEADERS,
                params={"events_limit": 4},
            )
        assert response.status_code == 200
        assert len(response.json()["events_since"]) == 4
