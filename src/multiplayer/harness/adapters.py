"""The two harnesses that exist today, behind one contract and no behaviour change.

``NexusHarness`` wraps :class:`NexusAgentBridge`; ``ModelProviderHarness`` wraps
anything with ``acomplete(prompt, schema)``, so both configured model providers
satisfy it. Prompts, step schema and provenance are untouched by either.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from ..domain.models import AgentInstance, Execution, Session
from .protocol import (
    PROTOCOL_VERSION,
    HarnessError,
    HarnessInfo,
    PromptRequest,
    RunContext,
    SessionHandle,
    SessionUpdate,
    StopReason,
    TurnResult,
    UpdateKind,
    UpdateSink,
)

NEXUS_HARNESS_ID = "nexus"
MODEL_PROVIDER_HARNESS_ID = "model-provider"
# agent_instances.harness_id selects from this registry; an unknown id refuses to
# launch, which is why the set is durable rather than derived from configuration.
KNOWN_HARNESS_IDS: frozenset[str] = frozenset({NEXUS_HARNESS_ID, MODEL_PROVIDER_HARNESS_ID})


@dataclass(frozen=True, slots=True)
class NexusLaunch:
    """The durable records a bridge run is opened from, resolved by run id."""

    agent: AgentInstance
    session: Session
    execution: Execution


class NexusLaunchResolver(Protocol):
    async def __call__(self, run_id: str) -> NexusLaunch: ...


class SupportsAcomplete(Protocol):
    async def acomplete(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]: ...


class _NexusBridge(Protocol):
    async def create_execution(
        self,
        agent_instance: AgentInstance,
        session: Session,
        task_description: str,
        execution: Execution,
    ) -> Any: ...

    async def execute_step(
        self, execution_id: str, prompt: str, schema: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...

    async def get_run_id_for_execution(self, execution_id: str) -> str | None: ...

    async def request_cancellation(self, run_id: str) -> None: ...


class NexusHarness:
    """The existing bridge, addressed as a harness."""

    harness_id = NEXUS_HARNESS_ID

    def __init__(self, bridge: _NexusBridge, resolve: NexusLaunchResolver) -> None:
        self._bridge = bridge
        self._resolve = resolve

    async def initialize(self, challenge: bytes | None) -> tuple[HarnessInfo, bytes | None]:
        # In-process: no key, so no answer. A challenge arriving here is refused by
        # the caller rather than waved through as answered.
        del challenge
        return HarnessInfo(self.harness_id, PROTOCOL_VERSION, frozenset()), None

    async def session_new(self, run: RunContext) -> SessionHandle:
        launch = await self._resolve(run.run_id)
        # The bridge derives each turn's provider prompt at execute_step, so the
        # task description it accepts here is not the prompt and is not read.
        await self._bridge.create_execution(launch.agent, launch.session, "", launch.execution)
        return SessionHandle(run_id=run.run_id, harness_session_id=launch.execution.execution_id)

    async def session_prompt(self, request: PromptRequest, on_update: UpdateSink) -> TurnResult:
        result = await self._bridge.execute_step(
            request.handle.harness_session_id, request.prompt, request.response_schema
        )
        status = str(result.get("status", ""))
        if status == "error":
            raise HarnessError(str(result.get("error", "")) or "agent step failed")
        if status == "cancelled":
            return TurnResult(StopReason.CANCELLED, dict(result), {})
        action = str(result.get("action", ""))
        await on_update(
            SessionUpdate(
                run_id=request.handle.run_id,
                kind=UpdateKind.TOOL_CALL if action == "tool" else UpdateKind.MESSAGE_DELTA,
                payload={"action": action, "tool": str(result.get("tool", ""))},
            )
        )
        raw_provenance = result.get("provenance")
        provenance = raw_provenance if isinstance(raw_provenance, dict) else {}
        # The harness's turn is over either way: "finish" ends the run, and any other
        # action hands the next move back to the server, which prompts again.
        return TurnResult(StopReason.END_TURN, dict(result), provenance)

    async def session_cancel(self, handle: SessionHandle, reason: str) -> None:
        del reason
        run_id = await self._bridge.get_run_id_for_execution(handle.harness_session_id)
        if run_id is None:
            return
        await self._bridge.request_cancellation(run_id)


class ModelProviderHarness:
    """One prompt is one ``acomplete``, one ``MESSAGE_DELTA``, then ``END_TURN``."""

    harness_id = MODEL_PROVIDER_HARNESS_ID

    def __init__(self, provider: SupportsAcomplete) -> None:
        self._provider = provider
        self._cancelled: set[str] = set()
        self._lock = asyncio.Lock()

    async def initialize(self, challenge: bytes | None) -> tuple[HarnessInfo, bytes | None]:
        del challenge
        return HarnessInfo(self.harness_id, PROTOCOL_VERSION, frozenset()), None

    async def session_new(self, run: RunContext) -> SessionHandle:
        async with self._lock:
            self._cancelled.discard(run.run_id)
        return SessionHandle(run_id=run.run_id, harness_session_id=run.run_id)

    async def session_prompt(self, request: PromptRequest, on_update: UpdateSink) -> TurnResult:
        async with self._lock:
            cancelled = request.handle.run_id in self._cancelled
        if cancelled:
            return TurnResult(StopReason.CANCELLED, {"status": "cancelled"}, {})
        try:
            response = await self._provider.acomplete(request.prompt, request.response_schema)
        except Exception as exc:
            raise HarnessError(str(exc) or "model provider failed") from exc
        raw_output = response.get("output")
        output = dict(raw_output) if isinstance(raw_output, dict) else {"content": raw_output}
        await on_update(
            SessionUpdate(
                run_id=request.handle.run_id,
                kind=UpdateKind.MESSAGE_DELTA,
                payload={"content": str(output.get("content", ""))},
            )
        )
        return TurnResult(
            StopReason.END_TURN,
            {
                "status": "ok",
                "action": str(response.get("action", "finish")),
                "result": output,
            },
            {
                "provider_input": request.prompt,
                "provider_name": str(response.get("provider_name", "")),
                "provider_model": str(response.get("provider_model", "")),
                "provider_response_id": str(response.get("provider_response_id", "")),
                "interventions": [],
                "provider_evidence": str(response.get("provider_evidence", "")),
            },
        )

    async def session_cancel(self, handle: SessionHandle, reason: str) -> None:
        del reason
        async with self._lock:
            self._cancelled.add(handle.run_id)
