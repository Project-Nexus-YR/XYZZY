"""Finding 13: a room editor's invite grants irrevocable workspace-scoped
standing to any account on the deployment, because no route or CLI verb
could remove a workspace member. ``remove_workspace_member`` closes that:
admin-only, it strips membership in every room of the workspace along with
the workspace row itself.
"""

from __future__ import annotations

import pytest

from multiplayer.db.connection import Database
from multiplayer.domain.models import DomainError
from multiplayer.manage import open_database, remove_workspace_member
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.authorization import AuthorizationError
from multiplayer.services.service import MultiplayerService


@pytest.fixture
async def service() -> MultiplayerService:
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner", "guest"}))
    await svc.initialize()
    yield svc
    await db.close()


@pytest.mark.asyncio
async def test_a_room_editors_invite_grant_can_be_undone_by_a_workspace_admin(
    service: MultiplayerService,
) -> None:
    svc = service
    org = await svc.create_organization("Org", "org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    await svc.invite_room_member(room.room_id, "guest", "editor", "owner")

    # The invite is exactly the gap the finding describes: an editor's invite
    # made "guest" a workspace member with no route to undo it.
    member = await svc.repos.workspaces.get_member(workspace.workspace_id, "guest")
    assert member is not None

    await svc.remove_workspace_member(workspace.workspace_id, "guest", "owner")

    assert await svc.repos.workspaces.get_member(workspace.workspace_id, "guest") is None
    assert await svc.repos.room_members.get(room.room_id, "guest") is None
    types = [e.event_type.value for e in await svc.get_room_events(room.room_id)]
    assert "workspace.member_removed" in types


@pytest.mark.asyncio
async def test_a_non_admin_may_not_remove_a_workspace_member(
    service: MultiplayerService,
) -> None:
    svc = service
    org = await svc.create_organization("Org", "org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    await svc.invite_room_member(room.room_id, "guest", "editor", "owner")

    with pytest.raises(AuthorizationError):
        await svc.remove_workspace_member(workspace.workspace_id, "guest", "guest")

    assert await svc.repos.workspaces.get_member(workspace.workspace_id, "guest") is not None


@pytest.mark.asyncio
async def test_removing_an_unknown_member_raises_domain_error(
    service: MultiplayerService,
) -> None:
    svc = service
    org = await svc.create_organization("Org", "org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")

    with pytest.raises(DomainError):
        await svc.remove_workspace_member(workspace.workspace_id, "nobody", "owner")


@pytest.mark.asyncio
async def test_the_manage_cli_verb_removes_a_workspace_member_directly(tmp_path) -> None:
    db_path = str(tmp_path / "app.db")
    db = await open_database(db_path)
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner", "guest"}))
    org = await svc.create_organization("Org", "org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    await svc.invite_room_member(room.room_id, "guest", "editor", "owner")
    await db.close()

    db2 = await open_database(db_path)
    try:
        removed = await remove_workspace_member(db2, workspace.workspace_id, "guest")
        assert removed is True
        again = await remove_workspace_member(db2, workspace.workspace_id, "guest")
        assert again is False
    finally:
        await db2.close()
