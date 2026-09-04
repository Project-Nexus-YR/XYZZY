"""Finding 69: token usage a provider reports must survive the bridge, instead of
being dropped at the first mapping site it passes through.
"""

from __future__ import annotations

from typing import Any

from multiplayer.domain.models import AgentInstance, Execution, Session
from multiplayer.harness.adapters import ModelProviderHarness
from multiplayer.harness.protocol import PromptRequest, SessionHandle
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge


class _MeteredProvider:
    provider_name = "openai"
    provider_model = "gpt-test"

    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, schema
        return {
            "action": "finish",
            "tool": "",
            "input": {},
            "output": {"content": "done", "provider": "openai", "model": "gpt-test"},
            "token_usage": 42,
            "provider_name": "openai",
            "provider_model": "gpt-test",
            "provider_response_id": "resp_1",
            "provider_evidence": "done",
        }


async def test_execute_step_surfaces_provider_token_usage() -> None:
    bridge = NexusAgentBridge(model_provider=_MeteredProvider())

    agent = AgentInstance(
        agent_id="agent_1",
        template_id="template_1",
        room_id="room_1",
        name="Reviewer",
        role="reviewer",
        system_prompt="Review the proposal.",
        capabilities=frozenset(),
        model_provider="openai",
        model_name="gpt-test",
    )
    session = Session(session_id="session_1", room_id="room_1", agent_id="agent_1")
    execution = Execution(
        execution_id="execution_1",
        session_id="session_1",
        agent_id="agent_1",
    )
    await bridge.create_execution(agent, session, "task", execution)

    result = await bridge.execute_step("execution_1", "do the review")

    assert result["token_usage"] == 42


async def test_synthesis_surfaces_provider_token_usage() -> None:
    bridge = NexusAgentBridge(model_provider=_MeteredProvider())

    class _SynthesisProvider(_MeteredProvider):
        async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
            del prompt, schema
            return {
                "output": {
                    "content": (
                        '{"summary": "ok", "claims": ['
                        '{"text": "a", "source_output_ids": ["out_1"], "confidence": 0.5}]}'
                    ),
                    "provider": "openai",
                    "model": "gpt-test",
                    "simulated": False,
                },
                "token_usage": 99,
                "provider_name": "openai",
                "provider_model": "gpt-test",
                "provider_response_id": "resp_2",
                "provider_evidence": "ok",
            }

    bridge._model = _SynthesisProvider()

    result = await bridge.synthesize_selected_outputs(
        title="t",
        prompt="p",
        outputs=[{"output_id": "out_1", "agent_id": "agent_1", "content": "a"}],
    )

    assert result["token_usage"] == 99


async def test_model_provider_harness_surfaces_token_usage() -> None:
    harness = ModelProviderHarness(_MeteredProvider())
    handle = SessionHandle(run_id="run_1", harness_session_id="run_1")
    updates: list[Any] = []

    async def _record(update: Any) -> None:
        updates.append(update)

    turn = await harness.session_prompt(
        PromptRequest(handle=handle, prompt="hi", response_schema={}, offered_tools=()),
        _record,
    )

    assert turn.output["token_usage"] == 42
