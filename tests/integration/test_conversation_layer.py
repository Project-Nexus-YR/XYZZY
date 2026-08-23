"""Conversation layer acceptance: threads, mentions, reactions, and read state.

The bar this holds: reply counts are derived from the durable reply rows and never
stored, mentions come from the message text alone, a mention never runs an agent
unless the author asked and holds the capability, a removed reaction leaves its row
behind, and a read position survives a reconnect.
"""

from __future__ import annotations

from typing import Any

import pytest

from multiplayer.db.connection import Database
from multiplayer.domain.events import EventType
from multiplayer.domain.models import (
    MAX_THREAD_DEPTH,
    AgentTrigger,
    DomainError,
    ExecutionStatus,
    MentionTargetType,
    MessageRole,
    ParticipantType,
)
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.authorization import AuthorizationError
from multiplayer.services.service import MultiplayerService


@pytest.fixture
async def service():
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(
        db, RealtimeHub(), known_users=frozenset({"owner", "teammate", "restricted"})
    )
    await svc.initialize()
    yield svc
    await db.close()


async def _room(svc: MultiplayerService) -> str:
    org = await svc.create_organization("Conv org", "conv-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    return room.room_id


# ── Threading ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_reply_to_a_reply_keeps_one_root_and_a_growing_depth(
    service: MultiplayerService,
) -> None:
    room_id = await _room(service)
    root = await service.send_message(room_id, MessageRole.HUMAN, "owner", "Should we migrate?")
    first = await service.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "Only with rollback.",
        parent_message_id=root.message_id,
    )
    second = await service.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "Rollback needs a dual-write window.",
        parent_message_id=first.message_id,
    )

    assert first.root_message_id == root.message_id
    assert first.thread_depth == 1
    assert second.root_message_id == root.message_id
    assert second.parent_message_id == first.message_id
    assert second.thread_depth == 2

    # Asking the deepest reply for its thread still returns the whole thread.
    thread = await service.list_thread(second.message_id)
    assert [entry.message.message_id for entry in thread] == [
        root.message_id,
        first.message_id,
        second.message_id,
    ]
    assert [entry.reply_count for entry in thread] == [1, 1, 0]


@pytest.mark.asyncio
async def test_every_reply_carries_its_own_room_sequence(service: MultiplayerService) -> None:
    room_id = await _room(service)
    root = await service.send_message(room_id, MessageRole.HUMAN, "owner", "Root")
    reply = await service.send_message(
        room_id, MessageRole.HUMAN, "owner", "Reply", parent_message_id=root.message_id
    )

    assert reply.event_sequence > root.event_sequence > 0
    events = await service.get_room_events(room_id, root.event_sequence)
    reply_events = [e for e in events if e.payload.get("message_id") == reply.message_id]
    assert len(reply_events) == 1
    assert reply_events[0].sequence == reply.event_sequence
    assert reply_events[0].payload["parent_message_id"] == root.message_id


@pytest.mark.asyncio
async def test_a_reply_cannot_cross_a_room_boundary(service: MultiplayerService) -> None:
    org = await service.create_organization("Iso org", "iso-org", "owner")
    workspace = await service.create_workspace(org.org_id, "Main", "main", "owner")
    room_a = await service.create_room(workspace.workspace_id, "A", "owner")
    room_b = await service.create_room(workspace.workspace_id, "B", "owner")
    root = await service.send_message(room_a.room_id, MessageRole.HUMAN, "owner", "In A")

    with pytest.raises(DomainError):
        await service.send_message(
            room_b.room_id,
            MessageRole.HUMAN,
            "owner",
            "Reply from B",
            parent_message_id=root.message_id,
        )


@pytest.mark.asyncio
async def test_a_listing_resumes_from_a_sequence_cursor(service: MultiplayerService) -> None:
    room_id = await _room(service)
    first = await service.send_message(room_id, MessageRole.HUMAN, "owner", "one")
    second = await service.send_message(room_id, MessageRole.HUMAN, "owner", "two")
    third = await service.send_message(room_id, MessageRole.HUMAN, "owner", "three")

    resumed = await service.list_room_messages(room_id, after_sequence=first.event_sequence)
    assert [m.message_id for m in resumed] == [second.message_id, third.message_id]
    assert await service.list_room_messages(room_id, after_sequence=third.event_sequence) == []


