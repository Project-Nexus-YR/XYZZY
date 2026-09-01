"""OpenAI-compatible chat-completions provider for local model servers.

Any server that speaks the OpenAI chat-completions wire format works here:
Ollama, LM Studio, vLLM, and llama.cpp's server all qualify. The base URL is
the caller's `/v1` root; this provider appends `/chat/completions` itself.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx

from .openai_responses import ModelProviderError, _Step, _step_content, _string_enum

_DEFAULT_TIMEOUT_SECONDS = 45.0


class OpenAIChatCompletionsProvider:
    """Small chat-completions client with injectable transports for deterministic testing."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        async_transport: httpx.AsyncBaseTransport | None = None,
        sync_transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("base_url must be non-empty")
        if not model.strip():
            raise ValueError("model must be non-empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key.strip() if api_key else ""
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._async_transport = async_transport
        self._sync_transport = sync_transport

    #: The verified identity of this provider, reused wherever a response
    #: omits its own — never a caller-supplied string.
    provider_name = "openai-chat-completions"

    @property
    def provider_model(self) -> str:
        return self.model

    @property
    def _url(self) -> str:
        return f"{self._base_url}/chat/completions"

    def _request_payload(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
        }
        properties = response_schema.get("properties")
        # Mirror the Responses provider: synthesis owns a complete closed schema and
        # is the only call that can opt into strict Structured Outputs; a step schema
        # leaves output and input free-form and is sent unstrict.
        if isinstance(properties, Mapping) and "claims" in properties:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "xyzzy_response",
                    "strict": True,
                    "schema": response_schema,
                },
            }
        elif _string_enum(response_schema, "action"):
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "xyzzy_step",
                    "strict": False,
                    "schema": response_schema,
                },
            }
        return payload

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def acomplete(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self._async_transport,
            ) as client:
                response = await client.post(
                    self._url,
                    headers=self._headers(),
                    json=self._request_payload(prompt, response_schema),
                )
            return self._decode_response(response, response_schema)
        except httpx.TimeoutException as exc:
            raise ModelProviderError(
                f"model request timed out after {self.timeout_seconds:g} seconds"
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelProviderError("model provider request failed") from exc

    def complete(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        """Synchronous adapter used only when the optional NEXUS runtime owns execution."""
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self._sync_transport,
            ) as client:
                response = client.post(
                    self._url,
                    headers=self._headers(),
                    json=self._request_payload(prompt, response_schema),
                )
            return self._decode_response(response, response_schema)
        except httpx.TimeoutException as exc:
            raise ModelProviderError(
                f"model request timed out after {self.timeout_seconds:g} seconds"
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelProviderError("model provider request failed") from exc

    def _decode_response(
        self, response: httpx.Response, response_schema: dict[str, Any]
    ) -> dict[str, Any]:
        if not response.is_success:
            # Deliberately omit the body: an upstream error may echo sensitive request data.
            raise ModelProviderError(f"model provider returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ModelProviderError("model provider returned invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise ModelProviderError("model provider returned an invalid response")
        content = self._extract_message_content(payload)
        if not content:
            raise ModelProviderError("model provider returned no text output")
        step = self._decode_step(response_schema, content)
        usage = payload.get("usage")
        token_usage = usage.get("total_tokens", 0) if isinstance(usage, Mapping) else 0
        return {
            "action": step.action,
            "tool": step.tool,
            "input": step.tool_input,
            "output": {
                "content": step.content,
                "provider": "openai-chat-completions",
                "model": self.model,
                "simulated": False,
            },
            "token_usage": token_usage if isinstance(token_usage, int) else 0,
            "provider_name": self.provider_name,
            "provider_model": self.provider_model,
            "provider_response_id": (
                str(payload["id"]) if isinstance(payload.get("id"), str) else ""
            ),
            "provider_evidence": step.content,
        }

    @staticmethod
    def _decode_step(response_schema: dict[str, Any], content: str) -> _Step:
        """The action the model chose, refusing anything the run did not offer.

        Same contract as the Responses provider's step decoder: a schema with no
        action enum asked for no choice, so the text is the whole answer.
        """
        actions = _string_enum(response_schema, "action")
        if not actions:
            return _Step("finish", "", {}, content)
        try:
            decoded = json.loads(content)
        except ValueError as exc:
            raise ModelProviderError("model provider returned no decodable action") from exc
        if not isinstance(decoded, Mapping):
            raise ModelProviderError("model provider returned no decodable action")
        action = decoded.get("action")
        if not isinstance(action, str) or action not in actions:
            raise ModelProviderError("model provider chose an action this run did not offer")
        text = _step_content(decoded, content)
        if action != "tool":
            return _Step(action, "", {}, text)
        tool = decoded.get("tool")
        if not isinstance(tool, str) or tool not in _string_enum(response_schema, "tool"):
            raise ModelProviderError("model provider requested a tool this run was not offered")
        tool_input = decoded.get("input")
        return _Step(
            action, tool, dict(tool_input) if isinstance(tool_input, Mapping) else {}, text
        )

    @staticmethod
    def _extract_message_content(payload: Mapping[str, Any]) -> str:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first = choices[0]
        if not isinstance(first, Mapping):
            return ""
        message = first.get("message")
        if not isinstance(message, Mapping):
            return ""
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        return ""
