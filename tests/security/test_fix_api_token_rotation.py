"""Finding 11: XYZZY_AUTH_TOKENS ingestion was insert-only, so removing or
replacing a token in the map never retired it. `ingest_bootstrap_tokens` now
revokes every bootstrap-labelled token absent from the current call's map, on
every start, which is what lets rotation actually retire a token rather than
merely stop minting new sessions with it.
"""

from __future__ import annotations

import pytest

from multiplayer.db.connection import Database
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.auth import (
    AuthenticationError,
    TokenAuthenticator,
    ingest_bootstrap_tokens,
)
from multiplayer.services.service import MultiplayerService


async def _migrated_db() -> Database:
    db = Database(":memory:")
    await db.connect()
    await MultiplayerService(db, RealtimeHub(), known_users=frozenset()).initialize()
    return db


async def test_a_token_dropped_from_the_map_is_revoked_on_the_next_start():
    """Fails before the fix: token-A kept authenticating after a "rotation" to
    {token-B} that never named it again — ingestion was insert-only.
    """
    db = await _migrated_db()
    try:
        await ingest_bootstrap_tokens(db, {"token-A": "user_1"})
        authenticator = TokenAuthenticator(db)
        assert (await authenticator.authenticate("Bearer token-A")).user_id == "user_1"

        await ingest_bootstrap_tokens(db, {"token-B": "user_1"})
        with pytest.raises(AuthenticationError):
            await authenticator.authenticate("Bearer token-A")
        assert (await authenticator.authenticate("Bearer token-B")).user_id == "user_1"
    finally:
        await db.close()


async def test_a_token_still_in_the_map_survives_reingestion():
    db = await _migrated_db()
    try:
        await ingest_bootstrap_tokens(db, {"token-A": "user_1", "token-B": "user_2"})
        await ingest_bootstrap_tokens(db, {"token-A": "user_1", "token-B": "user_2"})
        authenticator = TokenAuthenticator(db)
        assert (await authenticator.authenticate("Bearer token-A")).user_id == "user_1"
        assert (await authenticator.authenticate("Bearer token-B")).user_id == "user_2"
    finally:
        await db.close()


async def test_an_operator_revoked_bootstrap_token_is_not_resurrected_by_rotation():
    """The pre-existing guarantee (round 1) must still hold once retirement is
    added: revoking by hand, then reconfiguring with a DIFFERENT map that no
    longer names the revoked token, must not bring it back.
    """
    from multiplayer import manage

    db = await _migrated_db()
    try:
        await ingest_bootstrap_tokens(db, {"token-A": "user_1"})
        assert await manage.revoke_token(db, "token-A") is True
        await ingest_bootstrap_tokens(db, {"token-B": "user_1"})
        with pytest.raises(AuthenticationError):
            await TokenAuthenticator(db).authenticate("Bearer token-A")
    finally:
        await db.close()


async def test_a_minted_non_bootstrap_token_is_never_touched_by_retirement():
    """Retirement scopes to label='bootstrap' only: a token minted through the
    CLI/session flow for a real user must survive a bootstrap-map rotation
    that says nothing about it at all.
    """
    from multiplayer import manage

    db = await _migrated_db()
    try:
        await manage.add_user(db, "alice", "alice@example.com", None)
        minted = await manage.mint_token(db, "alice", "laptop")
        await ingest_bootstrap_tokens(db, {"token-A": "user_1"})
        await ingest_bootstrap_tokens(db, {"token-B": "user_1"})
        principal = await TokenAuthenticator(db).authenticate(f"Bearer {minted}")
        assert principal.user_id == "alice"
    finally:
        await db.close()


async def test_an_empty_map_retires_every_bootstrap_token():
    db = await _migrated_db()
    try:
        await ingest_bootstrap_tokens(db, {"token-A": "user_1"})
        await ingest_bootstrap_tokens(db, {})
        with pytest.raises(AuthenticationError):
            await TokenAuthenticator(db).authenticate("Bearer token-A")
    finally:
        await db.close()
