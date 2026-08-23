"""One harness contract, and the two implementations that satisfy it today.

Streaming is a callback rather than an async generator, because a generator's return
value is untyped under mypy and the terminal ``StopReason`` must be one checked value.
The conformance suite below is what makes "one contract" a claim rather than a hope: it
runs unchanged against the NEXUS bridge and against a bare model provider.

``MAX_TOKENS`` is deliberately unexercised. ``OpenAIResponsesProvider._decode_response``
reads the action the model chose but no truncation field, so nothing here can reach that
state; the value stays because a truncated turn is a real terminal outcome and adding it
later would reopen a closed state machine.
"""

from __future__ import annotations

from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import (
    AgentInstance,
    BranchMode,
    Execution,
    Session,
    new_id,
)
from multiplayer.harness import (
    PROTOCOL_VERSION,
    AgentHarness,
    HarnessError,
    ModelProviderHarness,
    NexusHarness,
    NexusLaunch,
    PromptRequest,
    RunContext,
    SessionHandle,
    SessionUpdate,
    StopReason,
    UpdateKind,
)
from multiplayer.model_providers import WorkflowOnlyModelProvider
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService

_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"action": {"type": "string"}}}


class _Provider:
    def __init__(self, action: str = "finish", tool: str = "") -> None:
        self.action = action
        self.tool = tool
        self.calls = 0

    async def acomplete(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, response_schema
        self.calls += 1
        return {
            "action": self.action,
            "tool": self.tool,
            "input": {"limit": 5},
            "output": {"content": "assessed"},
            "provider_name": "test-model",
            "provider_model": "harness-test",
            "provider_response_id": "response_finish",
            "provider_evidence": "finished",
        }


class _FailingProvider:
    async def acomplete(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, response_schema
        raise bridge_module.ModelProviderError("the provider is down")


def _run_context(run_id: str) -> RunContext:
    return RunContext(
        run_id=run_id,
        agent_id="agent_1",
        identity_id="ident_1",
        room_id="room_1",
        run_credential="credential",
        authorized_by="owner",
        acting_user_id="owner",
    )


def _nexus_harness(provider: Any) -> tuple[NexusHarness, str]:
    bridge_module._HAS_NEXUS = False
    bridge = NexusAgentBridge(model_provider=provider)
    run_id = new_id("arun")
    session = Session(session_id=new_id("sess"), room_id="room_1", agent_id="agent_1")
    execution = Execution(
        execution_id=new_id("exec"),
        session_id=session.session_id,
        agent_id="agent_1",
        authorized_by="owner",
    )
    agent = AgentInstance(
        agent_id="agent_1",
        template_id="tmpl_1",
        room_id="room_1",
        name="Researcher",
        role="Researcher",
    )
    launch = NexusLaunch(agent=agent, session=session, execution=execution)

    async def resolve(requested: str) -> NexusLaunch:
        del requested
        return launch

    return NexusHarness(bridge, resolve), run_id


def _model_provider_harness(provider: Any) -> tuple[ModelProviderHarness, str]:
    return ModelProviderHarness(provider), new_id("arun")


HARNESSES = {"nexus": _nexus_harness, "model-provider": _model_provider_harness}


# ── The conformance suite, run against both ──────────────────────────────────


@pytest.mark.parametrize("build", list(HARNESSES.values()), ids=list(HARNESSES))
@pytest.mark.asyncio
async def test_initialize_answers_none_when_it_is_handed_none(build: Any) -> None:
    harness, _ = build(_Provider())
    info, answer = await harness.initialize(None)

    assert info.protocol_version == PROTOCOL_VERSION
    assert info.harness_id == harness.harness_id
    assert answer is None
    # advertised_capabilities is display metadata, never a capability term.
    assert info.advertised_capabilities == frozenset()


@pytest.mark.parametrize("build", list(HARNESSES.values()), ids=list(HARNESSES))
@pytest.mark.asyncio
async def test_an_in_process_harness_signs_nothing_when_it_is_handed_a_challenge(
    build: Any,
) -> None:
    """It holds no key, so it answers nothing, and the caller refuses the launch."""
    harness, _ = build(_Provider())
    _, answer = await harness.initialize(b"a-random-challenge")
    assert answer is None


@pytest.mark.parametrize("build", list(HARNESSES.values()), ids=list(HARNESSES))
@pytest.mark.asyncio
async def test_every_prompt_terminates_in_exactly_one_stop_reason(build: Any) -> None:
    harness, run_id = build(_Provider())
    handle = await harness.session_new(_run_context(run_id))
    assert handle.run_id == run_id
    updates: list[SessionUpdate] = []

    async def on_update(update: SessionUpdate) -> None:
        updates.append(update)

    result = await harness.session_prompt(
        PromptRequest(
            handle=handle, prompt="Assess it.", response_schema=_SCHEMA, offered_tools=()
        ),
        on_update,
    )

    assert result.stop_reason is StopReason.END_TURN
    assert isinstance(result.stop_reason, StopReason)
    assert [update.run_id for update in updates] == [run_id]
    assert updates[0].kind is UpdateKind.MESSAGE_DELTA


@pytest.mark.parametrize("build", list(HARNESSES.values()), ids=list(HARNESSES))
@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_the_next_prompt_is_cancelled(build: Any) -> None:
    harness, run_id = build(_Provider())
    handle = await harness.session_new(_run_context(run_id))

    await harness.session_cancel(handle, "human requested cancel")
    await harness.session_cancel(handle, "human requested cancel")

    async def on_update(update: SessionUpdate) -> None:
        del update

    result = await harness.session_prompt(
        PromptRequest(
            handle=handle, prompt="Assess it.", response_schema=_SCHEMA, offered_tools=()
        ),
        on_update,
    )
    assert result.stop_reason is StopReason.CANCELLED


@pytest.mark.parametrize("build", list(HARNESSES.values()), ids=list(HARNESSES))
@pytest.mark.asyncio
async def test_cancelling_an_unknown_session_is_not_an_error(build: Any) -> None:
    harness, _ = build(_Provider())
    await harness.session_cancel(SessionHandle(run_id="nobody", harness_session_id="nobody"), "x")


@pytest.mark.parametrize("build", list(HARNESSES.values()), ids=list(HARNESSES))
@pytest.mark.asyncio
async def test_a_turn_that_produced_nothing_raises_rather_than_inventing_a_stop_reason(
    build: Any,
) -> None:
    """An error is not a terminal outcome of a turn; it is the absence of one."""
    harness, run_id = build(_FailingProvider())
    handle = await harness.session_new(_run_context(run_id))

    async def on_update(update: SessionUpdate) -> None:
        del update

    with pytest.raises(HarnessError):
        await harness.session_prompt(
            PromptRequest(
                handle=handle, prompt="Assess it.", response_schema=_SCHEMA, offered_tools=()
            ),
            on_update,
        )


@pytest.mark.parametrize("build", list(HARNESSES.values()), ids=list(HARNESSES))
def test_both_implementations_satisfy_the_protocol(build: Any) -> None:
    harness, _ = build(_Provider())
    checked: AgentHarness = harness
    assert checked is harness


@pytest.mark.asyncio
async def test_the_workflow_only_provider_satisfies_the_model_provider_harness() -> None:
    """`ModelProviderHarness` wraps anything with acomplete(prompt, schema)."""
    harness = ModelProviderHarness(WorkflowOnlyModelProvider())
    run_id = new_id("arun")
    handle = await harness.session_new(_run_context(run_id))
    seen: list[SessionUpdate] = []

    async def on_update(update: SessionUpdate) -> None:
        seen.append(update)

    result = await harness.session_prompt(
        PromptRequest(
            handle=handle, prompt="Assess it.", response_schema=_SCHEMA, offered_tools=()
        ),
        on_update,
    )

    assert result.stop_reason is StopReason.END_TURN
    assert result.provenance["provider_name"] == "workflow-only"
    assert "SIMULATED" in seen[0].payload["content"]


@pytest.mark.parametrize("build", list(HARNESSES.values()), ids=list(HARNESSES))
@pytest.mark.asyncio
async def test_a_tool_action_is_reported_as_a_tool_call_update(build: Any) -> None:
    """Both harnesses hand the server the tool and its input, or the gateway sees none."""
    harness, run_id = build(_Provider(action="tool", tool="channel.read_context"))
    handle = await harness.session_new(_run_context(run_id))
    seen: list[SessionUpdate] = []

    async def on_update(update: SessionUpdate) -> None:
        seen.append(update)

    result = await harness.session_prompt(
        PromptRequest(
            handle=handle,
            prompt="Assess it.",
            response_schema=_SCHEMA,
            offered_tools=("channel.read_context",),
        ),
        on_update,
    )

    assert seen[0].kind is UpdateKind.TOOL_CALL
    assert result.output["action"] == "tool"
    assert result.output["tool"] == "channel.read_context"
    assert result.output["input"] == {"limit": 5}


# ── The golden test: the seeded branch run keeps its provenance ──────────────


@pytest.mark.asyncio
async def test_a_seeded_branch_run_keeps_identical_agent_output_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two implementations, no behaviour change: the recorded evidence is untouched."""
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    try:
        svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner"}))
        await svc.initialize()
        svc.nexus = NexusAgentBridge(model_provider=_Provider())
        org = await svc.create_organization("Golden org", "gold-org", "owner")
        workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
        room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
        templates = await svc.list_agent_templates()
        agent = await svc.spawn_agent(
            room.room_id,
            next(t.template_id for t in templates if t.name == "Researcher"),
            name="Researcher",
            requested_by="owner",
        )
        branch, runs = await svc.start_branch(
            room.room_id,
            BranchMode.TURN_LOCKED_SINGLE,
            "Should we ship on Friday?",
            "owner",
            [agent.agent_id],
        )
        await svc.execute_branch_run(branch.branch_id, runs[0].execution_id)

        output = await svc.repos.agent_outputs.get_by_execution(runs[0].execution_id)
        assert output is not None
        assert output.provider_name == "test-model"
        assert output.provider_model == "harness-test"
        assert output.provider_response_id == "response_finish"
        assert output.provider_evidence == "finished"
        assert output.provider_interventions == ()
        # The exact rendered provider request, still recorded verbatim.
        assert output.source_prompt == "Should we ship on Friday?"
        assert "Should we ship on Friday?" in output.provider_input
        assert branch.context_hash in output.provider_input
        assert output.content == "assessed"
    finally:
        await db.close()