# ── Mentions ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mentions_are_derived_from_the_text_against_the_room_roster(
    service: MultiplayerService,
) -> None:
    room_id = await _room(service)
    await service.invite_room_member(room_id, "teammate", "editor", "owner")
    template = (await service.list_agent_templates())[0]
    agent = await service.spawn_agent(room_id, template.template_id, name="Architect")

    message = await service.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "@teammate and @Architect, what about @stranger and email a@b.com?",
    )
    mentions = await service.list_message_mentions(message.message_id)

    assert {(m.target_type, m.target_id) for m in mentions} == {
        (MentionTargetType.USER, "teammate"),
        (MentionTargetType.AGENT, agent.agent_id),
    }
    # An unknown handle resolves to nothing, and no mention ran anything.
    assert all(m.invoked_execution_id is None for m in mentions)


@pytest.mark.asyncio
async def test_a_mentioned_user_is_notified(service: MultiplayerService) -> None:
    room_id = await _room(service)
    await service.invite_room_member(room_id, "teammate", "editor", "owner")
    await service.send_message(room_id, MessageRole.HUMAN, "owner", "@teammate please review")

    notifications = await service.list_notifications("teammate")
    assert [n.notification_type for n in notifications] == ["mention"]


@pytest.mark.asyncio
async def test_mentioning_an_agent_records_the_mention_without_running_it(
    service: MultiplayerService,
) -> None:
    room_id = await _room(service)
    template = (await service.list_agent_templates())[0]
    agent = await service.spawn_agent(room_id, template.template_id, name="Architect")

    await service.send_message(room_id, MessageRole.HUMAN, "owner", "@Architect thoughts?")

    assert await service.repos.executions.list_by_room(room_id) == []
    assert agent.agent_id


@pytest.mark.asyncio
async def test_an_explicit_invocation_opens_a_turn_that_says_why_it_ran(
    service: MultiplayerService,
) -> None:
    room_id = await _room(service)
    template = (await service.list_agent_templates())[0]
    agent = await service.spawn_agent(room_id, template.template_id, name="Architect")

    message = await service.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "@Architect please assess this",
        invoke_mentioned_agents=True,
    )

    runs = await service.repos.executions.list_by_room(room_id)
    assert len(runs) == 1
    assert runs[0].agent_id == agent.agent_id
    assert runs[0].triggered_by is AgentTrigger.MENTION
    mention = (await service.list_message_mentions(message.message_id))[0]
    assert mention.invoked_execution_id == runs[0].execution_id
    started = [
        e
        for e in await service.get_room_events(room_id)
        if e.event_type.value == "agent.run.started"
    ]
    assert started[0].payload["triggered_by"] == "MENTION"
    assert started[0].payload["requested_by"] == "owner"


@pytest.mark.asyncio
async def test_a_direct_run_records_a_direct_trigger(service: MultiplayerService) -> None:
    room_id = await _room(service)
    template = (await service.list_agent_templates())[0]
    agent = await service.spawn_agent(room_id, template.template_id)
    session = await service.start_agent_session(room_id, agent.agent_id)
    execution = await service.start_execution(session.session_id, "owner")

    assert execution.triggered_by is AgentTrigger.DIRECT


@pytest.mark.asyncio
async def test_a_member_without_the_capability_cannot_invoke_and_nothing_is_written(
    service: MultiplayerService,
) -> None:
    room_id = await _room(service)
    template = (await service.list_agent_templates())[0]
    await service.spawn_agent(room_id, template.template_id, name="Architect")
    await service.invite_room_member(room_id, "restricted", "editor", "owner")
    # An empty grant means this member may lend an agent nothing at all.
    await service.set_member_capabilities(room_id, "restricted", [], "owner")

    before = len(await service.list_room_messages(room_id))
    with pytest.raises(AuthorizationError):
        await service.send_message(
            room_id,
            MessageRole.HUMAN,
            "restricted",
            "@Architect run this for me",
            invoke_mentioned_agents=True,
        )

    # The whole write rolled back: no turn, no message, no mention.
    assert await service.repos.executions.list_by_room(room_id) == []
    assert len(await service.list_room_messages(room_id)) == before


