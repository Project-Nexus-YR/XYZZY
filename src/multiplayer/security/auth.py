"""Small, explicit authentication boundary for API and realtime connections."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from ..db.connection import Database, serialize_datetime
from ..domain.models import UserSession, utcnow
from .authorization import AuthorizationError

# What the authenticator calls when a session is spent, wired in by the server.
SessionUsed = Callable[[UserSession, datetime], Awaitable[None]]

log = logging.getLogger(__name__)


class AuthenticationError(ValueError):
    """Raised when a request has no valid configured credential."""


# The browser session cookie carries the access token and nothing else — no
# refresh token ever reaches it. `__Host-` is used whenever the deployment is
# HTTPS: it binds the cookie to this exact host and path, which a plain name
# cannot. A plain-http local run falls back to the bare name, mirroring the
# existing login-binding cookie's own secure/insecure split.
SECURE_SESSION_COOKIE = "__Host-xyzzy_session"
SESSION_COOKIE = "xyzzy_session"


def session_cookie_name(secure: bool) -> str:
    return SECURE_SESSION_COOKIE if secure else SESSION_COOKIE


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    user_id: str
    # Present when the credential was minted for a sign-in rather than by an
    # operator. It is what lets "sign me out" end this session and no other.
    session_id: str | None = None


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

    def __init__(self, db: Database, on_session_used: SessionUsed | None = None) -> None:
        self._db = db
        # Set at wiring time rather than imported, because the session service
        # already depends on this module and the cycle would be the only reason
        # either of them knew about the other.
        self._on_session_used = on_session_used

    async def authenticate(
        self, authorization: str | None, *, extend_idle: bool = True
    ) -> AuthenticatedUser:
        scheme, separator, credential = (authorization or "").partition(" ")
        if not separator or scheme.lower() != "bearer" or not credential:
            raise AuthenticationError("valid bearer token required")

        # Equality runs on the digest of a high-entropy token, so a lookup miss
        # exposes no timing signal a full scan would have hidden. The session is
        # read in the same statement: a credential and the session it belongs to
        # cannot be judged separately without inventing a moment where one is
        # valid and the other is not.
        row = await self._db.fetch_one(
            "SELECT t.user_id, t.session_id, t.expires_at AS credential_expires_at, "
            "s.idle_expires_at, s.absolute_expires_at, "
            "s.revoked_at AS session_revoked_at, s.created_at AS session_created_at, "
            "s.issuer, s.subject, s.idp_session_id "
            "FROM user_tokens t LEFT JOIN user_sessions s ON s.session_id = t.session_id "
            "WHERE t.token_hash = ? AND t.revoked_at IS NULL",
            (hash_token(credential),),
        )
        if row is None:
            raise AuthenticationError("valid bearer token required")

        # A credential can expire on its own, and that is the half revocation
        # cannot cover: revoking helps whoever knows to revoke, while a stolen
        # credential nobody has noticed is bounded only by its own lifetime.
        credential_expiry = row["credential_expires_at"]
        if credential_expiry is not None and utcnow() >= datetime.fromisoformat(credential_expiry):
            raise AuthenticationError("valid bearer token required")

        session_id = row["session_id"]
        if session_id is None:
            # An operator-minted credential. It answers to revocation and to
            # nothing else, exactly as it did before sessions existed.
            return AuthenticatedUser(user_id=str(row["user_id"]))

        if row["idle_expires_at"] is None:
            # The credential names a session that is not there. Refusing is the
            # only safe reading: the row that would say whether it is still alive
            # is the row that is missing.
            raise AuthenticationError("valid bearer token required")

        session = UserSession(
            session_id=str(session_id),
            user_id=str(row["user_id"]),
            issuer=str(row["issuer"]),
            subject=str(row["subject"]),
            idp_session_id=row["idp_session_id"],
            created_at=datetime.fromisoformat(row["session_created_at"]),
            idle_expires_at=datetime.fromisoformat(row["idle_expires_at"]),
            absolute_expires_at=datetime.fromisoformat(row["absolute_expires_at"]),
            revoked_at=(
                datetime.fromisoformat(row["session_revoked_at"])
                if row["session_revoked_at"]
                else None
            ),
        )
        if not session.alive_at(utcnow()):
            raise AuthenticationError("valid bearer token required")
        if extend_idle and self._on_session_used is not None:
            try:
                await self._on_session_used(session, utcnow())
            except AuthorizationError:
                # Extending a session is fenced outside the agent surface, and a
                # model-driven turn holding a human's credential trips it. That
                # must not become an authentication failure: the credential is
                # valid, the clock simply does not move. Letting the refusal
                # escape here answered 403 to a request that was authenticated.
                log.info("Idle clock not extended: session %s", session.session_id)
        return AuthenticatedUser(user_id=session.user_id, session_id=session.session_id)


async def ingest_bootstrap_tokens(db: Database, tokens: Mapping[str, str]) -> None:
    """Store configured bootstrap credentials without resurrecting revoked ones."""
    for token, user_id in tokens.items():
        await db.execute(
            "INSERT INTO user_tokens(token_hash, user_id, label, created_at) "
            "VALUES (?, ?, 'bootstrap', ?) ON CONFLICT(token_hash) DO NOTHING",
            (hash_token(str(token)), str(user_id), serialize_datetime(utcnow())),
        )
