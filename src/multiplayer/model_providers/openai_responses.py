"""OpenAI Responses API provider with an explicitly labelled local fallback."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import httpx

_RESPONSES_URL = "https://api.openai.com/v1/responses"
_DEFAULT_MODEL = "gpt-5.4-mini"
_DEFAULT_TIMEOUT_SECONDS = 45.0


class ModelProviderError(RuntimeError):
    """A safe, user-visible model failure that never contains credentials."""


class WorkflowOnlyModelProvider:
    """Credential-free fallback for exercising workflow mechanics.

    The payload is intentionally conspicuous: it must never be mistaken for a real model
    analysis, even though it finishes the local workflow for development and tests.
    """

    _CONTENT = (
        "SIMULATED WORKFLOW OUTPUT — no model provider is configured. "
        "This output verifies collaboration, selection, reconnect, and provenance mechanics; "
        "it is not an AI analysis and must not be used to make the decision."
    )

    def complete(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, response_schema
        return {
            "action": "finish",
            "output": {
                "content": self._CONTENT,
                "provider": "workflow-only",
                "model": None,
                "simulated": True,
            },
            "token_usage": 0,
            "provider_name": "workflow-only",
            "provider_model": "",
            "provider_response_id": "",
            "provider_evidence": self._CONTENT,
        }

    async def acomplete(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        return self.complete(prompt, response_schema)


class OpenAIResponsesProvider:
    """Small Responses API client with injectable transports for deterministic testing."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = _DEFAULT_MODEL,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        async_transport: httpx.AsyncBaseTransport | None = None,
        sync_transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("api_key must be non-empty")
        if not model.strip():
            raise ValueError("model must be non-empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._async_transport = async_transport
        self._sync_transport = sync_transport

    def _request_payload(self, prompt: str) -> dict[str, Any]:
        return {
            "model": self.model,
            "input": prompt,
            "store": False,
            "text": {"verbosity": "medium"},
        }

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def acomplete(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        del response_schema
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self._async_transport,
            ) as client:
                response = await client.post(
                    _RESPONSES_URL,
                    headers=self._headers(),
                    json=self._request_payload(prompt),
                )
            return self._decode_response(response)
        except httpx.TimeoutException as exc:
            raise ModelProviderError(
                f"model request timed out after {self.timeout_seconds:g} seconds"
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelProviderError("model provider request failed") from exc

    def complete(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        """Synchronous adapter used only when the optional NEXUS runtime owns execution."""
        del response_schema
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self._sync_transport,
            ) as client:
                response = client.post(
                    _RESPONSES_URL,
                    headers=self._headers(),
                    json=self._request_payload(prompt),
                )
            return self._decode_response(response)
        except httpx.TimeoutException as exc:
            raise ModelProviderError(
                f"model request timed out after {self.timeout_seconds:g} seconds"
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelProviderError("model provider request failed") from exc

    def _decode_response(self, response: httpx.Response) -> dict[str, Any]:
        if not response.is_success:
            # Deliberately omit the body: an upstream error may echo sensitive request data.
            raise ModelProviderError(f"model provider returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ModelProviderError("model provider returned invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise ModelProviderError("model provider returned an invalid response")
        content = self._extract_output_text(payload)
        if not content:
            raise ModelProviderError("model provider returned no text output")
        usage = payload.get("usage")
        token_usage = usage.get("total_tokens", 0) if isinstance(usage, Mapping) else 0
        return {
            "action": "finish",
            "output": {
                "content": content,
                "provider": "openai",
                "model": self.model,
                "simulated": False,
            },
            "token_usage": token_usage if isinstance(token_usage, int) else 0,
            "provider_name": "openai",
            "provider_model": self.model,
            "provider_response_id": (
                str(payload["id"]) if isinstance(payload.get("id"), str) else ""
            ),
            "provider_evidence": content,
        }

    @staticmethod
    def _extract_output_text(payload: Mapping[str, Any]) -> str:
        direct = payload.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        output = payload.get("output")
        if not isinstance(output, list):
            return ""
        chunks: list[str] = []
        for item in output:
            if not isinstance(item, Mapping):
                continue
            parts = item.get("content")
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, Mapping) or part.get("type") != "output_text":
                    continue
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    chunks.append(text.strip())
        return "\n\n".join(chunks)


def model_provider_from_environment(
    environ: Mapping[str, str] | None = None,
) -> OpenAIResponsesProvider | WorkflowOnlyModelProvider:
    """Build the server model provider without accepting credentials from requests or storage."""
    values = os.environ if environ is None else environ
    api_key = values.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return WorkflowOnlyModelProvider()
    model = values.get("MULTIAI_OPENAI_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
    raw_timeout = values.get("MULTIAI_MODEL_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT_SECONDS))
    try:
        timeout = float(raw_timeout)
    except ValueError as exc:
        raise ValueError("MULTIAI_MODEL_TIMEOUT_SECONDS must be a number") from exc
    return OpenAIResponsesProvider(api_key=api_key, model=model, timeout_seconds=timeout)
