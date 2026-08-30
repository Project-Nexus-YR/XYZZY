"""The audit export is the whole room, not the first page of it.

EventRepo.list_since pages at 500 by default; an export that read one page and
called it done would silently truncate any room past that mark. These tests
pin: an admin-only gate, a chain_verified line that calls verify_event_chain
rather than recomputing anything, and a room with more than 500 events whose
export line count equals the room's own counter.
"""

import json

import pytest
from fastapi.testclient import TestClient

from multiplayer.db.connection import Database
from multiplayer.db.repositories import EventRepo
from multiplayer.domain.events import EventType, RoomEvent
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.audit import verify_event_chain
from multiplayer.server import create_app
from multiplayer.services.service import MultiplayerService

OWNER_HEADERS = {"Authorization": "Bearer owner-token"}
OUTSIDER_HEADERS = {"Authorization": "Bearer outsider-token"}
TIMESTAMP = "2026-01-01T00:00:00+00:00"


def _seed_room(client: TestClient) -> str:
    org = client.post(
        "/api/v1/organizations", headers=OWNER_HEADERS, json={"name": "Acme", "slug": "acme"}
    ).json()
    workspace = client.post(
        f"/api/v1/organizations/{org['org_id']}/workspaces",
        headers=OWNER_HEADERS,
        json={"name": "Main", "slug": "main"},
    ).json()
    room = client.post(
        f"/api/v1/workspaces/{workspace['workspace_id']}/rooms",
        headers=OWNER_HEADERS,
        json={"name": "General"},
    ).json()
    return str(room["room_id"])


def _app() -> object:
    return create_app(
        ":memory:",
        auth_tokens={"owner-token": "user-owner", "outsider-token": "user-outsider"},
    )


def _parse_ndjson(text: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in text.strip().split("\n") if line]


def test_a_non_admin_cannot_export_the_room() -> None:
    with TestClient(_app()) as client:
        room_id = _seed_room(client)
        invite = client.post(
            f"/api/v1/rooms/{room_id}/members/invitations",
            headers=OWNER_HEADERS,
            json={"user_id": "user-outsider", "role": "editor"},
        )
        assert invite.status_code == 200
        response = client.get(f"/api/v1/rooms/{room_id}/audit-export", headers=OUTSIDER_HEADERS)
        assert response.status_code == 403


def test_export_covers_the_room_and_verifies_the_chain() -> None:
    with TestClient(_app()) as client:
        room_id = _seed_room(client)
        client.post(
            f"/api/v1/rooms/{room_id}/messages", headers=OWNER_HEADERS, json={"content": "hi"}
        )
        response = client.get(f"/api/v1/rooms/{room_id}/audit-export", headers=OWNER_HEADERS)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        assert f"xyzzy-audit-{room_id}-" in response.headers["content-disposition"]

        lines = _parse_ndjson(response.text)
        summary = lines[-1]["export_summary"]
        assert summary["room_id"] == room_id
        assert summary["chain_verified"] is True
        assert summary["events"] == len(lines) - 1
        # The production-side proof, not just an incidental count: the summary
        # names the room's own counter, and the export claims to equal it.
        assert summary["events"] == summary["sequence_counter"]
        for line in lines[:-1]:
            assert {"sequence", "event_type", "actor", "created_at", "payload", "event_hash"} <= (
                line.keys()
            )


def test_a_missing_room_is_403_before_ever_being_404() -> None:
    """Same deny-by-default shape as every other room route: an unknown room
    grants no membership, so the capability gate refuses before existence is
    ever checked — nothing distinguishes "not yours" from "does not exist"."""
    with TestClient(_app()) as client:
        response = client.get("/api/v1/rooms/room_missing/audit-export", headers=OWNER_HEADERS)
        assert response.status_code == 403


