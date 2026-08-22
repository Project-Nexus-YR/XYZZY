"""NEXUS bridge: adapts NEXUS runtime into the multiplayer workspace context."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..domain.models import (
    AgentInstance,
    DomainError,
    Execution,
    Session,
    new_id,
)
from ..domain.synthesis import document_schema, spec_for, unavailable_sections
from ..model_providers import ModelProviderError, model_provider_from_environment

log = logging.getLogger(__name__)

# Add NEXUS to path
_nexus_src = str(Path(__file__).resolve().parents[4] / "NEXUS" / "src")
if _nexus_src not in sys.path:
    sys.path.insert(0, _nexus_src)

try:
    from nexus_runtime.agent import AgentExecutor  # type: ignore[import-not-found]
    from nexus_runtime.contracts import (  # type: ignore[import-not-found]
        MemoryProvider,
        ModelProvider,
    )
    from nexus_runtime.events import InMemoryEventBus  # type: ignore[import-not-found]
    from nexus_runtime.models import (  # type: ignore[import-not-found]
        Agent,
        AgentRunState,
        Budget,
    )
    from nexus_runtime.models import (
        DomainError as NexusDomainError,
    )
    from nexus_runtime.persistence import SQLiteStateStore  # type: ignore[import-not-found]
    from nexus_runtime.policy import PolicyEngine  # type: ignore[import-not-found]
    from nexus_runtime.tools import ToolRegistry  # type: ignore[import-not-found]

    _HAS_NEXUS = True
except ImportError:
    log.warning("NEXUS runtime not available; using bridge-native model execution")
    _HAS_NEXUS = False
    NexusDomainError = Exception


@dataclass(frozen=True, slots=True)
class _SpecialistContext:
    name: str
    role: str
    instructions: str
    provider_name: str
    provider_model: str


class StubMemoryProvider:
    """Placeholder memory provider."""

    def recall(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        return []

    def remember(self, record: dict[str, Any]) -> str:
        return new_id("mem")


class NexusAgentBridge:
    """Bridges NEXUS agent execution into the multiplayer room context.

    This class:
    - Creates NEXUS Agent instances from multiplayer AgentInstance records
    - Manages execution lifecycle through NEXUS AgentExecutor
    - Translates NEXUS events into multiplayer RoomEvents
    - Supports pause/resume/cancel/intervene operations

    Thread safety: All mutable shared state is protected by an asyncio.Lock.
    """

    def __init__(
        self,
        model_provider: ModelProvider | None = None,
        memory_provider: MemoryProvider | None = None,
        db_path: str | Path = ":memory:",
    ) -> None:
        self._model = model_provider or model_provider_from_environment()
        self._memory = memory_provider or StubMemoryProvider()
        self._lock = asyncio.Lock()

        if _HAS_NEXUS:
            self._event_bus = InMemoryEventBus()
            self._state_store = SQLiteStateStore(db_path)
            self._executor = AgentExecutor(
                model=self._model,
                memory=self._memory,
                tools=ToolRegistry(PolicyEngine({})),
                event_bus=self._event_bus,
                state_store=self._state_store,
            )
        else:
            self._executor = None

        self._active_runs: dict[str, str] = {}  # execution_id -> run_id
        self._run_executions: dict[str, str] = {}  # run_id -> execution_id
        self._agent_executions: dict[str, str] = {}  # agent_id -> execution_id
        self._cancellation_flags: dict[str, bool] = {}
        self._interventions: dict[str, list[str]] = {}  # run_id -> list of interventions
        self._pending_execution_interventions: dict[str, list[str]] = {}
        self._fallback_states: dict[str, str] = {}
        self._specialist_contexts: dict[str, _SpecialistContext] = {}

    def create_nexus_agent(self, agent_instance: AgentInstance) -> Agent:
        """Convert a multiplayer AgentInstance to a NEXUS Agent."""
        if not _HAS_NEXUS:
            return agent_instance
        return Agent(
            name=agent_instance.name,
            role=agent_instance.role,
            capabilities=agent_instance.capabilities,
            instructions=agent_instance.system_prompt,
            agent_id=agent_instance.agent_id,
        )

    async def create_execution(
        self,
        agent_instance: AgentInstance,
        session: Session,
        task_description: str,
        execution: Execution,
    ) -> tuple[Agent, Budget]:
        """Create a NEXUS agent run for a multiplayer execution."""
        nexus_agent = self.create_nexus_agent(agent_instance)
        specialist_context = _SpecialistContext(
            name=agent_instance.name,
            role=agent_instance.role,
            instructions=agent_instance.system_prompt,
            provider_name=agent_instance.model_provider,
            provider_model=agent_instance.model_name,
        )
        if not _HAS_NEXUS:
            run_id = f"run_{execution.execution_id}"
            async with self._lock:
                self._active_runs[execution.execution_id] = run_id
                self._run_executions[run_id] = execution.execution_id
                self._agent_executions[agent_instance.agent_id] = execution.execution_id
                self._fallback_states[run_id] = "CREATED"
                self._specialist_contexts[execution.execution_id] = specialist_context
                pending = self._pending_execution_interventions.pop(execution.execution_id, [])
                if pending:
                    self._interventions.setdefault(run_id, []).extend(pending)
            return nexus_agent, None
        budget = Budget(
            max_tokens=100_000,
            max_wall_time=timedelta(minutes=30),
            max_tool_calls=50,
            max_workers=4,
            max_experiment_resources=10,
        )
        run = self._executor.create_run(
            agent=nexus_agent,
            investigation_id=session.session_id,
            budget=budget,
            task_id=session.task_id,
            run_id=f"run_{execution.execution_id}",
        )
        async with self._lock:
            self._active_runs[execution.execution_id] = run.run_id
            self._run_executions[run.run_id] = execution.execution_id
            self._agent_executions[agent_instance.agent_id] = execution.execution_id
            self._specialist_contexts[execution.execution_id] = specialist_context
            pending = self._pending_execution_interventions.pop(execution.execution_id, [])
            if pending:
                self._interventions.setdefault(run.run_id, []).extend(pending)
        return nexus_agent, budget

    async def execute_step(
        self,
        execution_id: str,
        prompt: str,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute one step of an agent run. This is the core execution loop."""
        async with self._lock:
            run_id = self._active_runs.get(execution_id)
            if not run_id:
                raise DomainError(f"no active run for execution {execution_id}")

            if self._cancellation_flags.get(run_id):
                return {"status": "cancelled", "reason": "cancellation requested"}

            # Collect and clear interventions atomically
            interventions = list(self._interventions.get(run_id, []))
            if interventions:
                self._interventions[run_id] = []

        if schema is None:
            schema = {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["tool", "finish", "delegate", "wait"]},
                    "tool": {"type": "string"},
                    "input": {"type": "object"},
                    "output": {"type": "object"},
                },
                "required": ["action"],
            }

        # Inject interventions into the prompt
        if interventions:
            intervention_text = "\n".join(f"- HUMAN INTERVENTION: {i}" for i in interventions)
            prompt = f"{prompt}\n\n{intervention_text}\n\nPlease incorporate these instructions."

        context = self._specialist_contexts.get(execution_id)
        provider_prompt = self._build_specialist_prompt(prompt, context)

        if not _HAS_NEXUS:
            try:
                async_complete = getattr(self._model, "acomplete", None)
                if callable(async_complete):
                    response = await async_complete(provider_prompt, schema)
                else:
                    response = await asyncio.to_thread(
                        self._model.complete, provider_prompt, schema
                    )
            except ModelProviderError as exc:
                async with self._lock:
                    self._fallback_states[run_id] = "FAILED"
                return {"status": "error", "error": str(exc)}
            except Exception as exc:
                log.error(
                    "Unexpected model provider failure for execution %s (%s)",
                    execution_id,
                    type(exc).__name__,
                )
                async with self._lock:
                    self._fallback_states[run_id] = "FAILED"
                return {"status": "error", "error": "internal model provider error"}
            if not isinstance(response, dict):
                async with self._lock:
                    self._fallback_states[run_id] = "FAILED"
                return {"status": "error", "error": "model provider returned invalid data"}
            action = str(response.get("action", "finish"))
            raw_output = response.get("output", {})
            output = dict(raw_output) if isinstance(raw_output, dict) else {"content": raw_output}
            if context:
                output.update(
                    {
                        "agent_name": context.name,
                        "analysis_role": context.role,
                    }
                )
            async with self._lock:
                self._fallback_states[run_id] = "COMPLETED" if action == "finish" else "RUNNING"
            raw_input = response.get("input", {})
            return {
                "status": "ok",
                "result": output,
                "action": action,
                "tool": str(response.get("tool", "")),
                "input": dict(raw_input) if isinstance(raw_input, dict) else {},
                "provenance": self._provider_provenance(
                    response=response,
                    output=output,
                    provider_input=provider_prompt,
                    interventions=interventions,
                    context=context,
                ),
            }

        run = self._executor.get_run(run_id)

        # Start if created
        if run.state == AgentRunState.CREATED:
            self._executor.transition(run_id, AgentRunState.RUNNING, "begin execution")

        # Execute one step
        try:
            response = self._executor.reason(run_id, provider_prompt, schema)
            action = self._executor.choose_action(run_id, response)
            result = self._executor.execute_action(run_id, action)
            self._executor.update_state(run_id)
            result_data = result if isinstance(result, dict) else {"result": result}
            return {
                "status": "ok",
                "result": result,
                "action": action.get("action"),
                "provenance": self._provider_provenance(
                    response={},
                    output=result_data,
                    provider_input=provider_prompt,
                    interventions=interventions,
                    context=context,
                ),
            }
        except ModelProviderError as e:
            return {"status": "error", "error": str(e)}
        except DomainError as e:
            return {"status": "error", "error": str(e)}
        except NexusDomainError as e:
            return {"status": "error", "error": str(e)}
        except Exception as exc:
            log.error("Unexpected agent execution failure (%s)", type(exc).__name__)
            return {"status": "error", "error": "internal agent execution error"}

    @staticmethod
    def _build_specialist_prompt(prompt: str, context: _SpecialistContext | None) -> str:
        """Send only the requested task and this specialist's configured context."""
        if context is None:
            return prompt
        instructions = context.instructions.strip() or "Analyze from your assigned role."
        return (
            "You are one specialist in a governed technical decision workflow.\n"
            f"Specialist name: {context.name}\n"
            f"Specialist role: {context.role}\n"
            f"Template instructions: {instructions}\n\n"
            "Work independently from the supplied decision prompt. Clearly distinguish known facts "
            "from AI-derived judgment. Give a recommendation, supporting reasons, material risks, "
            "uncertainties, and the next validation step. Do not claim access to context that is "
            "not included below.\n\n"
            f"Decision prompt:\n{prompt}"
        )

    @staticmethod
    def _provider_provenance(
        *,
        response: dict[str, Any],
        output: dict[str, Any],
        provider_input: str,
        interventions: list[str],
        context: _SpecialistContext | None,
    ) -> dict[str, Any]:
        """Capture only request/response evidence, never provider credentials."""
        evidence = response.get("provider_evidence")
        if not isinstance(evidence, str):
            content = output.get("content")
            evidence = content if isinstance(content, str) else ""
        provider_name = response.get("provider_name") or output.get("provider")
        provider_model = response.get("provider_model") or output.get("model")
        return {
            "provider_input": provider_input,
            "provider_name": str(
                provider_name or (context.provider_name if context is not None else "")
            ),
            "provider_model": str(
                provider_model or (context.provider_model if context is not None else "")
            ),
            "provider_response_id": str(response.get("provider_response_id") or ""),
            "interventions": list(interventions),
            "provider_evidence": evidence,
        }

    async def synthesize_selected_outputs(
        self,
        *,
        title: str,
        prompt: str,
        outputs: list[dict[str, str]],
        synthesis_type: str = "DECISION_BRIEF",
    ) -> dict[str, Any]:
        """Synthesize only the explicitly selected immutable outputs."""
        spec = spec_for(synthesis_type)
        provider_input = self.build_synthesis_provider_input(
            title=title, prompt=prompt, outputs=outputs, synthesis_type=synthesis_type
        )
        schema = document_schema(spec)
        try:
            async_complete = getattr(self._model, "acomplete", None)
            response = (
                await async_complete(provider_input, schema)
                if callable(async_complete)
                else await asyncio.to_thread(self._model.complete, provider_input, schema)
            )
        except ModelProviderError:
            raise
        except Exception as exc:
            log.error("Unexpected synthesis provider failure (%s)", type(exc).__name__)
            raise ModelProviderError("internal model provider error") from exc
        if not isinstance(response, dict):
            raise ModelProviderError("model provider returned invalid synthesis data")
        output = response.get("output")
        output_data = dict(output) if isinstance(output, dict) else {}
        simulated = bool(output_data.get("simulated")) or response.get("provider_name") == (
            "workflow-only"
        )
        if simulated:
            claims = [
                {
                    "text": item["content"],
                    "source_output_ids": [item["output_id"]],
                    "confidence": 0.0,
                }
                for item in outputs
            ]
            parsed: dict[str, Any] = {
                "summary": (
                    "SIMULATED SYNTHESIS — no model provider is configured. "
                    "This deterministic artifact verifies branch selection and provenance only."
                ),
                "claims": claims,
                **unavailable_sections(spec),
            }
        else:
            raw_content = output_data.get("content")
            if not isinstance(raw_content, str):
                raise ModelProviderError("model provider returned no synthesis content")
            try:
                parsed = self._decode_synthesis_json(raw_content)
            except ModelProviderError:
                # Injected legacy transports may ignore the requested JSON schema.
                # The provider response still supplies the synthesis narrative;
                # deterministic source claims keep provenance complete.
                parsed = {
                    "summary": raw_content,
                    "claims": [
                        {
                            "text": item["content"],
                            "source_output_ids": [item["output_id"]],
                            "confidence": 0.5,
                        }
                        for item in outputs
                    ],
                    **unavailable_sections(spec),
                }
        allowed = {item["output_id"] for item in outputs}
        claims_value = parsed.get("claims")
        if not isinstance(claims_value, list) or not claims_value:
            raise ModelProviderError("synthesis must contain at least one sourced claim")
        normalized_claims: list[dict[str, Any]] = []
        for claim in claims_value:
            if not isinstance(claim, dict):
                raise ModelProviderError("synthesis claim is invalid")
            text = str(claim.get("text", "")).strip()
            source_ids = claim.get("source_output_ids")
            confidence = claim.get("confidence")
            if (
                not text
                or not isinstance(source_ids, list)
                or not source_ids
                or any(not isinstance(item, str) or item not in allowed for item in source_ids)
                or not isinstance(confidence, (int, float))
                or not 0 <= float(confidence) <= 1
            ):
                raise ModelProviderError("synthesis claim provenance is invalid")
            normalized_claims.append(
                {
                    "text": text,
                    "source_output_ids": list(dict.fromkeys(source_ids)),
                    "confidence": float(confidence),
                }
            )
        parsed["claims"] = normalized_claims
        evidence = response.get("provider_evidence")
        return {
            "document": parsed,
            "provider_input": provider_input,
            "provider_name": str(
                response.get("provider_name") or output_data.get("provider") or ""
            ),
            "provider_model": str(response.get("provider_model") or output_data.get("model") or ""),
            "provider_response_id": str(response.get("provider_response_id") or ""),
            "provider_evidence": (
                evidence if isinstance(evidence, str) else str(output_data.get("content", ""))
            ),
            "simulated": simulated,
        }

    @staticmethod
    def build_synthesis_provider_input(
        *,
        title: str,
        prompt: str,
        outputs: list[dict[str, str]],
        synthesis_type: str = "DECISION_BRIEF",
    ) -> str:
        spec = spec_for(synthesis_type)
        source_blocks = "\n\n".join(
            f"AgentOutput {item['output_id']} (agent {item['agent_id']}):\n{item['content']}"
            for item in outputs
        )
        return (
            "You are the synthesis stage of a governed technical decision workflow.\n"
            "Use only the selected AgentOutputs below. Preserve disagreement and uncertainty.\n"
            f"{spec.instruction}\n\n"
            f"{spec.artifact_name} title: {title}\n"
            f"Branch prompt: {prompt}\n\n"
            f"Selected AgentOutputs:\n{source_blocks}"
        )

    @staticmethod
    def _decode_synthesis_json(content: str) -> dict[str, Any]:
        stripped = content.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            if len(lines) >= 3 and lines[-1].strip() == "```":
                stripped = "\n".join(lines[1:-1])
                if stripped.lstrip().startswith("json"):
                    stripped = stripped.lstrip()[4:].lstrip()
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ModelProviderError("model provider returned invalid synthesis JSON") from exc
        if not isinstance(parsed, dict):
            raise ModelProviderError("model provider returned invalid synthesis JSON")
        return parsed

    async def request_cancellation(self, run_id: str) -> None:
        async with self._lock:
            self._cancellation_flags[run_id] = True

    async def add_intervention(self, run_id: str, instruction: str) -> None:
        async with self._lock:
            if run_id not in self._interventions:
                self._interventions[run_id] = []
            self._interventions[run_id].append(instruction)

    async def add_execution_intervention(self, execution_id: str, instruction: str) -> None:
        """Queue an intervention even when the provider run has not been created yet."""
        async with self._lock:
            run_id = self._active_runs.get(execution_id)
            if run_id is not None:
                self._interventions.setdefault(run_id, []).append(instruction)
                return
            self._pending_execution_interventions.setdefault(execution_id, []).append(instruction)

    async def get_run_state(self, execution_id: str) -> AgentRunState | None:
        async with self._lock:
            run_id = self._active_runs.get(execution_id)
        if not run_id:
            return None
        if not _HAS_NEXUS:
            return self._fallback_states.get(run_id)
        run = self._executor.get_run(run_id)
        return run.state

    async def pause_execution(self, execution_id: str) -> bool:
        async with self._lock:
            run_id = self._active_runs.get(execution_id)
        if not run_id:
            return False
        if not _HAS_NEXUS:
            async with self._lock:
                self._fallback_states[run_id] = "PAUSED"
            return True
        try:
            self._executor.transition(run_id, AgentRunState.PAUSED, "human requested pause")
            return True
        except (DomainError, NexusDomainError):
            return False

    async def resume_execution(self, execution_id: str) -> bool:
        async with self._lock:
            run_id = self._active_runs.get(execution_id)
        if not run_id:
            return False
        if not _HAS_NEXUS:
            async with self._lock:
                self._fallback_states[run_id] = "RUNNING"
            return True
        try:
            self._executor.transition(run_id, AgentRunState.RUNNING, "human requested resume")
            return True
        except (DomainError, NexusDomainError):
            return False

    async def cancel_execution(self, execution_id: str) -> bool:
        async with self._lock:
            run_id = self._active_runs.get(execution_id)
            if not run_id:
                return False
            self._cancellation_flags[run_id] = True
            if not _HAS_NEXUS:
                self._fallback_states[run_id] = "CANCELLED"
                return True
        try:
            self._executor.transition(run_id, AgentRunState.CANCELLED, "human requested cancel")
            return True
        except (DomainError, NexusDomainError):
            return False

    async def get_execution_for_agent(self, agent_id: str) -> str | None:
        """Get the active execution_id for a given agent_id."""
        async with self._lock:
            return self._agent_executions.get(agent_id)

    async def get_run_id_for_execution(self, execution_id: str) -> str | None:
        """Get the run_id for a given execution_id."""
        async with self._lock:
            return self._active_runs.get(execution_id)

    async def build_delegation(
        self,
        parent_execution_id: str,
        delegated_agent: AgentInstance,
        task: str,
    ) -> str:
        """Create a delegated child run. Returns the child run_id."""
        async with self._lock:
            parent_run_id = self._active_runs.get(parent_execution_id)
        if not parent_run_id:
            raise DomainError(f"no active run for execution {parent_execution_id}")

        child_agent = self.create_nexus_agent(delegated_agent)
        child_budget = Budget(
            max_tokens=50_000,
            max_wall_time=timedelta(minutes=15),
            max_tool_calls=25,
            max_workers=2,
            max_experiment_resources=5,
        )
        child = self._executor.build_delegation(
            parent_run_id=parent_run_id,
            delegated_to=child_agent,
            task=task,
            budget=child_budget,
        )
        return str(child.run_id)

    async def checkpoint(self, execution_id: str) -> None:
        async with self._lock:
            run_id = self._active_runs.get(execution_id)
        if run_id:
            self._executor.checkpoint(run_id)

    async def get_run_outputs(self, execution_id: str) -> dict[str, Any]:
        async with self._lock:
            run_id = self._active_runs.get(execution_id)
        if not run_id:
            return {}
        run = self._executor.get_run(run_id)
        outputs = run.outputs
        return dict(outputs) if isinstance(outputs, dict) else {}

    async def cleanup_execution(self, execution_id: str) -> None:
        """Remove tracking state for a completed/failed execution."""
        async with self._lock:
            run_id = self._active_runs.pop(execution_id, None)
            if run_id:
                self._run_executions.pop(run_id, None)
                self._cancellation_flags.pop(run_id, None)
                self._interventions.pop(run_id, None)
            self._pending_execution_interventions.pop(execution_id, None)
            self._specialist_contexts.pop(execution_id, None)
            # Remove from agent_executions
            agent_to_remove = None
            for aid, eid in self._agent_executions.items():
                if eid == execution_id:
                    agent_to_remove = aid
                    break
            if agent_to_remove:
                self._agent_executions.pop(agent_to_remove)
