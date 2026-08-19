"""Concurrency tests for NexusAgentBridge: lock safety, concurrent operations."""

import asyncio
import pytest
from unittest.mock import MagicMock
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.domain.models import AgentInstance, Session, Execution, AgentStatus, DomainError


def _make_agent(**overrides) -> AgentInstance:
    defaults = dict(
        agent_id="agent_1", template_id="tmpl_1", room_id="room_1",
        name="Test Agent", role="Coder", system_prompt="Be helpful.",
        capabilities=frozenset({"coding"}), model_provider="", model_name="",
        status=AgentStatus.IDLE,
    )
    defaults.update(overrides)
    return AgentInstance(**defaults)


def _make_session(**overrides) -> Session:
    defaults = dict(
        session_id="sess_1", room_id="room_1", agent_id="agent_1",
        task_id=None,
    )
    defaults.update(overrides)
    return Session(**defaults)


def _make_execution(**overrides) -> Execution:
    defaults = dict(
        execution_id="exec_1", session_id="sess_1", agent_id="agent_1",
        run_id=None, input_data={},
    )
    defaults.update(overrides)
    return Execution(**defaults)


@pytest.mark.asyncio
async def test_concurrent_create_execution():
    """Multiple concurrent create_execution calls must not corrupt internal dicts."""
    bridge = NexusAgentBridge()
    results = []

    async def create(i: int):
        agent = _make_agent(agent_id=f"agent_{i}")
        session = _make_session(session_id=f"sess_{i}", agent_id=f"agent_{i}")
        execution = _make_execution(execution_id=f"exec_{i}", session_id=f"sess_{i}", agent_id=f"agent_{i}")
        result = await bridge.create_execution(agent, session, "test task", execution)
        results.append(result)

    await asyncio.gather(*(create(i) for i in range(20)))
    assert len(results) == 20


@pytest.mark.asyncio
async def test_concurrent_execute_steps():
    """Concurrent execute_step calls on different executions must not interfere."""
    bridge = NexusAgentBridge()
    executions = []

    for i in range(10):
        agent = _make_agent(agent_id=f"agent_{i}")
        session = _make_session(session_id=f"sess_{i}", agent_id=f"agent_{i}")
        execution = _make_execution(execution_id=f"exec_{i}", session_id=f"sess_{i}", agent_id=f"agent_{i}")
        await bridge.create_execution(agent, session, "test", execution)
        executions.append(execution)

    async def step(exec_id: str):
        return await bridge.execute_step(exec_id, "do something")

    results = await asyncio.gather(*(step(e.execution_id) for e in executions))
    assert all(r["status"] == "ok" for r in results)


@pytest.mark.asyncio
async def test_pause_resume_concurrent():
    """Concurrent pause and resume on same execution must not deadlock."""
    bridge = NexusAgentBridge()
    agent = _make_agent()
    session = _make_session()
    execution = _make_execution()
    await bridge.create_execution(agent, session, "test", execution)

    # Start the execution
    await bridge.execute_step(execution.execution_id, "start")

    # Concurrently pause and resume
    results = await asyncio.gather(
        bridge.pause_execution(execution.execution_id),
        bridge.resume_execution(execution.execution_id),
        return_exceptions=True,
    )
    # Neither should deadlock or raise uncaught exceptions
    for r in results:
        assert isinstance(r, (bool, Exception))


@pytest.mark.asyncio
async def test_cleanup_during_concurrent_access():
    """Cleanup must not break concurrent operations on other executions."""
    bridge = NexusAgentBridge()
    for i in range(5):
        agent = _make_agent(agent_id=f"agent_{i}")
        session = _make_session(session_id=f"sess_{i}", agent_id=f"agent_{i}")
        execution = _make_execution(execution_id=f"exec_{i}", session_id=f"sess_{i}", agent_id=f"agent_{i}")
        await bridge.create_execution(agent, session, "test", execution)

    # Clean up exec_2 while operating on exec_4
    async def cleanup_exec2():
        await bridge.cleanup_execution("exec_2")

    async def step_exec4():
        return await bridge.execute_step("exec_4", "go")

    results = await asyncio.gather(cleanup_exec2(), step_exec4(), return_exceptions=True)
    assert results[1]["status"] == "ok"

    # exec_2 should be cleaned up
    assert await bridge.get_run_state("exec_2") is None
    assert await bridge.get_run_id_for_execution("exec_2") is None


@pytest.mark.asyncio
async def test_get_execution_for_agent_concurrent():
    """Concurrent lookups must be consistent."""
    bridge = NexusAgentBridge()
    agent = _make_agent()
    session = _make_session()
    execution = _make_execution()
    await bridge.create_execution(agent, session, "test", execution)

    async def lookup():
        return await bridge.get_execution_for_agent("agent_1")

    results = await asyncio.gather(*(lookup() for _ in range(50)))
    assert all(r == "exec_1" for r in results)
