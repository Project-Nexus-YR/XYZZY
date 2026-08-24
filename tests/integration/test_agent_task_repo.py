"""Integration tests for the agent task repository."""

import asyncio
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from multiplayer.db.connection import Database
from multiplayer.db.repositories import Repos
from multiplayer.domain.agent_tasks import (
    TERMINAL_STATES,
    AgentTask,
    AgentTaskState,
    DelegationCycleError,
    Part,
    PartKind,
    TaskMessageRole,
    new_context_id,
)
from multiplayer.domain.models import DomainError, new_id, utcnow

# Deliberately not in alphabetical order, and not in reverse either. A chain
# whose ids happen to sort the way they were delegated cannot tell "root first"
# apart from "sorted", which is the whole claim the ancestry test makes.
CHAIN = ("zeta", "alpha", "mid")


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.connect()
    for migration in sorted(Path("src/multiplayer/migrations").glob("*.sql")):
        await database.execute_script(migration.read_text())
    yield database
    await database.close()


@pytest.fixture
async def repo(db):
    return Repos(db).agent_tasks


def make_task(**overrides) -> AgentTask:
    defaults = {
        "task_id": new_id("a2atask"),
        "context_id": new_context_id(),
        "room_id": "r1",
        "target_agent_id": "reviewer",
        "authorized_by": "u1",
    }
    return AgentTask(**{**defaults, **overrides})


async def drive_to(repo, task_id: str, terminal: AgentTaskState) -> None:
    """Walk a fresh task to one terminal state along legal edges only.

    Every guard that closes over TERMINAL_STATES is exercised against all four,
    because a guard listing three of them behaves correctly on every test that
    only ever reaches those three.
    """
    if terminal in (AgentTaskState.COMPLETED, AgentTaskState.FAILED):
        await repo.transition(task_id, AgentTaskState.SUBMITTED, AgentTaskState.WORKING)
        await repo.transition(task_id, AgentTaskState.WORKING, terminal)
        return
    await repo.transition(task_id, AgentTaskState.SUBMITTED, terminal)


def text(content: str) -> tuple[Part, ...]:
    return (Part(kind=PartKind.TEXT, content=content),)


async def test_create_and_get_round_trip_every_field(repo):
    created = utcnow()
    task = make_task(
        delegating_agent_id="planner",
        delegating_run_id="run1",
        execution_id="exec1",
        state=AgentTaskState.WORKING,
        accepted_output_modes=("text/plain", "application/json"),
        depth=2,
        created_at=created,
        updated_at=created + timedelta(seconds=5),
        terminal_at=None,
        refusal_reason="",
    )

    await repo.create(task, ("root", "planner"))

    assert await repo.get(task.task_id) == task


async def test_accepted_output_modes_survive_as_a_tuple(repo):
    task = make_task(accepted_output_modes=("text/plain",))
    await repo.create(task, ())

    stored = await repo.get(task.task_id)

    assert stored.accepted_output_modes == ("text/plain",)


async def test_get_is_none_for_a_task_that_was_never_written(repo):
    assert await repo.get("a2atask_nope") is None


async def test_depth_is_counted_from_the_chain_not_taken_from_the_caller(repo):
    # The caller claims a task nobody delegated. The chain says three agents did.
    task = make_task(depth=0, delegating_agent_id="mid")

    returned = await repo.create(task, CHAIN)

    assert returned.depth == len(CHAIN)
    stored = await repo.get(task.task_id)
    assert stored.depth == len(CHAIN)
    assert stored.opened_by_a_human is False


async def test_ancestry_comes_back_in_position_order(repo):
    task = make_task()
    await repo.create(task, CHAIN)

    assert await repo.ancestry(task.task_id) == CHAIN
    # Naming the two orders it must not be mistaken for, so the assertion above
    # cannot pass by coincidence if the sort key drifts to the agent id.
    assert CHAIN != tuple(sorted(CHAIN))
    assert CHAIN != tuple(sorted(CHAIN, reverse=True))


async def test_ancestry_sorts_by_position_not_by_the_order_rows_were_written(repo, db):
    """Written middle-first, so physical order and delegation order disagree.

    Every chain that goes through create() is inserted in position order, which
    makes ORDER BY position and ORDER BY rowid indistinguishable. Writing the
    rows directly, out of order, is the only way to tell them apart.
    """
    task = make_task()
    await repo.create(task, ())
    for position, agent_id in ((2, "mid"), (0, "zeta"), (1, "alpha")):
        await db.execute(
            "INSERT INTO agent_task_chain(task_id, position, agent_id) VALUES (?, ?, ?)",
            (task.task_id, position, agent_id),
        )
    await db.commit()

    assert await repo.ancestry(task.task_id) == CHAIN


async def test_a_repeated_agent_in_the_chain_is_refused_by_name(repo):
    task = make_task()

    with pytest.raises(DelegationCycleError) as refusal:
        await repo.create(task, ("planner", "reviewer", "planner"))

    assert "planner" in str(refusal.value)
    # The refusal has to land before anything is written, or a rolled-back task
    # would still be the difference between a cycle and a half-written one.
    assert await repo.get(task.task_id) is None
    assert await repo.ancestry(task.task_id) == ()