@pytest.mark.asyncio
async def test_the_same_member_may_still_mention_the_agent_without_invoking(
    service: MultiplayerService,
) -> None:
    room_id = await _room(service)
    template = (await service.list_agent_templates())[0]
    await service.spawn_agent(room_id, template.template_id, name="Architect")
    await service.invite_room_member(room_id, "restricted", "editor", "owner")
    await service.set_member_capabilities(room_id, "restricted", [], "owner")

    message = await service.send_message(
        room_id, MessageRole.HUMAN, "restricted", "@Architect for your awareness"
    )
    mentions = await service.list_message_mentions(message.message_id)

    assert len(mentions) == 1
    assert mentions[0].invoked_execution_id is None


# ── Reactions ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_reaction_is_added_removed_and_re_added_over_one_durable_row(
    service: MultiplayerService,
) -> None:
    room_id = await _room(service)
    message = await service.send_message(room_id, MessageRole.HUMAN, "owner", "Ship it")

    await service.add_reaction(message.message_id, "owner", "👍")
    assert [r.actor_id for r in await service.list_reactions(message.message_id)] == ["owner"]

    removed = await service.remove_reaction(message.message_id, "owner", "👍")
    assert removed.removed_at is not None
    assert await service.list_reactions(message.message_id) == []

    await service.add_reaction(message.message_id, "owner", "👍")
    live = await service.list_reactions(message.message_id)
    assert [r.actor_id for r in live] == ["owner"]
    assert live[0].removed_at is None

    # The row was never deleted; the soft removal is still the same primary key.
    rows = await service.db.fetch_all(
        "SELECT * FROM message_reactions WHERE message_id = ?", (message.message_id,)
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_repeating_a_reaction_is_idempotent_and_appends_no_event(
    service: MultiplayerService,
) -> None:
    room_id = await _room(service)
    message = await service.send_message(room_id, MessageRole.HUMAN, "owner", "Ship it")

    await service.add_reaction(message.message_id, "owner", "👍")
    after_first = await service.repos.events.get_latest_sequence(room_id)
    await service.add_reaction(message.message_id, "owner", "👍")

    assert await service.repos.events.get_latest_sequence(room_id) == after_first
    assert len(await service.list_reactions(message.message_id)) == 1

    await service.remove_reaction(message.message_id, "owner", "👍")
    after_removal = await service.repos.events.get_latest_sequence(room_id)
    await service.remove_reaction(message.message_id, "owner", "👍")
    assert await service.repos.events.get_latest_sequence(room_id) == after_removal


@pytest.mark.asyncio
async def test_removing_a_reaction_that_was_never_added_is_rejected(
    service: MultiplayerService,
) -> None:
    room_id = await _room(service)
    message = await service.send_message(room_id, MessageRole.HUMAN, "owner", "Ship it")

    with pytest.raises(DomainError):
        await service.remove_reaction(message.message_id, "owner", "👍")
    assert (
        await service.db.fetch_all(
            "SELECT * FROM message_reactions WHERE message_id = ?", (message.message_id,)
        )
        == []
    )


@pytest.mark.asyncio
async def test_a_non_member_cannot_react(service: MultiplayerService) -> None:
    room_id = await _room(service)
    message = await service.send_message(room_id, MessageRole.HUMAN, "owner", "Ship it")

    with pytest.raises(AuthorizationError):
        await service.add_reaction(message.message_id, "outsider", "👍")


# ── Read cursor ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_read_cursor_is_durable_and_survives_a_reconnect(
    service: MultiplayerService,
) -> None:
    room_id = await _room(service)
    await service.invite_room_member(room_id, "teammate", "editor", "owner")
    first = await service.send_message(room_id, MessageRole.HUMAN, "owner", "one")
    await service.send_message(room_id, MessageRole.HUMAN, "teammate", "two")

    await service.set_read_cursor(room_id, "owner", first.event_sequence)

    # A reconnect is a fresh service over the same durable database.
    reconnected = MultiplayerService(service.db, RealtimeHub())
    await reconnected.initialize()
    cursor = await reconnected.get_read_cursor(room_id, "owner")

    assert cursor["last_read_sequence"] == first.event_sequence
    assert cursor["unread_messages"] == 1
    assert cursor["latest_sequence"] >= first.event_sequence


