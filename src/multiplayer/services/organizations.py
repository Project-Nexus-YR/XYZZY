"""Organizations and workspaces: creation, listing, and the demo bootstrap context."""

from __future__ import annotations

import asyncio
import logging

from ..domain.events import EventType, RoomEvent
from ..domain.models import (
    BootstrapContext,
    DomainError,
    Organization,
    OrgMember,
    ParticipantType,
    Room,
    RoomMember,
    User,
    Workspace,
    WorkspaceMember,
    new_id,
)
from ._shared import (
    _SharedMixin,
)

log = logging.getLogger(__name__)


class _OrganizationsMixin(_SharedMixin):
    """Mixin providing the organizations surface of MultiplayerService."""

    async def create_organization(self, name: str, slug: str, creator_id: str) -> Organization:
        name = self._validate_non_empty(name, "organization name")
        slug = self._validate_non_empty(slug, "organization slug")
        if slug.casefold().startswith("bootstrap-"):
            raise DomainError("organization slug namespace is reserved")
        org = Organization(org_id=new_id("org"), name=name, slug=slug)
        # One transaction, like create_room: a failure between the two writes
        # would otherwise leave a memberless org, invisible to list_for_user,
        # unadministrable, and holding its globally unique slug forever.
        async with self.db.transaction():
            await self.repos.orgs.create(org)
            await self.repos.orgs.add_member(
                OrgMember(org_id=org.org_id, user_id=creator_id, role="admin")
            )
        return org

    async def get_user_context(
        self, user_id: str
    ) -> tuple[list[Organization], list[Workspace], list[Room]]:
        """Discover durable collaboration boundaries visible to one principal.

        This read-only path is used by browser reconnect. Each query is anchored
        to its own membership table so stale browser identifiers cannot reveal or
        attach the principal to an unauthorized organization, workspace, or room.
        """
        organizations, workspaces, rooms = await asyncio.gather(
            self.repos.orgs.list_for_user(user_id),
            self.repos.workspaces.list_for_user(user_id),
            self.repos.rooms.list_for_user(user_id),
        )
        return organizations, workspaces, rooms

    async def bootstrap_user_workspace(
        self,
        user_id: str,
        display_name: str,
        room_name: str,
    ) -> tuple[Organization, Workspace, Room]:
        """Atomically get or create the principal's stable first workspace.

        Discovery remains a useful read optimization, but this transaction is
        the idempotency boundary. Concurrent browser tabs that both observed an
        empty context serialize here and resolve the same durable hierarchy.
        """
        user_id = self._validate_id(user_id, "user id")
        display_name = self._validate_non_empty(display_name, "display name")
        room_name = self._validate_non_empty(room_name, "room name")
        created_event: RoomEvent | None = None

        async with self.db.transaction():
            # The typed display name is only ever known here; a session-authenticated
            # principal has no other path that records it. Heal it in on every
            # bootstrap call, fresh or idempotent, without touching an existing row.
            if await self.repos.users.get(user_id) is None:
                await self.repos.users.create(
                    User(
                        user_id=user_id,
                        display_name=display_name,
                        # No email is known at bootstrap; users.email is UNIQUE, so a
                        # per-user placeholder keeps two bootstraps from colliding.
                        email=f"{user_id}@bootstrap.local",
                    )
                )

            bootstrap = await self.repos.bootstrap_contexts.get(user_id)
            if bootstrap is not None:
                organization = await self.repos.orgs.get(bootstrap.org_id)
                workspace = await self.repos.workspaces.get(bootstrap.workspace_id)
                room = await self.repos.rooms.get(bootstrap.room_id)
                org_member = await self.repos.orgs.get_member(bootstrap.org_id, user_id)
                workspace_member = await self.repos.workspaces.get_member(
                    bootstrap.workspace_id, user_id
                )
                room_member = await self.repos.room_members.get(bootstrap.room_id, user_id)
                valid = (
                    organization is not None
                    and workspace is not None
                    and room is not None
                    and workspace.org_id == bootstrap.org_id
                    and room.workspace_id == bootstrap.workspace_id
                    and org_member is not None
                    and org_member.role == "admin"
                    and workspace_member is not None
                    and workspace_member.role == "admin"
                    and room_member is not None
                    and room_member.role == "admin"
                )
                if not valid:
                    raise DomainError("bootstrap context failed ownership validation")
                assert organization is not None
                assert workspace is not None
                assert room is not None
                return organization, workspace, room

            org_id = new_id("org")
            organization = Organization(
                org_id=org_id,
                name=f"{display_name}'s workspace",
                slug=f"bootstrap-{org_id}",
            )
            await self.repos.orgs.create(organization)
            await self.repos.orgs.add_member(
                OrgMember(org_id=org_id, user_id=user_id, role="admin")
            )

            workspace = Workspace(
                workspace_id=new_id("ws"),
                org_id=org_id,
                name="Main",
                slug="main",
            )
            await self.repos.workspaces.create(workspace)
            await self.repos.workspaces.add_member(
                WorkspaceMember(
                    workspace_id=workspace.workspace_id,
                    user_id=user_id,
                    role="admin",
                )
            )

            room = Room(
                room_id=new_id("room"),
                workspace_id=workspace.workspace_id,
                name=room_name,
                created_by=user_id,
            )
            await self.repos.rooms.create(room)
            await self.repos.room_members.add(
                RoomMember(room_id=room.room_id, user_id=user_id, role="admin")
            )
            await self._issue_handle(room.room_id, ParticipantType.USER, user_id, user_id)
            created_event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room.room_id,
                    sequence=0,
                    event_type=EventType.ROOM_CREATED,
                    payload={"name": room_name, "description": ""},
                    actor_id=user_id,
                    actor_type="user",
                )
            )
            await self.repos.bootstrap_contexts.create(
                BootstrapContext(
                    user_id=user_id,
                    org_id=organization.org_id,
                    workspace_id=workspace.workspace_id,
                    room_id=room.room_id,
                )
            )

        if created_event is not None:
            await self._broadcast_persisted_events([created_event])
        return organization, workspace, room

    async def create_workspace(
        self, org_id: str, name: str, slug: str, creator_id: str
    ) -> Workspace:
        name = self._validate_non_empty(name, "workspace name")
        slug = self._validate_non_empty(slug, "workspace slug")
        ws = Workspace(workspace_id=new_id("ws"), org_id=org_id, name=name, slug=slug)
        # Same guarantee as create_organization, for the same reason: a memberless
        # workspace is invisible, unadministrable, and undeletable through the API.
        async with self.db.transaction():
            await self.repos.workspaces.create(ws)
            await self.repos.workspaces.add_member(
                WorkspaceMember(workspace_id=ws.workspace_id, user_id=creator_id, role="admin")
            )
        return ws

    async def list_workspaces(self, org_id: str) -> list[Workspace]:
        return await self.repos.workspaces.list_by_org(org_id)
