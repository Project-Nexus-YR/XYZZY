"""Search authorization: the query itself decides, and nothing is indexed by default.

Two invariants are proven here. A non-member searching for a private room's exact
words gets zero rows out of SQLite — not rows that some later Python filter is
trusted to drop. And an object kind that nobody opted in cannot enter the index at
all, so a new sensitive kind is unsearchable by default rather than searchable
until someone remembers to exclude it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from multiplayer.db.connection import Database
from multiplayer.db.repositories import SearchRepo
from multiplayer.domain.models import MessageRole, SearchObjectKind
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.authorization import (
    _ROLE_CAPABILITIES,
    RoomCapability,
    capabilities_for_role,
)
from multiplayer.server import create_app
from multiplayer.services.service import MultiplayerService

OWNER_HEADERS = {"Authorization": "Bearer owner-token"}
OUTSIDER_HEADERS = {"Authorization": "Bearer outsider-token"}

SECRET = "quicksilver rollback rehearsal"


def _seed_private_room(client: TestClient) -> str:
    org = client.post(
        "/api/v1/organizations",
        headers=OWNER_HEADERS,
        json={"name": "Private org", "slug": "private-org"},
    ).json()
    workspace = client.post(
        f"/api/v1/organizations/{org['org_id']}/workspaces",
        headers=OWNER_HEADERS,
        json={"name": "Private workspace", "slug": "private-workspace"},
    ).json()
    room = client.post(
        f"/api/v1/workspaces/{workspace['workspace_id']}/rooms",
        headers=OWNER_HEADERS,
        json={"name": "Private decision"},
    ).json()
    posted = client.post(
        f"/api/v1/rooms/{room['room_id']}/messages",
        headers=OWNER_HEADERS,
        json={"content": SECRET},
    )
    assert posted.status_code == 200, posted.text
    return str(room["room_id"])


def test_a_non_member_searching_private_content_retrieves_zero_rows() -> None:
    app = create_app(
        ":memory:",
        auth_tokens={"owner-token": "user-a", "outsider-token": "user-b"},
    )
    with TestClient(app) as client:
        room_id = _seed_private_room(client)

        owner_hits = client.get(
            "/api/v1/search", headers=OWNER_HEADERS, params={"q": "quicksilver rollback"}
        )
        assert owner_hits.status_code == 200
        assert [hit["object_kind"] for hit in owner_hits.json()] == ["MESSAGE"]
        assert owner_hits.json()[0]["room_id"] == room_id

        # Unscoped, room-scoped, and single-word searches all return nothing, and the
        # zero rows come from the database rather than from a filter after the fact.
        for params in (
            {"q": "quicksilver rollback"},
            {"q": "quicksilver rollback", "room_id": room_id},
            {"q": "quicksilver"},
            {"q": "rehearsal"},
        ):
            denied = client.get("/api/v1/search", headers=OUTSIDER_HEADERS, params=params)
            assert denied.status_code == 200, denied.text
            assert denied.json() == [], params

        assert client.get("/api/v1/search", params={"q": "quicksilver"}).status_code == 401


def test_search_stops_matching_the_moment_membership_is_removed() -> None:
    app = create_app(
        ":memory:",
        auth_tokens={"owner-token": "user-a", "member-token": "user-b"},
    )
    with TestClient(app) as client:
        room_id = _seed_private_room(client)
        member_headers = {"Authorization": "Bearer member-token"}
        invited = client.post(
            f"/api/v1/rooms/{room_id}/members/invitations",
            headers=OWNER_HEADERS,
            json={"user_id": "user-b", "role": "viewer"},
        )
        assert invited.status_code == 200, invited.text

        found = client.get("/api/v1/search", headers=member_headers, params={"q": "quicksilver"})
        assert [hit["room_id"] for hit in found.json()] == [room_id]

        removed = client.delete(f"/api/v1/rooms/{room_id}/members/user-b", headers=OWNER_HEADERS)
        assert removed.status_code == 200, removed.text

        after = client.get("/api/v1/search", headers=member_headers, params={"q": "quicksilver"})
        assert after.json() == []


@pytest.mark.asyncio
async def test_an_object_kind_nobody_opted_in_cannot_enter_the_index() -> None:
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub())
    await svc.initialize()
    try:
        org = await svc.create_organization("Allow org", "allow-org", "owner")
        workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
        room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
        await svc.send_message(room.room_id, MessageRole.HUMAN, "owner", SECRET)

        listed = await db.fetch_all(
            "SELECT object_kind FROM search_indexed_kinds ORDER BY object_kind"
        )
        kinds = [row["object_kind"] for row in listed]
        assert kinds == ["AGENT_OUTPUT", "ARTIFACT_VERSION", "DECISION", "MESSAGE", "TASK"]
        # The table and the enum are one allowlist expressed twice, never two that
        # can drift: a kind added to either alone fails here.
        assert kinds == sorted(kind.value for kind in SearchObjectKind)

        with pytest.raises(Exception, match="FOREIGN KEY"):
            await db.execute(
                "INSERT INTO search_documents(object_kind, object_id, room_id, content, "
                "created_at) VALUES ('CREDENTIAL', 'cred_1', ?, 'root password', '2026-01-01')",
                (room.room_id,),
            )

        assert await svc.search("owner", "password") == []
        assert len(await svc.search("owner", "quicksilver")) == 1
    finally:
        await db.close()


def test_the_searching_roles_cannot_drift_from_the_capability_table() -> None:
    """The authorizing join's role predicate is the policy's, not a copy beside it.

    A second list of role names is a second place to forget: this fails the moment
    the search query's roles stop being exactly the roles that carry READ.
    """
    expected = tuple(
        sorted(
            role
            for role in _ROLE_CAPABILITIES
            if RoomCapability.READ in capabilities_for_role(role)
        )
    )
    assert SearchRepo._READING_ROLES == expected
    assert all(
        RoomCapability.READ in capabilities_for_role(role) for role in SearchRepo._READING_ROLES
    )


@pytest.mark.asyncio
async def test_a_membership_role_outside_the_policy_matches_nothing() -> None:
    """Deny by default: a role the policy never granted READ reads no rows."""
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub())
    await svc.initialize()
    try:
        org = await svc.create_organization("Role org", "role-org", "owner")
        workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
        room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
        await svc.send_message(room.room_id, MessageRole.HUMAN, "owner", SECRET)
        await db.execute(
            "INSERT INTO room_members(room_id, user_id, role, joined_at) "
            "VALUES (?, 'observer-user', 'observer', '2026-01-01T00:00:00+00:00')",
            (room.room_id,),
        )

        assert capabilities_for_role("observer") == frozenset()
        assert await svc.search("observer-user", "quicksilver") == []
        assert len(await svc.search("owner", "quicksilver")) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_hit_names_the_room_it_lives_in() -> None:
    """A result the reader cannot place is a result they cannot act on."""
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub())
    await svc.initialize()
    try:
        org = await svc.create_organization("Named org", "named-org", "owner")
        workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
        room = await svc.create_room(workspace.workspace_id, "Authentication migration", "owner")
        await svc.send_message(room.room_id, MessageRole.HUMAN, "owner", SECRET)

        hits = await svc.search("owner", "quicksilver")
        assert [hit.room_name for hit in hits] == ["Authentication migration"]
        assert [hit.room_id for hit in hits] == [room.room_id]
    finally:
        await db.close()
