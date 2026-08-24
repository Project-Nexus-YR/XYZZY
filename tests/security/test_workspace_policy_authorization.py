"""A workspace-wide governance write demands the admin role, re-read at commit.

The room tier grades every member into a capability set and re-checks it
inside the write's transaction; the workspace tier graded nobody, so
set_workspace_policy - which bounds every room in the workspace - was gated
on bare membership. These tests pin the repaired symmetry, and the matching
fence on remove_room_member, whose route demanded ADMINISTER while the
service never re-checked it.
"""

import pytest

from multiplayer.db.connection import Database
from multiplayer.domain.models import WorkspaceMember
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security import AuthorizationError
from multiplayer.services.service import MultiplayerService


@pytest.fixture
async def service():
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(
        db,
        RealtimeHub(),
        known_users=frozenset({"admin_1", "admin_2", "mallory", "victim", "expired"}),
    )
    await svc.initialize()
    yield svc
    await db.close()


async def _workspace_with_room(svc: MultiplayerService):
    org = await svc.create_organization("Acme", "acme", "admin_1")
    ws = await svc.create_workspace(org.org_id, "Main", "main", "admin_1")
    room = await svc.create_room(ws.workspace_id, "Ops", "admin_1")
    return ws, room


async def test_a_plain_workspace_member_cannot_bound_every_room(service):
    ws, _ = await _workspace_with_room(service)
    await service.repos.workspaces.add_member(
        WorkspaceMember(workspace_id=ws.workspace_id, user_id="mallory", role="member")
    )
    with pytest.raises(AuthorizationError):
        await service.set_workspace_policy(ws.workspace_id, ["READ"], "mallory")


async def test_a_non_member_cannot_bound_every_room(service):
    ws, _ = await _workspace_with_room(service)
    with pytest.raises(AuthorizationError):
        await service.set_workspace_policy(ws.workspace_id, ["READ"], "outsider")


async def test_the_workspace_admin_can_and_every_room_logs_it(service):
    ws, room = await _workspace_with_room(service)
    await service.set_workspace_policy(ws.workspace_id, ["READ"], "admin_1")
    stored = await service.repos.workspaces.get(ws.workspace_id)
    assert stored is not None
    events = await service.repos.events.list_since(room.room_id, 0)
    assert any(e.event_type.value == "workspace.policy_updated" for e in events)


async def test_an_admin_demoted_before_the_write_commits_is_refused(service):
    ws, _ = await _workspace_with_room(service)
    # The route check passed while admin_2 was an admin; the demotion lands
    # before the service write. The in-transaction re-read must refuse.
    await service.repos.workspaces.add_member(
        WorkspaceMember(workspace_id=ws.workspace_id, user_id="admin_2", role="admin")
    )
    await service.db.execute(
        "UPDATE workspace_members SET role = 'member' WHERE workspace_id = ? AND user_id = ?",
        (ws.workspace_id, "admin_2"),
    )
    with pytest.raises(AuthorizationError):
        await service.set_workspace_policy(ws.workspace_id, ["READ"], "admin_2")


async def test_a_demoted_admin_cannot_remove_a_member(service):
    _, room = await _workspace_with_room(service)
    await service.invite_room_member(room.room_id, "victim", "editor", "admin_1")
    await service.invite_room_member(room.room_id, "expired", "editor", "admin_1")
    # The route authorized ADMINISTER while 'expired' held it; here they hold
    # editor, so the in-transaction re-check must refuse the removal.
    with pytest.raises(AuthorizationError):
        await service.remove_room_member(room.room_id, "victim", "expired")
    still_there = await service.repos.room_members.get(room.room_id, "victim")
    assert still_there is not None
