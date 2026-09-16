"""A2A task state follows the durable run across approval and cancellation."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest

from multiplayer.db.repositories import AgentTaskRepo, EventRepo
from multiplayer.domain.agent_tasks import AgentTaskState, Part, PartKind, TaskNotFoundError
from multiplayer.domain.events import EventType
from multiplayer.domain.models import HarnessState, RunSettlement, utcnow
from tests.security.test_model_tool_gateway import _room, _service, _ToolChoosingTransport


@pytest.fixture
async def task_case():
    transport = _ToolChoosingTransport("artifact.write", {"name": "Reviewed note"})
    svc = await _service(transport)
    room_id = await _room(svc)
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(
        room_id,
        next(t.template_id for t in templates if t.name == "Synthesizer"),
        requested_by="owner",
    )
    task = await svc.open_agent_task(
        room_id,
        agent.agent_id,
        (Part(kind=PartKind.TEXT, content="Write the reviewed note."),),
        requested_by="owner",
    )
    yield svc, transport, task
    await svc.db.close()


async def _pause(case):
    svc, _, task = case
    await svc._dispatch_agent_task_run(task)
    task = await svc.get_agent_task(task.task_id, viewer_id="owner")
    pending = await svc.list_pending_approvals(task.room_id)
    assert len(pending) == 1
    return task, pending[0]


async def _states(svc, task):
    return [
        event.payload["state"]
        for event in await svc.get_room_events(task.room_id)
        if event.event_type is EventType.TASK_DELEGATED
        and event.payload.get("task_id") == task.task_id
    ]


@pytest.mark.asyncio
async def test_approval_pause_keeps_task_alive_and_success_publishes_one_answer(task_case):
    svc, _, _ = task_case
    task, approval = await _pause(task_case)
    assert task.state is AgentTaskState.AUTH_REQUIRED
    assert task.terminal_at is None

    await svc.approve_action(approval.approval_id, "owner", require_member=True)

    done = await svc.get_agent_task(task.task_id, viewer_id="owner")
    assert done.state is AgentTaskState.COMPLETED
    assert done.terminal_at is not None
    messages = await svc.list_agent_task_messages(task.task_id, viewer_id="owner")
    assert len(messages) == 2
    assert messages[-1].parts[0].content == "answered using artifact.write"
    assert await _states(svc, task) == [
        "submitted",
        "working",
        "auth-required",
        "working",
        "completed",
    ]
    assert await svc.sweep_stranded_working_agent_tasks() == 0
    assert len(await svc.list_room_artifacts(task.room_id)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("continue_turn", [False, True])
async def test_approval_rejection_settles_or_resumes_the_task(task_case, continue_turn):
    svc, _, _ = task_case
    task, approval = await _pause(task_case)
    await svc.reject_action(
        approval.approval_id, "owner", require_member=True, continue_turn=continue_turn
    )
    done = await svc.get_agent_task(task.task_id, viewer_id="owner")
    assert done.state is (AgentTaskState.COMPLETED if continue_turn else AgentTaskState.REJECTED)
    assert await svc.list_room_artifacts(task.room_id) == []
    assert (await _states(svc, task))[-1] == done.state.value


@pytest.mark.asyncio
async def test_approval_expiry_fails_task_without_waiting_for_another_sweep(task_case):
    svc, _, _ = task_case
    task, _ = await _pause(task_case)
    await svc.db.execute(
        "UPDATE agent_runs SET lease_expires_at = ? WHERE execution_id = ?",
        ((utcnow() - timedelta(seconds=1)).isoformat(), task.execution_id),
    )
    await svc.db.commit()
    assert await svc.sweep_expired_run_leases() == 1
    done = await svc.get_agent_task(task.task_id, viewer_id="owner")
    assert done.state is AgentTaskState.FAILED
    assert "APPROVAL_EXPIRED" in done.refusal_reason
    assert await svc.list_pending_approvals(task.room_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_task", [False, True])
async def test_cancel_closes_task_run_and_pending_approval(task_case, cancel_task):
    svc, _, _ = task_case
    task, _ = await _pause(task_case)
    if cancel_task:
        await svc.cancel_agent_task(task.task_id, requested_by="owner")
    else:
        await svc.cancel_execution(task.execution_id, "owner", require_member=True)
    done = await svc.get_agent_task(task.task_id, viewer_id="owner")
    run = await svc.repos.agent_runs.get_by_execution(task.execution_id)
    assert done.state is AgentTaskState.CANCELED
    assert run.harness_state is HarnessState.SETTLED
    assert run.settlement is RunSettlement.CANCELLED
    assert await svc.list_pending_approvals(task.room_id) == []
    assert await svc.repos.suspended_turns.claim(task.execution_id) is None
    assert await svc.list_room_artifacts(task.room_id) == []


@pytest.mark.asyncio
async def test_cancel_during_provider_call_prevents_late_output(task_case):
    svc, transport, task = task_case
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()

    transport.before_answering = hold
    running = asyncio.create_task(svc._dispatch_agent_task_run(task))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        cancelled = await svc.cancel_agent_task(task.task_id, requested_by="owner")
        assert cancelled.state is AgentTaskState.CANCELED
        release.set()
        await running
        run = await svc.repos.agent_runs.get_by_execution(cancelled.execution_id)
        assert run.settlement is RunSettlement.CANCELLED
        assert await svc.list_pending_approvals(task.room_id) == []
        assert await svc.repos.agent_outputs.list_by_room(task.room_id) == []
    finally:
        release.set()
        await running


@pytest.mark.asyncio
async def test_timeout_after_approval_fails_both_records(task_case):
    svc, transport, _ = task_case
    task, approval = await _pause(task_case)

    async def timeout():
        raise httpx.ReadTimeout("test provider timed out")

    transport.before_answering = timeout
    await svc.approve_action(approval.approval_id, "owner", require_member=True)
    done = await svc.get_agent_task(task.task_id, viewer_id="owner")
    run = await svc.repos.agent_runs.get_by_execution(task.execution_id)
    assert done.state is AgentTaskState.FAILED
    assert run.settlement is RunSettlement.FAILED
    assert (await _states(svc, task))[-1] == "failed"


@pytest.mark.asyncio
async def test_task_completion_event_failure_rolls_back_run_output_and_answer(
    task_case, monkeypatch
):
    svc, _, task = task_case
    started = await svc.start_agent_task(task.task_id)
    original = EventRepo.append_with_next_sequence_in_transaction

    async def fail_completion(self, event):
        if (
            event.event_type is EventType.TASK_DELEGATED
            and event.payload.get("state") == "completed"
        ):
            raise RuntimeError("injected task event failure")
        return await original(self, event)

    monkeypatch.setattr(EventRepo, "append_with_next_sequence_in_transaction", fail_completion)
    # Skip the tool request so the first provider answer finishes the task.
    task_case[1].requests.append({})
    with pytest.raises(RuntimeError, match="injected task event failure"):
        await svc.execute_agent_step(started.execution_id, "Answer now.", "owner")
    current = await svc.get_agent_task(task.task_id, viewer_id="owner")
    run = await svc.repos.agent_runs.get_by_execution(started.execution_id)
    assert current.state is AgentTaskState.WORKING
    assert run.harness_state is HarnessState.STREAMING
    assert len(await svc.list_agent_task_messages(task.task_id, viewer_id="owner")) == 1
    assert await svc.repos.agent_outputs.list_by_room(task.room_id) == []
    assert "completed" not in await _states(svc, task)


@pytest.mark.asyncio
async def test_old_execution_settlement_does_not_complete_newer_task_run(task_case):
    svc, _, task = task_case
    first = await svc.start_agent_task(task.task_id)
    await svc.require_agent_task_input(
        task.task_id,
        (Part(kind=PartKind.TEXT, content="More detail?"),),
        by_agent_id=task.target_agent_id,
    )
    second = await svc.start_agent_task(task.task_id)
    old_run = await svc.repos.agent_runs.get_by_execution(first.execution_id)
    await svc._settle_run(old_run, RunSettlement.FAILED, "system", "old run ended")
    current = await svc.get_agent_task(task.task_id, viewer_id="owner")
    assert current.execution_id == second.execution_id
    assert current.state is AgentTaskState.WORKING
    assert (await _states(svc, task))[-1] == "working"


@pytest.mark.asyncio
async def test_recovery_preserves_answer_committed_by_an_older_process(task_case, monkeypatch):
    svc, transport, task = task_case
    started = await svc.start_agent_task(task.task_id)
    transport.requests.append({})

    async def old_settlement(*args, **kwargs):
        return []

    with monkeypatch.context() as patch:
        patch.setattr(AgentTaskRepo, "settle_for_execution_in_transaction", old_settlement)
        await svc.execute_agent_step(started.execution_id, "Answer now.", "owner")
    assert (
        await svc.get_agent_task(task.task_id, viewer_id="owner")
    ).state is AgentTaskState.WORKING

    assert await svc.sweep_stranded_working_agent_tasks() == 1
    assert await svc.sweep_stranded_working_agent_tasks() == 0
    done = await svc.get_agent_task(task.task_id, viewer_id="owner")
    assert done.state is AgentTaskState.COMPLETED
    messages = await svc.list_agent_task_messages(task.task_id, viewer_id="owner")
    assert len(messages) == 2
    assert messages[-1].parts[0].content == "answered using artifact.write"
    assert (await _states(svc, task))[-1] == "completed"


@pytest.mark.asyncio
async def test_cancel_keeps_completed_and_unknown_tasks_indistinguishable_to_stranger(task_case):
    svc, _, _ = task_case
    task, approval = await _pause(task_case)
    await svc.approve_action(approval.approval_id, "owner", require_member=True)
    errors = []
    for task_id in (task.task_id, "absent-task"):
        with pytest.raises(TaskNotFoundError) as exc:
            await svc.cancel_agent_task(task_id, requested_by="stranger")
        errors.append(str(exc.value))
    assert errors[0] == errors[1]