@pytest.mark.asyncio
async def test_an_unset_cursor_reports_everything_unread(service: MultiplayerService) -> None:
    room_id = await _room(service)
    await service.invite_room_member(room_id, "teammate", "editor", "owner")
    await service.send_message(room_id, MessageRole.HUMAN, "teammate", "one")

    cursor = await service.get_read_cursor(room_id, "owner")
    assert cursor["last_read_sequence"] == 0
    assert cursor["unread_messages"] == 1


@pytest.mark.asyncio
async def test_a_cursor_cannot_pass_the_rooms_latest_sequence(
    service: MultiplayerService,
) -> None:
    room_id = await _room(service)
    latest = await service.repos.events.get_latest_sequence(room_id)

    with pytest.raises(DomainError):
        await service.set_read_cursor(room_id, "owner", latest + 5)


@pytest.mark.asyncio
async def test_a_non_member_cannot_set_a_read_cursor(service: MultiplayerService) -> None:
    room_id = await _room(service)

    with pytest.raises(AuthorizationError):
        await service.set_read_cursor(room_id, "outsider", 1)


# ── Mention invocation runs, and its answer lands in the conversation ────────


@pytest.mark.asyncio
async def test_an_invoked_mention_runs_and_its_answer_lands_in_the_thread(
    service: MultiplayerService,
) -> None:
    room_id = await _room(service)
    template = (await service.list_agent_templates())[0]
    agent = await service.spawn_agent(room_id, template.template_id, name="Architect")

    mention = await service.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "@Architect please assess this",
        invoke_mentioned_agents=True,
    )

    # The turn ran; it is not a PENDING row nobody will ever pick up.
    runs = await service.repos.executions.list_by_room(room_id)
    assert [run.status for run in runs] == [ExecutionStatus.COMPLETED]
    assert runs[0].triggered_by is AgentTrigger.MENTION

    # The mention's own text was the prompt.
    outputs = await service.list_room_outputs(room_id)
    assert len(outputs) == 1
    assert outputs[0].source_prompt == "@Architect please assess this"

    # The answer is a message in the mention's thread that points at the output.
    thread = await service.list_thread(mention.message_id)
    answer = thread[-1].message
    assert answer.role is MessageRole.AGENT
    assert answer.sender_id == agent.agent_id
    assert answer.parent_message_id == mention.message_id
    assert answer.root_message_id == mention.message_id
    assert answer.thread_depth == 1
    assert answer.metadata["output_id"] == outputs[0].output_id
    assert answer.metadata["execution_id"] == runs[0].execution_id

    # The output survives as the first-class record, not replaced by the message.
    assert await service.repos.agent_outputs.get(outputs[0].output_id) is not None


@pytest.mark.asyncio
async def test_a_mention_run_whose_dispatch_fails_is_settled_and_says_why(
    service: MultiplayerService,
) -> None:
    room_id = await _room(service)
    template = (await service.list_agent_templates())[0]
    await service.spawn_agent(room_id, template.template_id, name="Architect")

    async def exploding_step(execution_id: str, prompt: str) -> dict[str, Any]:
        raise RuntimeError("provider exploded")

    service.execute_agent_step = exploding_step  # type: ignore[method-assign]
    message = await service.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "@Architect please assess this",
        invoke_mentioned_agents=True,
    )

    # The mention is durable, and the run it opened reached a terminal state.
    assert (await service.list_message_mentions(message.message_id))[0].invoked_execution_id
    runs = await service.repos.executions.list_by_room(room_id)
    assert [run.status for run in runs] == [ExecutionStatus.FAILED]
    assert "provider exploded" in runs[0].error
    failures = [
        event
        for event in await service.get_room_events(room_id)
        if event.event_type is EventType.EXECUTION_FAILED
    ]
    assert [event.payload["execution_id"] for event in failures] == [runs[0].execution_id]
    assert failures[0].payload["triggered_by"] == "MENTION"


