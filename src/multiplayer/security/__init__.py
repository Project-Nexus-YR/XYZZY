"""Authentication and deterministic authorization primitives."""

from .auth import (
    AuthenticatedUser,
    AuthenticationError,
    TokenAuthenticator,
    hash_token,
    ingest_bootstrap_tokens,
)
from .authorization import AuthorizationError, RoomCapability, RoomPolicy
from .capabilities import allowed_tools

__all__ = [
    "AuthenticatedUser",
    "AuthenticationError",
    "AuthorizationError",
    "allowed_tools",
    "hash_token",
    "ingest_bootstrap_tokens",
    "RoomCapability",
    "RoomPolicy",
    "TokenAuthenticator",
]
