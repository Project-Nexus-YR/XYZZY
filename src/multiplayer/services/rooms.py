"""Rooms and membership: creation, joining, roles, policy, and posture."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from ..domain.events import EventType, RoomEvent
from ..domain.models import (
    AddressingMode,
    AgentTemplate,
    DomainError,
    Notification,
    ParticipantType,
    Room,
    RoomMember,
    RoomStatus,
    RoomTemplate,
    WorkspaceMember,
    new_id,
)
from ..harness import (
    NEXUS_HARNESS_ID,
)
from ..security.authorization import (
    AuthorizationError,
    RoomCapability,
)
from ..security.boundary import require_human_boundary
from ..security.capabilities import (
    Posture,
)
from ..security.screening import fenced, screen
from ._shared import (
    _policy_json,
    _SharedMixin,
)

log = logging.getLogger(__name__)


class _RoomsMixin(_SharedMixin):
    """Mixin providing the rooms surface of MultiplayerService."""

    async def create_room(
        self,
        workspace_id: str,
        name: str,
        creator_id: str,
        description: str = "",
        room_template_id: str | None = None,
    ) -> Room:
        name = self._validate_non_empty(name, "room name")
        room = Room(
            room_id=new_id("room"),
            workspace_id=workspace_id,
            name=name,
            description=description,
            created_by=creator_id,
        )
        room_template: RoomTemplate | None = None
        if room_template_id is not None:
            room_template = await self.repos.room_templates.get(room_template_id)
            if room_template is None or room_template.deleted_at is not None:
                raise DomainError(f"room template not found: {room_template_id}")
            if room_template.workspace_id != workspace_id:
                raise DomainError(f"room template not found in workspace: {room_template_id}")
        async with self.db.transaction():
            # Serializing the duplicate check and the insert turns a concurrent
            # duplicate create into a clean rejection rather than two identical
            # sidebar entries.
            existing = await self.repos.rooms.list_by_workspace(workspace_id)
            if any(
                r.status != RoomStatus.ARCHIVED and r.name.casefold() == name.casefold()
                for r in existing
            ):
                raise DomainError("a channel with that name already exists")
            # A recipe is read once, at save time, and again here, fresh: a
            # specialist it named can have been deleted or unshared since. This
            # room must not exist half-populated, so the whole create is refused
            # before a single row is written.
            spawn_templates: list[AgentTemplate] = []
            if room_template is not None:
                for agent_template_id in room_template.agent_template_ids:
                    agent_template = await self.repos.agents.get_template(agent_template_id)
                    if agent_template is None or not await self._agent_template_usable_in_workspace(
                        agent_template, workspace_id
                    ):
                        raise DomainError(
                            "room template names a specialist no longer available: "
                            f"{agent_template_id}"
                        )
                    spawn_templates.append(agent_template)
            await self.repos.rooms.create(room)
            await self.repos.room_members.add(
                RoomMember(room_id=room.room_id, user_id=creator_id, role="admin")
            )
            await self._issue_handle(room.room_id, ParticipantType.USER, creator_id, creator_id)
            payload: dict[str, Any] = {"name": name, "description": description}
            if room_template_id is not None:
                payload["room_template_id"] = room_template_id
            events = [
                await self.repos.events.append_with_next_sequence_in_transaction(
                    RoomEvent(
                        room_id=room.room_id,
                        sequence=0,
                        event_type=EventType.ROOM_CREATED,
                        payload=payload,
                        actor_id=creator_id,
                        actor_type="user",
                    )
                )
            ]
            # The room row and every preselected specialist commit or roll back
            # together: writing the spawns here, inside this same transaction,
            # rather than as separate spawn_agent calls after commit, closes the
            # 19th appearance of the check-then-use class — a template deleted or
            # unshared in the gap between commit and a later spawn call would
            # otherwise leave a committed room half-populated.
            for agent_template in spawn_templates:
                if agent_template.workspace_id is not None:
                    agent_template_prompt = fenced(
                        screen(agent_template.system_prompt, "agent template")
                    )
                else:
                    agent_template_prompt = agent_template.system_prompt
                # Template spawns carry no caller-declared model identity, so
                # resolution can never refuse here; it still runs so the row
                # stores the configured identity, same as any direct spawn.
                resolved_provider, resolved_model = self._resolve_model_identity("", "")
                _agent, agent_events = await self._spawn_agent_writes_in_transaction(
                    room.room_id,
                    agent_template,
                    agent_template_prompt,
                    None,
                    None,
                    resolved_provider,
                    resolved_model,
                    creator_id,
                    NEXUS_HARNESS_ID,
                    AddressingMode.ANYONE,
                    room,
                )
                events.extend(agent_events)
        await self._broadcast_persisted_events(events)
        return room

    async def list_rooms(self, workspace_id: str) -> list[Room]:
        return await self.repos.rooms.list_by_workspace(workspace_id)

    async def _is_known_user(self, user_id: str) -> bool:
        """Invitations name accounts: a configured principal or a bootstrapped user row."""
        if user_id in self.known_users:
            return True
        return await self.repos.users.get(user_id) is not None

    async def join_room(self, room_id: str, user_id: str) -> None:
        """Mark an already invited member present; never create membership."""
        await self.authorization.require(room_id, user_id, RoomCapability.READ)
        await self.presence.user_joined(user_id, room_id)
        await self._append_room_event(
            room_id, EventType.USER_JOINED_ROOM, {"user_id": user_id}, user_id, "user"
        )

    async def invite_room_member(
        self,
        room_id: str,
        invited_user_id: str,
        role: str,
        invited_by: str,
    ) -> RoomMember:
        require_human_boundary("member.invite")
        if role not in {"viewer", "editor"}:
            raise DomainError("invitation role must be viewer or editor")
        invited_user_id = self._validate_id(invited_user_id, "user id")
        member = RoomMember(room_id=room_id, user_id=invited_user_id, role=role)
        async with self.db.transaction():
            # Serializing the read and the insert turns a concurrent duplicate invite
            # into a clean rejection rather than a UNIQUE-constraint failure, and the
            # recheck fences out an inviter demoted after the route authorized them.
            await self._require_mutate_in_transaction(room_id, invited_by)
            if not await self._is_known_user(invited_user_id):
                raise DomainError("no account with that user id")
            if await self.repos.room_members.get(room_id, invited_user_id) is not None:
                raise DomainError("user is already a channel member")
            room_for_membership = await self.repos.rooms.get(room_id)
            await self.repos.room_members.add(member)
            if room_for_membership is not None:
                # Mirror bootstrap: a room member without workspace membership gets
                # 403 "workspace access forbidden" on every workspace-scoped route.
                # Never overwrite an existing row/role - this only fills a gap.
                await self.repos.workspaces.add_member_if_absent(
                    WorkspaceMember(
                        workspace_id=room_for_membership.workspace_id,
                        user_id=invited_user_id,
                        role="member",
                    )
                )
            await self._issue_handle(
                room_id, ParticipantType.USER, invited_user_id, invited_user_id
            )
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.USER_INVITED_ROOM,
                    payload={"user_id": invited_user_id, "role": role},
                    actor_id=invited_by,
                    actor_type="user",
                )
            )
            # The durable half of telling them: a live socket message reaches
            # only whoever is connected this instant, and an invitation is
            # exactly the message someone offline must still find later.
            room_name = room_for_membership.name if room_for_membership else room_id
            inviter_names = await self.repos.room_members.display_names(room_id)
            await self.repos.notifications.create(
                Notification(
                    notification_id=new_id("notif"),
                    user_id=invited_user_id,
                    room_id=room_id,
                    title=f"You were invited to #{room_name}",
                    body=f"Invited as {role} by {inviter_names.get(invited_by, invited_by)}",
                    notification_type="invitation",
                )
            )
        await self._broadcast_persisted_events([event])
        # The invitee is not subscribed to this room yet; tell their open sockets
        # directly so the channel appears in their sidebar without a reload.
        room = await self.repos.rooms.get(room_id)
        await self.hub.send_to_user(
            invited_user_id,
            {
                "type": "room_invited",
                "room_id": room_id,
                "room_name": room.name if room else room_id,
                "role": role,
            },
        )
        return member

    async def leave_room(self, room_id: str, user_id: str) -> None:
        """Give up membership durably: the row and the event commit together."""
        require_human_boundary("member.leave")
        async with self.db.transaction():
            member = await self.repos.room_members.get(room_id, user_id)
            if member is None:
                raise DomainError("user is not a channel member")
            if member.role == "admin":
                others = [
                    other
                    for other in await self.repos.room_members.list(room_id)
                    if other.user_id != user_id
                ]
                if others and not any(other.role == "admin" for other in others):
                    raise DomainError("the last admin cannot leave while others remain")
            await self.repos.room_members.remove(room_id, user_id)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.USER_LEFT_ROOM,
                    payload={"user_id": user_id, "role": member.role},
                    actor_id=user_id,
                    actor_type="user",
                )
            )
        await self.hub.revoke_room_access(user_id, room_id)
        await self.presence.user_left(user_id, room_id)
        await self._broadcast_persisted_events([event])

    async def get_room_members(self, room_id: str) -> list[RoomMember]:
        return await self.repos.room_members.list(room_id)

    async def update_room_member_role(
        self, room_id: str, user_id: str, role: str, changed_by: str
    ) -> RoomMember:
        """Change another member's access, including promoting to or demoting from admin.

        Demoting the room's last admin is refused - the channel must always keep one,
        the same invariant leave_room enforces. Changing your own membership is still
        leave_room's job, not this route's.
        """
        require_human_boundary("member.role")
        if role not in {"viewer", "editor", "admin"}:
            raise DomainError("member role must be viewer, editor, or admin")
        if user_id == changed_by:
            raise DomainError("use leave to change your own membership")
        async with self.db.transaction():
            # Re-read the changer's authority inside BEGIN IMMEDIATE, like every
            # other ADMINISTER write here: an admin removed after the route
            # authorized them must not still hand out admin.
            await self._require_capability_in_transaction(
                room_id, changed_by, RoomCapability.ADMINISTER
            )
            member = await self.repos.room_members.get(room_id, user_id)
            if member is None:
                raise DomainError("user is not a channel member")
            if member.role == role:
                return member
            if member.role == "admin" and role != "admin":
                others = await self.repos.room_members.list(room_id)
                if not any(o.user_id != user_id and o.role == "admin" for o in others):
                    raise DomainError("cannot demote the last admin of the room")
            await self.repos.room_members.update_role(room_id, user_id, role)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.USER_ROLE_CHANGED,
                    payload={"user_id": user_id, "role": role, "previous_role": member.role},
                    actor_id=changed_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return replace(member, role=role)

    async def set_room_policy(
        self, room_id: str, allowed: list[str] | None, changed_by: str
    ) -> None:
        """Bound every run in this channel to a capability list. None lifts the bound."""
        require_human_boundary("room.policy")
        stored = _policy_json(allowed)
        async with self.db.transaction():
            await self._require_capability_in_transaction(
                room_id, changed_by, RoomCapability.ADMINISTER
            )
            await self.repos.rooms.set_allowed_capabilities(room_id, stored)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.ROOM_POLICY_UPDATED,
                    payload={"allowed_capabilities": allowed},
                    actor_id=changed_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])

    async def declare_room_posture(self, room_id: str, posture: Posture, declared_by: str) -> str:
        """Say how much of this channel's work stops at a human. Never what it may do.

        Administering the channel, because raising the bar and lowering it are the
        same act seen from two sides and both are governance: the check is on the
        write, so a posture cannot be reached through any door that is not this one.
        require_human_boundary is that sentence for the agent surface.

        Loosening is permitted, and the reason is that a posture which only rises
        makes one mistaken STRICT permanent and the channel disposable; the harm a
        one-way rule would prevent does not exist here, because the posture is read
        once, when a call is decided, so loosening cannot reach a call already parked
        at a reviewer — that call is released by the reviewer or by nobody.

        Nothing is overwritten. The declaration is a row, so what governed an action
        stays answerable from records that could not have changed since.
        """
        require_human_boundary("room.posture")
        async with self.db.transaction():
            await self._require_capability_in_transaction(
                room_id, declared_by, RoomCapability.ADMINISTER
            )
            declaration_id = await self.repos.room_postures.declare(room_id, posture, declared_by)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.ROOM_POSTURE_DECLARED,
                    payload={
                        "declaration_id": declaration_id,
                        "posture": posture.value,
                        "declared_by": declared_by,
                    },
                    actor_id=declared_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return declaration_id

    async def set_member_capabilities(
        self, room_id: str, user_id: str, allowed: list[str] | None, changed_by: str
    ) -> None:
        """Bound what one member may lend to the agents they run. None restores the role default."""
        require_human_boundary("member.capabilities")
        stored = _policy_json(allowed)
        async with self.db.transaction():
            await self._require_capability_in_transaction(
                room_id, changed_by, RoomCapability.ADMINISTER
            )
            member = await self.repos.room_members.get(room_id, user_id)
            if member is None:
                raise DomainError("user is not a channel member")
            await self.repos.room_members.set_allowed_capabilities(room_id, user_id, stored)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.ROOM_POLICY_UPDATED,
                    payload={"user_id": user_id, "allowed_capabilities": allowed},
                    actor_id=changed_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])

    async def set_workspace_policy(
        self, workspace_id: str, allowed: list[str] | None, changed_by: str
    ) -> None:
        """Bound every channel in the workspace. Logged in each of its rooms."""
        require_human_boundary("workspace.policy")
        stored = _policy_json(allowed)
        events: list[RoomEvent] = []
        async with self.db.transaction():
            # A workspace-wide bound outranks any single room's, so it demands
            # the workspace admin role, re-read inside the transaction that
            # writes - the same fence every room-tier governance write has.
            member = await self.repos.workspaces.get_member(workspace_id, changed_by)
            if member is None or member.role != "admin":
                raise AuthorizationError("workspace access forbidden")
            await self.repos.workspaces.set_allowed_capabilities(workspace_id, stored)
            for room in await self.repos.rooms.list_by_workspace(workspace_id):
                events.append(
                    await self.repos.events.append_with_next_sequence_in_transaction(
                        RoomEvent(
                            room_id=room.room_id,
                            sequence=0,
                            event_type=EventType.WORKSPACE_POLICY_UPDATED,
                            payload={
                                "workspace_id": workspace_id,
                                "allowed_capabilities": allowed,
                            },
                            actor_id=changed_by,
                            actor_type="user",
                        )
                    )
                )
        await self._broadcast_persisted_events(events)

    async def remove_room_member(self, room_id: str, user_id: str, removed_by: str) -> None:
        """Revoke a non-admin member's access, including any live realtime subscription."""
        require_human_boundary("member.remove")
        if user_id == removed_by:
            raise DomainError("use leave to remove yourself")
        async with self.db.transaction():
            # The route authorized ADMINISTER; a demotion committing in between
            # must not let a former admin's removal land. Same fence as invite.
            await self._require_capability_in_transaction(
                room_id, removed_by, RoomCapability.ADMINISTER
            )
            member = await self.repos.room_members.get(room_id, user_id)
            if member is None:
                raise DomainError("user is not a channel member")
            if member.role == "admin":
                raise DomainError("admin membership cannot be removed here")
            await self.repos.room_members.remove(room_id, user_id)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.USER_REMOVED_ROOM,
                    payload={"user_id": user_id, "role": member.role},
                    actor_id=removed_by,
                    actor_type="user",
                )
            )
        await self.hub.revoke_room_access(user_id, room_id)
        # Their subscriptions to this room are gone; reach their other open sockets.
        await self.hub.send_to_user(user_id, {"type": "room_removed", "room_id": room_id})
        await self.presence.user_left(user_id, room_id)
        await self._broadcast_persisted_events([event])