@pytest.mark.asyncio
async def test_a_mention_run_orphaned_by_a_crash_is_settled_at_the_next_startup(
    service: MultiplayerService,
) -> None:
    room_id = await _room(service)
    template = (await service.list_agent_templates())[0]
    await service.spawn_agent(room_id, template.template_id, name="Architect")

    async def never_dispatched(execution_id: str, prompt: str) -> None:
        # The process dies between the commit and the dispatch: the run is written
        # and nothing ever claims it, which is what makes it an orphan.
        return None

    service._dispatch_mention_run = never_dispatched  # type: ignore[method-assign]
    await service.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "@Architect please assess this",
        invoke_mentioned_agents=True,
    )
    orphan = (await service.repos.executions.list_by_room(room_id))[0]
    assert orphan.status is ExecutionStatus.PENDING

    restarted = MultiplayerService(service.db, RealtimeHub())
    await restarted.initialize()

    settled = (await restarted.repos.executions.list_by_room(room_id))[0]
    assert settled.status is ExecutionStatus.FAILED
    assert "dispatcher stopped" in settled.error
    assert any(
        event.event_type is EventType.EXECUTION_FAILED
        and event.payload["execution_id"] == orphan.execution_id
        for event in await restarted.get_room_events(room_id)
    )


# ── The flat channel ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_flat_channel_carries_top_level_and_broadcast_messages_only(
    service: MultiplayerService,
) -> None:
    room_id = await _room(service)
    root = await service.send_message(room_id, MessageRole.HUMAN, "owner", "Should we migrate?")
    quiet = await service.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "Detail for the thread only.",
        parent_message_id=root.message_id,
        broadcast_to_room=False,
    )
    loud = await service.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "Summary for the channel.",
        parent_message_id=root.message_id,
        broadcast_to_room=True,
    )

    # The rule is in the query, so every caller of the listing sees one channel.
    listed = await service.list_room_messages(room_id)
    assert [m.message_id for m in listed] == [root.message_id, loud.message_id]
    resumed = await service.list_room_messages(room_id, after_sequence=root.event_sequence)
    assert [m.message_id for m in resumed] == [loud.message_id]

    # The thread still holds everything.
    thread = await service.list_thread(root.message_id)
    assert {entry.message.message_id for entry in thread} == {
        root.message_id,
        quiet.message_id,
        loud.message_id,
    }


# ── Thread summaries ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_channel_counts_the_whole_thread_and_who_is_in_it(
    service: MultiplayerService,
) -> None:
    room_id = await _room(service)
    await service.invite_room_member(room_id, "teammate", "editor", "owner")
    root = await service.send_message(room_id, MessageRole.HUMAN, "owner", "Should we migrate?")

    # Two branches of six, so exactly two replies answer the root directly and the
    # thread holds twelve. A count grouped on the parent would say two.
    last = root
    for branch in range(2):
        parent = root
        for step in range(6):
            parent = await service.send_message(
                room_id,
                MessageRole.HUMAN,
                "teammate" if step % 2 else "owner",
                f"Branch {branch} step {step}",
                parent_message_id=parent.message_id,
            )
        last = parent

    summaries = await service.repos.messages.thread_summaries_by_room(room_id)
    summary = summaries[root.message_id]
    assert summary.descendant_count == 12
    assert summary.participant_count == 2
    assert summary.last_reply_at == last.created_at
    assert await service.repos.messages.count_replies(root.message_id) == 2

    state = await service.get_room_state(room_id, user_id="owner")
    record = next(m for m in state["messages"] if m["message_id"] == root.message_id)
    assert record["reply_count"] == 12
    assert record["participant_count"] == 2
    assert record["last_reply_at"] == last.created_at.isoformat()


@pytest.mark.asyncio
async def test_a_message_with_no_thread_summarises_as_its_author_alone(
    service: MultiplayerService,
) -> None:
    room_id = await _room(service)
    lonely = await service.send_message(room_id, MessageRole.HUMAN, "owner", "Nobody replied")

    assert await service.repos.messages.thread_summaries_by_room(room_id) == {}
    state = await service.get_room_state(room_id, user_id="owner")
    record = next(m for m in state["messages"] if m["message_id"] == lonely.message_id)
    assert record["reply_count"] == 0
    assert record["participant_count"] == 1
    assert record["last_reply_at"] is None


# ── Thread depth ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_thread_stops_nesting_at_the_depth_limit(service: MultiplayerService) -> None:
    room_id = await _room(service)
    root = await service.send_message(room_id, MessageRole.HUMAN, "owner", "Root")

    deepest = root
    for depth in range(1, MAX_THREAD_DEPTH + 1):
        deepest = await service.send_message(
            room_id,
            MessageRole.HUMAN,
            "owner",
            f"Depth {depth}",
            parent_message_id=deepest.message_id,
        )
        assert deepest.thread_depth == depth

    with pytest.raises(DomainError, match="thread depth limit"):
        await service.send_message(
            room_id,
            MessageRole.HUMAN,
            "owner",
            "One too deep",
            parent_message_id=deepest.message_id,
        )
    assert len(await service.list_thread(root.message_id)) == MAX_THREAD_DEPTH + 1


