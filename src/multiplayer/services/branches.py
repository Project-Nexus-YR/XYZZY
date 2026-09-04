"""Branches and synthesis: parallel runs, decision briefs, and their ontology."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import replace
from typing import Any

from ..domain.events import EventType, RoomEvent
from ..domain.models import (
    AgentOutput,
    AgentRun,
    Artifact,
    ArtifactClaim,
    ArtifactType,
    ArtifactVersion,
    Branch,
    BranchMode,
    BranchStatus,
    BranchSynthesis,
    BranchSynthesisInput,
    BranchSynthesisStatus,
    ClaimSource,
    DecisionStatus,
    DomainError,
    Execution,
    ExecutionStatus,
    IdempotencyConflict,
    OntologyDerivationKind,
    OntologyEntity,
    OntologyEntityKind,
    OntologyRelationship,
    OntologyRelationshipKind,
    OutputDisposition,
    OutputSelection,
    Session,
    SessionStatus,
    TurnLock,
    TurnLockScopeType,
    TurnLockStatus,
    new_id,
    utcnow,
)
from ..domain.synthesis import (
    SynthesisSpec,
    SynthesisType,
    spec_for,
)
from ..domain.synthesis import (
    render as render_synthesis,
)
from ..model_providers import ModelProviderError
from ..security.authorization import (
    AuthorizationError,
    RoomCapability,
    capabilities_for_role,
)
from ..security.screening import fenced, screen
from ._shared import (
    AgentLaunchRefused,
    _SharedMixin,
)

log = logging.getLogger(__name__)


class _BranchesMixin(_SharedMixin):
    """Mixin providing the branches surface of MultiplayerService."""

    async def start_branch(
        self,
        room_id: str,
        mode: BranchMode,
        initiating_prompt: str,
        initiated_by: str,
        agent_ids: list[str],
        idempotency_key: str | None = None,
    ) -> tuple[Branch, list[Execution]]:
        """Atomically freeze context, create AgentRuns, and optionally own the room turn."""
        initiating_prompt = self._validate_non_empty(initiating_prompt, "branch prompt")
        if idempotency_key is not None:
            idempotency_key = self._validate_idempotency_key(idempotency_key)
        request = {"mode": mode.value, "prompt": initiating_prompt, "agent_ids": list(agent_ids)}
        unique_agent_ids = list(dict.fromkeys(agent_ids))
        if unique_agent_ids != agent_ids:
            raise DomainError("branch agent ids must be unique")
        expected = 1 if mode == BranchMode.TURN_LOCKED_SINGLE else None
        if expected is not None and len(agent_ids) != expected:
            raise DomainError("turn-locked single mode requires exactly one agent")
        if mode == BranchMode.PARALLEL and not 2 <= len(agent_ids) <= 3:
            raise DomainError("parallel mode requires two or three agents")
        agents = [await self.get_agent(agent_id) for agent_id in agent_ids]
        if any(agent.room_id != room_id for agent in agents):
            raise DomainError("every branch agent must belong to the room")
        # Addressing and identity are gates that close before a run row exists. The
        # BEFORE INSERT triggers below repeat the identity leg, so a revocation racing
        # this preparation is still refused at the write.
        prepared: dict[str, AgentRun] = {}
        for agent in agents:
            try:
                await self._require_addressable(agent, room_id, initiated_by)
                prepared_run = await self._prepare_agent_run(agent, room_id, initiated_by)
            except AgentLaunchRefused as refusal:
                await self._record_launch_refusal(refusal)
                raise
            prepared[agent.agent_id] = prepared_run

        persisted_events: list[RoomEvent] = []
        executions: list[Execution] = []
        async with self.db.transaction():
            await self._require_mutate_in_transaction(room_id, initiated_by)
            if idempotency_key is not None:
                prior = await self._claim_idempotency(
                    room_id, initiated_by, idempotency_key, "branch.start", request
                )
                if prior is not None:
                    replay = await self.repos.branches.get(prior.result_ref)
                    if replay is None:
                        raise DomainError("idempotent branch replay lost its result")
                    return replay, await self.repos.executions.list_by_branch(replay.branch_id)
            active_lock = await self.repos.turn_locks.get_active(TurnLockScopeType.ROOM, room_id)
            if active_lock is not None:
                raise DomainError(f"room turn is locked by branch {active_lock.branch_id}")
            sequence = await self.repos.events.get_latest_sequence(room_id)
            messages = await self.repos.messages.list_by_room(room_id, limit=50)
            events = await self.repos.events.list_since(room_id, max(0, sequence - 100), limit=100)
            snapshot = {
                "schema": "xyzzy.branch-context.v1",
                "limits": {"messages": 50, "events": 100},
                "messages": [
                    {
                        "message_id": message.message_id,
                        "role": message.role.value,
                        "sender_id": message.sender_id,
                        "content": message.content,
                        "metadata": message.metadata,
                        "created_at": message.created_at.isoformat(),
                    }
                    for message in messages
                ],
                "events": [
                    {
                        "event_id": event.event_id,
                        "sequence": event.sequence,
                        "event_type": event.event_type.value,
                        "payload": event.payload,
                        "actor_id": event.actor_id,
                        "actor_type": event.actor_type,
                        "timestamp": event.timestamp.isoformat(),
                    }
                    for event in events
                    if event.sequence <= sequence
                ],
            }
            message_ids = tuple(message.message_id for message in messages)
            context_envelope = {
                "initiating_prompt": initiating_prompt,
                "context_event_sequence": sequence,
                "context_message_ids": list(message_ids),
                "context_snapshot": snapshot,
            }
            context_hash = hashlib.sha256(
                json.dumps(
                    context_envelope,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            branch = Branch(
                branch_id=new_id("branch"),
                room_id=room_id,
                mode=mode,
                status=BranchStatus.RUNNING,
                initiated_by=initiated_by,
                initiating_prompt=initiating_prompt,
                context_event_sequence=sequence,
                context_message_ids=message_ids,
                context_snapshot=snapshot,
                context_hash=context_hash,
            )
            await self.repos.branches.create(branch)
            lock: TurnLock | None = None
            if mode == BranchMode.TURN_LOCKED_SINGLE:
                lock = TurnLock(
                    lock_id=new_id("lock"),
                    scope_type=TurnLockScopeType.ROOM,
                    scope_id=room_id,
                    branch_id=branch.branch_id,
                    status=TurnLockStatus.ACTIVE,
                    acquired_by=initiated_by,
                )
                await self.repos.turn_locks.create(lock)
            for agent in agents:
                session = Session(
                    session_id=new_id("sess"),
                    room_id=room_id,
                    agent_id=agent.agent_id,
                    status=SessionStatus.ACTIVE,
                )
                execution = Execution(
                    execution_id=new_id("exec"),
                    session_id=session.session_id,
                    agent_id=agent.agent_id,
                    authorized_by=initiated_by,
                    branch_id=branch.branch_id,
                    status=ExecutionStatus.PENDING,
                    input_data={
                        "initiating_prompt": initiating_prompt,
                        "context_hash": context_hash,
                    },
                )
                await self.repos.sessions.create(session)
                await self.repos.executions.create(execution)
                await self.repos.agent_runs.create_in_transaction(
                    replace(prepared[agent.agent_id], execution_id=execution.execution_id)
                )
                executions.append(execution)
            events_to_persist = [
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.BRANCH_STARTED,
                    payload={
                        "branch_id": branch.branch_id,
                        "mode": mode.value,
                        "status": branch.status.value,
                        "context_event_sequence": sequence,
                        "context_message_ids": list(message_ids),
                        "context_hash": context_hash,
                        "execution_ids": [run.execution_id for run in executions],
                    },
                    actor_id=initiated_by,
                    actor_type="user",
                )
            ]
            if lock is not None:
                events_to_persist.append(
                    RoomEvent(
                        room_id=room_id,
                        sequence=0,
                        event_type=EventType.TURN_LOCK_ACQUIRED,
                        payload={
                            "lock_id": lock.lock_id,
                            "scope_type": lock.scope_type.value,
                            "scope_id": lock.scope_id,
                            "branch_id": branch.branch_id,
                        },
                        actor_id=initiated_by,
                        actor_type="user",
                    )
                )
            for run in executions:
                events_to_persist.append(
                    RoomEvent(
                        room_id=room_id,
                        sequence=0,
                        event_type=EventType.AGENT_RUN_STARTED,
                        payload={
                            "branch_id": branch.branch_id,
                            "execution_id": run.execution_id,
                            "session_id": run.session_id,
                            "agent_id": run.agent_id,
                        },
                        actor_id=initiated_by,
                        actor_type="user",
                    )
                )
            for event in events_to_persist:
                persisted_events.append(
                    await self.repos.events.append_with_next_sequence_in_transaction(event)
                )
            if idempotency_key is not None:
                await self._record_idempotency(
                    room_id,
                    initiated_by,
                    idempotency_key,
                    "branch.start",
                    request,
                    branch.branch_id,
                )
        await self._broadcast_persisted_events(persisted_events)
        return branch, executions

    async def list_room_branches(self, room_id: str) -> list[Branch]:
        await self.get_room(room_id)
        return await self.repos.branches.list_by_room(room_id)

    async def list_branch_runs(self, branch_id: str) -> list[Execution]:
        await self.get_branch(branch_id)
        return await self.repos.executions.list_by_branch(branch_id)

    @staticmethod
    def _branch_execution_prompt(branch: Branch) -> str:
        if not branch.lifecycle_managed:
            return branch.initiating_prompt
        snapshot = json.dumps(
            branch.context_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        # The snapshot is member-authored room history - the widest untrusted
        # surface any prompt carries - so it enters screened and fenced.
        return (
            f"Branch prompt:\n{branch.initiating_prompt}\n\n"
            f"Immutable bounded channel context (hash {branch.context_hash}):\n"
            f"{fenced(screen(snapshot, 'channel context'))}"
        )

    async def select_output(
        self,
        room_id: str,
        output_id: str,
        disposition: OutputDisposition,
        decided_by: str,
    ) -> OutputSelection:
        output = await self.repos.agent_outputs.get(output_id)
        if output is None or output.room_id != room_id:
            raise DomainError("agent output not found in room")
        selection = OutputSelection(
            room_id=room_id,
            output_id=output_id,
            disposition=disposition,
            decided_by=decided_by,
            branch_id=output.branch_id,
        )
        async with self.db.transaction():
            await self._require_mutate_in_transaction(room_id, decided_by)
            event = await self.repos.output_selections.upsert_with_event_in_transaction(
                selection,
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.OUTPUT_SELECTION_UPDATED,
                    payload={
                        "branch_id": output.branch_id,
                        "output_id": output_id,
                        "disposition": disposition.value,
                    },
                    actor_id=decided_by,
                    actor_type="user",
                ),
            )
        await self._broadcast_persisted_events([event])
        return selection

    async def list_output_selections(self, room_id: str) -> list[OutputSelection]:
        await self.get_room(room_id)
        return await self.repos.output_selections.list_by_room(room_id)

    async def select_branch_output(
        self,
        branch_id: str,
        output_id: str,
        disposition: OutputDisposition,
        decided_by: str,
    ) -> OutputSelection:
        branch = await self.get_branch(branch_id)
        output = await self.repos.agent_outputs.get(output_id)
        if output is None or output.branch_id != branch_id:
            raise DomainError("agent output not found in branch")
        return await self.select_output(branch.room_id, output_id, disposition, decided_by)

    async def synthesize_decision_brief(
        self, room_id: str, title: str | None, created_by: str
    ) -> tuple[Artifact, ArtifactVersion]:
        """Compatibility route: resolve one selected Branch, then synthesize that unit."""
        selections = await self.list_output_selections(room_id)
        selected_ids = {
            item.output_id for item in selections if item.disposition == OutputDisposition.INCLUDED
        }
        outputs = [
            output
            for output in await self.list_room_outputs(room_id)
            if output.output_id in selected_ids
        ]
        branch_ids = {output.branch_id for output in outputs}
        if len(branch_ids) != 1:
            raise DomainError("selected outputs must belong to exactly one branch")
        return await self.synthesize_branch_decision_brief(branch_ids.pop(), title, created_by)

    async def synthesize_branch_decision_brief(
        self,
        branch_id: str,
        title: str | None,
        created_by: str,
        idempotency_key: str | None = None,
    ) -> tuple[Artifact, ArtifactVersion]:
        """Compatibility route: the Decision Brief is one of three synthesis types."""
        return await self.synthesize_branch(
            branch_id,
            title,
            created_by,
            synthesis_type=SynthesisType.DECISION_BRIEF,
            idempotency_key=idempotency_key,
        )

    async def synthesize_branch(
        self,
        branch_id: str,
        title: str | None,
        created_by: str,
        synthesis_type: str = SynthesisType.DECISION_BRIEF,
        idempotency_key: str | None = None,
    ) -> tuple[Artifact, ArtifactVersion]:
        """Run model-backed synthesis over this Branch's explicit selected outputs."""
        spec = spec_for(synthesis_type)
        if idempotency_key is not None:
            idempotency_key = self._validate_idempotency_key(idempotency_key)
        branch = await self.get_branch(branch_id)
        if title is None or not title.strip():
            # No caller-supplied title: derive one from what this branch is actually
            # about, so every untitled brief is not stamped with the same stale
            # placeholder decision.
            prompt = branch.initiating_prompt.strip()
            title = prompt[:80] if prompt else "Decision"
        title = self._validate_non_empty(title, f"{spec.artifact_name.lower()} title")
        operation = f"branch.synthesis.{spec.type.lower()}"
        request = {"title": title}
        outputs = await self.repos.agent_outputs.list_by_branch(branch_id)
        selections = await self.repos.output_selections.list_by_branch(branch_id)
        decisions = {selection.output_id: selection.disposition for selection in selections}
        minimum_included = 1 if branch.mode == BranchMode.TURN_LOCKED_SINGLE else 2
        if len(outputs) < minimum_included:
            raise DomainError(
                f"at least {minimum_included} branch output(s) are required for this mode"
            )
        unreviewed = [output.output_id for output in outputs if output.output_id not in decisions]
        if unreviewed:
            raise DomainError("every branch output must be included or excluded")
        included = [
            output
            for output in outputs
            if decisions[output.output_id] == OutputDisposition.INCLUDED
        ]
        if len(included) < minimum_included:
            raise DomainError(
                f"at least {minimum_included} branch output(s) must be included for this mode"
            )
        runs = await self.repos.executions.list_by_branch(branch_id)
        if any(
            run.status
            not in {
                ExecutionStatus.COMPLETED,
                ExecutionStatus.FAILED,
                ExecutionStatus.CANCELLED,
            }
            for run in runs
        ):
            raise DomainError("branch synthesis requires every AgentRun to be terminal")

        selected_records = [
            {"output_id": output.output_id, "agent_id": output.agent_id, "content": output.content}
            for output in included
        ]
        provider_input = self.nexus.build_synthesis_provider_input(
            title=title,
            prompt=branch.initiating_prompt,
            outputs=selected_records,
            synthesis_type=spec.type.value,
        )
        synthesis = BranchSynthesis(
            synthesis_id=new_id("syn"),
            branch_id=branch_id,
            room_id=branch.room_id,
            title=title,
            initiated_by=created_by,
            status=BranchSynthesisStatus.RUNNING,
            synthesis_type=spec.type.value,
            provider_input=provider_input,
        )
        inputs = [
            BranchSynthesisInput(
                synthesis_id=synthesis.synthesis_id,
                output_id=output.output_id,
                ordinal=ordinal,
            )
            for ordinal, output in enumerate(included, start=1)
        ]
        async with self.db.transaction():
            await self._require_mutate_in_transaction(branch.room_id, created_by)
            if idempotency_key is not None:
                prior = await self._claim_idempotency(
                    branch_id, created_by, idempotency_key, operation, request
                )
                if prior is not None:
                    return await self._replay_branch_synthesis(prior.result_ref)
            await self.repos.branch_syntheses.create_with_inputs(synthesis, inputs)
            if idempotency_key is not None:
                await self._record_idempotency(
                    branch_id,
                    created_by,
                    idempotency_key,
                    operation,
                    request,
                    synthesis.synthesis_id,
                )
        try:
            model_result = await self.nexus.synthesize_selected_outputs(
                title=title,
                prompt=branch.initiating_prompt,
                outputs=selected_records,
                synthesis_type=spec.type.value,
            )
            return await self._complete_branch_synthesis(
                branch, synthesis, inputs, included, title, created_by, model_result, spec
            )
        except Exception as exc:
            # The key was claimed before the model call. Any failure after that
            # point must leave a terminal FAILED row, so a replay says "retry with
            # a new key" instead of reporting the synthesis as running forever.
            await self._fail_branch_synthesis(branch, synthesis, inputs, created_by, str(exc))
            if isinstance(exc, ModelProviderError):
                raise DomainError(str(exc)) from exc
            raise

    async def _fail_branch_synthesis(
        self,
        branch: Branch,
        synthesis: BranchSynthesis,
        inputs: list[BranchSynthesisInput],
        created_by: str,
        error: str,
    ) -> None:
        async with self.db.transaction():
            current = await self.repos.branch_syntheses.get(synthesis.synthesis_id)
            if current is None or current.status is not BranchSynthesisStatus.RUNNING:
                return
            await self.repos.branch_syntheses.mark_failed(synthesis.synthesis_id, error)
            member = await self.repos.room_members.get(branch.room_id, created_by)
            if RoomCapability.MUTATE not in capabilities_for_role(member.role if member else None):
                # Initiator lost write access during the model call: the RUNNING row
                # is now terminal, but attribute no ordered event to a non-member.
                return
            started_event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=branch.room_id,
                    sequence=0,
                    event_type=EventType.BRANCH_SYNTHESIS_STARTED,
                    payload={
                        "branch_id": branch.branch_id,
                        "synthesis_id": synthesis.synthesis_id,
                        "selected_output_ids": [item.output_id for item in inputs],
                    },
                    actor_id=created_by,
                    actor_type="user",
                    timestamp=synthesis.created_at,
                )
            )
            failed_event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=branch.room_id,
                    sequence=0,
                    event_type=EventType.BRANCH_SYNTHESIS_FAILED,
                    payload={
                        "branch_id": branch.branch_id,
                        "synthesis_id": synthesis.synthesis_id,
                    },
                    actor_id=created_by,
                    actor_type="user",
                )
            )
        await self._broadcast_persisted_events([started_event, failed_event])

    async def _complete_branch_synthesis(
        self,
        branch: Branch,
        synthesis: BranchSynthesis,
        inputs: list[BranchSynthesisInput],
        included: list[AgentOutput],
        title: str,
        created_by: str,
        model_result: dict[str, Any],
        spec: SynthesisSpec,
    ) -> tuple[Artifact, ArtifactVersion]:
        branch_id = branch.branch_id
        self._record_model_tokens(model_result)
        document_value = model_result.get("document")
        if not isinstance(document_value, dict):
            raise DomainError("model provider returned invalid synthesis document")
        document = document_value
        content = render_synthesis(spec, title, document, bool(model_result["simulated"]))
        existing = None
        for artifact in await self.list_room_artifacts(branch.room_id):
            # Name alone is not identity: a synthesis only ever extends a lineage that a
            # synthesis published, never one someone wrote by hand under the same name.
            if artifact.name == spec.artifact_name and await self._is_published_synthesis(
                artifact.artifact_id
            ):
                existing = artifact
                break
        create_artifact = existing is None
        artifact = existing or Artifact(
            artifact_id=new_id("art"),
            room_id=branch.room_id,
            name=spec.artifact_name,
            artifact_type=ArtifactType.DOCUMENT,
            description="Human-selected, provenance-complete specialist synthesis",
            created_by=created_by,
        )
        version_id = new_id("ver")
        version = ArtifactVersion(
            version_id=version_id,
            artifact_id=artifact.artifact_id,
            version_number=artifact.current_version + 1,
            content=content,
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            branch_synthesis_id=synthesis.synthesis_id,
            created_by=created_by,
        )
        output_by_id = {output.output_id: output for output in included}
        claims_and_sources: list[tuple[ArtifactClaim, ClaimSource]] = []
        raw_claims = document.get("claims")
        if not isinstance(raw_claims, list):
            raise DomainError("synthesis claims are invalid")
        for ordinal, raw_claim in enumerate(raw_claims, start=1):
            if not isinstance(raw_claim, dict):
                raise DomainError("synthesis claim is invalid")
            claim = ArtifactClaim(
                claim_id=new_id("claim"),
                version_id=version_id,
                ordinal=ordinal,
                text=str(raw_claim["text"]),
                is_ai_derived=True,
                confidence=float(raw_claim["confidence"]),
            )
            for output_id in raw_claim["source_output_ids"]:
                output = output_by_id[str(output_id)]
                claims_and_sources.append((claim, self._claim_source(claim.claim_id, output)))
        provenance_records = [
            self._claim_provenance_record(claim, source) for claim, source in claims_and_sources
        ]
        version = replace(
            version,
            provenance_hash=self._artifact_provenance_hash(version, provenance_records),
        )
        raw_token_usage = model_result.get("token_usage", 0)
        terminal_synthesis = replace(
            synthesis,
            status=BranchSynthesisStatus.COMPLETED,
            provider_name=str(model_result["provider_name"]),
            provider_model=str(model_result["provider_model"]),
            provider_response_id=str(model_result["provider_response_id"]),
            provider_evidence=str(model_result["provider_evidence"]),
            simulated=bool(model_result["simulated"]),
            content=content,
            artifact_version_id=version.version_id,
            completed_at=utcnow(),
            token_usage=raw_token_usage if isinstance(raw_token_usage, int) else 0,
        )
        event_types: list[RoomEvent] = []
        if create_artifact:
            event_types.append(
                RoomEvent(
                    room_id=branch.room_id,
                    sequence=0,
                    event_type=EventType.ARTIFACT_CREATED,
                    payload={
                        "artifact_id": artifact.artifact_id,
                        "name": artifact.name,
                        "type": artifact.artifact_type.value,
                    },
                    actor_id=created_by,
                    actor_type="user",
                )
            )
        event_types.extend(
            [
                RoomEvent(
                    room_id=branch.room_id,
                    sequence=0,
                    event_type=EventType.BRANCH_SYNTHESIS_STARTED,
                    payload={
                        "branch_id": branch_id,
                        "synthesis_id": synthesis.synthesis_id,
                        "selected_output_ids": [item.output_id for item in inputs],
                    },
                    actor_id=created_by,
                    actor_type="user",
                    timestamp=synthesis.created_at,
                ),
                RoomEvent(
                    room_id=branch.room_id,
                    sequence=0,
                    event_type=(
                        EventType.DECISION_BRIEF_SYNTHESIZED
                        if spec.type is SynthesisType.DECISION_BRIEF
                        else EventType.SYNTHESIS_PUBLISHED
                    ),
                    payload={
                        "branch_id": branch_id,
                        "synthesis_type": spec.type.value,
                        "synthesis_id": synthesis.synthesis_id,
                        "artifact_id": artifact.artifact_id,
                        "version_id": version.version_id,
                        "version": version.version_number,
                        "content_hash": version.content_hash,
                        "provenance_hash": version.provenance_hash,
                        "selected_output_ids": [output.output_id for output in included],
                        "simulated": terminal_synthesis.simulated,
                    },
                    actor_id=created_by,
                    actor_type="user",
                ),
                RoomEvent(
                    room_id=branch.room_id,
                    sequence=0,
                    event_type=EventType.BRANCH_SYNTHESIS_COMPLETED,
                    payload={
                        "branch_id": branch_id,
                        "synthesis_id": synthesis.synthesis_id,
                        "artifact_version_id": version.version_id,
                        "simulated": terminal_synthesis.simulated,
                    },
                    actor_id=created_by,
                    actor_type="user",
                ),
            ]
        )
        ontology_entities: list[OntologyEntity] = []
        ontology_relationships: list[OntologyRelationship] = []
        if spec.type is SynthesisType.DECISION_BRIEF:
            # Only a Decision Brief asserts a decision; a synthesis or a progress report
            # would materialize a DECISION entity that nobody made.
            ontology_entities, ontology_relationships = await self._decision_brief_ontology(
                room_id=branch.room_id,
                title=title,
                created_by=created_by,
                artifact=artifact,
                version=version,
                claims_and_sources=claims_and_sources,
                included=included,
            )
            event_types.append(
                RoomEvent(
                    room_id=branch.room_id,
                    sequence=0,
                    event_type=EventType.ONTOLOGY_MATERIALIZED,
                    payload={
                        "artifact_id": artifact.artifact_id,
                        "version_id": version.version_id,
                        "entity_ids": [entity.entity_id for entity in ontology_entities],
                        "relationship_ids": [
                            item.relationship_id for item in ontology_relationships
                        ],
                    },
                    actor_id=created_by,
                    actor_type="user",
                )
            )
        aborted = False
        persisted_events: list[RoomEvent] = []
        async with self.db.transaction():
            member = await self.repos.room_members.get(branch.room_id, created_by)
            if RoomCapability.MUTATE not in capabilities_for_role(member.role if member else None):
                # Demoted during the model call: terminate the RUNNING row without
                # attributing any ordered event to a member who lost write access.
                await self.repos.branch_syntheses.mark_failed(
                    synthesis.synthesis_id, "initiator lost write access during synthesis"
                )
                aborted = True
            else:
                persisted_events = await self.repos.artifacts.create_synthesis_in_transaction(
                    artifact,
                    version,
                    claims_and_sources,
                    ontology_entities,
                    ontology_relationships,
                    event_types,
                    create_artifact=create_artifact,
                    synthesis=terminal_synthesis,
                )
        if aborted:
            raise AuthorizationError("room access forbidden")
        await self._broadcast_persisted_events(persisted_events)
        return replace(artifact, current_version=version.version_number), version

    @staticmethod
    def _claim_source(claim_id: str, output: AgentOutput) -> ClaimSource:
        return ClaimSource(
            claim_id=claim_id,
            output_id=output.output_id,
            evidence=output.content,
            agent_id=output.agent_id,
            execution_id=output.execution_id,
            source_prompt=output.source_prompt,
            provider_input=output.provider_input,
            provider_name=output.provider_name,
            provider_model=output.provider_model,
            provider_response_id=output.provider_response_id,
            provider_interventions=output.provider_interventions,
            provider_evidence=output.provider_evidence,
        )

    @staticmethod
    def _ontology_id(prefix: str, room_id: str, *source_ids: str) -> str:
        material = ":".join((room_id, *source_ids)).encode()
        return f"{prefix}_{hashlib.sha256(material).hexdigest()[:24]}"

    async def _decision_brief_ontology(
        self,
        *,
        room_id: str,
        title: str,
        created_by: str,
        artifact: Artifact,
        version: ArtifactVersion,
        claims_and_sources: list[tuple[ArtifactClaim, ClaimSource]],
        included: list[AgentOutput],
    ) -> tuple[list[OntologyEntity], list[OntologyRelationship]]:
        """Project the published brief without inferring beyond frozen evidence."""
        room = await self.get_room(room_id)
        creator = await self.repos.users.get(created_by)
        selected_output_ids = tuple(output.output_id for output in included)
        claim_ids = tuple(claim.claim_id for claim, _source in claims_and_sources)
        timestamp = version.created_at

        project_id = self._ontology_id("ont", room_id, "Project", room_id)
        person_id = self._ontology_id("ont", room_id, "Person", created_by)
        artifact_entity_id = self._ontology_id("ont", room_id, "Artifact", version.version_id)
        decision_id = self._ontology_id("ont", room_id, "Decision", version.version_id)
        entities = [
            OntologyEntity(
                entity_id=project_id,
                room_id=room_id,
                kind=OntologyEntityKind.PROJECT,
                source_object_id=room_id,
                label=room.name,
                properties={"workspace_id": room.workspace_id},
                evidence_ids=(room_id,),
                source_ids=(room_id,),
                created_at=timestamp,
                updated_at=timestamp,
            ),
            OntologyEntity(
                entity_id=person_id,
                room_id=room_id,
                kind=OntologyEntityKind.PERSON,
                source_object_id=created_by,
                label=creator.display_name if creator is not None else created_by,
                properties={"user_id": created_by},
                evidence_ids=(created_by,),
                source_ids=(created_by,),
                created_at=timestamp,
                updated_at=timestamp,
            ),
            OntologyEntity(
                entity_id=artifact_entity_id,
                room_id=room_id,
                kind=OntologyEntityKind.ARTIFACT,
                source_object_id=version.version_id,
                label=f"{artifact.name} v{version.version_number}",
                properties={
                    "artifact_id": artifact.artifact_id,
                    "version_id": version.version_id,
                    "version_number": version.version_number,
                    "content_hash": version.content_hash,
                    "provenance_hash": version.provenance_hash,
                },
                evidence_ids=(version.version_id,),
                source_ids=(artifact.artifact_id, version.version_id),
                created_at=timestamp,
                updated_at=timestamp,
            ),
            OntologyEntity(
                entity_id=decision_id,
                room_id=room_id,
                kind=OntologyEntityKind.DECISION,
                source_object_id=version.version_id,
                label=title,
                properties={
                    # A published Decision Brief is a decision taken, and it says so
                    # here: every Decision entity carries its status, so the question
                    # "what has been decided" is a query rather than an inference.
                    "status": DecisionStatus.ACTIVE.value,
                    "artifact_id": artifact.artifact_id,
                    "version_id": version.version_id,
                    "claim_ids": list(claim_ids),
                },
                derivation_kind=OntologyDerivationKind.AI_DERIVED,
                confidence=1.0,
                evidence_ids=selected_output_ids,
                source_ids=(version.version_id, *claim_ids),
                created_at=timestamp,
                updated_at=timestamp,
            ),
        ]
        claim_entity_ids: dict[str, str] = {}
        output_entity_ids: dict[str, str] = {}
        for output in included:
            output_entity_id = self._ontology_id("ont", room_id, "AgentOutput", output.output_id)
            output_entity_ids[output.output_id] = output_entity_id
            entities.append(
                OntologyEntity(
                    entity_id=output_entity_id,
                    room_id=room_id,
                    kind=OntologyEntityKind.AGENT_OUTPUT,
                    source_object_id=output.output_id,
                    label=f"Agent output {output.output_id}",
                    properties={
                        "agent_id": output.agent_id,
                        "execution_id": output.execution_id,
                        "provider_name": output.provider_name,
                        "provider_model": output.provider_model,
                    },
                    derivation_kind=OntologyDerivationKind.AI_DERIVED,
                    confidence=1.0,
                    evidence_ids=(output.output_id,),
                    source_ids=(output.output_id, output.execution_id),
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
        for claim, source in claims_and_sources:
            claim_entity_id = self._ontology_id("ont", room_id, "Claim", claim.claim_id)
            claim_entity_ids[claim.claim_id] = claim_entity_id
            entities.append(
                OntologyEntity(
                    entity_id=claim_entity_id,
                    room_id=room_id,
                    kind=OntologyEntityKind.CLAIM,
                    source_object_id=claim.claim_id,
                    label=claim.text,
                    properties={
                        "version_id": claim.version_id,
                        "ordinal": claim.ordinal,
                        "is_ai_derived": claim.is_ai_derived,
                    },
                    derivation_kind=OntologyDerivationKind.AI_DERIVED,
                    confidence=claim.confidence,
                    evidence_ids=(source.output_id,),
                    source_ids=(claim.claim_id, claim.version_id, source.output_id),
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )

        relationships: list[OntologyRelationship] = []

        def relationship(
            kind: OntologyRelationshipKind,
            from_entity_id: str,
            to_entity_id: str,
            derivation_kind: OntologyDerivationKind,
            evidence_ids: tuple[str, ...],
            source_ids: tuple[str, ...],
            source_object: tuple[str, str],
        ) -> None:
            relationships.append(
                OntologyRelationship(
                    relationship_id=self._ontology_id(
                        "rel", room_id, kind.value, from_entity_id, to_entity_id
                    ),
                    room_id=room_id,
                    kind=kind,
                    from_entity_id=from_entity_id,
                    to_entity_id=to_entity_id,
                    derivation_kind=derivation_kind,
                    evidence_ids=evidence_ids,
                    source_ids=source_ids,
                    # The durable row whose content states the relation, so a
                    # relationship-centric answer can drill down to it.
                    source_object_kind=source_object[0],
                    source_object_id=source_object[1],
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )

        published_version = (OntologyEntityKind.ARTIFACT.value, version.version_id)
        relationship(
            OntologyRelationshipKind.OWNS,
            project_id,
            artifact_entity_id,
            OntologyDerivationKind.SYSTEM_MATERIALIZED,
            (version.version_id,),
            (room_id, artifact.artifact_id, version.version_id),
            published_version,
        )
        relationship(
            OntologyRelationshipKind.OWNS,
            person_id,
            artifact_entity_id,
            OntologyDerivationKind.SYSTEM_MATERIALIZED,
            (version.version_id,),
            (created_by, artifact.artifact_id, version.version_id),
            published_version,
        )
        relationship(
            OntologyRelationshipKind.REFERENCES,
            artifact_entity_id,
            decision_id,
            OntologyDerivationKind.SYSTEM_MATERIALIZED,
            (version.version_id,),
            (artifact.artifact_id, version.version_id),
            published_version,
        )
        for claim, source in claims_and_sources:
            claim_entity_id = claim_entity_ids[claim.claim_id]
            output_entity_id = output_entity_ids[source.output_id]
            exact_evidence = (source.output_id,)
            stating_claim = (OntologyEntityKind.CLAIM.value, claim.claim_id)
            relationship(
                OntologyRelationshipKind.SUPPORTS,
                claim_entity_id,
                decision_id,
                OntologyDerivationKind.AI_DERIVED,
                exact_evidence,
                (claim.claim_id, source.output_id, version.version_id),
                stating_claim,
            )
            relationship(
                OntologyRelationshipKind.DERIVED_FROM,
                claim_entity_id,
                output_entity_id,
                OntologyDerivationKind.AI_DERIVED,
                exact_evidence,
                (claim.claim_id, source.output_id),
                stating_claim,
            )
            relationship(
                OntologyRelationshipKind.DERIVED_FROM,
                decision_id,
                output_entity_id,
                OntologyDerivationKind.AI_DERIVED,
                exact_evidence,
                (version.version_id, claim.claim_id, source.output_id),
                (OntologyEntityKind.AGENT_OUTPUT.value, source.output_id),
            )
        return entities, relationships

    @staticmethod
    def _claim_provenance_record(claim: ArtifactClaim, source: ClaimSource) -> dict[str, Any]:
        return {
            "claim_id": claim.claim_id,
            "ordinal": claim.ordinal,
            "text": claim.text,
            "is_ai_derived": int(claim.is_ai_derived),
            "confidence": claim.confidence,
            "output_id": source.output_id,
            "evidence": source.evidence,
            "agent_id": source.agent_id,
            "execution_id": source.execution_id,
            "source_prompt": source.source_prompt,
            "provider_input": source.provider_input,
            "provider_name": source.provider_name,
            "provider_model": source.provider_model,
            "provider_response_id": source.provider_response_id,
            "provider_interventions": list(source.provider_interventions),
            "provider_evidence": source.provider_evidence,
        }

    @classmethod
    def verify_artifact_provenance_hash(
        cls, version: ArtifactVersion, claims: list[dict[str, Any]]
    ) -> bool:
        actual_content_hash = hashlib.sha256(version.content.encode()).hexdigest()
        if actual_content_hash != version.content_hash:
            return False
        expected_hash = cls._artifact_provenance_hash(
            replace(version, content_hash=actual_content_hash), claims
        )
        return bool(version.provenance_hash) and version.provenance_hash == expected_hash

    async def _replay_branch_synthesis(self, synthesis_id: str) -> tuple[Artifact, ArtifactVersion]:
        synthesis = await self.repos.branch_syntheses.get(synthesis_id)
        if synthesis is None:
            raise DomainError("idempotent synthesis replay lost its result")
        if synthesis.status is BranchSynthesisStatus.FAILED:
            raise IdempotencyConflict(
                f"synthesis {synthesis_id} failed; retry with a new idempotency key"
            )
        if synthesis.artifact_version_id is None:
            raise IdempotencyConflict(
                f"synthesis {synthesis_id} is still running; replay the key after it completes"
            )
        version = await self.repos.artifacts.get_version(synthesis.artifact_version_id)
        artifact = await self.repos.artifacts.get(version.artifact_id) if version else None
        if version is None or artifact is None:
            raise DomainError("idempotent synthesis replay lost its artifact")
        return artifact, version
