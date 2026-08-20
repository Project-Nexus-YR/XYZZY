"""Small, explicit authentication boundary for API and realtime connections."""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from dataclasses import dataclass


class AuthenticationError(ValueError):
    """Raised when a request has no valid configured credential."""


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    user_id: str


class TokenAuthenticator:
    """Resolve opaque bearer tokens through a server-owned allowlist.

    Tokens are never interpreted as user identifiers. The caller cannot select an
    identity with a query parameter or request body; only the configured mapping can
    bind a credential to a user.
    """

    def __init__(self, tokens: Mapping[str, str]) -> None:
        self._tokens = tuple((str(token), str(user_id)) for token, user_id in tokens.items())

    def authenticate(self, authorization: str | None) -> AuthenticatedUser:
        scheme, separator, credential = (authorization or "").partition(" ")
        if not separator or scheme.lower() != "bearer" or not credential:
            raise AuthenticationError("valid bearer token required")

        resolved_user: str | None = None
        # Compare every configured token to avoid exposing a useful early-exit timing signal.
        for configured_token, user_id in self._tokens:
            if hmac.compare_digest(credential, configured_token):
                resolved_user = user_id
        if resolved_user is None:
            raise AuthenticationError("valid bearer token required")
        return AuthenticatedUser(user_id=resolved_user)