async def test_export_pages_past_the_500_row_default_without_truncating() -> None:
    """More events than list_since's own page size, exported and counted exactly."""
    db = Database(":memory:")
    await db.connect()
    try:
        svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset())
        await svc.initialize()
        await db.execute(
            "INSERT INTO organizations(org_id, name, slug, created_at) VALUES (?, ?, ?, ?)",
            ("org_1", "Org", "org", TIMESTAMP),
        )
        await db.execute(
            "INSERT INTO workspaces(workspace_id, org_id, name, slug, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("ws_1", "org_1", "Ws", "ws", TIMESTAMP),
        )
        await db.execute(
            "INSERT INTO rooms(room_id, workspace_id, name, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("room_1", "ws_1", "Room", "user_1", TIMESTAMP),
        )
        total = 640
        repo = EventRepo(db)
        for index in range(total):
            await repo.append_with_next_sequence(
                RoomEvent(
                    room_id="room_1",
                    sequence=0,
                    event_type=EventType.ROOM_UPDATED,
                    payload={"note": f"event {index + 1}"},
                    actor_id="user_1",
                    actor_type="user",
                )
            )
        lines = [line async for line in svc.export_room_audit("room_1")]
        parsed = [json.loads(line) for line in lines]
        summary = parsed[-1]["export_summary"]
        counter = await svc.repos.events.get_sequence_counter("room_1")
        assert counter == total
        assert summary["events"] == total
        # The comparison export_room_audit itself makes, not one this test
        # recomputes independently: the counter it names is the one exported
        # equals, on the production side, not merely in this assertion.
        assert summary["sequence_counter"] == counter
        assert summary["events"] == summary["sequence_counter"]
        assert summary["chain_verified"] is True
        assert len(parsed) - 1 == total
        # Sequence-ordered and unbroken across the internal page boundary.
        assert [line["sequence"] for line in parsed[:-1]] == list(range(1, total + 1))
    finally:
        await db.close()


async def test_export_flags_chain_unverified_when_its_own_paging_undercounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chain_verified must catch export_room_audit's OWN read undercounting too,
    not only a break already visible in the stored chain.

    The room here is perfectly healthy — three valid, unbroken events — so
    verify_event_chain alone would call it verified. list_since_with_chain is
    monkeypatched to stop one row short of the room's real event count, the way
    a future paging regression in export_room_audit would: this must still be
    caught, because chain_verified now depends on what the export itself read
    matching the room's own counter, not only on the chain's own hashes.
    """
    import multiplayer.db.repositories as repositories_module

    db = Database(":memory:")
    await db.connect()
    try:
        svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset())
        await svc.initialize()
        await db.execute(
            "INSERT INTO organizations(org_id, name, slug, created_at) VALUES (?, ?, ?, ?)",
            ("org_1", "Org", "org", TIMESTAMP),
        )
        await db.execute(
            "INSERT INTO workspaces(workspace_id, org_id, name, slug, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("ws_1", "org_1", "Ws", "ws", TIMESTAMP),
        )
        await db.execute(
            "INSERT INTO rooms(room_id, workspace_id, name, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("room_1", "ws_1", "Room", "user_1", TIMESTAMP),
        )
        repo = EventRepo(db)
        for index in range(3):
            await repo.append_with_next_sequence(
                RoomEvent(
                    room_id="room_1",
                    sequence=0,
                    event_type=EventType.ROOM_UPDATED,
                    payload={"note": f"event {index + 1}"},
                    actor_id="user_1",
                    actor_type="user",
                )
            )
        # Fully healthy chain: verify_event_chain alone would report no breaks.
        verified, breaks = await verify_event_chain(db)
        assert (verified, breaks) == (3, [])

        real_page = repositories_module.EventRepo.list_since_with_chain

        async def truncated_page(self: object, room_id: str, after_sequence: int, limit: int = 500):
            if after_sequence == 0:
                return await real_page(self, room_id, after_sequence, limit=1)
            return []

        monkeypatch.setattr(repositories_module.EventRepo, "list_since_with_chain", truncated_page)

        parsed = [json.loads(line) async for line in svc.export_room_audit("room_1")]
        summary = parsed[-1]["export_summary"]
        assert summary["sequence_counter"] == 3
        assert summary["events"] == 1
        assert summary["chain_verified"] is False
    finally:
        await db.close()
