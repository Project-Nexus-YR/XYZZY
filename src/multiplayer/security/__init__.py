"""Authentication and deterministic authorization primitives."""

from .auth import (
    SECURE_SESSION_COOKIE,
    SESSION_COOKIE,
    AuthenticatedUser,
    AuthenticationError,
    TokenAuthenticator,
    hash_token,
    ingest_bootstrap_tokens,
    session_cookie_name,
)
from .authorization import AuthorizationError, RoomCapability, RoomPolicy
from .capabilities import allowed_tools

__all__ = [
    "SECURE_SESSION_COOKIE",
    "SESSION_COOKIE",
    "AuthenticatedUser",
    "AuthenticationError",
    "AuthorizationError",
    "allowed_tools",
    "hash_token",
    "ingest_bootstrap_tokens",
    "RoomCapability",
    "RoomPolicy",
    "TokenAuthenticator",
    "session_cookie_name",
]
