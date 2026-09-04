"""Agents: templates, spawning, identity, addressing, and removal from a room."""

from __future__ import annotations

import logging
from dataclasses import replace

from ..domain.events import EventType, RoomEvent
from ..domain.models import (
    TERMINAL_EXECUTION_STATUSES,
    AddressingMode,
    AgentAddressing,
    AgentIdentity,
    AgentInstance,
    AgentRoomMembership,
    AgentRun,
    AgentStatus,
    AgentTemplate,
    DomainError,
    ExecutionStatus,
    HarnessState,
    ParticipantType,
    ProofMode,
    Room,
    RoomTemplate,
    RunSettlement,
    new_id,
    utcnow,
)
from ..harness import (
    KNOWN_HARNESS_IDS,
    NEXUS_HARNESS_ID,
    NexusLaunch,
    SessionHandle,
)
from ..security.authorization import (
    AuthorizationError,
    RoomCapability,
)
from ..security.boundary import require_human_boundary
from ..security.capabilities import (
    CAPABILITIES,
)
from ..security.screening import fenced, screen
from ._shared import (
    VALID_AGENT_TRANSITIONS,
    _SharedMixin,
    _validate_transition,
)

log = logging.getLogger(__name__)


class _AgentsMixin(_SharedMixin):
    """Mixin providing the agents surface of MultiplayerService."""

    async def list_agent_templates(self) -> list[AgentTemplate]:
        return await self.repos.agents.list_templates()

    async def list_workspace_agent_templates(self, workspace_id: str) -> list[AgentTemplate]:
        """Built-ins plus this workspace's own live templates."""
        return await self.repos.agents.list_visible_to_workspace(workspace_id)

    async def _is_shared_into(self, template: AgentTemplate, target_workspace_id: str) -> bool:
        """Whether a shared template is currently spawnable from another workspace.

        Re-read fresh at every call site, never cached: unsetting shared_at must
        revoke spawnability from outside the origin workspace immediately.
        """
        if template.shared_at is None or template.workspace_id is None:
            return False
        target = await self.repos.workspaces.get(target_workspace_id)
        origin = await self.repos.workspaces.get(template.workspace_id)
        return target is not None and origin is not None and target.org_id == origin.org_id

    async def _agent_template_usable_in_workspace(
        self, template: AgentTemplate, workspace_id: str
    ) -> bool:
        """A built-in, this workspace's own live template, or one shared into it."""
        if template.deleted_at is not None:
            return False
        if template.workspace_id is None or template.workspace_id == workspace_id:
            return True
        return await self._is_shared_into(template, workspace_id)

    async def create_agent_template(
        self, workspace_id: str, name: str, role: str, system_prompt: str, created_by: str
    ) -> AgentTemplate:
        """A workspace-authored specialist. Its prompt is member text, not developer text."""
        require_human_boundary("agent_template.create")
        name = self._validate_non_empty(name, "template name")
        role = self._validate_non_empty(role, "template role")
        system_prompt = self._validate_non_empty(system_prompt, "template system_prompt")
        template = AgentTemplate(
            template_id=new_id("tmpl"),
            name=name,
            description="",
            role=role,
            system_prompt=system_prompt,
            # The creation body names no capabilities (spec: {name, role, system_prompt}),
            # and the five-way intersection in _lendable_terms bounds a run by the
            # narrowest of user/agent/skill/channel/workspace — an empty skill term
            # here would make every agent spawned from this template unusable by
            # anyone, forever. The built-ins each carry a real, non-empty subset for
            # the same reason; this grants the full set and lets the other four terms
            # do the actual narrowing, same as an "admin"/"editor" room role does.
            capabilities=CAPABILITIES,
            workspace_id=workspace_id,
            created_by=created_by,
        )
        async with self.db.transaction():
            # The route already confirmed membership; re-read it here so a removal
            # committing in between cannot let a former member's write land.
            await self.authorization.require_workspace_member(workspace_id, created_by)
            existing = await self.repos.agents.list_visible_to_workspace(workspace_id)
            if any(t.name.casefold() == name.casefold() for t in existing):
                raise DomainError(f"a template named {name!r} already exists in this workspace")
            await self.repos.agents.create_template(template)
        return template

    async def delete_agent_template(
        self, workspace_id: str, template_id: str, requested_by: str
    ) -> None:
        require_human_boundary("agent_template.delete")
        async with self.db.transaction():
            template = await self.repos.agents.get_template(template_id)
            if template is None:
                raise DomainError(f"agent template not found: {template_id}")
            if template.workspace_id is None:
                raise DomainError("built-in agent templates cannot be deleted")
            if template.workspace_id != workspace_id:
                raise DomainError(f"agent template not found in workspace: {template_id}")
            member = await self.repos.workspaces.get_member(workspace_id, requested_by)
            is_admin = member is not None and member.role == "admin"
            if not is_admin and requested_by != template.created_by:
                raise AuthorizationError("workspace access forbidden")
            # Agents already spawned from this template copied its fields onto
            # themselves at spawn time, so marking it deleted rather than removing
            # the row breaks nothing they still read, and keeps the FK
            # agent_instances.template_id holds against this row intact.
            await self.repos.agents.soft_delete_template(template_id, utcnow())

    async def list_org_shared_agent_templates(self, workspace_id: str) -> list[AgentTemplate]:
        """Live templates other workspaces in this workspace's organization shared."""
        workspace = await self.repos.workspaces.get(workspace_id)
        if workspace is None:
            raise DomainError(f"workspace not found: {workspace_id}")
        return await self.repos.agents.list_shared_for_org(workspace.org_id, workspace_id)

    async def share_agent_template(
        self, workspace_id: str, template_id: str, requested_by: str
    ) -> AgentTemplate:
        """Distribution/trust machinery beyond the organization stays parked (spec §G):
        this only flips org-wide visibility on, owned and retractable by this workspace.
        """
        require_human_boundary("agent_template.share")
        async with self.db.transaction():
            template = await self.repos.agents.get_template(template_id)
            if template is None:
                raise DomainError(f"agent template not found: {template_id}")
            if template.workspace_id is None:
                raise DomainError("built-in agent templates are already global")
            if template.workspace_id != workspace_id:
                raise DomainError(f"agent template not found in workspace: {template_id}")
            if template.deleted_at is not None:
                raise DomainError(f"agent template was deleted: {template_id}")
            member = await self.repos.workspaces.get_member(workspace_id, requested_by)
            is_admin = member is not None and member.role == "admin"
            if not is_admin and requested_by != template.created_by:
                raise AuthorizationError("workspace access forbidden")
            shared_at = utcnow()
            await self.repos.agents.share_template(template_id, shared_at)
        return replace(template, shared_at=shared_at)

    async def unshare_agent_template(
        self, workspace_id: str, template_id: str, requested_by: str
    ) -> AgentTemplate:
        require_human_boundary("agent_template.unshare")
        async with self.db.transaction():
            template = await self.repos.agents.get_template(template_id)
            if template is None:
                raise DomainError(f"agent template not found: {template_id}")
            if template.workspace_id != workspace_id:
                raise DomainError(f"agent template not found in workspace: {template_id}")
            member = await self.repos.workspaces.get_member(workspace_id, requested_by)
            is_admin = member is not None and member.role == "admin"
            if not is_admin and requested_by != template.created_by:
                raise AuthorizationError("workspace access forbidden")
            await self.repos.agents.unshare_template(template_id)
        return replace(template, shared_at=None)

    async def create_room_template(
        self,
        workspace_id: str,
        name: str,
        description: str,
        agent_template_ids: list[str],
        created_by: str,
    ) -> RoomTemplate:
        """A workspace's saved room recipe. Every specialist it names must be one
        this workspace could spawn right now, or the recipe would make a promise
        room creation could not keep."""
        require_human_boundary("room_template.create")
        name = self._validate_non_empty(name, "room template name")
        template = RoomTemplate(
            template_id=new_id("rtmpl"),
            workspace_id=workspace_id,
            name=name,
            description=description,
            agent_template_ids=tuple(agent_template_ids),
            created_by=created_by,
        )
        async with self.db.transaction():
            await self.authorization.require_workspace_member(workspace_id, created_by)
            existing = await self.repos.room_templates.list_live_by_workspace(workspace_id)
            if any(t.name.casefold() == name.casefold() for t in existing):
                raise DomainError(
                    f"a room template named {name!r} already exists in this workspace"
                )
            for agent_template_id in agent_template_ids:
                agent_template = await self.repos.agents.get_template(agent_template_id)
                if agent_template is None or not await self._agent_template_usable_in_workspace(
                    agent_template, workspace_id
                ):
                    raise DomainError(
                        f"agent template not spawnable in this workspace: {agent_template_id}"
                    )
            await self.repos.room_templates.create(template)
        return template

    async def list_room_templates(self, workspace_id: str) -> list[RoomTemplate]:
        return await self.repos.room_templates.list_live_by_workspace(workspace_id)

    async def delete_room_template(
        self, workspace_id: str, template_id: str, requested_by: str
    ) -> None:
        require_human_boundary("room_template.delete")
        async with self.db.transaction():
            template = await self.repos.room_templates.get(template_id)
            if template is None or template.deleted_at is not None:
                raise DomainError(f"room template not found: {template_id}")
            if template.workspace_id != workspace_id:
                raise DomainError(f"room template not found in workspace: {template_id}")
            member = await self.repos.workspaces.get_member(workspace_id, requested_by)
            is_admin = member is not None and member.role == "admin"
            if not is_admin and requested_by != template.created_by:
                raise AuthorizationError("workspace access forbidden")
            await self.repos.room_templates.soft_delete(template_id, utcnow())

    def _resolve_model_identity(self, model_provider: str, model_name: str) -> tuple[str, str]:
        """Refuse configuration a spawn cannot honor; fill in what it means when unset.

        A non-empty ``model_provider``/``model_name`` that disagrees with the
        provider this process actually runs would let the API accept
        configuration it silently ignores, and the mismatch would then read
        back from the agent row as if it had been honored. Empty stays
        allowed and means "the configured provider" - which is what actually
        runs - and is stored as such so the row describes itself.
        """
        configured_provider, configured_model = self.nexus.provider_identity
        if model_provider and model_provider != configured_provider:
            raise DomainError(
                f"model provider {model_provider!r} was requested but this deployment "
                f"runs {configured_provider!r}"
            )
        if model_name and model_name != configured_model:
            raise DomainError(
                f"model {model_name!r} was requested but this deployment runs {configured_model!r}"
            )
        return model_provider or configured_provider, model_name or configured_model

    async def _spawn_agent_writes_in_transaction(
        self,
        room_id: str,
        template: AgentTemplate,
        template_system_prompt: str,
        name: str | None,
        system_prompt: str | None,
        model_provider: str,
        model_name: str,
        requested_by: str,
        harness_id: str,
        addressing_mode: AddressingMode,
        room: Room | None,
    ) -> tuple[AgentInstance, list[RoomEvent]]:
        """The write phase of a spawn, assuming the caller already holds an open
        transaction and has already validated the template. Shared by spawn_agent's
        own transaction and by create_room's room-plus-recipe transaction, so a
        room created from a template either commits with every specialist or not
        at all — never half-populated.
        """
        agent = AgentInstance(
            agent_id=new_id("agent"),
            template_id=template.template_id,
            room_id=room_id,
            name=name or template.name,
            role=template.role,
            system_prompt=system_prompt or template_system_prompt,
            capabilities=template.capabilities,
            model_provider=model_provider,
            model_name=model_name,
            harness_id=harness_id,
        )
        identity = AgentIdentity(
            identity_id=new_id("ident"),
            agent_id=agent.agent_id,
            proof_mode=ProofMode.IN_PROCESS,
        )
        addressing = AgentAddressing(
            agent_id=agent.agent_id,
            room_id=room_id,
            mode=addressing_mode,
            owner_user_id=requested_by or (room.created_by if room is not None else ""),
            updated_by=requested_by or "system",
        )
        await self.repos.agents.create_instance(agent)
        await self.repos.agents.add_room_membership(
            AgentRoomMembership(agent_id=agent.agent_id, room_id=room_id)
        )
        await self.repos.agent_identities.create_in_transaction(identity)
        await self.repos.agent_addressing.upsert_in_transaction(addressing)
        handle = await self._issue_handle(
            room_id, ParticipantType.AGENT, agent.agent_id, agent.name
        )
        events = [
            await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.AGENT_JOINED_ROOM,
                    payload={
                        "agent_id": agent.agent_id,
                        "name": agent.name,
                        "handle": handle,
                        "role": agent.role,
                    },
                    actor_id=agent.agent_id,
                    actor_type="agent",
                )
            ),
            await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.AGENT_IDENTITY_REGISTERED,
                    payload={
                        "agent_id": agent.agent_id,
                        "identity_id": identity.identity_id,
                        "proof_mode": identity.proof_mode.value,
                        "harness_id": harness_id,
                    },
                    actor_id=agent.agent_id,
                    actor_type="agent",
                )
            ),
        ]
        return agent, events

    async def spawn_agent(
        self,
        room_id: str,
        template_id: str,
        name: str | None = None,
        system_prompt: str | None = None,
        model_provider: str = "",
        model_name: str = "",
        *,
        requested_by: str = "",
        require_member: bool = False,
        harness_id: str = NEXUS_HARNESS_ID,
        addressing_mode: AddressingMode = AddressingMode.ANYONE,
    ) -> AgentInstance:
        require_human_boundary("agent.spawn")
        model_provider, model_name = self._resolve_model_identity(model_provider, model_name)
        template = await self.repos.agents.get_template(template_id)
        if not template:
            raise DomainError(f"agent template not found: {template_id}")
        if template.deleted_at is not None:
            raise DomainError(f"agent template was deleted: {template_id}")
        if harness_id not in KNOWN_HARNESS_IDS:
            raise DomainError(f"no harness is registered as {harness_id!r}")
        room = await self.repos.rooms.get(room_id)
        cross_workspace = False
        if template.workspace_id is not None:
            if room is None:
                raise DomainError(f"agent template {template_id} belongs to a different workspace")
            cross_workspace = template.workspace_id != room.workspace_id
            if cross_workspace and not await self._is_shared_into(template, room.workspace_id):
                raise DomainError(f"agent template {template_id} belongs to a different workspace")
            # A workspace member wrote this prompt, not this deployment's developer.
            # It reaches the model exactly like any other member-authored text does,
            # whether the spawning room belongs to the authoring workspace or to
            # another workspace this template was shared into.
            template_system_prompt = fenced(screen(template.system_prompt, "agent template"))
        else:
            template_system_prompt = template.system_prompt
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(room_id, requested_by)
            if cross_workspace:
                # The check-then-use class this schema has relocated eighteen
                # times (033-040): re-read shared_at fresh, inside the
                # transaction that spawns, so an unshare committing in between
                # the check above and this write revokes spawnability in time.
                assert room is not None
                fresh_template = await self.repos.agents.get_template(template_id)
                if fresh_template is None or not await self._is_shared_into(
                    fresh_template, room.workspace_id
                ):
                    raise DomainError(
                        f"agent template {template_id} belongs to a different workspace"
                    )
            agent, events = await self._spawn_agent_writes_in_transaction(
                room_id,
                template,
                template_system_prompt,
                name,
                system_prompt,
                model_provider,
                model_name,
                requested_by,
                harness_id,
                addressing_mode,
                room,
            )
        await self._broadcast_persisted_events(events)
        return agent

    async def list_room_agents(self, room_id: str) -> list[AgentInstance]:
        return await self.repos.agents.list_instances_by_room(room_id)

    async def update_agent_status(self, agent_id: str, status: AgentStatus) -> None:
        agent = await self.get_agent(agent_id)
        _validate_transition(agent.status, status, VALID_AGENT_TRANSITIONS, "agent")
        await self.repos.agents.update_status(agent_id, status)
        await self._append_room_event(
            agent.room_id,
            EventType.AGENT_STATUS_CHANGED,
            {"agent_id": agent_id, "status": status.value},
            agent_id,
            "agent",
        )

    async def _resolve_nexus_launch(self, run_id: str) -> NexusLaunch:
        """The durable records a bridge run is opened from, read by run id."""
        run = await self.repos.agent_runs.get(run_id)
        # A run written before this envelope existed is addressed by its execution id.
        execution_id = run.execution_id if run is not None else run_id
        execution = await self.repos.executions.get(execution_id)
        if execution is None:
            raise DomainError(f"agent run {run_id} names no execution")
        session = await self.repos.sessions.get(execution.session_id)
        if session is None:
            raise DomainError("session not found")
        return NexusLaunch(await self.get_agent(execution.agent_id), session, execution)

    async def get_agent_identity(self, agent_id: str) -> AgentIdentity:
        identity = await self.repos.agent_identities.get_for_agent(agent_id)
        if identity is None:
            raise DomainError(f"agent identity not found: {agent_id}")
        return identity

    async def get_agent_addressing(self, agent_id: str) -> AgentAddressing:
        addressing = await self.repos.agent_addressing.get(agent_id)
        if addressing is None:
            raise DomainError(f"agent addressing not found: {agent_id}")
        return addressing

    async def set_agent_addressing(
        self,
        agent_id: str,
        mode: AddressingMode,
        updated_by: str,
        *,
        owner_user_id: str | None = None,
        allowlist: frozenset[str] = frozenset(),
        require_member: bool = False,
    ) -> AgentAddressing:
        """Who may point this agent. Room ADMINISTER, because it is a grant."""
        require_human_boundary("agent.addressing")
        agent = await self.get_agent(agent_id)
        current = await self.repos.agent_addressing.get(agent_id)
        addressing = AgentAddressing(
            agent_id=agent_id,
            room_id=agent.room_id,
            mode=mode,
            owner_user_id=owner_user_id
            or (current.owner_user_id if current is not None else updated_by),
            allowlist=allowlist,
            updated_by=updated_by,
        )
        async with self.db.transaction():
            if require_member:
                await self._require_capability_in_transaction(
                    agent.room_id, updated_by, RoomCapability.ADMINISTER
                )
            await self.repos.agent_addressing.upsert_in_transaction(addressing)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=agent.room_id,
                    sequence=0,
                    event_type=EventType.AGENT_ADDRESSING_UPDATED,
                    payload={
                        "agent_id": agent_id,
                        "mode": mode.value,
                        "owner_user_id": addressing.owner_user_id,
                        "allowlist": sorted(allowlist),
                    },
                    actor_id=updated_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return addressing

    async def revoke_agent_identity(
        self, agent_id: str, revoked_by: str, *, require_member: bool = False
    ) -> None:
        """Revoke once, not per run: no later run of this agent may launch."""
        require_human_boundary("agent.identity.revoke")
        agent = await self.get_agent(agent_id)
        if require_member:
            await self.authorization.require(agent.room_id, revoked_by, RoomCapability.ADMINISTER)
        if not await self.repos.agent_identities.revoke(agent_id, utcnow()):
            return
        await self._append_room_event(
            agent.room_id,
            EventType.AGENT_IDENTITY_REVOKED,
            {"agent_id": agent_id, "revoked_by": revoked_by},
            revoked_by,
            "user",
        )

    async def remove_agent_from_room(
        self, agent_id: str, room_id: str, removed_by: str, *, require_member: bool = False
    ) -> None:
        """Take an agent out of a room and settle everything it had in flight.

        Settlement is decided here and telling the harness is best-effort, so an
        in-flight turn can still land. What stops it writing is the settled-run refusal
        inside complete_execution, not the credential.
        """
        require_human_boundary("agent.remove")
        agent = await self.get_agent(agent_id)
        if agent.room_id != room_id:
            raise DomainError("agent is not in this room")
        events: list[RoomEvent] = []
        settled: list[AgentRun] = []
        async with self.db.transaction():
            if require_member:
                await self._require_capability_in_transaction(
                    room_id, removed_by, RoomCapability.ADMINISTER
                )
            await self.repos.agents.remove_room_membership_in_transaction(
                agent_id, room_id, utcnow()
            )
            # The handle is the address, so it goes back to the room with the
            # membership: a later @mention of a removed agent resolves to nobody
            # rather than opening a fresh run for it.
            await self.repos.handles.release_in_transaction(
                room_id, ParticipantType.AGENT, agent_id
            )
            for run in await self.repos.agent_runs.list_open_by_agent_room(agent_id, room_id):
                execution = await self.repos.executions.get(run.execution_id)
                if execution is None:
                    continue
                # Through CANCEL_REQUESTED to SETTLED: the settlement is decided before
                # the harness is told, and the record says so even if it is never told.
                await self.repos.agent_runs.advance(
                    run.run_id, HarnessState.CANCEL_REQUESTED, utcnow(), removed_by
                )
                if execution.status in TERMINAL_EXECUTION_STATUSES:
                    for event in await self.repos.agent_runs.settle_in_transaction(
                        run.execution_id, RunSettlement.AGENT_REMOVED, removed_by
                    ):
                        events.append(
                            await self.repos.events.append_with_next_sequence_in_transaction(event)
                        )
                    settled.append(run)
                    continue
                events.extend(
                    await self.repos.executions.terminalize_without_output_in_transaction(
                        execution,
                        ExecutionStatus.CANCELLED,
                        "agent removed from room",
                        [],
                        RunSettlement.AGENT_REMOVED,
                        removed_by,
                    )
                )
                settled.append(run)
            # These runs settle here rather than through _settle_run, so the turn any
            # of them was holding at a reviewer is released here too, in the same
            # transaction. Nothing prompts a settled run again.
            for run in settled:
                await self.repos.suspended_turns.discard(run.execution_id)
            events.append(
                await self.repos.events.append_with_next_sequence_in_transaction(
                    RoomEvent(
                        room_id=room_id,
                        sequence=0,
                        event_type=EventType.AGENT_LEFT_ROOM,
                        payload={
                            "agent_id": agent_id,
                            "removed_by": removed_by,
                            "settled_run_ids": [run.run_id for run in settled],
                        },
                        actor_id=removed_by,
                        actor_type="user",
                    )
                )
            )
        # The approvals those runs were holding at end with them. It happens outside
        # the transaction above because closing one is a transaction of its own, and
        # the alternative — leaving it — is the row that outlives what it gated.
        for run in settled:
            await self._expire_undecided_approvals(run.execution_id, "agent removed from room")
        await self._broadcast_persisted_events(events)
        harness = self._harness(agent.harness_id) if agent.harness_id in KNOWN_HARNESS_IDS else None
        for run in settled:
            if harness is None:
                continue
            try:
                await harness.session_cancel(
                    SessionHandle(run_id=run.run_id, harness_session_id=run.execution_id),
                    "agent removed from room",
                )
            except Exception:
                log.exception("Could not tell the harness that run %s was settled", run.run_id)

    async def rejoin_agent_to_room(
        self, agent_id: str, room_id: str, rejoined_by: str, *, require_member: bool = False
    ) -> AgentRoomMembership:
        """Put a removed agent back in a room, as a new membership beside the old one.

        Rejoining had no path at all: ``add_room_membership`` is INSERT OR IGNORE, so
        it silently no-opped against the removed row, and no verb reached it. The
        only thing that did work was reversing the removal in the database, which
        erased the departure — which is why the schema now refuses that and this
        writes a new row naming the departure it follows instead. The record shows
        the agent joined, left, and came back; nothing in it is overwritten.

        ADMINISTER, the same grant removal takes: putting an agent back in a channel
        is a membership change, and the removal it reverses was one.
        """
        require_human_boundary("agent.rejoin")
        agent = await self.get_agent(agent_id)
        if agent.room_id != room_id:
            raise DomainError("agent is not in this room")
        async with self.db.transaction():
            if require_member:
                await self._require_capability_in_transaction(
                    room_id, rejoined_by, RoomCapability.ADMINISTER
                )
            previous = await self.repos.agents.latest_membership(agent_id, room_id)
            if previous is None:
                raise DomainError(f"agent {agent_id} has never been a member of room {room_id}")
            if previous.removed_at is None:
                raise DomainError(f"agent {agent_id} is already in room {room_id}")
            membership = AgentRoomMembership(
                agent_id=agent_id,
                room_id=room_id,
                rejoined_from_membership_id=previous.membership_id,
            )
            await self.repos.agents.rejoin_room_membership_in_transaction(membership)
            # The handle went back to the room with the membership, so the returning
            # agent is addressed again rather than staying unmentionable.
            handle = await self._issue_handle(room_id, ParticipantType.AGENT, agent_id, agent.name)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.AGENT_REJOINED_ROOM,
                    payload={
                        "agent_id": agent_id,
                        "handle": handle,
                        "rejoined_by": rejoined_by,
                        "membership_id": membership.membership_id,
                        "rejoined_from_membership_id": previous.membership_id,
                        "left_at": previous.removed_at.isoformat(),
                    },
                    actor_id=rejoined_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return membership