@pytest.mark.asyncio
async def test_a_mention_cannot_be_invoked_where_its_answer_would_not_fit(
    service: MultiplayerService,
) -> None:
    room_id = await _room(service)
    template = (await service.list_agent_templates())[0]
    await service.spawn_agent(room_id, template.template_id, name="Architect")
    deepest = await service.send_message(room_id, MessageRole.HUMAN, "owner", "Root")
    for depth in range(1, MAX_THREAD_DEPTH):
        deepest = await service.send_message(
            room_id,
            MessageRole.HUMAN,
            "owner",
            f"Depth {depth}",
            parent_message_id=deepest.message_id,
        )

    # The agent's answer is a reply one level below, and there is no level left.
    with pytest.raises(DomainError, match="thread depth limit"):
        await service.send_message(
            room_id,
            MessageRole.HUMAN,
            "owner",
            "@Architect assess this",
            parent_message_id=deepest.message_id,
            invoke_mentioned_agents=True,
        )
    assert await service.repos.executions.list_by_room(room_id) == []


@pytest.mark.asyncio
async def test_setting_a_read_cursor_appends_no_room_event(
    service: MultiplayerService,
) -> None:
    """A read position is one member's private state, so the shared log omits it."""
    room_id = await _room(service)
    await service.send_message(room_id, MessageRole.HUMAN, "owner", "one")
    latest = await service.repos.events.get_latest_sequence(room_id)

    await service.set_read_cursor(room_id, "owner", latest)

    assert await service.repos.events.get_latest_sequence(room_id) == latest


# ── Handles ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_agent_whose_name_has_a_space_is_addressable(
    service: MultiplayerService,
) -> None:
    """The shipped "Security Reviewer" template used to be unmentionable by any spelling."""
    room_id = await _room(service)
    template = next(
        t for t in await service.list_agent_templates() if t.name == "Security Reviewer"
    )
    agent = await service.spawn_agent(room_id, template.template_id)

    handle = await service.repos.handles.get_for_participant(
        room_id, ParticipantType.AGENT, agent.agent_id
    )
    assert handle is not None
    assert handle.handle == "security-reviewer"

    message = await service.send_message(
        room_id, MessageRole.HUMAN, "owner", "@security-reviewer please review"
    )
    mentions = await service.list_message_mentions(message.message_id)
    assert [(m.target_type, m.target_id) for m in mentions] == [
        (MentionTargetType.AGENT, agent.agent_id)
    ]


@pytest.mark.asyncio
async def test_two_participants_with_one_name_get_two_handles(
    service: MultiplayerService,
) -> None:
    room_id = await _room(service)
    template = (await service.list_agent_templates())[0]
    first = await service.spawn_agent(room_id, template.template_id, name="Architect")
    second = await service.spawn_agent(room_id, template.template_id, name="Architect")

    handles = {
        record.participant_id: record.handle
        for record in await service.repos.handles.list_by_room(room_id)
    }
    assert handles[first.agent_id] == "architect"
    assert handles[second.agent_id] == "architect-2"

    # Each handle addresses exactly one of them, so neither is unreachable.
    for agent_id, handle in ((first.agent_id, "architect"), (second.agent_id, "architect-2")):
        message = await service.send_message(room_id, MessageRole.HUMAN, "owner", f"@{handle} hi")
        assert [m.target_id for m in await service.list_message_mentions(message.message_id)] == [
            agent_id
        ]


@pytest.mark.asyncio
async def test_a_handle_survives_a_rename(service: MultiplayerService) -> None:
    """The address is durable: a display name is a label, not a way to reach somebody."""
    room_id = await _room(service)
    template = (await service.list_agent_templates())[0]
    agent = await service.spawn_agent(room_id, template.template_id, name="Architect")

    await service.db.execute(
        "UPDATE agent_instances SET name = ? WHERE agent_id = ?", ("Renamed", agent.agent_id)
    )
    await service.db.commit()

    message = await service.send_message(room_id, MessageRole.HUMAN, "owner", "@architect still?")
    assert [m.target_id for m in await service.list_message_mentions(message.message_id)] == [
        agent.agent_id
    ]


