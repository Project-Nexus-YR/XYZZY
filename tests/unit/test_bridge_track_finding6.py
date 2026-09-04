"""Finding 6: a provider that answers outside the requested JSON schema must fail
the synthesis, never be rewritten into a fabricated COMPLETED document.
"""

from __future__ import annotations

from typing import Any

import pytest

from multiplayer.model_providers import ModelProviderError
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge

_REFUSAL_TEXT = "Sorry, I cannot help with that."


class _RefusingProvider:
    """A real, correctly identified provider that ignores the requested schema."""

    provider_name = "openai"
    provider_model = "gpt-test"

    async def acomplete(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, schema
        return {
            "output": {
                "content": _REFUSAL_TEXT,
                "provider": "openai",
                "model": "gpt-test",
                "simulated": False,
            },
            "provider_name": "openai",
            "provider_model": "gpt-test",
            "provider_response_id": "resp_1",
            "provider_evidence": _REFUSAL_TEXT,
        }


async def test_non_json_provider_output_raises_instead_of_fabricating_synthesis() -> None:
    bridge = NexusAgentBridge(model_provider=_RefusingProvider())
    outputs = [
        {"output_id": "out_1", "agent_id": "agent_1", "content": "ship it"},
        {"output_id": "out_2", "agent_id": "agent_2", "content": "do not ship"},
    ]

    with pytest.raises(ModelProviderError):
        await bridge.synthesize_selected_outputs(
            title="Migration decision",
            prompt="Should we ship?",
            outputs=outputs,
        )
