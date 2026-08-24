"""A credential is a row: hashed at rest, revocable without a restart.

Authentication reads user_tokens on every call, so these tests pin the
properties the environment blob could not offer: the plaintext token is never
stored, a revocation takes effect on the next request, and re-ingesting the
bootstrap configuration does not resurrect a credential an operator revoked.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import multiplayer.realtime.websocket as websocket_module
from multiplayer import manage
from multiplayer.db.connection import Database
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.auth import (
    AuthenticationError,
    TokenAuthenticator,
    hash_token,
    ingest_bootstrap_tokens,
)
from multiplayer.server import create_app
from multiplayer.services.service import MultiplayerService


async def _migrated_db() -> Database:
    db = Database(":memory:")
    await db.connect()
    await MultiplayerService(db, RealtimeHub(), known_users=frozenset()).initialize()
    return db


async def test_bootstrap_token_authenticates_and_only_its_hash_is_stored():
    db = await _migrated_db()
    try:
        await ingest_bootstrap_tokens(db, {"owner-token": "user_1"})
        principal = await TokenAuthenticator(db).authenticate("Bearer owner-token")
        assert principal.user_id == "user_1"
        rows = await db.fetch_all("SELECT token_hash FROM user_tokens")
        assert [row["token_hash"] for row in rows] == [hash_token("owner-token")]
    finally:
        await db.close()


async def test_unknown_and_malformed_credentials_are_refused():
    db = await _migrated_db()
    try:
        authenticator = TokenAuthenticator(db)
        for authorization in (None, "", "Bearer", "Bearer wrong", "Basic owner-token"):
            with pytest.raises(AuthenticationError):
                await authenticator.authenticate(authorization)
    finally:
        await db.close()


async def test_revocation_takes_effect_on_the_next_call():
    db = await _migrated_db()
    try:
        await manage.add_user(db, "alice", "alice@example.com", None)
        token = await manage.mint_token(db, "alice", "laptop")
        authenticator = TokenAuthenticator(db)
        principal = await authenticator.authenticate(f"Bearer {token}")
        assert principal.user_id == "alice"

        assert await manage.revoke_token(db, token) is True
        with pytest.raises(AuthenticationError):
            await authenticator.authenticate(f"Bearer {token}")
    finally:
        await db.close()


async def test_reingestion_does_not_resurrect_a_revoked_bootstrap_token():
    db = await _migrated_db()
    try:
        await ingest_bootstrap_tokens(db, {"owner-token": "user_1"})
        assert await manage.revoke_token(db, "owner-token") is True
        await ingest_bootstrap_tokens(db, {"owner-token": "user_1"})
        with pytest.raises(AuthenticationError):
            await TokenAuthenticator(db).authenticate("Bearer owner-token")
    finally:
        await db.close()


async def test_revoking_by_hash_works_when_the_token_is_lost():
    db = await _migrated_db()
    try:
        await manage.add_user(db, "bob", "bob@example.com", None)
        token = await manage.mint_token(db, "bob", None)
        assert await manage.revoke_token(db, hash_token(token)) is True
        with pytest.raises(AuthenticationError):
            await TokenAuthenticator(db).authenticate(f"Bearer {token}")
    finally:
        await db.close()


async def test_minting_requires_an_existing_user():
    db = await _migrated_db()
    try:
        with pytest.raises(ValueError, match="does not exist"):
            await manage.mint_token(db, "nobody", None)
    finally:
        await db.close()


def test_a_revoked_credential_closes_an_already_open_socket(tmp_path, monkeypatch):
    """An operator revokes from another process; the live socket must notice."""
    monkeypatch.setattr(websocket_module, "REAUTH_SECONDS", 0.05)
    db_path = tmp_path / "ws.db"
    app = create_app(str(db_path), auth_tokens={"live-token": "user_1"})
    headers = {"Authorization": "Bearer live-token"}
    with TestClient(app) as client:
        bootstrap = client.post(
            "/api/v1/me/bootstrap",
            headers=headers,
            json={"display_name": "Owner", "room_name": "Ops"},
        )
        assert bootstrap.status_code == 200, bootstrap.text
        room_id = bootstrap.json()["room"]["room_id"]

        with client.websocket_connect(f"/ws?room_id={room_id}", headers=headers) as ws:
            assert ws.receive_json()["type"] == "connected"

            # Out-of-band revocation, exactly as `manage token revoke` does it.
            out_of_band = sqlite3.connect(db_path)
            try:
                out_of_band.execute(
                    "UPDATE user_tokens SET revoked_at = '2026-01-01T00:00:00+00:00'"
                )
                out_of_band.commit()
            finally:
                out_of_band.close()

            with pytest.raises(WebSocketDisconnect) as disconnect:
                for _ in range(100):
                    ws.receive_json()
            assert disconnect.value.code == 4401
