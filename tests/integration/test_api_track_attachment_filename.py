"""An attachment named with non-latin-1 characters (CJK, an emoji) must upload
and then download without ever raising inside the route. Finding 10: the
route's Content-Disposition header used to interpolate the raw filename,
which Starlette encodes as latin-1, so a non-latin-1 name 500ed every
download of that attachment, permanently.
"""

from urllib.parse import quote

from fastapi.testclient import TestClient

from multiplayer.server import create_app

OWNER_HEADERS = {"Authorization": "Bearer owner-token"}


def _app() -> object:
    return create_app(
        ":memory:",
        auth_tokens={"owner-token": "user-owner"},
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


def test_download_of_a_non_latin1_filename_returns_200_with_rfc6266_header() -> None:
    filename = "café-你好-😀.pdf"
    with TestClient(_app()) as client:
        room_id = _seed_room(client)
        uploaded = client.post(
            f"/api/v1/rooms/{room_id}/attachments",
            headers=OWNER_HEADERS,
            files={"file": (filename, b"pdf bytes", "application/pdf")},
        )
        assert uploaded.status_code == 200
        attachment_id = uploaded.json()["attachment_id"]

        downloaded = client.get(f"/api/v1/attachments/{attachment_id}", headers=OWNER_HEADERS)

        assert downloaded.status_code == 200
        assert downloaded.content == b"pdf bytes"
        disposition = downloaded.headers["content-disposition"]
        assert disposition.startswith('attachment; filename="')
        assert f"filename*=UTF-8''{quote(filename)}" in disposition