@pytest.mark.asyncio
async def test_participants_that_predate_handles_are_backfilled(
    service: MultiplayerService,
) -> None:
    room_id = await _room(service)
    template = (await service.list_agent_templates())[0]
    agent = await service.spawn_agent(room_id, template.template_id, name="Security Reviewer")

    # An upgrade from a database written before migration 015 has the rows and no
    # handles; the backfill has to address everybody it finds, not only new joiners.
    await service.db.execute("DELETE FROM room_participant_handles")
    await service.db.commit()
    assert await service.repos.handles.list_by_room(room_id) == []

    await MultiplayerService(service.db, RealtimeHub()).initialize()

    handles = {
        record.participant_id: record.handle
        for record in await service.repos.handles.list_by_room(room_id)
    }
    assert handles == {"owner": "owner", agent.agent_id: "security-reviewer"}


@pytest.mark.asyncio
async def test_an_unrecognized_handle_is_reported_rather_than_swallowed(
    service: MultiplayerService,
) -> None:
    """Silence was the bug: the author waited for an answer nobody was asked for."""
    room_id = await _room(service)
    template = (await service.list_agent_templates())[0]
    await service.spawn_agent(room_id, template.template_id, name="Architect")

    content = "@architect and @nobody, also @Architekt?"
    message = await service.send_message(room_id, MessageRole.HUMAN, "owner", content)

    assert len(await service.list_message_mentions(message.message_id)) == 1
    assert await service.unrecognized_mention_handles(room_id, content) == [
        "nobody",
        "Architekt",
    ]
    # A message that addressed everybody it named reports nothing.
    assert await service.unrecognized_mention_handles(room_id, "@architect only") == []


# ── Agent reactions ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_agent_in_the_room_can_react(service: MultiplayerService) -> None:
    """👀 is the cheapest way for an agent to say it has the message."""
    room_id = await _room(service)
    template = (await service.list_agent_templates())[0]
    agent = await service.spawn_agent(room_id, template.template_id, name="Architect")
    message = await service.send_message(room_id, MessageRole.HUMAN, "owner", "Ship it")

    reaction = await service.add_agent_reaction(message.message_id, agent.agent_id, "👀")
    assert reaction.actor_type is ParticipantType.AGENT

    live = await service.list_reactions(message.message_id)
    assert [(r.actor_id, r.actor_type) for r in live] == [(agent.agent_id, ParticipantType.AGENT)]

    # The room event says an agent acted, not a user.
    events = await service.get_room_events(room_id)
    reacted = [e for e in events if e.event_type is EventType.MESSAGE_REACTION_ADDED]
    assert [(e.actor_id, e.actor_type) for e in reacted] == [(agent.agent_id, "agent")]

    await service.remove_agent_reaction(message.message_id, agent.agent_id, "👀")
    assert await service.list_reactions(message.message_id) == []


@pytest.mark.asyncio
async def test_an_agent_from_another_room_cannot_react(service: MultiplayerService) -> None:
    """Room isolation holds for agents exactly as it does for members."""
    room_id = await _room(service)
    other_room = await service.create_room(
        (await service.get_room(room_id)).workspace_id, "Elsewhere", "owner"
    )
    template = (await service.list_agent_templates())[0]
    outsider = await service.spawn_agent(other_room.room_id, template.template_id, name="Nosy")
    message = await service.send_message(room_id, MessageRole.HUMAN, "owner", "Ship it")

    with pytest.raises(AuthorizationError):
        await service.add_agent_reaction(message.message_id, outsider.agent_id, "👀")
    assert await service.list_reactions(message.message_id) == []


@pytest.mark.asyncio
async def test_a_member_cannot_react_under_an_agents_identity(
    service: MultiplayerService,
) -> None:
    """The member-facing path never grants agent attribution, whatever id it is given."""
    room_id = await _room(service)
    template = (await service.list_agent_templates())[0]
    agent = await service.spawn_agent(room_id, template.template_id, name="Architect")
    message = await service.send_message(room_id, MessageRole.HUMAN, "owner", "Ship it")

    # An agent id is not a room member, so the member path denies it outright.
    with pytest.raises(AuthorizationError):
        await service.add_reaction(message.message_id, agent.agent_id, "👀")
    assert await service.list_reactions(message.message_id) == []


