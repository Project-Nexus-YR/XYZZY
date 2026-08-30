"""Public API proof for shareable read-only brief links (§E): create, list, revoke,
and the unauthenticated `/share/{token}` page those routes hand out.

Sharing outward is a governance act, so every write here is gated on room
ADMINISTER — a plain member (viewer or editor) can read the brief inside the
room but may not open a door for it to the outside world.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from multiplayer.server import create_app

ADMIN = {"Authorization": "Bearer admin-token"}
VIEWER = {"Authorization": "Bearer viewer-token"}
TOKENS = {"admin-token": "admin_user", "viewer-token": "viewer_user"}


def _bootstrap_artifact(client: TestClient) -> tuple[str, str, str]:
    """Admin opens a workspace and publishes one artifact with real content.

    Returns (room_id, artifact_id, admin_user_id).
    """
    bootstrap = client.post(
        "/api/v1/me/bootstrap",
        headers=ADMIN,
        json={"display_name": "Admin", "room_name": "Payments Decision"},
    ).json()
    room_id = bootstrap["room"]["room_id"]
    art = client.post(
        f"/api/v1/rooms/{room_id}/artifacts",
        headers=ADMIN,
        json={
            "name": "Vendor Brief",
            "artifact_type": "DOCUMENT",
            "content": "# Verdict\n\nGo with **Adyen**. See `T+1` settlement.",
        },
    ).json()
    return room_id, art["artifact_id"], "admin_user"


def test_admin_can_create_list_revoke_a_share_and_the_page_serves_and_then_404s() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id, artifact_id, _ = _bootstrap_artifact(client)

        created = client.post(f"/api/v1/artifacts/{artifact_id}/shares", headers=ADMIN)
        assert created.status_code == 200
        payload = created.json()
        assert set(payload) == {"share_id", "url_path", "created_at"}
        assert payload["url_path"].startswith("/share/")
        token = payload["url_path"].removeprefix("/share/")

        listed = client.get(f"/api/v1/artifacts/{artifact_id}/shares", headers=ADMIN)
        assert listed.status_code == 200
        assert [s["share_id"] for s in listed.json()] == [payload["share_id"]]
        assert listed.json()[0]["revoked_at"] is None

        page = client.get(f"/share/{token}")
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        body = page.text
        assert "Vendor Brief" in body
        assert "Adyen" in body and "<strong>Adyen</strong>" in body
        assert "T+1" in body and "<code>T+1</code>" in body
        assert "Decided with" in body
        # Nothing about the room or its members leaks onto the public page.
        assert "Payments Decision" not in body
        assert "admin_user" not in body
        assert room_id not in body
        assert artifact_id not in body

        revoked = client.delete(
            f"/api/v1/artifacts/{artifact_id}/shares/{payload['share_id']}", headers=ADMIN
        )
        assert revoked.status_code == 200

        gone = client.get(f"/share/{token}")
        assert gone.status_code == 404


def test_non_admin_cannot_create_or_revoke_a_share() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id, artifact_id, _ = _bootstrap_artifact(client)
        client.post(
            f"/api/v1/rooms/{room_id}/members/invitations",
            headers=ADMIN,
            json={"user_id": "viewer_user", "role": "viewer"},
        )

        refused = client.post(f"/api/v1/artifacts/{artifact_id}/shares", headers=VIEWER)
        assert refused.status_code == 403

        # A viewer cannot revoke either, even a share an admin already created.
        share_id = client.post(f"/api/v1/artifacts/{artifact_id}/shares", headers=ADMIN).json()[
            "share_id"
        ]
        refused = client.delete(
            f"/api/v1/artifacts/{artifact_id}/shares/{share_id}", headers=VIEWER
        )
        assert refused.status_code == 403


def test_unknown_and_revoked_tokens_404_the_same_way() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        _room_id, artifact_id, _ = _bootstrap_artifact(client)
        share = client.post(f"/api/v1/artifacts/{artifact_id}/shares", headers=ADMIN).json()
        token = share["url_path"].removeprefix("/share/")
        client.delete(f"/api/v1/artifacts/{artifact_id}/shares/{share['share_id']}", headers=ADMIN)

        revoked_page = client.get(f"/share/{token}")
        unknown_page = client.get("/share/not-a-real-token-at-all")
        malformed_page = client.get("/share/!!!not-even-shaped-like-a-token!!!")

        assert revoked_page.status_code == 404
        assert unknown_page.status_code == 404
        assert malformed_page.status_code == 404
        assert revoked_page.text == unknown_page.text == malformed_page.text


def test_hostile_artifact_content_is_neutralized_on_the_public_page() -> None:
    """The share page is the one place member-authored text meets an
    unauthenticated reader, so it gets the hostile-input proof: script tags,
    event-handler attributes, and javascript: links must all leave the page as
    inert text, and the only hyperlink on the page stays the fixed footer one."""
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        bootstrap = client.post(
            "/api/v1/me/bootstrap",
            headers=ADMIN,
            json={"display_name": "Admin", "room_name": "Hostile Room"},
        ).json()
        room_id = bootstrap["room"]["room_id"]
        artifact_id = client.post(
            f"/api/v1/rooms/{room_id}/artifacts",
            headers=ADMIN,
            json={
                "name": '<script>alert("t")</script>',
                "artifact_type": "DOCUMENT",
                "content": (
                    '<script>alert("c")</script>\n\n'
                    '<img src=x onerror=alert("i")>\n\n'
                    "**<b>bold-wrapped</b>** and `<code-wrapped>`\n\n"
                    '[click](javascript:alert("l"))'
                ),
            },
        ).json()["artifact_id"]
        token = (
            client.post(f"/api/v1/artifacts/{artifact_id}/shares", headers=ADMIN)
            .json()["url_path"]
            .removeprefix("/share/")
        )

        page = client.get(f"/share/{token}")
        assert page.status_code == 200
        body = page.text

        # No injected markup survives anywhere — title, heading, or body.
        assert "<script" not in body
        assert "<img" not in body
        assert "onerror=" not in body.replace("onerror=alert(&quot;i&quot;)", "")
        assert "&lt;script&gt;" in body and "&lt;img" in body

        # Inline transforms run on escaped text, never the original.
        assert "<strong>&lt;b&gt;bold-wrapped&lt;/b&gt;</strong>" in body
        assert "<code>&lt;code-wrapped&gt;</code>" in body

        # Content can never mint a hyperlink: the fixed footer link is the only
        # href on the page, and no javascript: URL appears unescaped.
        assert body.count("href=") == 1
        assert 'href="https://github.com/Project-Nexus-YR/XYZZY"' in body
        assert 'href="javascript:' not in body


def test_cross_room_artifact_id_is_refused_on_revoke() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        bootstrap = client.post(
            "/api/v1/me/bootstrap",
            headers=ADMIN,
            json={"display_name": "Admin", "room_name": "Room A"},
        ).json()
        workspace_id = bootstrap["workspace"]["workspace_id"]
        artifact_a = client.post(
            f"/api/v1/rooms/{bootstrap['room']['room_id']}/artifacts",
            headers=ADMIN,
            json={"name": "Brief A", "artifact_type": "DOCUMENT", "content": "Content A"},
        ).json()["artifact_id"]

        # A second room in the same workspace, still admin-owned, gives a second
        # artifact whose share does not belong under artifact_a's path.
        room_b = client.post(
            f"/api/v1/workspaces/{workspace_id}/rooms", headers=ADMIN, json={"name": "Room B"}
        ).json()
        art_b = client.post(
            f"/api/v1/rooms/{room_b['room_id']}/artifacts",
            headers=ADMIN,
            json={"name": "Brief B", "artifact_type": "DOCUMENT", "content": "Content B"},
        ).json()
        share_b = client.post(
            f"/api/v1/artifacts/{art_b['artifact_id']}/shares", headers=ADMIN
        ).json()

        cross_room = client.delete(
            f"/api/v1/artifacts/{artifact_a}/shares/{share_b['share_id']}", headers=ADMIN
        )
        assert cross_room.status_code == 404
