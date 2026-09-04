"""Finding 44: the Chat Completions and Responses providers must decode a step
through the same function, not two copies that can silently drift apart.
"""

from __future__ import annotations

from multiplayer.model_providers import openai_chat_completions, openai_responses
from multiplayer.model_providers._decoding import ModelProviderError, decode_step


def test_both_providers_use_the_one_shared_decode_step() -> None:
    assert openai_chat_completions.decode_step is decode_step
    assert openai_responses.decode_step is decode_step


def test_decode_step_refuses_an_action_the_run_did_not_offer() -> None:
    schema = {"properties": {"action": {"enum": ["finish"]}}}
    try:
        decode_step(schema, '{"action": "tool"}')
    except ModelProviderError:
        pass
    else:
        raise AssertionError("expected ModelProviderError for an unoffered action")
