"""OpenAI Responses API provider with an explicitly labelled local fallback."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import httpx

from ._decoding import ModelProviderError, _string_enum, decode_step

if TYPE_CHECKING:
    from .openai_chat_completions import OpenAIChatCompletionsProvider

_RESPONSES_URL = "https://api.openai.com/v1/responses"
_DEFAULT_MODEL = "gpt-5.4-mini"
_DEFAULT_TIMEOUT_SECONDS = 45.0
#: Sent as ``max_output_tokens`` on every call, so an unbounded prompt cannot
#: turn into an unbounded bill; ``XYZZY_MODEL_MAX_OUTPUT_TOKENS`` overrides it.
_DEFAULT_MAX_OUTPUT_TOKENS = 4096


class WorkflowOnlyModelProvider:
    """Credential-free fallback for exercising workflow mechanics.

    The payload is intentionally conspicuous: it must never be mistaken for a real model
    analysis, even though it finishes the local workflow for development and tests.

    It always finishes, and that is correct rather than the same defect: no model
    chose anything here, so a tool request from this provider would put a call
    nobody made through the gateway and into the audit log.
    """

    _CONTENT = (
        "SIMULATED WORKFLOW OUTPUT — no model provider is configured. "
        "This output verifies collaboration, selection, reconnect, and provenance mechanics; "
        "it is not an AI analysis and must not be used to make the decision."
    )

    #: The verified identity of this provider: never a real model, always
    #: labelled plainly so it can never be mistaken for one in an audit trail.
    provider_name = "simulated"
    provider_model = ""

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
            "provider_name": self.provider_name,
            "provider_model": self.provider_model,
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
        max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS,
        async_transport: httpx.AsyncBaseTransport | None = None,
        sync_transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("api_key must be non-empty")
        if not model.strip():
            raise ValueError("model must be non-empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        self._api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self._async_transport = async_transport
        self._sync_transport = sync_transport

    #: The verified identity of this provider, reused wherever a response
    #: omits its own — never a caller-supplied string.
    provider_name = "openai"

    @property
    def provider_model(self) -> str:
        return self.model

    def _request_payload(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        text: dict[str, Any] = {"verbosity": "medium"}
        properties = response_schema.get("properties")
        # Synthesis owns a complete closed schema and is the only call that can opt
        # into strict Structured Outputs.
        if isinstance(properties, Mapping) and "claims" in properties:
            text["format"] = {
                "type": "json_schema",
                "name": "xyzzy_response",
                "strict": True,
                "schema": response_schema,
            }
        elif _string_enum(response_schema, "action"):
            # A step schema leaves output and input free-form, so it cannot satisfy
            # strict mode's closed-object requirement. Sending it unstrict is still
            # what turns the answer into an action the run can read back, instead of
            # prose the decoder would have to guess a choice from.
            text["format"] = {
                "type": "json_schema",
                "name": "xyzzy_step",
                "strict": False,
                "schema": response_schema,
            }
        return {
            "model": self.model,
            "input": prompt,
            "store": False,
            "text": text,
            "max_output_tokens": self.max_output_tokens,
        }

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def acomplete(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self._async_transport,
            ) as client:
                response = await client.post(
                    _RESPONSES_URL,
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
                    _RESPONSES_URL,
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
        content = self._extract_output_text(payload)
        if not content:
            raise ModelProviderError("model provider returned no text output")
        step = decode_step(response_schema, content)
        usage = payload.get("usage")
        token_usage = usage.get("total_tokens", 0) if isinstance(usage, Mapping) else 0
        return {
            "action": step.action,
            "tool": step.tool,
            "input": step.tool_input,
            "output": {
                "content": step.content,
                "provider": "openai",
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
) -> OpenAIResponsesProvider | WorkflowOnlyModelProvider | OpenAIChatCompletionsProvider:
    """Build the server model provider without accepting credentials from requests or storage."""
    values = os.environ if environ is None else environ
    model = values.get("XYZZY_OPENAI_MODEL", _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
    raw_timeout = values.get("XYZZY_MODEL_TIMEOUT_SECONDS", str(_DEFAULT_TIMEOUT_SECONDS))
    try:
        timeout = float(raw_timeout)
    except ValueError as exc:
        raise ValueError("XYZZY_MODEL_TIMEOUT_SECONDS must be a number") from exc
    raw_max_output_tokens = values.get(
        "XYZZY_MODEL_MAX_OUTPUT_TOKENS", str(_DEFAULT_MAX_OUTPUT_TOKENS)
    )
    try:
        max_output_tokens = int(raw_max_output_tokens)
    except ValueError as exc:
        raise ValueError("XYZZY_MODEL_MAX_OUTPUT_TOKENS must be a number") from exc

    base_url = values.get("XYZZY_LOCAL_MODEL_BASE_URL", "").strip()
    if base_url:
        # Imported lazily to avoid a circular import: the chat-completions module
        # reuses this module's schema and step-decoding helpers.
        from .openai_chat_completions import OpenAIChatCompletionsProvider

        api_key = values.get("OPENAI_API_KEY", "").strip() or None
        return OpenAIChatCompletionsProvider(
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout_seconds=timeout,
            max_output_tokens=max_output_tokens,
        )

    api_key = values.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return WorkflowOnlyModelProvider()
    return OpenAIResponsesProvider(
        api_key=api_key,
        model=model,
        timeout_seconds=timeout,
        max_output_tokens=max_output_tokens,
    )
