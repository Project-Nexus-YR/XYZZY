"""One harness contract, modelled on ACP over stdio.

The Protocol is transport-agnostic so an in-process and a subprocess harness satisfy
the same type. Streaming is a callback rather than an async generator, because a
generator's return value is untyped under mypy and the terminal ``StopReason`` must be
one checked value.

The challenge is optional because the boundary is: ``initialize`` takes and returns
``bytes | None``, present exactly in ``SIGNED_CHALLENGE`` mode. An in-process harness
is handed ``None`` and returns ``None`` rather than signing against a key the server
already holds.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

PROTOCOL_VERSION = 1


class HarnessError(RuntimeError):
    """A turn that produced no result. Terminal for the run, not for the process."""


class StopReason(StrEnum):
    END_TURN = "end_turn"
    CANCELLED = "cancelled"
    # No provider here reports truncation yet, so nothing can reach this state; the
    # value stays because adding it later would reopen a closed state machine.
    MAX_TOKENS = "max_tokens"


class UpdateKind(StrEnum):
    MESSAGE_DELTA = "message_delta"
    THOUGHT = "thought"
    TOOL_CALL = "tool_call"


@dataclass(frozen=True, slots=True)
class HarnessInfo:
    """advertised_capabilities is display only; never a capability term."""

    harness_id: str
    protocol_version: int
    advertised_capabilities: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class RunContext:
    """What the server hands a harness; authority travels with the run."""

    run_id: str
    agent_id: str
    identity_id: str
    room_id: str
    run_credential: str
    authorized_by: str
    acting_user_id: str  # initiator, then whoever moved it last


@dataclass(frozen=True, slots=True)
class SessionHandle:
    run_id: str
    harness_session_id: str


@dataclass(frozen=True, slots=True)
class PromptRequest:
    handle: SessionHandle
    prompt: str
    response_schema: dict[str, Any]
    offered_tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SessionUpdate:
    run_id: str
    kind: UpdateKind
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TurnResult:
    stop_reason: StopReason
    output: dict[str, Any]
    provenance: dict[str, Any]


UpdateSink = Callable[[SessionUpdate], Awaitable[None]]


class AgentHarness(Protocol):
    async def initialize(self, challenge: bytes | None) -> tuple[HarnessInfo, bytes | None]: ...

    async def session_new(self, run: RunContext) -> SessionHandle: ...

    async def session_prompt(self, request: PromptRequest, on_update: UpdateSink) -> TurnResult: ...

    async def session_cancel(self, handle: SessionHandle, reason: str) -> None: ...
