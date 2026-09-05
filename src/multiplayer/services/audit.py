"""Audit and export: the room's event log, its full state, and the audit export feed."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from ..domain.events import RoomEvent
from ..domain.models import (
    DomainError,
    Message,
    ParticipantType,
    ThreadSummary,
    TurnLockScopeType,
    utcnow,
)
from ..security.audit import verify_event_chain
from ._shared import (
    _ROOM_EVENTS_DEFAULT_LIMIT,
    _ROOM_EVENTS_MAX_LIMIT,
    _SharedMixin,
)

log = logging.getLogger(__name__)


class _AuditMixin(_SharedMixin):
    """Mixin providing the audit surface of MultiplayerService."""

    async def get_room_events(
        self, room_id: str, after_sequence: int = 0, limit: int = _ROOM_EVENTS_DEFAULT_LIMIT
    ) -> list[RoomEvent]:
        """Up to ``limit`` events past after_sequence, paging list_since itself.

        A reconnecting client asked for everything it missed, and a single
        list_since call silently truncates at its own page cap, the same defect
        class already fixed once for the audit export - but "everything" is not
        unbounded either: a room's history is bounded by practice, not by
        anything this method enforces, so a caller past the cap gets the cap,
        never the whole table built in memory first and trimmed after.
        """
        capped_limit = max(1, min(limit, _ROOM_EVENTS_MAX_LIMIT))
        after = max(0, after_sequence)
        events: list[RoomEvent] = []
        while len(events) < capped_limit:
            page_size = min(500, capped_limit - len(events))
            page = await self.repos.events.list_since(room_id, after, limit=page_size)
            if not page:
                break
            events.extend(page)
            after = page[-1].sequence
            if len(page) < page_size:
                break
        return events[:capped_limit]

    async def get_latest_sequence(self, room_id: str) -> int:
        """The room's own head sequence (0 for a room with no events yet)."""
        return await self.repos.events.get_latest_sequence(room_id)

    async def export_room_audit(self, room_id: str) -> AsyncIterator[str]:
        """Every event this room ever recorded, one JSON line each, then a summary.

        Pages on after_sequence rather than trusting one read: list_since's own
        page is capped at 500, and a room with more events than that would have
        its export silently stop there — the same shape of bug 030's migration
        already named once in this codebase, reborn in a new reader.
        """
        room = await self.repos.rooms.get(room_id)
        if room is None:
            raise DomainError(f"room not found: {room_id}")
        after_sequence = 0
        exported = 0
        while True:
            page = await self.repos.events.list_since_with_chain(room_id, after_sequence)
            if not page:
                break
            for row in page:
                exported += 1
                payload = json.loads(row["payload"])
                line: dict[str, Any] = {
                    "sequence": row["sequence"],
                    "event_type": row["event_type"],
                    "actor": {"actor_id": row["actor_id"], "actor_type": row["actor_type"]},
                    "created_at": row["timestamp"],
                    "payload": payload,
                    "event_hash": row["event_hash"],
                    "prev_hash": row["prev_hash"],
                }
                # A marker payload names a redaction id; the row it replaced tells an
                # auditor when the content was removed, why, and by whom, without
                # ever handing back what was removed.
                if payload.get("redacted") is True and isinstance(payload.get("redaction_id"), str):
                    redaction = await self.repos.event_redactions.get_by_event_id(row["event_id"])
                    if redaction is not None:
                        line["redaction"] = {
                            "redaction_id": redaction["redaction_id"],
                            "redacted_at": redaction["redacted_at"],
                            "reason": redaction["reason"],
                            "actor_id": redaction["actor_id"],
                        }
                yield json.dumps(line) + "\n"
            after_sequence = int(page[-1]["sequence"])
        sequence_counter = await self.repos.events.get_sequence_counter(room_id)
        _, breaks = await verify_event_chain(self.db, room_id=room_id)
        # A break already covers a divergent hash or a missing sequence; it does not
        # cover this reader stopping early. verify_event_chain makes exactly this
        # comparison for its own break detection (log end vs. room counter) — the
        # same fact, read here instead of recomputed, because a reader whose page
        # count silently fell short of the counter is unverified for the same
        # reason a broken hash is: what it exported is not what the room holds.
        chain_verified = (
            not any(b.room_id == room_id for b in breaks) and exported == sequence_counter
        )
        yield (
            json.dumps(
                {
                    "export_summary": {
                        "room_id": room_id,
                        "events": exported,
                        "sequence_counter": sequence_counter,
                        "chain_verified": chain_verified,
                        "verified_at": utcnow().isoformat(),
                    }
                }
            )
            + "\n"
        )

    @staticmethod
    def _thread_state(message: Message, summaries: dict[str, ThreadSummary]) -> dict[str, Any]:
        """How a channel describes one message's thread, every field counted on read.

        The whole thread, not just the direct answers: a message with no thread has
        no replies, no later reply time, and one participant — its own author.

        A reply broadcast to the channel is summarised by the thread it belongs to,
        not by itself. Summaries are keyed on roots, so looking one up by the reply's
        own id found nothing and the channel drew it as a message with no thread at
        all — offering "Reply" on something that was already an answer. It is told
        here that it is a reply, and which conversation it came out of.
        """
        root_id = message.root_message_id or message.message_id
        summary = summaries.get(root_id)
        return {
            "reply_count": summary.descendant_count if summary else 0,
            "participant_count": summary.participant_count if summary else 1,
            "last_reply_at": (
                summary.last_reply_at.isoformat()
                if summary is not None and summary.last_reply_at is not None
                else None
            ),
            "is_thread_reply": message.root_message_id is not None,
            "thread_root_id": root_id,
        }

    async def get_room_state(
        self,
        room_id: str,
        last_sequence: int = 0,
        user_id: str = "",
        event_limit: int = _ROOM_EVENTS_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        # Read before any other query in this method, including get_room
        # below: an event whose commit lands between this read and the
        # events/messages reads that follow would otherwise have a sequence
        # at or below latest_sequence while being absent from both, so a
        # socket that subscribes at last_sequence=latest_sequence would
        # never replay it (nothing after this read can be missed this way,
        # since anything at or below the number this returns is guaranteed
        # to already be in every collection read after it).
        latest_sequence = await self.get_latest_sequence(room_id)
        room = await self.get_room(room_id)
        events = await self.get_room_events(room_id, last_sequence, limit=event_limit)
        members = await self.get_room_members(room_id)
        member_display_names = await self.repos.room_members.display_names(room_id)
        agents = await self.list_room_agents(room_id)
        # The roster is where a reader learns what to type: a participant whose
        # handle the client cannot see is a participant nobody can address.
        handles = {
            (record.participant_type.value, record.participant_id): record.handle
            for record in await self.repos.handles.list_by_room(room_id)
        }
        tasks = await self.list_room_tasks(room_id)
        messages = await self.list_room_messages(room_id, limit=50)
        thread_summaries = await self.repos.messages.thread_summaries_by_room(room_id)
        reactions: dict[str, list[dict[str, str]]] = {}
        for reaction in await self.repos.reactions.list_live_by_room(room_id):
            reactions.setdefault(reaction.message_id, []).append(
                {
                    "emoji": reaction.emoji,
                    "actor_id": reaction.actor_id,
                    "actor_type": reaction.actor_type.value,
                }
            )
        artifacts = await self.list_room_artifacts(room_id)
        decisions = await self.list_room_decisions(room_id)
        memories = await self.list_room_memories(room_id)
        pending_approvals = await self.list_pending_approvals(room_id)
        # Why each parked call is parked, from the call's own row. A reader answering
        # an approval can see whether the channel's posture stopped it or the tool's
        # own floor did, which is the difference between "this room pauses everything"
        # and "this action always pauses".
        approval_reasons = {
            approval.approval_id: gated.reason
            for approval in pending_approvals
            if (gated := await self.repos.tool_requests.get_by_approval(approval.approval_id))
        }
        posture = await self.repos.room_postures.current(room_id)
        runs = await self.repos.executions.list_by_room(room_id)
        branches = await self.repos.branches.list_by_room(room_id)
        outputs = await self.list_room_outputs(room_id)
        output_selections = await self.list_output_selections(room_id)
        branch_syntheses = [
            synthesis
            for branch in branches
            for synthesis in await self.repos.branch_syntheses.list_by_branch(branch.branch_id)
        ]
        active_turn_lock = await self.repos.turn_locks.get_active(TurnLockScopeType.ROOM, room_id)
        ontology = await self.get_room_ontology(room_id)
        artifact_state: list[dict[str, Any]] = []
        for artifact in artifacts:
            versions = await self.repos.artifacts.list_versions(artifact.artifact_id)
            latest = versions[0] if versions else None
            artifact_state.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "name": artifact.name,
                    "type": artifact.artifact_type.value,
                    "version": artifact.current_version,
                    "version_id": latest.version_id if latest else None,
                    "content": latest.content if latest else "",
                    "content_hash": latest.content_hash if latest else "",
                    "provenance_hash": latest.provenance_hash if latest else "",
                    "branch_synthesis_id": latest.branch_synthesis_id if latest else None,
                }
            )
        presence = await self.presence.get_room_presence(room_id)
        # latest_sequence was read first, above; see the comment there.
        # Kept here as the room's true head, not events_since's own capped
        # page of it (see events_limit above): a reconnect that only ever
        # saw a snapshot's events_since watermark undercounts the head on
        # any room past that cap, and a stale-cursor 4408 close
        # (realtime/websocket.py) needs the real number to reconnect
        # without replaying the whole gap.
        return {
            "room": {
                "room_id": room.room_id,
                "name": room.name,
                "description": room.description,
                "status": room.status.value,
                "workspace_id": room.workspace_id,
                # Derived from the declaration rows on this read, like every other
                # reader of a posture. Nothing here is a value spent later.
                "posture": posture.value,
            },
            "latest_sequence": latest_sequence,
            "events_since": [
                {
                    "event_id": e.event_id,
                    "sequence": e.sequence,
                    "event_type": e.event_type.value,
                    "payload": e.payload,
                    "actor_id": e.actor_id,
                    "actor_type": e.actor_type,
                    "timestamp": e.timestamp.isoformat(),
                }
                for e in events
            ],
            "members": [
                {
                    "user_id": m.user_id,
                    "role": m.role,
                    "handle": handles.get((ParticipantType.USER.value, m.user_id), ""),
                    "display_name": member_display_names.get(m.user_id, m.user_id),
                }
                for m in members
            ],
            "agents": [
                {
                    "agent_id": a.agent_id,
                    "name": a.name,
                    "handle": handles.get((ParticipantType.AGENT.value, a.agent_id), ""),
                    "role": a.role,
                    "status": a.status.value,
                }
                for a in agents
            ],
            "branches": [
                {
                    "branch_id": branch.branch_id,
                    "mode": branch.mode.value,
                    "status": branch.status.value,
                    "initiated_by": branch.initiated_by,
                    "initiating_prompt": branch.initiating_prompt,
                    "context_event_sequence": branch.context_event_sequence,
                    "context_message_ids": list(branch.context_message_ids),
                    "context_snapshot": branch.context_snapshot,
                    "context_hash": branch.context_hash,
                    "lifecycle_managed": branch.lifecycle_managed,
                    "execution_ids": [
                        run.execution_id for run in runs if run.branch_id == branch.branch_id
                    ],
                    "created_at": branch.created_at.isoformat(),
                    "updated_at": branch.updated_at.isoformat(),
                    "completed_at": (
                        branch.completed_at.isoformat() if branch.completed_at else None
                    ),
                }
                for branch in branches
            ],
            "runs": [
                {
                    "execution_id": run.execution_id,
                    "session_id": run.session_id,
                    "agent_id": run.agent_id,
                    "run_id": run.run_id,
                    "branch_id": run.branch_id,
                    "status": run.status.value,
                    # Half of "why did this agent speak"; the other half is the event.
                    "triggered_by": run.triggered_by.value,
                    "started_at": run.started_at.isoformat(),
                    "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                }
                for run in runs
            ],
            "outputs": [
                {
                    "output_id": output.output_id,
                    "branch_id": output.branch_id,
                    "execution_id": output.execution_id,
                    "session_id": output.session_id,
                    "agent_id": output.agent_id,
                    "content": output.content,
                    "output_data": output.output_data,
                    "source_prompt": output.source_prompt,
                    "provider_input": output.provider_input,
                    "provider_name": output.provider_name,
                    "provider_model": output.provider_model,
                    "provider_response_id": output.provider_response_id,
                    "provider_interventions": list(output.provider_interventions),
                    "provider_evidence": output.provider_evidence,
                    "created_at": output.created_at.isoformat(),
                }
                for output in outputs
            ],
            "output_selections": [
                {
                    "output_id": selection.output_id,
                    "branch_id": selection.branch_id,
                    "disposition": selection.disposition.value,
                    "decided_by": selection.decided_by,
                    "updated_at": selection.updated_at.isoformat(),
                }
                for selection in output_selections
            ],
            "branch_syntheses": [
                {
                    "synthesis_id": synthesis.synthesis_id,
                    "branch_id": synthesis.branch_id,
                    "status": synthesis.status.value,
                    "title": synthesis.title,
                    "provider_name": synthesis.provider_name,
                    "provider_model": synthesis.provider_model,
                    "provider_response_id": synthesis.provider_response_id,
                    "simulated": synthesis.simulated,
                    "artifact_version_id": synthesis.artifact_version_id,
                    "created_at": synthesis.created_at.isoformat(),
                    "completed_at": (
                        synthesis.completed_at.isoformat() if synthesis.completed_at else None
                    ),
                }
                for synthesis in branch_syntheses
            ],
            "turn_lock": (
                {
                    "lock_id": active_turn_lock.lock_id,
                    "scope_type": active_turn_lock.scope_type.value,
                    "scope_id": active_turn_lock.scope_id,
                    "branch_id": active_turn_lock.branch_id,
                    "status": active_turn_lock.status.value,
                    "acquired_by": active_turn_lock.acquired_by,
                    "acquired_at": active_turn_lock.acquired_at.isoformat(),
                }
                if active_turn_lock is not None
                else None
            ),
            "tasks": [
                {
                    "task_id": t.task_id,
                    "title": t.title,
                    "status": t.status.value,
                    "priority": t.priority.value,
                    "assigned_agent_id": t.assigned_agent_id,
                }
                for t in tasks
            ],
            "messages": [
                {
                    "message_id": m.message_id,
                    "role": m.role.value,
                    "sender_id": m.sender_id,
                    "content": m.content,
                    "metadata": m.metadata,
                    "sequence": m.event_sequence,
                    "parent_message_id": m.parent_message_id,
                    "root_message_id": m.root_message_id,
                    "thread_depth": m.thread_depth,
                    "broadcast_to_room": m.broadcast_to_room,
                    # Derived here too: the snapshot never carries a stored counter.
                    **self._thread_state(m, thread_summaries),
                    "reactions": reactions.get(m.message_id, []),
                    "created_at": m.created_at.isoformat(),
                    # Metadata only, never the blob.
                    "attachments": [
                        {
                            "attachment_id": a.attachment_id,
                            "filename": a.filename,
                            "content_type": a.content_type,
                            "size_bytes": a.size_bytes,
                        }
                        for a in await self.repos.attachments.list_for_message(m.message_id)
                    ],
                }
                for m in messages
            ],
            "read_cursor": (await self.get_read_cursor(room_id, user_id) if user_id else None),
            "artifacts": artifact_state,
            "decisions": [
                {"decision_id": d.decision_id, "title": d.title, "status": d.status.value}
                for d in decisions
            ],
            "memories": [
                {"memory_id": m.memory_id, "content": m.content, "type": m.memory_type}
                for m in memories
            ],
            "pending_approvals": [
                {
                    "approval_id": a.approval_id,
                    "action": a.action_description,
                    "agent_id": a.agent_id,
                    "reason": approval_reasons.get(a.approval_id, ""),
                }
                for a in pending_approvals
            ],
            "presence": [{"user_id": p.user_id, "status": p.status.value} for p in presence],
            "ontology": ontology,
        }
