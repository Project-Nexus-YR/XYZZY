"""A credential is a row: hashed at rest, revocable without a restart.

Authentication reads user_tokens on every call, so these tests pin the
properties the environment blob could not offer: the plaintext token is never
stored, a revocation takes effect on the next request, and re-ingesting the
bootstrap configuration does not resurrect a credential an operator revoked.
"""

import pytest

from multiplayer import manage
from multiplayer.db.connection import Database
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.auth import (
    AuthenticationError,
    TokenAuthenticator,
    hash_token,
    ingest_bootstrap_tokens,
)
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
