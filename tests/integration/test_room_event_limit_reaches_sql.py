"""The room event cap is enforced by the query, not by slicing in Python.

A room's log is never pruned, so a route that reads the whole table and
trims the response still pays for every row on every call. These tests
trace the LIMIT each query carries, so a regression to read-then-slice
fails here even though the response length would look right.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

import multiplayer.api.routes as routes
from multiplayer.domain.events import EventType, RoomEvent
from multiplayer.server import create_app

OWNER_HEADERS = {"Authorization": "Bearer owner-token"}
SEEDED = 1300


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
        await svc.repos.events.append_with_next_sequence(
            RoomEvent(
                room_id=room_id,
                sequence=0,
                event_type=EventType.SESSION_STARTED,
                payload={"i": i},
                actor_id="user-owner",
                actor_type="user",
            )
        )
    return room_id


class _LimitTrace:
    """Records the LIMIT bound of every room_events query the database runs."""

    def __init__(self, db: Any) -> None:
        self._db = db
        self._original = db.fetch_all
        self.limits: list[int] = []

    def install(self) -> None:
        async def traced(query: str, params: tuple[Any, ...] = ()) -> Any:
            if "room_events" in query and "LIMIT" in query:
                self.limits.append(int(params[-1]))
            return await self._original(query, params)

        self._db.fetch_all = traced

    def remove(self) -> None:
        self._db.fetch_all = self._original


@pytest.mark.asyncio
async def test_both_routes_push_the_cap_into_the_query() -> None:
    app = create_app(":memory:", auth_tokens={"owner-token": "user-owner"})
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            room_id = await _room_with_events(c, SEEDED)
            svc = routes._svc
            assert svc is not None
            trace = _LimitTrace(svc.repos.events.db)
            trace.install()
            try:
                r = await c.get(
                    f"/api/v1/rooms/{room_id}/events",
                    headers=OWNER_HEADERS,
                    params={"limit": 10},
                )
                assert r.status_code == 200
                assert len(r.json()) == 10
                assert trace.limits and all(v <= 10 for v in trace.limits), trace.limits

                trace.limits.clear()
                r = await c.get(
                    f"/api/v1/rooms/{room_id}/state",
                    headers=OWNER_HEADERS,
                    params={"events_limit": 10},
                )
                assert r.status_code == 200
                assert len(r.json()["events_since"]) == 10
                assert trace.limits and all(v <= 10 for v in trace.limits), trace.limits

                trace.limits.clear()
                r = await c.get(
                    f"/api/v1/rooms/{room_id}/state",
                    headers=OWNER_HEADERS,
                    params={"events_limit": 1000},
                )
                assert r.status_code == 200
                assert len(r.json()["events_since"]) == 1000
                assert len(trace.limits) <= 3, trace.limits
                assert all(v <= 500 for v in trace.limits), trace.limits
            finally:
                trace.remove()


@pytest.mark.asyncio
async def test_the_cap_has_a_ceiling_a_floor_and_a_default() -> None:
    app = create_app(":memory:", auth_tokens={"owner-token": "user-owner"})
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            room_id = await _room_with_events(c, SEEDED)
            for route, param in (("events", "limit"), ("state", "events_limit")):
                url = f"/api/v1/rooms/{room_id}/{route}"
                for bad in (0, 1001):
                    r = await c.get(url, headers=OWNER_HEADERS, params={param: bad})
                    assert r.status_code == 422, (route, bad, r.text)
            r = await c.get(f"/api/v1/rooms/{room_id}/state", headers=OWNER_HEADERS)
            assert r.status_code == 200
            assert len(r.json()["events_since"]) <= 500


@pytest.mark.asyncio
async def test_a_cursor_past_the_cap_still_reaches_the_tail() -> None:
    app = create_app(":memory:", auth_tokens={"owner-token": "user-owner"})
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            room_id = await _room_with_events(c, SEEDED)
            r = await c.get(
                f"/api/v1/rooms/{room_id}/events",
                headers=OWNER_HEADERS,
                params={"after": 1290, "limit": 10},
            )
            assert r.status_code == 200
            data = r.json()
            assert len(data) == 10
            assert data[0]["sequence"] == 1291
