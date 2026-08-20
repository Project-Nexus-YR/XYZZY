"""Authentication and deterministic authorization primitives."""

from .auth import AuthenticatedUser, AuthenticationError, TokenAuthenticator
from .authorization import AuthorizationError, RoomCapability, RoomPolicy

__all__ = [
    "AuthenticatedUser",
    "AuthenticationError",
    "AuthorizationError",
    "RoomCapability",
    "RoomPolicy",
    "TokenAuthenticator",
]
