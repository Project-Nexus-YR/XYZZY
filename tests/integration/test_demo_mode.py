"""The solo on-ramp (§D): XYZZY_DEMO / --demo seeds one realistic offline scene
into an empty database, refuses to run beside a real deployment's identity
configuration, and never reseeds a database that already has a workspace.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from multiplayer.server import create_app

DEMO = {"Authorization": "Bearer demo"}


def test_demo_is_off_by_default() -> None:
    app = create_app(":memory:", auth_tokens={"owner-token": "user_1"})
    with TestClient(app) as client:
        assert client.get("/api/v1/auth/config").json()["demo"] is False


def test_demo_mode_seeds_a_realistic_scene_with_no_api_key() -> None:
    app = create_app(":memory:", demo=True)
    with TestClient(app) as client:
        config = client.get("/api/v1/auth/config").json()
        assert config["demo"] is True

        context = client.get("/api/v1/me/context", headers=DEMO).json()
        assert context["user_id"] == "user_demo"
        assert len(context["rooms"]) == 1
        room_id = context["rooms"][0]["room_id"]

        messages = client.get(f"/api/v1/rooms/{room_id}/messages", headers=DEMO).json()
        assert len(messages) >= 8
        senders = {m["sender_id"] for m in messages}
        assert {"user_demo", "user_demo_second", "user_demo_third"} <= senders
        # The scene must not read as a fixture written in one instant: message
        # timestamps are spread back across a plausible stretch of conversation.
        minutes = {m["created_at"][:16] for m in messages}
        assert len(minutes) >= 5

        branches = client.get(f"/api/v1/rooms/{room_id}/branches", headers=DEMO).json()
        assert len(branches) == 1
        assert branches[0]["status"] == "COMPLETED"

        artifacts = client.get(f"/api/v1/rooms/{room_id}/artifacts", headers=DEMO).json()
        brief = next(a for a in artifacts if a["name"] == "Decision Brief")
        assert brief["version"] >= 1


def test_demo_seed_does_not_repeat_on_a_second_startup(tmp_path) -> None:
    db_path = str(tmp_path / "demo.db")
    with TestClient(create_app(db_path, demo=True)):
        pass
    with TestClient(create_app(db_path, demo=True)):
        pass
    conn = sqlite3.connect(db_path)
    try:
        (org_count,) = conn.execute("SELECT COUNT(*) FROM organizations").fetchone()
        (message_count,) = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
    finally:
        conn.close()
    assert org_count == 1
    assert message_count > 0  # the seeded scene exists...
    # ...and was written exactly once, not doubled by the second startup.
    assert message_count < 20


def test_demo_refused_when_oidc_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XYZZY_OIDC_ISSUER", "https://idp.example")
    monkeypatch.setenv("XYZZY_OIDC_CLIENT_ID", "client")
    monkeypatch.setenv("XYZZY_OIDC_REDIRECT_URI", "https://xyzzy.example/callback")
    with pytest.raises(RuntimeError, match="XYZZY_DEMO"):
        create_app(":memory:", demo=True)


def test_demo_refused_when_auth_tokens_are_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XYZZY_AUTH_TOKENS", '{"real-token": "real_user"}')
    with pytest.raises(RuntimeError, match="XYZZY_DEMO"):
        create_app(":memory:", demo=True)


def test_demo_env_var_turns_on_the_same_path_as_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XYZZY_DEMO", "1")
    app = create_app(":memory:")
    with TestClient(app) as client:
        assert client.get("/api/v1/auth/config").json()["demo"] is True
        assert client.get("/api/v1/me/context", headers=DEMO).status_code == 200
