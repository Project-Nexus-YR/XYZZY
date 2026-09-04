"""Finding 22: claim_authorization compares the browser binding exactly.

``claim_authorization`` used to fail open on an empty stored
``browser_binding_hash``, treating it as a wildcard that matched ANY caller,
including one that also sent no binding at all. Unreachable today because
``begin_login`` is the only writer and it always hashes a real binding, but a
future writer (a migration default, a fixture, a second entry point) that
ever left the column empty would silently disable the one check that stops a
stranger holding a state value from consuming somebody else's in-flight
login. This proves the comparison is exact rather than a wildcard.

An empty stored binding matched by an empty caller binding still succeeds
under an exact comparison: sessions.py hashing a missing cookie unconditionally
(so it can never coincide with an unset column) is the other half of the
finding's suggested fix, and it lives outside this track's owned files.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from multiplayer.db.connection import Database
from multiplayer.db.repositories import UserSessionRepo
from multiplayer.domain.models import OidcAuthorization, utcnow


@pytest.fixture
async def repo():
    db = Database(":memory:")
    await db.connect()
    await db.execute(
        "CREATE TABLE oidc_authorizations("
        "state TEXT PRIMARY KEY, nonce TEXT, code_verifier TEXT, "
        "browser_binding_hash TEXT, created_at TEXT, expires_at TEXT, consumed_at TEXT)"
    )
    await db.commit()
    yield UserSessionRepo(db)
    await db.close()


@pytest.mark.asyncio
async def test_an_empty_stored_binding_is_not_a_wildcard(repo: UserSessionRepo) -> None:
    moment = utcnow()
    await repo.start_authorization(
        OidcAuthorization(
            state="state-1",
            nonce="nonce-1",
            code_verifier="verifier-1",
            browser_binding_hash="",
            created_at=moment,
            expires_at=moment + timedelta(minutes=5),
        )
    )

    # A caller who did not open this attempt, holding an arbitrary binding,
    # must be refused against a row whose stored binding is empty: the empty
    # column is a value to match exactly, never a caller nobody has to match.
    claimed = await repo.claim_authorization("state-1", utcnow(), "some-other-hash")
    assert claimed is None
