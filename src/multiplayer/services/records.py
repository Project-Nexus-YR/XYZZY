"""Room records: tasks, artifacts and their shares, decisions, memories, and notifications."""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import replace

from ..domain.events import EventType, RoomEvent
from ..domain.models import (
    Artifact,
    ArtifactShare,
    ArtifactType,
    ArtifactVersion,
    Decision,
    DecisionStatus,
    DomainError,
    Memory,
    MemoryScope,
    Notification,
    Task,
    TaskPriority,
    TaskStatus,
    new_id,
)
from ..domain.synthesis import (
    RESERVED_ARTIFACT_NAMES,
)
from ..security.authorization import (
    RoomCapability,
)
from ..security.capabilities import (
    RunAuthorization,
)
from ._shared import (
    VALID_DECISION_TRANSITIONS,
    VALID_TASK_TRANSITIONS,
    _SharedMixin,
    _validate_transition,
)

log = logging.getLogger(__name__)


class _RecordsMixin(_SharedMixin):
    """Mixin providing the records surface of MultiplayerService."""

    async def create_task(
        self,
        room_id: str,
        title: str,
        description: str = "",
        priority: TaskPriority = TaskPriority.NORMAL,
        created_by: str = "",
        parent_task_id: str | None = None,
        *,
        require_member: bool = False,
        authorization: RunAuthorization | None = None,
    ) -> Task:
        title = self._validate_non_empty(title, "task title")
        task = Task(
            task_id=new_id("task"),
            room_id=room_id,
            title=title,
            description=description,
            priority=priority,
            created_by=created_by,
            parent_task_id=parent_task_id,
        )
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(room_id, created_by)
            if authorization is not None:
                await self._require_run_authority_in_transaction(authorization, "task.create")
            await self.repos.tasks.create(task)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.TASK_CREATED,
                    payload={"task_id": task.task_id, "title": title},
                    actor_id=created_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return task

    async def assign_task(
        self, task_id: str, agent_id: str, *, requested_by: str = "", require_member: bool = False
    ) -> Task:
        async with self.db.transaction():
            task = await self.repos.tasks.get(task_id)
            if not task:
                raise DomainError(f"task not found: {task_id}")
            if require_member:
                await self._require_mutate_in_transaction(task.room_id, requested_by)
            _validate_transition(task.status, TaskStatus.ASSIGNED, VALID_TASK_TRANSITIONS, "task")
            task = Task(
                task_id=task.task_id,
                room_id=task.room_id,
                title=task.title,
                description=task.description,
                status=TaskStatus.ASSIGNED,
                priority=task.priority,
                assigned_agent_id=agent_id,
                created_by=task.created_by,
                parent_task_id=task.parent_task_id,
                delegation_id=task.delegation_id,
            )
            await self.repos.tasks.update(task)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=task.room_id,
                    sequence=0,
                    event_type=EventType.TASK_ASSIGNED,
                    payload={"task_id": task_id, "agent_id": agent_id},
                    actor_id=agent_id,
                    actor_type="agent",
                )
            )
        await self._broadcast_persisted_events([event])
        return task

    async def delegate_task(
        self,
        task_id: str,
        from_agent_id: str,
        to_agent_id: str,
        description: str = "",
        *,
        requested_by: str = "",
        require_member: bool = False,
    ) -> Task:
        async with self.db.transaction():
            task = await self.repos.tasks.get(task_id)
            if not task:
                raise DomainError(f"task not found: {task_id}")
            if require_member:
                await self._require_mutate_in_transaction(task.room_id, requested_by)
            delegation_id = new_id("deleg")
            child = Task(
                task_id=new_id("task"),
                room_id=task.room_id,
                title=f"Delegated: {task.title}",
                description=description or task.description,
                status=TaskStatus.ASSIGNED,
                priority=task.priority,
                assigned_agent_id=to_agent_id,
                created_by=from_agent_id,
                parent_task_id=task_id,
                delegation_id=delegation_id,
            )
            await self.repos.tasks.create(child)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=task.room_id,
                    sequence=0,
                    event_type=EventType.TASK_DELEGATED,
                    payload={
                        "parent_task_id": task_id,
                        "child_task_id": child.task_id,
                        "from_agent": from_agent_id,
                        "to_agent": to_agent_id,
                    },
                    actor_id=from_agent_id,
                    actor_type="agent",
                )
            )
        await self._broadcast_persisted_events([event])
        return child

    async def complete_task(
        self, task_id: str, *, requested_by: str = "", require_member: bool = False
    ) -> Task:
        async with self.db.transaction():
            task = await self.repos.tasks.get(task_id)
            if not task:
                raise DomainError(f"task not found: {task_id}")
            if require_member:
                await self._require_mutate_in_transaction(task.room_id, requested_by)
            _validate_transition(task.status, TaskStatus.COMPLETED, VALID_TASK_TRANSITIONS, "task")
            task = Task(
                task_id=task.task_id,
                room_id=task.room_id,
                title=task.title,
                description=task.description,
                status=TaskStatus.COMPLETED,
                priority=task.priority,
                assigned_agent_id=task.assigned_agent_id,
                created_by=task.created_by,
                parent_task_id=task.parent_task_id,
                delegation_id=task.delegation_id,
            )
            await self.repos.tasks.update(task)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=task.room_id,
                    sequence=0,
                    event_type=EventType.TASK_COMPLETED,
                    payload={"task_id": task_id},
                    actor_id=task.assigned_agent_id or "system",
                    actor_type="agent",
                )
            )
        await self._broadcast_persisted_events([event])
        return task

    async def cancel_task(
        self, task_id: str, *, requested_by: str = "", require_member: bool = False
    ) -> Task:
        async with self.db.transaction():
            task = await self.repos.tasks.get(task_id)
            if not task:
                raise DomainError(f"task not found: {task_id}")
            if require_member:
                await self._require_mutate_in_transaction(task.room_id, requested_by)
            _validate_transition(task.status, TaskStatus.CANCELLED, VALID_TASK_TRANSITIONS, "task")
            task = Task(
                task_id=task.task_id,
                room_id=task.room_id,
                title=task.title,
                description=task.description,
                status=TaskStatus.CANCELLED,
                priority=task.priority,
                assigned_agent_id=task.assigned_agent_id,
                created_by=task.created_by,
                parent_task_id=task.parent_task_id,
                delegation_id=task.delegation_id,
            )
            await self.repos.tasks.update(task)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=task.room_id,
                    sequence=0,
                    event_type=EventType.TASK_CANCELLED,
                    payload={"task_id": task_id},
                    actor_id=task.created_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return task

    async def list_room_tasks(self, room_id: str, *, limit: int | None = None) -> list[Task]:
        return await self.repos.tasks.list_by_room(room_id, limit=limit)

    async def _is_published_synthesis(self, artifact_id: str) -> bool:
        """True when any version of this artifact was published by a branch synthesis."""
        versions = await self.repos.artifacts.list_versions(artifact_id)
        return any(version.branch_synthesis_id for version in versions)

    async def create_artifact(
        self,
        room_id: str,
        name: str,
        artifact_type: ArtifactType,
        description: str = "",
        created_by: str = "",
        content: str = "",
        *,
        require_member: bool = False,
        authorization: RunAuthorization | None = None,
    ) -> Artifact:
        name = self._validate_non_empty(name, "artifact name")
        if name in RESERVED_ARTIFACT_NAMES:
            raise DomainError(f"{name!r} names a published synthesis and cannot be created by hand")
        artifact = Artifact(
            artifact_id=new_id("art"),
            room_id=room_id,
            name=name,
            artifact_type=artifact_type,
            description=description,
            current_version=1 if content else 0,
            created_by=created_by,
        )
        version: ArtifactVersion | None = None
        if content:
            version = ArtifactVersion(
                version_id=new_id("ver"),
                artifact_id=artifact.artifact_id,
                version_number=1,
                content=content,
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
                created_by=created_by,
            )
            version = replace(
                version,
                provenance_hash=self._artifact_provenance_hash(version, []),
            )
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(room_id, created_by)
            if authorization is not None:
                await self._require_run_authority_in_transaction(authorization, "artifact.write")
            await self.repos.artifacts.create(artifact)
            if version is not None:
                await self.repos.artifacts.create_version_in_transaction(version)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.ARTIFACT_CREATED,
                    payload={
                        "artifact_id": artifact.artifact_id,
                        "name": name,
                        "type": artifact_type.value,
                    },
                    actor_id=created_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return artifact

    async def update_artifact(
        self, artifact_id: str, content: str, updated_by: str = "", *, require_member: bool = False
    ) -> ArtifactVersion:
        artifact = await self.repos.artifacts.get(artifact_id)
        if not artifact:
            raise DomainError(f"artifact not found: {artifact_id}")
        if await self._is_published_synthesis(artifact_id):
            # Every version of a published synthesis carries the outputs it came from.
            # Appending hand-written text here would be indistinguishable from one.
            raise DomainError(
                "a published synthesis is extended by publishing a synthesis, "
                "not by writing a version"
            )
        new_ver = artifact.current_version + 1
        version = ArtifactVersion(
            version_id=new_id("ver"),
            artifact_id=artifact_id,
            version_number=new_ver,
            content=content,
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            created_by=updated_by,
        )
        version = replace(
            version,
            provenance_hash=self._artifact_provenance_hash(version, []),
        )
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(artifact.room_id, updated_by)
            await self.repos.artifacts.create_version_in_transaction(version)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=artifact.room_id,
                    sequence=0,
                    event_type=EventType.ARTIFACT_VERSION_CREATED,
                    payload={"artifact_id": artifact_id, "version": new_ver},
                    actor_id=updated_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return version

    async def create_artifact_share(
        self, artifact_id: str, created_by: str
    ) -> tuple[ArtifactShare, str]:
        """Mint a public read-only link for an artifact's latest published content.

        Sharing outward is a governance act, not authorship, so it is gated on
        room ADMINISTER rather than the MUTATE that writing a version needs. The
        bearer token is returned here and nowhere else; only its hash is stored.
        """
        artifact = await self.repos.artifacts.get(artifact_id)
        if artifact is None:
            raise DomainError(f"artifact not found: {artifact_id}")
        token = secrets.token_urlsafe(32)
        share = ArtifactShare(
            share_id=new_id("share"),
            artifact_id=artifact_id,
            room_id=artifact.room_id,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            created_by=created_by,
        )
        async with self.db.transaction():
            # Re-check ADMINISTER inside the write's own transaction: an admin
            # demoted after the route authorized them must not still be able to
            # open a door out of the room.
            await self._require_capability_in_transaction(
                artifact.room_id, created_by, RoomCapability.ADMINISTER
            )
            await self.repos.artifact_shares.create_in_transaction(share)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=artifact.room_id,
                    sequence=0,
                    event_type=EventType.ARTIFACT_SHARE_CREATED,
                    payload={"artifact_id": artifact_id, "share_id": share.share_id},
                    actor_id=created_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return share, token

    async def list_artifact_shares(self, artifact_id: str) -> list[ArtifactShare]:
        return await self.repos.artifact_shares.list_by_artifact(artifact_id)

    async def revoke_artifact_share(self, artifact_id: str, share_id: str, revoked_by: str) -> None:
        share = await self.repos.artifact_shares.get(share_id)
        if share is None or share.artifact_id != artifact_id:
            raise DomainError(f"artifact share not found: {share_id}")
        async with self.db.transaction():
            await self._require_capability_in_transaction(
                share.room_id, revoked_by, RoomCapability.ADMINISTER
            )
            revoked = await self.repos.artifact_shares.revoke_in_transaction(share_id)
            if revoked is None:
                raise DomainError(f"artifact share already revoked: {share_id}")
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=share.room_id,
                    sequence=0,
                    event_type=EventType.ARTIFACT_SHARE_REVOKED,
                    payload={"artifact_id": artifact_id, "share_id": share_id},
                    actor_id=revoked_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])

    async def resolve_public_share(self, token: str) -> tuple[Artifact, ArtifactVersion] | None:
        """The `/share/{token}` route's only lookup — unauthenticated, so this never
        raises: an unknown, malformed, or revoked token is the same None to the
        caller, which is what keeps the public 404 from becoming an oracle."""
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        share = await self.repos.artifact_shares.get_live_by_token_hash(token_hash)
        if share is None:
            return None
        artifact = await self.repos.artifacts.get(share.artifact_id)
        if artifact is None:
            return None
        versions = await self.repos.artifacts.list_versions(share.artifact_id)
        if not versions:
            return None
        return artifact, versions[0]

    async def create_decision(
        self,
        room_id: str,
        title: str,
        content: str,
        reason: str = "",
        created_by: str = "",
        *,
        require_member: bool = False,
    ) -> Decision:
        title = self._validate_non_empty(title, "decision title")
        decision = Decision(
            decision_id=new_id("dec"),
            room_id=room_id,
            title=title,
            content=content,
            reason=reason,
            created_by=created_by,
        )
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(room_id, created_by)
            await self.repos.decisions.create(decision)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.DECISION_CREATED,
                    payload={"decision_id": decision.decision_id, "title": title},
                    actor_id=created_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return decision

    async def update_decision_status(
        self,
        decision_id: str,
        status: DecisionStatus,
        *,
        reviewed_by: str = "",
        require_member: bool = False,
    ) -> Decision:
        """Move a decision between states, and say so in the room's order.

        Without this a decision could only ever be proposed, so the open list had
        nothing that could drain it and the made list could never match a row. The
        emitted event is the one the Decision invalidation class already listens
        for, so the assertion over this row stops reading as current the moment the
        row moves.
        """
        async with self.db.transaction():
            decision = await self.repos.decisions.get(decision_id)
            if decision is None:
                raise DomainError(f"decision not found: {decision_id}")
            if require_member:
                await self._require_mutate_in_transaction(decision.room_id, reviewed_by)
            _validate_transition(decision.status, status, VALID_DECISION_TRANSITIONS, "decision")
            await self.repos.decisions.update_status(decision_id, status, reviewed_by)
            decision = replace(decision, status=status, reviewed_by=reviewed_by)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=decision.room_id,
                    sequence=0,
                    event_type=(
                        EventType.DECISION_SUPERSEDED
                        if status is DecisionStatus.SUPERSEDED
                        else EventType.DECISION_UPDATED
                    ),
                    payload={"decision_id": decision_id, "status": status.value},
                    actor_id=reviewed_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return decision

    async def list_room_decisions(
        self, room_id: str, *, limit: int | None = None
    ) -> list[Decision]:
        return await self.repos.decisions.list_by_room(room_id, limit=limit)

    async def create_memory(
        self,
        room_id: str | None,
        workspace_id: str | None,
        org_id: str | None,
        scope: MemoryScope,
        content: str,
        memory_type: str = "fact",
        created_by: str = "",
        *,
        require_member: bool = False,
    ) -> Memory:
        content = self._validate_non_empty(content, "memory content")
        memory = Memory(
            memory_id=new_id("mem"),
            room_id=room_id,
            workspace_id=workspace_id,
            org_id=org_id,
            scope=scope,
            content=content,
            memory_type=memory_type,
            created_by=created_by,
        )
        if room_id is None:
            await self.repos.memories.create(memory)
            return memory
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(room_id, created_by)
            await self.repos.memories.create(memory)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.MEMORY_CREATED,
                    payload={"memory_id": memory.memory_id, "type": memory_type},
                    actor_id=created_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([event])
        return memory

    async def list_room_memories(self, room_id: str, *, limit: int | None = None) -> list[Memory]:
        return await self.repos.memories.list_by_room(room_id, limit=limit)

    async def create_notification(
        self,
        user_id: str,
        title: str,
        body: str,
        room_id: str | None = None,
        notification_type: str = "info",
    ) -> Notification:
        notif = Notification(
            notification_id=new_id("notif"),
            user_id=user_id,
            room_id=room_id,
            title=title,
            body=body,
            notification_type=notification_type,
        )
        await self.repos.notifications.create(notif)
        return notif

    async def list_notifications(
        self, user_id: str, *, limit: int | None = None
    ) -> list[Notification]:
        return await self.repos.notifications.list_unread(user_id, limit=limit)
