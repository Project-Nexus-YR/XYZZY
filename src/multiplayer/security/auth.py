"""Small, explicit authentication boundary for API and realtime connections."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass

from ..db.connection import Database, serialize_datetime
from ..domain.models import utcnow


class AuthenticationError(ValueError):
    """Raised when a request has no valid configured credential."""


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    user_id: str


def hash_token(token: str) -> str:
    """A credential at rest is its digest; the plaintext exists only in the caller's hands."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenAuthenticator:
    """Resolve opaque bearer tokens against the user_tokens table.

    Tokens are never interpreted as user identifiers. The caller cannot select an
    identity with a query parameter or request body; only a stored credential row
    can bind a token to a user. The row is read on every request, so a revocation
    takes effect on the very next call, without a restart.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    async def authenticate(self, authorization: str | None) -> AuthenticatedUser:
        scheme, separator, credential = (authorization or "").partition(" ")
        if not separator or scheme.lower() != "bearer" or not credential:
            raise AuthenticationError("valid bearer token required")

        # Equality runs on the digest of a high-entropy token, so a lookup miss
        # exposes no timing signal a full scan would have hidden.
        row = await self._db.fetch_one(
            "SELECT user_id FROM user_tokens WHERE token_hash = ? AND revoked_at IS NULL",
            (hash_token(credential),),
        )
        if row is None:
            raise AuthenticationError("valid bearer token required")
        return AuthenticatedUser(user_id=str(row["user_id"]))


async def ingest_bootstrap_tokens(db: Database, tokens: Mapping[str, str]) -> None:
    """Store configured bootstrap credentials without resurrecting revoked ones."""
    for token, user_id in tokens.items():
        await db.execute(
            "INSERT INTO user_tokens(token_hash, user_id, label, created_at) "
            "VALUES (?, ?, 'bootstrap', ?) ON CONFLICT(token_hash) DO NOTHING",
            (hash_token(str(token)), str(user_id), serialize_datetime(utcnow())),
        )
