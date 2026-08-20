"""Deterministic, deny-by-default room authorization policy."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class AuthorizationError(PermissionError):
    """Raised when an authenticated principal lacks a required capability."""


class RoomCapability(StrEnum):
    READ = "read"
    MUTATE = "mutate"
    ADMINISTER = "administer"


_ROLE_CAPABILITIES: dict[str, frozenset[RoomCapability]] = {
    "admin": frozenset(RoomCapability),
    "editor": frozenset({RoomCapability.READ, RoomCapability.MUTATE}),
    # Existing pre-auth records used "member"; it is equivalent to editor.
    "member": frozenset({RoomCapability.READ, RoomCapability.MUTATE}),
    "viewer": frozenset({RoomCapability.READ}),
}


class RoomPolicy:
    """Authorize effective room capabilities from durable membership only."""

    def __init__(self, repos: Any) -> None:
        self._repos = repos

    async def require(
        self,
        room_id: str,
        user_id: str,
        capability: RoomCapability,
    ) -> None:
        member = await self._repos.room_members.get(room_id, user_id)
        capabilities = _ROLE_CAPABILITIES.get(member.role, frozenset()) if member else frozenset()
        if capability not in capabilities:
            raise AuthorizationError("room access forbidden")

    async def require_workspace_member(self, workspace_id: str, user_id: str) -> None:
        if await self._repos.workspaces.get_member(workspace_id, user_id) is None:
            raise AuthorizationError("workspace access forbidden")

    async def require_org_member(self, org_id: str, user_id: str) -> None:
        if await self._repos.orgs.get_member(org_id, user_id) is None:
            raise AuthorizationError("organization access forbidden")