# ── The unread pill counts what the channel shows ────────────────────────────


@pytest.mark.asyncio
async def test_unread_counts_only_the_messages_the_channel_displays(
    service: MultiplayerService,
) -> None:
    """Reproduces "6 unread" over a three-message channel."""
    room_id = await _room(service)
    await service.invite_room_member(room_id, "teammate", "editor", "owner")
    root = await service.send_message(room_id, MessageRole.HUMAN, "teammate", "Should we migrate?")
    for index in range(3):
        await service.send_message(
            room_id,
            MessageRole.HUMAN,
            "teammate",
            f"Thread detail {index}",
            parent_message_id=root.message_id,
            broadcast_to_room=False,
        )
    await service.send_message(
        room_id,
        MessageRole.HUMAN,
        "teammate",
        "Summary for the channel.",
        parent_message_id=root.message_id,
        broadcast_to_room=True,
    )
    await service.send_message(room_id, MessageRole.HUMAN, "owner", "My own message.")

    displayed = await service.list_room_messages(room_id)
    cursor = await service.get_read_cursor(room_id, "owner")

    # Two: the root and the broadcast reply. Not the three quiet thread replies the
    # reader is never shown, and not the reader's own message.
    assert cursor["unread_messages"] == 2
    assert cursor["unread_messages"] == len([m for m in displayed if m.sender_id != "owner"])


@pytest.mark.asyncio
async def test_your_own_message_is_never_unread_to_you(service: MultiplayerService) -> None:
    room_id = await _room(service)
    await service.send_message(room_id, MessageRole.HUMAN, "owner", "one")
    await service.send_message(room_id, MessageRole.HUMAN, "owner", "two")

    assert (await service.get_read_cursor(room_id, "owner"))["unread_messages"] == 0


# ── A reply shown in the channel says what it is ─────────────────────────────


@pytest.mark.asyncio
async def test_a_broadcast_reply_is_labelled_as_a_reply_in_its_thread(
    service: MultiplayerService,
) -> None:
    """Summaries are keyed on roots, so a broadcast reply used to render as "Reply"."""
    room_id = await _room(service)
    root = await service.send_message(room_id, MessageRole.HUMAN, "owner", "Should we migrate?")
    quiet = await service.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "Detail for the thread only.",
        parent_message_id=root.message_id,
        broadcast_to_room=False,
    )
    loud = await service.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "Summary for the channel.",
        parent_message_id=root.message_id,
        broadcast_to_room=True,
    )

    state = await service.get_room_state(room_id, user_id="owner")
    by_id = {m["message_id"]: m for m in state["messages"]}

    reply = by_id[loud.message_id]
    assert reply["is_thread_reply"] is True
    assert reply["thread_root_id"] == root.message_id
    # It describes the conversation it came out of, which holds both replies.
    assert reply["reply_count"] == 2
    assert reply["last_reply_at"] == loud.created_at.isoformat()

    parent = by_id[root.message_id]
    assert parent["is_thread_reply"] is False
    assert parent["thread_root_id"] == root.message_id
    assert quiet.message_id not in by_id


# ── The agent message points at its output ───────────────────────────────────


@pytest.mark.asyncio
async def test_the_agent_message_references_its_output_instead_of_copying_it(
    service: MultiplayerService,
) -> None:
    room_id = await _room(service)
    template = (await service.list_agent_templates())[0]
    agent = await service.spawn_agent(room_id, template.template_id, name="Architect")

    mention = await service.send_message(
        room_id,
        MessageRole.HUMAN,
        "owner",
        "@architect please assess this",
        invoke_mentioned_agents=True,
    )

    output = (await service.list_room_outputs(room_id))[0]
    answer = (await service.list_thread(mention.message_id))[-1].message
    assert answer.sender_id == agent.agent_id

    # The record keeps the content; the message keeps a pointer and enough to read.
    assert answer.metadata["output_id"] == output.output_id
    assert answer.metadata["requested_by"] == "owner"
    assert answer.metadata["triggered_by"] == AgentTrigger.MENTION.value
    assert answer.content
    assert len(answer.content) <= len(output.content)
    if answer.metadata["output_excerpted"]:
        assert len(answer.content) < len(output.content)
