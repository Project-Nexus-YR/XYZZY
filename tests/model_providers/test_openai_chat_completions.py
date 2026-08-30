"""Chat-completions provider contract tests without live network calls or credentials."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from multiplayer.model_providers import (
    ModelProviderError,
    OpenAIChatCompletionsProvider,
    OpenAIResponsesProvider,
    WorkflowOnlyModelProvider,
    model_provider_from_environment,
)


def _chat_body(text: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-test",
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"total_tokens": 13},
    }


class _RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, content: str) -> None:
        self.requests: list[httpx.Request] = []
        self.payloads: list[dict[str, Any]] = []
        self._content = content

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.payloads.append(json.loads(request.content))
        return httpx.Response(200, request=request, json=_chat_body(self._content))


class _FailureTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            request=request,
            json={"error": "upstream echoed sk-test-must-never-be-persisted"},
        )


class _TimeoutTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream timeout details are suppressed", request=request)


class _MalformedTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request, json=_chat_body("not json at all"))


_STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["finish", "tool"]},
        "tool": {"type": "string", "enum": ["search"]},
    },
}


@pytest.mark.asyncio
async def test_request_hits_chat_completions_with_no_auth_header_when_no_key() -> None:
    transport = _RecordingTransport(json.dumps({"action": "finish", "output": {"content": "ok"}}))
    provider = OpenAIChatCompletionsProvider(
        base_url="http://localhost:11434/v1",
        model="llama3",
        async_transport=transport,
    )

    result = await provider.acomplete("hello", _STEP_SCHEMA)

    assert transport.requests[0].url == "http://localhost:11434/v1/chat/completions"
    assert "Authorization" not in transport.requests[0].headers
    assert transport.payloads[0]["model"] == "llama3"
    assert transport.payloads[0]["messages"] == [{"role": "user", "content": "hello"}]
    assert result["action"] == "finish"
    assert result["output"]["content"] == "ok"
    assert result["provider_name"] == "openai-chat-completions"
    assert result["token_usage"] == 13


@pytest.mark.asyncio
async def test_request_includes_bearer_auth_header_when_key_is_set() -> None:
    transport = _RecordingTransport(json.dumps({"action": "finish", "output": {"content": "ok"}}))
    provider = OpenAIChatCompletionsProvider(
        base_url="http://localhost:1234/v1",
        model="local-model",
        api_key="sk-test-not-real",
        async_transport=transport,
    )

    await provider.acomplete("hello", _STEP_SCHEMA)

    assert transport.requests[0].headers["Authorization"] == "Bearer sk-test-not-real"


@pytest.mark.asyncio
async def test_synthesis_schema_opts_into_strict_json_schema_response_format() -> None:
    transport = _RecordingTransport(json.dumps({"claims": []}))
    provider = OpenAIChatCompletionsProvider(
        base_url="http://localhost:11434/v1",
        model="llama3",
        async_transport=transport,
    )
    schema = {
        "type": "object",
        "properties": {"claims": {"type": "array", "items": {"type": "string"}}},
        "required": ["claims"],
        "additionalProperties": False,
    }

    await provider.acomplete("Specialist role: Synthesizer", schema)

    assert transport.payloads[0]["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "xyzzy_response", "strict": True, "schema": schema},
    }


@pytest.mark.asyncio
async def test_step_schema_opts_into_unstrict_json_schema_response_format() -> None:
    transport = _RecordingTransport(json.dumps({"action": "finish", "output": {"content": "ok"}}))
    provider = OpenAIChatCompletionsProvider(
        base_url="http://localhost:11434/v1",
        model="llama3",
        async_transport=transport,
    )

    await provider.acomplete("decide", _STEP_SCHEMA)

    assert transport.payloads[0]["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "xyzzy_step", "strict": False, "schema": _STEP_SCHEMA},
    }


@pytest.mark.asyncio
async def test_malformed_content_is_refused_not_guessed() -> None:
    provider = OpenAIChatCompletionsProvider(
        base_url="http://localhost:11434/v1",
        model="llama3",
        async_transport=_MalformedTransport(),
    )

    with pytest.raises(ModelProviderError, match="no decodable action"):
        await provider.acomplete("decide", _STEP_SCHEMA)


@pytest.mark.asyncio
async def test_provider_timeout_is_explicit_and_sanitized() -> None:
    provider = OpenAIChatCompletionsProvider(
        base_url="http://localhost:11434/v1",
        model="llama3",
        api_key="sk-test-must-never-appear",
        timeout_seconds=2,
        async_transport=_TimeoutTransport(),
    )

    with pytest.raises(ModelProviderError) as caught:
        await provider.acomplete("decision", {})

    assert str(caught.value) == "model request timed out after 2 seconds"
    assert "sk-test" not in str(caught.value)


@pytest.mark.asyncio
async def test_provider_failure_omits_upstream_body() -> None:
    provider = OpenAIChatCompletionsProvider(
        base_url="http://localhost:11434/v1",
        model="llama3",
        async_transport=_FailureTransport(),
    )

    with pytest.raises(ModelProviderError) as caught:
        await provider.acomplete("decision", {})

    assert str(caught.value) == "model provider returned HTTP 503"
    assert "sk-test" not in str(caught.value)


def test_environment_selects_local_provider_when_base_url_set() -> None:
    provider = model_provider_from_environment(
        {
            "XYZZY_LOCAL_MODEL_BASE_URL": "http://localhost:11434/v1",
            "XYZZY_OPENAI_MODEL": "llama3",
        }
    )

    assert isinstance(provider, OpenAIChatCompletionsProvider)
    assert provider.model == "llama3"


def test_environment_prefers_local_provider_over_openai_key() -> None:
    provider = model_provider_from_environment(
        {
            "XYZZY_LOCAL_MODEL_BASE_URL": "http://localhost:11434/v1",
            "OPENAI_API_KEY": "sk-test-not-real",
        }
    )

    assert isinstance(provider, OpenAIChatCompletionsProvider)


def test_environment_falls_back_to_responses_provider_without_local_base_url() -> None:
    provider = model_provider_from_environment({"OPENAI_API_KEY": "sk-test-not-real"})

    assert isinstance(provider, OpenAIResponsesProvider)


def test_environment_falls_back_to_workflow_only_with_neither_set() -> None:
    provider = model_provider_from_environment({})

    assert isinstance(provider, WorkflowOnlyModelProvider)
