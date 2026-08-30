"""A file a member uploads is scoped and gated exactly like everything else in
the room it landed in: MUTATE to upload, the same room's READ to download, the
uploader and the room to bind, and a serving path that never trusts the
content type the uploader claimed.
"""

import os

from fastapi.testclient import TestClient

from multiplayer.server import create_app

OWNER_HEADERS = {"Authorization": "Bearer owner-token"}
OUTSIDER_HEADERS = {"Authorization": "Bearer outsider-token"}


def _app() -> object:
    return create_app(
        ":memory:",
        auth_tokens={"owner-token": "user-owner", "outsider-token": "user-outsider"},
    )


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


def test_upload_bind_and_download_round_trip() -> None:
    with TestClient(_app()) as client:
        room_id = _seed_room(client)
        uploaded = client.post(
            f"/api/v1/rooms/{room_id}/attachments",
            headers=OWNER_HEADERS,
            files={"file": ("photo.png", b"\x89PNG fake bytes", "image/png")},
        )
        assert uploaded.status_code == 200
        attachment = uploaded.json()
        assert attachment["filename"] == "photo.png"
        assert attachment["size_bytes"] == len(b"\x89PNG fake bytes")

        sent = client.post(
            f"/api/v1/rooms/{room_id}/messages",
            headers=OWNER_HEADERS,
            json={"content": "see attached", "attachment_ids": [attachment["attachment_id"]]},
        )
        assert sent.status_code == 200
        message = sent.json()
        assert message["attachments"] == [
            {
                "attachment_id": attachment["attachment_id"],
                "filename": "photo.png",
                "content_type": "image/png",
                "size_bytes": len(b"\x89PNG fake bytes"),
            }
        ]

        downloaded = client.get(
            f"/api/v1/attachments/{attachment['attachment_id']}", headers=OWNER_HEADERS
        )
        assert downloaded.status_code == 200
        assert downloaded.content == b"\x89PNG fake bytes"
        assert downloaded.headers["content-type"].startswith("image/png")
        assert "attachment;" in downloaded.headers["content-disposition"]
        assert downloaded.headers["x-content-type-options"] == "nosniff"


def test_a_non_member_cannot_upload_to_the_room() -> None:
    with TestClient(_app()) as client:
        room_id = _seed_room(client)
        response = client.post(
            f"/api/v1/rooms/{room_id}/attachments",
            headers=OUTSIDER_HEADERS,
            files={"file": ("photo.png", b"bytes", "image/png")},
        )
        assert response.status_code == 403


def test_a_non_member_cannot_download_from_the_room() -> None:
    with TestClient(_app()) as client:
        room_id = _seed_room(client)
        uploaded = client.post(
            f"/api/v1/rooms/{room_id}/attachments",
            headers=OWNER_HEADERS,
            files={"file": ("photo.png", b"bytes", "image/png")},
        ).json()
        response = client.get(
            f"/api/v1/attachments/{uploaded['attachment_id']}", headers=OUTSIDER_HEADERS
        )
        assert response.status_code == 403


def test_binding_refuses_an_attachment_uploaded_by_someone_else() -> None:
    with TestClient(_app()) as client:
        room_id = _seed_room(client)
        invite = client.post(
            f"/api/v1/rooms/{room_id}/members/invitations",
            headers=OWNER_HEADERS,
            json={"user_id": "user-outsider", "role": "editor"},
        )
        assert invite.status_code == 200
        uploaded = client.post(
            f"/api/v1/rooms/{room_id}/attachments",
            headers=OWNER_HEADERS,
            files={"file": ("photo.png", b"bytes", "image/png")},
        ).json()
        response = client.post(
            f"/api/v1/rooms/{room_id}/messages",
            headers=OUTSIDER_HEADERS,
            json={"content": "not mine", "attachment_ids": [uploaded["attachment_id"]]},
        )
        assert response.status_code == 400


def test_binding_refuses_an_attachment_from_a_different_room() -> None:
    with TestClient(_app()) as client:
        room_id = _seed_room(client)
        workspace_id = client.get(f"/api/v1/rooms/{room_id}", headers=OWNER_HEADERS).json()[
            "workspace_id"
        ]
        other_room = client.post(
            f"/api/v1/workspaces/{workspace_id}/rooms",
            headers=OWNER_HEADERS,
            json={"name": "Other Room"},
        ).json()
        uploaded = client.post(
            f"/api/v1/rooms/{room_id}/attachments",
            headers=OWNER_HEADERS,
            files={"file": ("photo.png", b"bytes", "image/png")},
        ).json()
        response = client.post(
            f"/api/v1/rooms/{other_room['room_id']}/messages",
            headers=OWNER_HEADERS,
            json={"content": "wrong room", "attachment_ids": [uploaded["attachment_id"]]},
        )
        assert response.status_code == 400


def test_an_oversized_upload_is_refused_with_413() -> None:
    os.environ["XYZZY_MAX_ATTACHMENT_BYTES"] = "10"
    try:
        with TestClient(_app()) as client:
            room_id = _seed_room(client)
            response = client.post(
                f"/api/v1/rooms/{room_id}/attachments",
                headers=OWNER_HEADERS,
                files={"file": ("photo.png", b"this payload is way over ten bytes", "image/png")},
            )
            assert response.status_code == 413
    finally:
        del os.environ["XYZZY_MAX_ATTACHMENT_BYTES"]


def test_svg_is_never_served_with_its_own_content_type() -> None:
    with TestClient(_app()) as client:
        room_id = _seed_room(client)
        uploaded = client.post(
            f"/api/v1/rooms/{room_id}/attachments",
            headers=OWNER_HEADERS,
            files={"file": ("evil.svg", b"<svg onload=alert(1)></svg>", "image/svg+xml")},
        ).json()
        downloaded = client.get(
            f"/api/v1/attachments/{uploaded['attachment_id']}", headers=OWNER_HEADERS
        )
        assert downloaded.status_code == 200
        assert downloaded.headers["content-type"].startswith("application/octet-stream")