async def test_the_chain_table_itself_cannot_hold_an_agent_twice(repo, db):
    task = make_task()
    await repo.create(task, ("planner",))

    with pytest.raises(sqlite3.IntegrityError):
        await db.execute(
            "INSERT INTO agent_task_chain(task_id, position, agent_id) VALUES (?, ?, ?)",
            (task.task_id, 1, "planner"),
        )


async def test_a_human_opened_task_has_no_ancestry(repo):
    task = make_task()
    await repo.create(task, ())

    assert await repo.ancestry(task.task_id) == ()


async def test_list_by_context_is_oldest_first(repo):
    context = new_context_id()
    now = utcnow()
    # The older task sorts last by id, so a sort that lost created_at would
    # return these the wrong way round.
    older = make_task(
        task_id="a2atask_z", context_id=context, created_at=now - timedelta(minutes=1)
    )
    newer = make_task(task_id="a2atask_a", context_id=context, created_at=now)
    await repo.create(newer, ())
    await repo.create(older, ())

    listed = await repo.list_by_context(context)

    assert [t.task_id for t in listed] == ["a2atask_z", "a2atask_a"]


async def test_tasks_sharing_a_timestamp_come_back_in_a_stable_order(repo):
    context = new_context_id()
    now = utcnow()
    for task_id in ("a2atask_m", "a2atask_t", "a2atask_b"):
        await repo.create(make_task(task_id=task_id, context_id=context, created_at=now), ())

    listed = await repo.list_by_context(context)

    assert [t.task_id for t in listed] == ["a2atask_b", "a2atask_m", "a2atask_t"]


async def test_transition_moves_the_task_and_returns_what_it_wrote(repo):
    task = make_task()
    await repo.create(task, ())

    moved = await repo.transition(task.task_id, AgentTaskState.SUBMITTED, AgentTaskState.WORKING)

    assert moved.state is AgentTaskState.WORKING
    assert moved.updated_at > task.updated_at
    assert moved == await repo.get(task.task_id)


async def test_transition_does_not_read_the_row_back_after_writing_it(repo, monkeypatch):
    """The returned task must come from the statement that wrote it.

    A second read is a second chance for somebody else's transition to be the
    one the caller receives, so the test refuses to let one happen at all.
    """
    task = make_task()
    await repo.create(task, ())

    async def forbidden(*args, **kwargs):
        raise AssertionError("transition must not re-read; the row may have moved by then")

    monkeypatch.setattr(repo, "get", forbidden)

    moved = await repo.transition(task.task_id, AgentTaskState.SUBMITTED, AgentTaskState.WORKING)

    assert moved.state is AgentTaskState.WORKING


async def test_repeating_a_transition_is_refused_by_the_guard(repo):
    task = make_task()
    await repo.create(task, ())
    await repo.transition(task.task_id, AgentTaskState.SUBMITTED, AgentTaskState.WORKING)

    # SUBMITTED -> WORKING is a legal edge, so only the WHERE clause can refuse
    # the second caller: it read a state the first caller has already moved on.
    with pytest.raises(DomainError):
        await repo.transition(task.task_id, AgentTaskState.SUBMITTED, AgentTaskState.WORKING)


async def test_an_illegal_target_is_refused_before_the_row_is_touched(repo):
    task = make_task()
    await repo.create(task, ())

    with pytest.raises(DomainError):
        await repo.transition(task.task_id, AgentTaskState.SUBMITTED, AgentTaskState.COMPLETED)

    assert await repo.get(task.task_id) == task


async def test_a_terminal_transition_stamps_terminal_at_and_keeps_the_reason(repo):
    task = make_task()
    await repo.create(task, ())

    refused = await repo.transition(
        task.task_id,
        AgentTaskState.SUBMITTED,
        AgentTaskState.REJECTED,
        refusal_reason="the asker may not spend that",
    )

    assert refused.terminal_at is not None
    assert refused.refusal_reason == "the asker may not spend that"
    stored = await repo.get(task.task_id)
    assert stored.refusal_reason == "the asker may not spend that"
    assert stored.terminal_at == refused.terminal_at


async def test_a_non_terminal_transition_leaves_terminal_at_unset(repo):
    task = make_task()
    await repo.create(task, ())

    working = await repo.transition(task.task_id, AgentTaskState.SUBMITTED, AgentTaskState.WORKING)

    assert working.terminal_at is None
    assert (await repo.get(task.task_id)).terminal_at is None


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES))
async def test_list_open_for_agent_excludes_every_terminal_state(repo, terminal):
    now = utcnow()
    open_task = make_task(task_id="a2atask_open", target_agent_id="reviewer", created_at=now)
    done_task = make_task(task_id="a2atask_done", target_agent_id="reviewer", created_at=now)
    other_agent = make_task(task_id="a2atask_other", target_agent_id="planner", created_at=now)
    for task in (open_task, done_task, other_agent):
        await repo.create(task, ())
    await drive_to(repo, done_task.task_id, terminal)

    listed = await repo.list_open_for_agent("reviewer")

    assert [t.task_id for t in listed] == ["a2atask_open"]


