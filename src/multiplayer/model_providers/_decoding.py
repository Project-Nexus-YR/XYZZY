"""Step decoding shared by every model provider transport.

Both the Responses provider and the Chat Completions provider turn a decoded
JSON action into a `_Step`. This is the one place that agrees on the shape,
so a fix here reaches every provider at once instead of leaving a second copy
permissive.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class ModelProviderError(RuntimeError):
    """A safe, user-visible model failure that never contains credentials."""


@dataclass(frozen=True, slots=True)
class _Step:
    """One decoded turn: what the model chose, and the text that came with it."""

    action: str
    tool: str
    tool_input: dict[str, Any]
    content: str


def _string_enum(schema: Mapping[str, Any], field: str) -> tuple[str, ...]:
    """The closed set this schema offers for one property, empty when it offers none."""
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return ()
    entry = properties.get(field)
    if not isinstance(entry, Mapping):
        return ()
    values = entry.get("enum")
    if not isinstance(values, list):
        return ()
    return tuple(value for value in values if isinstance(value, str))


def _step_content(decoded: Mapping[str, Any], fallback: str) -> str:
    """The readable half of a decoded step; the raw answer when it carries none."""
    output = decoded.get("output")
    if isinstance(output, Mapping):
        text = output.get("content")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return fallback


def decode_step(response_schema: dict[str, Any], content: str) -> _Step:
    """The action the model chose, refusing anything the run did not offer.

    A schema with no action enum asked for no choice, so its text is the whole
    answer and the step finishes. Where a choice was offered, an answer that
    does not decode into one is an error rather than an invented finish: a
    fabricated action is indistinguishable from one the model made, and this
    is the seam where the tool gateway learns what was actually asked for.
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
    return _Step(action, tool, dict(tool_input) if isinstance(tool_input, Mapping) else {}, text)
