"""Conversation: messages, mentions, threads, reactions, attachments, and search."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import replace
from typing import Any

from ..domain.events import EventType, RoomEvent
from ..domain.models import (
    MAX_THREAD_DEPTH,
    AgentOutput,
    AgentTrigger,
    Attachment,
    DomainError,
    Execution,
    MentionTargetType,
    Message,
    MessageMention,
    MessageReaction,
    MessageRole,
    Notification,
    ParticipantType,
    ReadCursor,
    RoomParticipantHandle,
    SearchHit,
    Session,
    SessionStatus,
    ThreadReply,
    handle_from_display_name,
    new_id,
    utcnow,
)
from ..security.authorization import (
    AuthorizationError,
    RoomCapability,
)
from ..security.capabilities import (
    BoundingPrincipals,
    RunAuthorization,
)
from ..security.screening import fenced, screen
from ._shared import (
    _AGENT_MESSAGE_EXCERPT_CHARS,
    _MENTION_PATTERN,
    _SEARCH_TERM_PATTERN,
    AgentLaunchRefused,
    _SharedMixin,
)

log = logging.getLogger(__name__)


class _ConversationMixin(_SharedMixin):
    """Mixin providing the conversation surface of MultiplayerService."""

    async def _read_addressed_handles(
        self, room_id: str, content: str
    ) -> tuple[list[RoomParticipantHandle], list[str]]:
        """Split the @tokens in this text into the ones the room answers to and the rest.

        The room's issued handles are the entire vocabulary. A handle is matched
        exactly, so nothing here guesses from a prefix of somebody's display name,
        and a client cannot claim a mention the text does not contain. What the
        author typed is normalised the same way a handle is minted, which is what
        lets @Architect and @architect address the same agent and lets a mention end
        a sentence without the full stop becoming part of the address.

        The second list is the point of returning a tuple: an @handle that addresses
        nobody used to vanish silently, and the caller has to be able to say so.
        """
        typed = list(dict.fromkeys(_MENTION_PATTERN.findall(content)))
        if not typed:
            return [], []
        by_handle = {
            record.handle: record for record in await self.repos.handles.list_by_room(room_id)
        }
        resolved: list[RoomParticipantHandle] = []
        unresolved: list[str] = []
        seen: set[tuple[str, str]] = set()
        for token in typed:
            record = by_handle.get(handle_from_display_name(token))
            if record is None:
                unresolved.append(token)
                continue
            key = (record.participant_type.value, record.participant_id)
            if key in seen:
                continue
            seen.add(key)
            resolved.append(record)
        return resolved, unresolved

    async def _resolve_mentions(
        self, room_id: str, message_id: str, content: str
    ) -> tuple[list[MessageMention], list[str]]:
        """The addressed targets of one message, and the handles that addressed nobody."""
        resolved, unresolved = await self._read_addressed_handles(room_id, content)
        mentions = [
            MessageMention(
                message_id=message_id,
                room_id=room_id,
                target_type=MentionTargetType(record.participant_type.value),
                target_id=record.participant_id,
                handle=record.handle,
            )
            for record in resolved
        ]
        return mentions, unresolved

    async def unrecognized_mention_handles(self, room_id: str, content: str) -> list[str]:
        """The @handles in this text that address nobody in this room.

        Silence was the bug: a misspelled agent handle returned 200 with an empty
        mention list, so the author waited for an answer that was never coming.
        """
        _, unresolved = await self._read_addressed_handles(room_id, content)
        return unresolved

    async def uninvocable_mention_handles(self, message_id: str) -> list[str]:
        """The handles this message addressed that no agent turn can ever answer.

        Members and agents share one handle namespace, which is correct: @finance
        has to mean exactly one participant in a room. It also means a person can
        hold the handle an agent would otherwise have taken, and then a request to
        invoke the mentioned agents opens no run at all. The handle resolved, so it
        is not unrecognized; it resolved to somebody who cannot be invoked, and an
        author who asked for a turn has to be told which of the two happened.
        """
        return [
            mention.handle
            for mention in await self.repos.mentions.list_for_message(message_id)
            if mention.target_type is not MentionTargetType.AGENT
        ]

    async def _invoke_mentioned_agent_in_transaction(
        self, room_id: str, agent_id: str, requested_by: str, message_id: str
    ) -> tuple[Execution, RoomEvent]:
        """Open one agent turn that a mention explicitly asked for.

        The five-way capability intersection is the existing check for what a
        member may lend an agent. An empty effective set means this member may
        lend this agent nothing, so they may not make it speak, and raising here
        rolls the whole message write back rather than half-applying it.

        This only opens the turn. Running it is long provider I/O, so it happens
        after this transaction commits, in :meth:`_dispatch_mention_run`.
        """
        agent = await self.get_agent(agent_id)
        if agent.room_id != room_id:
            raise DomainError("mentioned agent is not in this room")
        mentioner = BoundingPrincipals(frozenset({requested_by}))
        if not (await self._lendable_terms(agent, room_id, mentioner)).lendable():
            raise AuthorizationError(
                f"{requested_by} may not invoke agent {agent_id}: no effective capability"
            )
        await self._require_addressable(agent, room_id, requested_by)
        run = await self._prepare_agent_run(agent, room_id, requested_by)
        session = Session(
            session_id=new_id("sess"),
            room_id=room_id,
            agent_id=agent_id,
            status=SessionStatus.ACTIVE,
        )
        execution = Execution(
            execution_id=new_id("exec"),
            session_id=session.session_id,
            agent_id=agent_id,
            authorized_by=requested_by,
            triggered_by=AgentTrigger.MENTION,
            input_data={"mention_message_id": message_id, "requested_by": requested_by},
        )
        await self.repos.sessions.create(session)
        execution = await self.repos.executions.create(execution)
        await self.repos.agent_runs.create_in_transaction(
            replace(run, execution_id=execution.execution_id)
        )
        event = await self.repos.events.append_with_next_sequence_in_transaction(
            RoomEvent(
                room_id=room_id,
                sequence=0,
                event_type=EventType.AGENT_RUN_STARTED,
                payload={
                    "execution_id": execution.execution_id,
                    "session_id": session.session_id,
                    "agent_id": agent_id,
                    "triggered_by": AgentTrigger.MENTION.value,
                    "mention_message_id": message_id,
                    "requested_by": requested_by,
                },
                actor_id=agent_id,
                actor_type="agent",
            )
        )
        return execution, event

    async def _dispatch_mention_run(self, execution_id: str, prompt: str) -> None:
        """Run a mention-invoked turn, after the write that recorded it committed.

        The invariant this holds is that no run is left in a state the system
        cannot describe. Provider failures are already terminalised by
        :meth:`execute_agent_step`; anything else that escapes it is settled here
        as FAILED with an event saying why. The one gap a running process cannot
        close is a crash between the commit and this call, and
        :meth:`_settle_orphaned_mention_runs` closes that at the next startup.

        Claiming the run first is what makes that sweep safe: from here on the run
        is visibly somebody's work, so no other process mistakes it for an orphan.
        A claim that does not take means another dispatcher already has it, or the
        run is no longer pending, and either way this process must not run it.
        """
        if not await self.repos.executions.claim_for_dispatch(execution_id, self._dispatch_claim):
            log.info("Mention-invoked run %s was already claimed; not dispatching", execution_id)
            return
        try:
            await self.execute_agent_step(execution_id, prompt)
        except Exception as exc:
            log.exception("Mention-invoked run %s did not complete", execution_id)
            try:
                await self._settle_undispatched_run(execution_id, f"dispatch failed: {exc}")
            except Exception:
                # The message itself is already committed. Failing its write here
                # would tell the author their message was lost when it was not.
                log.exception("Failed to settle mention run %s", execution_id)

    @staticmethod
    def _output_excerpt(content: str) -> tuple[str, bool]:
        """What the conversation shows of an output, and whether it is all of it."""
        text = " ".join(content.split())
        if len(text) <= _AGENT_MESSAGE_EXCERPT_CHARS:
            return text, False
        return text[:_AGENT_MESSAGE_EXCERPT_CHARS].rstrip() + "…", True

    async def _agent_message_for_mention(
        self, execution: Execution, session: Session, output: AgentOutput
    ) -> tuple[Message | None, RoomEvent | None]:
        """The conversational surface for a turn a mention asked for.

        An authenticated HTTP principal may never author an AGENT-role message, so
        the service authors it here instead, in the same transaction as the output.
        The output remains the first-class inspectable record; this message names it
        by output_id and sits at the mention's own thread coordinates, so the answer
        lands in the conversation that asked the question.

        The message carries an excerpt, not the output. Copying the content in full
        would put the same text in two places, and then the two places could be
        edited, exported or retracted apart from each other while both claimed to be
        what the agent said. The metadata says who asked for the turn, so the room
        can read why the agent spoke without opening anything.
        """
        if execution.triggered_by is not AgentTrigger.MENTION:
            return None, None
        mention_message_id = str(execution.input_data.get("mention_message_id", ""))
        mention = await self.repos.messages.get(mention_message_id) if mention_message_id else None
        if mention is None or mention.room_id != session.room_id:
            return None, None
        excerpt, excerpted = self._output_excerpt(output.content)
        message = Message(
            message_id=new_id("msg"),
            room_id=session.room_id,
            role=MessageRole.AGENT,
            sender_id=execution.agent_id,
            content=excerpt,
            metadata={
                "output_id": output.output_id,
                "execution_id": execution.execution_id,
                "triggered_by": execution.triggered_by.value,
                "requested_by": str(execution.input_data.get("requested_by", "")),
                # The reader is told when there is more in the record than here, so
                # a truncated excerpt is never mistaken for the whole answer.
                "output_excerpted": excerpted,
            },
            parent_message_id=mention.message_id,
            root_message_id=mention.root_message_id or mention.message_id,
            thread_depth=mention.thread_depth + 1,
            # An answer belongs wherever the question was asked.
            broadcast_to_room=mention.broadcast_to_room,
        )
        event = RoomEvent(
            room_id=session.room_id,
            sequence=0,
            event_type=EventType.MESSAGE_CREATED,
            payload={
                "message_id": message.message_id,
                "role": message.role.value,
                "sender_id": message.sender_id,
                "content": message.content[:500],
                "created_at": message.created_at.isoformat(),
                "parent_message_id": message.parent_message_id,
                "root_message_id": message.root_message_id,
                "thread_depth": message.thread_depth,
                "broadcast_to_room": message.broadcast_to_room,
                "output_id": output.output_id,
                "execution_id": execution.execution_id,
                "triggered_by": execution.triggered_by.value,
                "requested_by": str(execution.input_data.get("requested_by", "")),
                "mentions": [],
            },
            actor_id=execution.agent_id,
            actor_type="agent",
        )
        return message, event

    async def send_message(
        self,
        room_id: str,
        role: MessageRole,
        sender_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        parent_message_id: str | None = None,
        broadcast_to_room: bool = True,
        invoke_mentioned_agents: bool = False,
        attachment_ids: list[str] | None = None,
    ) -> Message:
        content = self._validate_non_empty(content, "message content")
        if idempotency_key is not None:
            idempotency_key = self._validate_idempotency_key(idempotency_key)
        request: dict[str, Any] = {
            "role": role.value,
            "content": content,
            "metadata": metadata or {},
            "parent_message_id": parent_message_id,
            "invoke_mentioned_agents": invoke_mentioned_agents,
        }
        # Folded in only when present, so every hash already stored for an
        # attachment-free send still matches and an old client retrying one
        # keeps working. A retry with the same key and different attachments
        # is a different request, not a replay of the first.
        if attachment_ids:
            request["attachment_ids"] = sorted(attachment_ids)
        msg = Message(
            message_id=new_id("msg"),
            room_id=room_id,
            role=role,
            sender_id=sender_id,
            content=content,
            metadata=metadata or {},
            broadcast_to_room=broadcast_to_room,
        )
        events: list[RoomEvent] = []
        invoked: dict[str, str] = {}
        try:
            async with self.db.transaction():
                if role is MessageRole.HUMAN:
                    await self._require_mutate_in_transaction(room_id, sender_id)
                if idempotency_key is not None:
                    prior = await self._claim_idempotency(
                        room_id, sender_id, idempotency_key, "message.create", request
                    )
                    if prior is not None:
                        replay = await self.repos.messages.get(prior.result_ref)
                        if replay is None:
                            raise DomainError("idempotent message replay lost its result")
                        return replay
                if parent_message_id is not None:
                    parent = await self.repos.messages.get(parent_message_id)
                    if parent is None or parent.room_id != room_id:
                        raise DomainError(f"parent message not found in room: {parent_message_id}")
                    if parent.thread_depth + 1 > MAX_THREAD_DEPTH:
                        raise DomainError(
                            f"thread depth limit reached: a reply may not nest deeper "
                            f"than {MAX_THREAD_DEPTH}"
                        )
                    msg = replace(
                        msg,
                        parent_message_id=parent.message_id,
                        root_message_id=parent.root_message_id or parent.message_id,
                        thread_depth=parent.thread_depth + 1,
                    )
                mentions, _ = await self._resolve_mentions(room_id, msg.message_id, content)
                if invoke_mentioned_agents and msg.thread_depth >= MAX_THREAD_DEPTH:
                    # The agent's answer is a reply to this message, and it has to fit.
                    raise DomainError(
                        "thread depth limit reached: no room for an agent's answer below "
                        f"depth {MAX_THREAD_DEPTH}"
                    )
                for mention in mentions:
                    if mention.target_type is not MentionTargetType.AGENT:
                        continue
                    if not invoke_mentioned_agents:
                        continue
                    execution, run_event = await self._invoke_mentioned_agent_in_transaction(
                        room_id, mention.target_id, sender_id, msg.message_id
                    )
                    invoked[mention.target_id] = execution.execution_id
                    events.append(run_event)
                message_event = (
                    await self.repos.messages.create_with_event_and_turn_guard_in_transaction(
                        msg,
                        RoomEvent(
                            room_id=room_id,
                            sequence=0,
                            event_type=EventType.MESSAGE_CREATED,
                            payload={
                                "message_id": msg.message_id,
                                "role": role.value,
                                "sender_id": sender_id,
                                "content": content[:500],
                                "created_at": msg.created_at.isoformat(),
                                "parent_message_id": msg.parent_message_id,
                                "root_message_id": msg.root_message_id,
                                "thread_depth": msg.thread_depth,
                                "broadcast_to_room": msg.broadcast_to_room,
                                # Filenames and sizes only, never bytes — the message
                                # event is what a model path and an export both read.
                                "attachment_ids": list(attachment_ids or []),
                                "mentions": [
                                    {
                                        "target_type": mention.target_type.value,
                                        "target_id": mention.target_id,
                                        "invoked_execution_id": invoked.get(mention.target_id),
                                    }
                                    for mention in mentions
                                ],
                            },
                            actor_id=sender_id,
                            actor_type=role.value.lower(),
                        ),
                    )
                )
                events.append(message_event)
                msg = replace(msg, event_sequence=message_event.sequence)
                for attachment_id in attachment_ids or []:
                    # Same room, same uploader, still unbound — checked and claimed
                    # in one statement, inside the transaction that writes the
                    # message. The message row must exist first: the FK this binds
                    # against is on the message this attachment is claimed for.
                    bound = await self.repos.attachments.bind_to_message_in_transaction(
                        attachment_id, room_id, sender_id, msg.message_id
                    )
                    if not bound:
                        raise DomainError(f"attachment not available to bind: {attachment_id}")
                for mention in mentions:
                    await self.repos.mentions.create(
                        replace(mention, invoked_execution_id=invoked.get(mention.target_id))
                    )
                    if mention.target_type is MentionTargetType.USER:
                        await self.repos.notifications.create(
                            Notification(
                                notification_id=new_id("notif"),
                                user_id=mention.target_id,
                                room_id=room_id,
                                title="You were mentioned",
                                body=content[:500],
                                notification_type="mention",
                            )
                        )
                if idempotency_key is not None:
                    await self._record_idempotency(
                        room_id,
                        sender_id,
                        idempotency_key,
                        "message.create",
                        request,
                        msg.message_id,
                    )
        except AgentLaunchRefused as refusal:
            # The message and the turn it asked for roll back together; the refusal is
            # appended after that rollback, or it would roll back with them.
            await self._record_launch_refusal(refusal)
            raise
        except DomainError:
            raise
        except ValueError as exc:
            raise DomainError(str(exc)) from exc
        await self._broadcast_persisted_events(events)
        # Dispatch belongs here, after the commit, beside the broadcast: a turn that
        # waited on a provider inside the write transaction would hold the room's
        # write lock for the length of the model call. The mention's own text is the
        # prompt, because that is what the author addressed to the agent - screened
        # and fenced, because any member can author it.
        for execution_id in invoked.values():
            await self._dispatch_mention_run(execution_id, fenced(screen(content, "room message")))
        return msg

    async def list_room_messages(
        self, room_id: str, limit: int = 100, after_sequence: int | None = None
    ) -> list[Message]:
        return await self.repos.messages.list_by_room(
            room_id, limit=self._validate_limit(limit), after_sequence=after_sequence
        )

    async def list_message_mentions(self, message_id: str) -> list[MessageMention]:
        return await self.repos.mentions.list_for_message(message_id)

    async def list_message_attachments(self, message_id: str) -> list[Attachment]:
        return await self.repos.attachments.list_for_message(message_id)

    async def upload_attachment(
        self,
        room_id: str,
        uploader_id: str,
        filename: str,
        content_type: str,
        data: bytes,
        max_bytes: int,
    ) -> Attachment:
        """Store a file a member uploaded, unbound until a message claims it.

        The bytes never leave this method except into the row: nothing here
        builds a model prompt, and nothing downstream of this call is handed
        the blob — only filename/content_type/size_bytes ever ride a message.
        """
        filename = self._validate_non_empty(filename, "filename")
        if len(data) > max_bytes:
            raise DomainError(f"attachment exceeds the {max_bytes}-byte limit")
        attachment = Attachment(
            attachment_id=new_id("att"),
            room_id=room_id,
            uploader_id=uploader_id,
            filename=filename,
            content_type=content_type,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            data=data,
        )
        async with self.db.transaction():
            await self._require_mutate_in_transaction(room_id, uploader_id)
            await self.repos.attachments.create(attachment)
        return attachment

    async def get_attachment(self, attachment_id: str) -> Attachment:
        attachment = await self.repos.attachments.get(attachment_id)
        if attachment is None:
            raise DomainError(f"attachment not found: {attachment_id}")
        return attachment

    async def get_message(self, message_id: str) -> Message:
        message = await self.repos.messages.get(message_id)
        if message is None:
            raise DomainError(f"message not found: {message_id}")
        return message

    async def list_thread(self, root_message_id: str, limit: int = 200) -> list[ThreadReply]:
        """The whole thread with counts derived from the reply rows on every read."""
        root = await self.get_message(root_message_id)
        if root.root_message_id is not None:
            root_message_id = root.root_message_id
        return await self.repos.messages.list_thread(root_message_id, limit)

    @staticmethod
    def _validate_emoji(value: str) -> str:
        value = value.strip()
        if not value or len(value) > 16 or any(char.isspace() for char in value):
            raise DomainError("reaction emoji must be a short non-empty token")
        return value

    async def _require_reaction_actor_in_transaction(
        self, room_id: str, actor_id: str, actor_type: ParticipantType
    ) -> None:
        """Authorize the reacting principal against its own kind of room membership.

        A member is checked against room_members, an agent against the agent's own
        durable membership of this room. Neither borrows the other's: an agent id is
        not in room_members and never gains MUTATE by being mentioned there, and an
        authenticated HTTP principal never reaches the agent branch because the
        routes only ever call the member-facing methods.
        """
        if actor_type is ParticipantType.AGENT:
            if not await self.repos.agents.has_room_membership(actor_id, room_id):
                raise AuthorizationError(f"agent {actor_id} is not a member of this room")
            return
        await self._require_mutate_in_transaction(room_id, actor_id)

    async def _set_reaction(
        self,
        message_id: str,
        actor_id: str,
        emoji: str,
        *,
        removed: bool,
        actor_type: ParticipantType = ParticipantType.USER,
        authorization: RunAuthorization | None = None,
    ) -> MessageReaction:
        emoji = self._validate_emoji(emoji)
        message = await self.get_message(message_id)
        async with self.db.transaction():
            await self._require_reaction_actor_in_transaction(message.room_id, actor_id, actor_type)
            if authorization is not None:
                await self._require_run_authority_in_transaction(authorization, "message.react")
            existing = await self.repos.reactions.get(message_id, actor_id, emoji)
            if existing is not None and (existing.removed_at is not None) == removed:
                # Repeating an add or a remove is a no-op, so a retry appends no event.
                return existing
            if existing is None and removed:
                raise DomainError("no such reaction to remove")
            reaction = await self.repos.reactions.set_removed_at(
                message_id,
                message.room_id,
                actor_id,
                emoji,
                utcnow() if removed else None,
                actor_type,
            )
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=message.room_id,
                    sequence=0,
                    event_type=(
                        EventType.MESSAGE_REACTION_REMOVED
                        if removed
                        else EventType.MESSAGE_REACTION_ADDED
                    ),
                    payload={
                        "message_id": message_id,
                        "actor_id": actor_id,
                        "actor_type": actor_type.value,
                        "emoji": emoji,
                    },
                    actor_id=actor_id,
                    actor_type=actor_type.value.lower(),
                )
            )
        await self._broadcast_persisted_events([event])
        return reaction

    async def add_reaction(self, message_id: str, actor_id: str, emoji: str) -> MessageReaction:
        """A member reacts. This is the only reaction path an HTTP route may reach."""
        return await self._set_reaction(message_id, actor_id, emoji, removed=False)

    async def remove_reaction(self, message_id: str, actor_id: str, emoji: str) -> MessageReaction:
        return await self._set_reaction(message_id, actor_id, emoji, removed=True)

    async def add_agent_reaction(
        self,
        message_id: str,
        agent_id: str,
        emoji: str,
        *,
        authorization: RunAuthorization | None = None,
    ) -> MessageReaction:
        """An agent reacts as itself, on its own membership of the room.

        Reached only through the message.react tool, so the agent asks for it during
        its own run and the gateway audits the request. Deliberately not reachable
        from a route: an agent reaction is attributed to the agent, so letting a
        human ask for one would let a human sign an agent's name.
        """
        return await self._set_reaction(
            message_id,
            agent_id,
            emoji,
            removed=False,
            actor_type=ParticipantType.AGENT,
            authorization=authorization,
        )

    async def remove_agent_reaction(
        self, message_id: str, agent_id: str, emoji: str
    ) -> MessageReaction:
        return await self._set_reaction(
            message_id, agent_id, emoji, removed=True, actor_type=ParticipantType.AGENT
        )

    async def list_reactions(self, message_id: str) -> list[MessageReaction]:
        return await self.repos.reactions.list_live(message_id)

    async def get_read_cursor(self, room_id: str, user_id: str) -> dict[str, Any]:
        cursor = await self.repos.read_cursors.get(room_id, user_id)
        last_read = cursor.last_read_sequence if cursor else 0
        latest = await self.repos.events.get_latest_sequence(room_id)
        return {
            "room_id": room_id,
            "user_id": user_id,
            "last_read_sequence": last_read,
            "latest_sequence": latest,
            "unread_messages": await self.repos.messages.count_since_sequence(
                room_id, last_read, user_id
            ),
            "updated_at": cursor.updated_at.isoformat() if cursor else None,
        }

    async def set_read_cursor(
        self, room_id: str, user_id: str, last_read_sequence: int
    ) -> dict[str, Any]:
        if last_read_sequence < 0:
            raise DomainError("read cursor sequence must not be negative")
        async with self.db.transaction():
            await self._require_capability_in_transaction(room_id, user_id, RoomCapability.READ)
            latest = await self.repos.events.get_latest_sequence(room_id)
            if last_read_sequence > latest:
                raise DomainError("read cursor cannot pass the room's latest sequence")
            await self.repos.read_cursors.set(
                ReadCursor(room_id=room_id, user_id=user_id, last_read_sequence=last_read_sequence)
            )
        return await self.get_read_cursor(room_id, user_id)

    async def search(
        self, user_id: str, query: str, room_id: str | None = None, limit: int = 50
    ) -> list[SearchHit]:
        """Authorization is a join inside the matching query, never a later filter."""
        terms = _SEARCH_TERM_PATTERN.findall(self._validate_non_empty(query, "search query"))
        if not terms:
            raise DomainError("search query must contain a searchable term")
        match_query = " ".join(f'"{term}"' for term in terms[:16])
        return await self.repos.search.search(
            user_id, match_query, room_id, self._validate_limit(limit)
        )