async def test_list_open_for_agent_is_stable_when_tasks_share_a_timestamp(repo):
    now = utcnow()
    for task_id in ("a2atask_m", "a2atask_t", "a2atask_b"):
        await repo.create(make_task(task_id=task_id, target_agent_id="fanout", created_at=now), ())

    listed = await repo.list_open_for_agent("fanout")

    assert [t.task_id for t in listed] == ["a2atask_b", "a2atask_m", "a2atask_t"]


async def test_attach_execution_records_the_run_on_that_task_alone(repo):
    task = make_task()
    bystander = make_task()
    await repo.create(task, ())
    await repo.create(bystander, ())

    await repo.attach_execution(task.task_id, "exec1")

    assert (await repo.get(task.task_id)).execution_id == "exec1"
    assert (await repo.get(bystander.task_id)).execution_id is None


async def test_attach_execution_refuses_a_task_that_was_never_written(repo):
    with pytest.raises(DomainError):
        await repo.attach_execution("a2atask_nope", "exec1")


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES))
async def test_attach_execution_refuses_every_terminal_state(repo, terminal):
    task = make_task()
    await repo.create(task, ())
    await drive_to(repo, task.task_id, terminal)

    with pytest.raises(DomainError):
        await repo.attach_execution(task.task_id, "exec1")

    assert (await repo.get(task.task_id)).execution_id is None


async def test_messages_come_back_in_sequence_order(repo):
    task = make_task()
    await repo.create(task, ())
    await repo.append_message_with_next_sequence(
        task.task_id, TaskMessageRole.ASKER, (Part(kind=PartKind.TEXT, content="review this"),)
    )
    await repo.append_message_with_next_sequence(
        task.task_id,
        TaskMessageRole.DELEGATE,
        (Part(kind=PartKind.URL, content="https://example.test/diff", media_type="text/uri-list"),),
    )

    messages = await repo.list_messages(task.task_id)

    assert [m.sequence for m in messages] == [1, 2]
    assert [m.role for m in messages] == [TaskMessageRole.ASKER, TaskMessageRole.DELEGATE]
    assert messages[1].parts == (
        Part(kind=PartKind.URL, content="https://example.test/diff", media_type="text/uri-list"),
    )


async def test_sequences_are_counted_per_task_not_across_the_database(repo):
    """Two tasks, interleaved, the second starting after the first has messages.

    A counter that reads the maximum across every task instead of across this
    one still produces 1, 2, 3 for a suite that only ever writes to a single
    task. Only a second task can tell the two apart.
    """
    first = make_task()
    second = make_task()
    await repo.create(first, ())
    await repo.create(second, ())

    await repo.append_message_with_next_sequence(first.task_id, TaskMessageRole.ASKER, text("f1"))
    await repo.append_message_with_next_sequence(
        first.task_id, TaskMessageRole.DELEGATE, text("f2")
    )
    opened = await repo.append_message_with_next_sequence(
        second.task_id, TaskMessageRole.ASKER, text("s1")
    )
    answered = await repo.append_message_with_next_sequence(
        second.task_id, TaskMessageRole.DELEGATE, text("s2")
    )
    resumed = await repo.append_message_with_next_sequence(
        first.task_id, TaskMessageRole.ASKER, text("f3")
    )

    assert [opened.sequence, answered.sequence] == [1, 2]
    assert resumed.sequence == 3
    assert [m.parts[0].content for m in await repo.list_messages(first.task_id)] == [
        "f1",
        "f2",
        "f3",
    ]
    assert [m.parts[0].content for m in await repo.list_messages(second.task_id)] == ["s1", "s2"]


async def test_concurrent_appends_take_distinct_sequences(repo):
    task = make_task()
    await repo.create(task, ())

    appended = await asyncio.gather(
        repo.append_message_with_next_sequence(
            task.task_id, TaskMessageRole.ASKER, (Part(kind=PartKind.TEXT, content="first"),)
        ),
        repo.append_message_with_next_sequence(
            task.task_id, TaskMessageRole.ASKER, (Part(kind=PartKind.TEXT, content="second"),)
        ),
    )

    assert sorted(m.sequence for m in appended) == [1, 2]
    assert [m.sequence for m in await repo.list_messages(task.task_id)] == [1, 2]


async def test_the_in_transaction_variants_refuse_a_caller_that_owns_nothing(repo):
    task = make_task()

    with pytest.raises(RuntimeError):
        await repo.create_in_transaction(task, ())
    with pytest.raises(RuntimeError):
        await repo.append_message_with_next_sequence_in_transaction(
            task.task_id, TaskMessageRole.ASKER, ()
        )
