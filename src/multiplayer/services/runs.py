"""Runs: sessions, leases, pause and resume, cancellation, approvals, and intervention."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import timedelta
from typing import Any

from ..domain.events import EventType, RoomEvent
from ..domain.models import (
    AgentStatus,
    AgentTrigger,
    Approval,
    ApprovalStatus,
    DomainError,
    Execution,
    ExecutionIntervention,
    ExecutionStatus,
    HarnessState,
    RunSettlement,
    Session,
    SessionStatus,
    ToolRequest,
    new_id,
    utcnow,
)
from ..harness import (
    SessionUpdate,
)
from ..security.authorization import (
    AuthorizationError,
    RoomCapability,
)
from ..security.boundary import require_human_boundary
from ..security.capabilities import (
    BoundingPrincipals,
)
from ..security.identity import (
    credential_matches,
)
from ._shared import (
    _STREAMING_LEASE,
    VALID_EXECUTION_TRANSITIONS,
    VALID_SESSION_TRANSITIONS,
    AgentLaunchRefused,
    _SharedMixin,
    _TurnContinuation,
    _validate_transition,
)

log = logging.getLogger(__name__)


class _RunsMixin(_SharedMixin):
    """Mixin providing the runs surface of MultiplayerService."""

    async def _renew_run_lease(self, update: SessionUpdate) -> None:
        """The streaming callback is the run's heartbeat: every update renews its lease."""
        run = await self.repos.agent_runs.get(update.run_id)
        if run is None or run.harness_state is HarnessState.SETTLED:
            return
        await self.repos.agent_runs.advance(
            run.run_id, run.harness_state, utcnow() + _STREAMING_LEASE, run.acting_user_id
        )

    async def _advance_run_for_execution(
        self,
        execution_id: str,
        state: HarnessState,
        acting_user_id: str,
        lease: timedelta,
        expected: HarnessState | None = None,
    ) -> bool:
        """Move the envelope and renew its lease. A settled run never moves.

        ``expected``, when given, refuses the move unless the run is still in
        that state, so a caller can tell a genuine advance from a race that
        already moved the run somewhere else.
        """
        run = await self.repos.agent_runs.get_by_execution(execution_id)
        if run is None or run.harness_state is HarnessState.SETTLED:
            return False
        if expected is not None and run.harness_state is not expected:
            return False
        return await self.repos.agent_runs.advance(
            run.run_id,
            state,
            utcnow() + lease,
            acting_user_id or run.acting_user_id,
            expected=expected,
        )

    async def sweep_expired_run_leases(self) -> int:
        """Settle every run whose lease ran out, so none sits unclaimed by anything.

        A run picked up its full allowance of attempts that died every time is PARKED
        rather than ORPHANED. Both are terminal; the difference is what a reader is
        told about why nothing is coming, which is the whole point of settling it.

        A run holding at a reviewer is a third thing, and it used to be told the
        second one: nothing was orphaned, nothing was dispatched and lost, and no
        attempt was spent — a person simply never answered. Naming that outcome is
        only half of it, because the approval row it belongs to sat PENDING for ever
        against a run that had ended. It is closed with the run, in
        :meth:`_settle_run`, rather than only here.
        """
        settled = 0
        for run in await self.repos.agent_runs.list_expired(utcnow()):
            if run.harness_state is HarnessState.AWAITING_APPROVAL:
                settlement = RunSettlement.APPROVAL_EXPIRED
                error = "no reviewer decided the approval this run was waiting on"
            elif run.attempts >= run.max_attempts:
                settlement = RunSettlement.PARKED
                error = f"lease expired after {run.attempts} attempt(s)"
            else:
                settlement = RunSettlement.ORPHANED
                error = f"lease expired after {run.attempts} attempt(s)"
            if await self._settle_run(run, settlement, "system", error):
                settled += 1
        # The periodic caller of this method (server.py's lease-sweep loop) is
        # the only thing that revisits a long-lived process's runs at all, so
        # it is also the thing that has to notice a task stranded WORKING
        # behind one of the runs just settled above, or by anything else.
        await self.sweep_stranded_working_agent_tasks()
        return settled

    async def record_session_update(
        self, run_id: str, credential: str, update: SessionUpdate
    ) -> None:
        """Accept one harness-originated update, or refuse it.

        The per-run credential is compared as an opaque token, and a settled run is
        refused whatever it presents: the turn it belonged to is over.
        """
        run = await self.repos.agent_runs.get(run_id)
        if run is None or not credential_matches(credential, run.credential_hash):
            raise AuthorizationError("run credential rejected")
        if run.harness_state is HarnessState.SETTLED:
            raise DomainError(f"run {run_id} is settled ({run.settlement}) and accepts no updates")
        await self.repos.agent_runs.advance(
            run.run_id, HarnessState.STREAMING, utcnow() + _STREAMING_LEASE, run.acting_user_id
        )
        del update

    async def start_agent_session(
        self, room_id: str, agent_id: str, task_id: str | None = None
    ) -> Session:
        agent = await self.get_agent(agent_id)
        if agent.room_id != room_id:
            raise DomainError("agent is not in this room")
        # The instance column says where the agent was created; membership says whether
        # it is still there. Every other launch door reads membership, and this one did
        # not, so a removed agent still got a durable session row and a room event
        # announcing that it had started work.
        if not await self.repos.agents.has_room_membership(agent_id, room_id):
            raise AgentLaunchRefused(
                agent_id, room_id, "not_a_member", f"agent {agent_id} is not in room {room_id}"
            )
        session = Session(
            session_id=new_id("sess"), room_id=room_id, agent_id=agent_id, task_id=task_id
        )
        await self.repos.sessions.create(session)
        await self._append_room_event(
            room_id,
            EventType.SESSION_STARTED,
            {"session_id": session.session_id, "agent_id": agent_id},
            agent_id,
            "agent",
        )
        return session

    async def start_execution(
        self, session_id: str, authorized_by: str, input_data: dict[str, Any] | None = None
    ) -> Execution:
        session = await self.repos.sessions.get(session_id)
        if not session:
            raise DomainError(f"session not found: {session_id}")
        _validate_transition(
            session.status, SessionStatus.ACTIVE, VALID_SESSION_TRANSITIONS, "session"
        )
        agent = await self.get_agent(session.agent_id)
        try:
            await self._require_addressable(agent, session.room_id, authorized_by)
            run = await self._prepare_agent_run(agent, session.room_id, authorized_by)
        except AgentLaunchRefused as refusal:
            await self._record_launch_refusal(refusal)
            raise
        execution = Execution(
            execution_id=new_id("exec"),
            session_id=session_id,
            agent_id=session.agent_id,
            authorized_by=authorized_by,
            input_data=input_data or {},
        )
        event = await self.repos.executions.start_with_event(
            execution,
            RoomEvent(
                room_id=session.room_id,
                sequence=0,
                event_type=EventType.AGENT_RUN_STARTED,
                payload={
                    "execution_id": execution.execution_id,
                    "session_id": session_id,
                    "agent_id": session.agent_id,
                },
                actor_id=session.agent_id,
                actor_type="agent",
            ),
            run,
        )
        await self._broadcast_persisted_events([event])
        await self._set_agent_status_safe(session.agent_id, AgentStatus.WORKING)
        persisted = await self.repos.executions.get(execution.execution_id)
        return persisted or execution

    async def execute_branch_run(
        self, branch_id: str, execution_id: str, acting_as: str = ""
    ) -> dict[str, Any]:
        branch = await self.get_branch(branch_id)
        execution = await self.repos.executions.get(execution_id)
        if execution is None or execution.branch_id != branch.branch_id:
            raise DomainError("agent run not found in branch")
        return await self.execute_agent_step(execution_id, branch.initiating_prompt, acting_as)

    async def pause_execution(self, execution_id: str, acting_as: str = "") -> bool:
        require_human_boundary("run.pause")
        execution = await self.repos.executions.get(execution_id)
        if execution is None:
            raise DomainError("execution not found")
        await self._require_delegated_authority(execution, acting_as)
        branch = await self.get_branch(execution.branch_id)
        if not branch.lifecycle_managed:
            return await self.nexus.pause_execution(execution_id)
        _validate_transition(
            execution.status, ExecutionStatus.PAUSED, VALID_EXECUTION_TRANSITIONS, "execution"
        )
        ok = await self.nexus.pause_execution(execution_id)
        if not ok:
            return False
        await self.repos.executions.update_status(
            execution_id, ExecutionStatus.PAUSED, execution.status
        )
        return True

    async def resume_execution(self, execution_id: str, acting_as: str = "") -> bool:
        require_human_boundary("run.resume")
        execution = await self.repos.executions.get(execution_id)
        if execution is None:
            raise DomainError("execution not found")
        await self._require_delegated_authority(execution, acting_as)
        branch = await self.get_branch(execution.branch_id)
        if not branch.lifecycle_managed:
            return await self.nexus.resume_execution(execution_id)
        _validate_transition(
            execution.status, ExecutionStatus.RUNNING, VALID_EXECUTION_TRANSITIONS, "execution"
        )
        ok = await self.nexus.resume_execution(execution_id)
        if not ok:
            return False
        await self.repos.executions.update_status(
            execution_id, ExecutionStatus.RUNNING, execution.status
        )
        return True

    async def resume_agent_run(
        self, run_id: str, resumed_by: str, *, require_member: bool = False
    ) -> Execution:
        """Continue a settled run as a new one, with the same identity and fresh authority.

        A settled run is never resumed in place: re-adopting a state nobody observed is
        exactly the ambiguity settling it removed. A parked run is not resumed at all —
        it has already used every attempt it was allowed.
        """
        require_human_boundary("run.reopen")
        previous = await self.repos.agent_runs.get(run_id)
        if previous is None:
            raise DomainError(f"agent run not found: {run_id}")
        if previous.harness_state is not HarnessState.SETTLED:
            raise DomainError(f"agent run {run_id} is still open")
        if previous.settlement is RunSettlement.PARKED:
            raise DomainError(f"agent run {run_id} is parked after {previous.attempts} attempts")
        agent = await self.get_agent(previous.agent_id)
        earlier = await self.repos.executions.get(previous.execution_id)
        try:
            await self._require_addressable(agent, previous.room_id, resumed_by)
            run = await self._prepare_agent_run(
                agent,
                previous.room_id,
                resumed_by,
                resumed_from_run_id=previous.run_id,
                attempts=previous.attempts + 1,
            )
        except AgentLaunchRefused as refusal:
            await self._record_launch_refusal(refusal)
            raise
        session = Session(
            session_id=new_id("sess"),
            room_id=previous.room_id,
            agent_id=agent.agent_id,
            status=SessionStatus.ACTIVE,
        )
        execution = Execution(
            execution_id=new_id("exec"),
            session_id=session.session_id,
            agent_id=agent.agent_id,
            # A resume does not re-root the run. On a delegated turn the chain's
            # root human is who the whole chain is authorized by, and writing the
            # resumer here replaced them — widening the bound to whoever pressed
            # resume. The resumer is a caller, which is a row, and it is written
            # below once the run exists to hang it on.
            authorized_by=(
                earlier.authorized_by
                if earlier is not None and earlier.agent_task_id
                else resumed_by
            ),
            triggered_by=earlier.triggered_by if earlier is not None else AgentTrigger.DIRECT,
            input_data=dict(earlier.input_data) if earlier is not None else {},
        )
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(previous.room_id, resumed_by)
            await self.repos.sessions.create(session)
            execution = await self.repos.executions.create(execution)
            await self.repos.agent_runs.create_in_transaction(
                replace(run, execution_id=execution.execution_id)
            )
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=previous.room_id,
                    sequence=0,
                    event_type=EventType.AGENT_RUN_STARTED,
                    payload={
                        "execution_id": execution.execution_id,
                        "session_id": session.session_id,
                        "agent_id": agent.agent_id,
                        "resumed_from_run_id": previous.run_id,
                        "attempt": run.attempts,
                    },
                    actor_id=resumed_by,
                    actor_type="user",
                )
            )
        await self.repos.executions.record_caller(execution.execution_id, resumed_by)
        await self._broadcast_persisted_events([event])
        return execution

    async def cancel_execution(
        self, execution_id: str, cancelled_by: str, *, require_member: bool = False
    ) -> bool:
        """Cancel a run durably, wherever the process driving it happens to be.

        The bridge's map of run to execution is one process's memory. It used to be
        the whole cancel on a branch that is not lifecycle-managed — which is every
        room's default branch — and a veto on one that is: a second process, or the
        same process after a restart, found nothing in the map, returned False and
        wrote nothing, so the run went on until the lease sweep named it something
        else. Telling the bridge is a best-effort stop signal to a turn that may be
        in flight here; the durable settlement below is the cancellation, and it is
        the same on any process.
        """
        require_human_boundary("run.cancel")
        execution = await self.repos.executions.get(execution_id)
        if execution is None:
            raise DomainError("execution not found")
        if require_member:
            await self._require_delegated_authority(execution, cancelled_by)
        if execution.status in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }:
            raise DomainError("execution is already terminal")
        await self.nexus.cancel_execution(execution_id)
        session = await self.repos.sessions.get(execution.session_id)
        if session is None:
            raise DomainError("session not found")
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(session.room_id, cancelled_by)
            events = await self.repos.executions.terminalize_without_output_in_transaction(
                execution,
                ExecutionStatus.CANCELLED,
                "cancelled by user",
                [
                    RoomEvent(
                        room_id=session.room_id,
                        sequence=0,
                        event_type=EventType.EXECUTION_CANCELLED,
                        payload={
                            "branch_id": execution.branch_id,
                            "execution_id": execution.execution_id,
                        },
                        actor_id=cancelled_by,
                        actor_type="user",
                    )
                ],
                RunSettlement.CANCELLED,
                cancelled_by,
            )
        # Nothing prompts a cancelled run again, so a turn held at a reviewer is not
        # waiting on one either — nor is the approval that turn stopped at.
        await self.repos.suspended_turns.discard(execution_id)
        await self._expire_undecided_approvals(execution_id, "cancelled by user")
        await self._broadcast_persisted_events(events)
        return True

    @staticmethod
    def _intervention_for(
        execution: Execution, intervened_by: str, instruction: str
    ) -> ExecutionIntervention:
        """The steer to persist: who steered and what they said, never what they held.

        A capability set written here would be an authorization input frozen at the
        moment the text was accepted, and the row is immutable, so narrowing that
        person afterwards could not reach it. The step that spends this instruction
        re-derives her grant instead, which is how every other authority in this
        service is read.
        """
        return ExecutionIntervention(
            intervention_id=new_id("interv"),
            execution_id=execution.execution_id,
            intervened_by=intervened_by,
            instruction=instruction,
        )

    async def intervene_execution(
        self, execution_id: str, user_id: str, instruction: str, *, require_member: bool = False
    ) -> None:
        """Record a human redirect against a running execution. The ordered event is
        appended inside the transaction that re-checks membership, so a member demoted
        while the runtime intervention is dispatched cannot author it."""
        require_human_boundary("run.intervene")
        execution = await self.repos.executions.get(execution_id)
        if execution is None:
            raise DomainError("execution not found")
        agent = await self.get_agent(execution.agent_id)
        if require_member:
            await self._require_delegated_authority(execution, user_id)
        intervention = self._intervention_for(execution, user_id, instruction)
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(agent.room_id, user_id)
            # The bound commits with the event that records the steer, before the
            # text is queued for a prompt: nothing reaches a provider unbounded.
            await self.repos.interventions.create(intervention)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=agent.room_id,
                    sequence=0,
                    event_type=EventType.HUMAN_REDIRECTED_AGENT,
                    payload={"agent_id": execution.agent_id, "instruction": instruction},
                    actor_id=user_id,
                    actor_type="user",
                )
            )
        await self.nexus.add_execution_intervention(execution_id, instruction)
        await self._broadcast_persisted_events([event])

    @staticmethod
    def _output_content(output_data: dict[str, Any]) -> str:
        """Derive readable content while preserving the complete structured payload."""
        for key in ("content", "result", "text", "answer"):
            value = output_data.get(key)
            if isinstance(value, str):
                return value
        return json.dumps(output_data, sort_keys=True, default=str)

    async def request_approval(
        self,
        room_id: str,
        execution_id: str,
        agent_id: str,
        action_description: str,
        *,
        requested_by: str = "",
        authorized_by: str = "",
        require_member: bool = False,
    ) -> Approval:
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(room_id, requested_by)
            approval, event = await self._request_approval_in_transaction(
                room_id, execution_id, agent_id, action_description, authorized_by
            )
        await self._set_agent_status_safe(agent_id, AgentStatus.WAITING_APPROVAL)
        await self._broadcast_persisted_events([event])
        return approval

    async def _request_approval_in_transaction(
        self,
        room_id: str,
        execution_id: str,
        agent_id: str,
        action_description: str,
        authorized_by: str,
    ) -> tuple[Approval, RoomEvent]:
        """Open one approval for a caller that already owns the write transaction.

        The gateway needs it, because the approval and the rest of the turn it holds
        up have to commit together or not at all.
        """
        approval = Approval(
            approval_id=new_id("appr"),
            room_id=room_id,
            execution_id=execution_id,
            agent_id=agent_id,
            action_description=action_description,
            authorized_by=authorized_by,
        )
        await self.repos.approvals.create(approval)
        event = await self.repos.events.append_with_next_sequence_in_transaction(
            RoomEvent(
                room_id=room_id,
                sequence=0,
                event_type=EventType.APPROVAL_REQUESTED,
                payload={
                    "approval_id": approval.approval_id,
                    "agent_id": agent_id,
                    "action": action_description,
                },
                actor_id=agent_id,
                actor_type="agent",
            )
        )
        return approval, event

    async def approve_action(
        self, approval_id: str, reviewer_id: str, comment: str = "", *, require_member: bool = False
    ) -> Approval:
        require_human_boundary("approval.approve")
        async with self.db.transaction():
            approval = await self.repos.approvals.get(approval_id)
            if not approval:
                raise DomainError(f"approval not found: {approval_id}")
            if approval.status != ApprovalStatus.PENDING:
                raise DomainError(
                    f"approval {approval_id} is not pending (current: {approval.status.value})"
                )
            if require_member:
                await self._require_capability_in_transaction(
                    approval.room_id, reviewer_id, RoomCapability.ADMINISTER
                )
            approval = Approval(
                approval_id=approval.approval_id,
                room_id=approval.room_id,
                execution_id=approval.execution_id,
                agent_id=approval.agent_id,
                action_description=approval.action_description,
                authorized_by=approval.authorized_by,
                status=ApprovalStatus.APPROVED,
                reviewer_id=reviewer_id,
                review_comment=comment,
                requested_at=approval.requested_at,
                reviewed_at=utcnow(),
            )
            await self.repos.approvals.update(approval)
            pending = self._request_this_approval_gated(
                approval, await self.repos.tool_requests.get_by_approval(approval_id)
            )
            if pending is not None:
                # The reviewer grants from their own capabilities, never above them:
                # an approval is not a way to lend what the reviewer does not hold.
                # She is written down first and the derivation below reads her back
                # out with everybody else — rather than being handed to it as the one
                # identity this door happens to know about.
                #
                # Against this call, not against the run. Releasing one call is not
                # taking the run over: recording her as a caller of it put her grant
                # over every call it made afterwards, so an administrator scoped to
                # `retrieval` who approved a single read turned the run's later writes
                # from paused into refused. It failed closed, so nobody obtained
                # anything — but they approved one call and bounded a hundred, which
                # is a reach, and one that teaches people not to answer approvals.
                await self.repos.tool_requests.record_reviewer(pending.request_id, reviewer_id)
                # Re-derived inside the transaction that grants rather than after it
                # closed; the re-stamped effective set is an audit record, never an
                # input, because the writer re-derives again inside its own.
                decision, effective = await self._current_tool_decision(pending)
                run = await self.repos.agent_runs.get_by_execution(pending.execution_id)
                if run is not None and run.harness_state is HarnessState.SETTLED:
                    # The run this call belongs to ended while the reviewer was
                    # deciding. Releasing the approval now would let output arrive
                    # after the settlement, through the one door that outlives it.
                    decision = replace(
                        decision,
                        allowed=False,
                        reason=f"run {run.run_id} is settled ({run.settlement})",
                    )
                stamped = json.dumps(sorted(effective))
                await self.repos.tool_requests.set_effective(pending.request_id, stamped)
                pending = replace(pending, effective_json=stamped)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=approval.room_id,
                    sequence=0,
                    event_type=EventType.APPROVAL_GRANTED,
                    payload={"approval_id": approval_id, "reviewer_id": reviewer_id},
                    actor_id=reviewer_id,
                    actor_type="user",
                )
            )
        await self._set_agent_status_safe(approval.agent_id, AgentStatus.WORKING)
        await self._broadcast_persisted_events([event])
        if pending is not None:
            if decision.allowed:
                # Under the principal the turn was parked on, not under the reviewer.
                # `agent_runs.advance` writes its acting human into the run's callers,
                # so naming her here would put back — through the database, where it
                # is harder to see — exactly the run-wide bound the line above stopped
                # taking. It is the same principal the park named, which is the same
                # one `_resume_suspended_turn` carries the rest of the turn under.
                await self._advance_run_for_execution(
                    pending.execution_id,
                    HarnessState.STREAMING,
                    pending.authorized_by,
                    _STREAMING_LEASE,
                )
                resolved = await self._execute_tool_request(pending)
            else:
                # The capability was withdrawn between the request and the grant; a
                # human's approval cannot restore what the policy no longer permits.
                await self._resolve_tool_request_terminal(
                    pending,
                    "REJECTED",
                    decision.reason,
                    "{}",
                    EventType.TOOL_CALL_REJECTED,
                    {
                        "request_id": pending.request_id,
                        "tool": pending.tool,
                        "required_capability": decision.required_capability,
                        "effective": sorted(effective),
                        "reason": decision.reason,
                    },
                )
                resolved = replace(pending, status="REJECTED", reason=decision.reason)
            # The turn stopped at this reviewer. Running the tool is not what the
            # room was waiting for; the answer is, so the rest of the turn runs now.
            await self._resume_suspended_turn(pending.execution_id, resolved)
        return approval

    async def _resume_suspended_turn(self, execution_id: str, request: ToolRequest | None) -> None:
        """Carry a turn that stopped at a reviewer through to its answer.

        It resumes under the principals it suspended under, not under the reviewer:
        she decided one tool call, and lending her grant to the rest of the turn
        would be a wider authority than anyone asked her for. Every prompt re-derives
        from durable records regardless, so a grant withdrawn while she deliberated
        still stops the next call.

        The approval is committed, and failing it here would tell the reviewer her
        decision was lost when it was not. Swallowing the refusal is not the same as
        absorbing it, though: ``claim`` has already deleted the continuation, so a
        refusal that goes nowhere leaves the run STREAMING with a NULL settlement,
        nobody about to prompt it and no record of why — the one state
        ``_continue_agent_turn`` promises cannot happen. A step that refuses itself
        settles the run on the way out; a refusal reaching here settled nothing, so
        it is settled here instead of vanishing.

        The same is true of finding no continuation at all. The gate that opened the
        approval writes both in one transaction, so absence here means the row was
        lost after that commit rather than never written — and the decision above has
        already issued the STREAMING lease it was meant to spend. Returning would leave
        that lease held by nobody, which is the fourth route into the state this
        docstring rules out. Nothing is carrying the run and nothing will, so it is
        settled ORPHANED now rather than by a sweep a quarter of an hour later.
        """
        parked = await self.repos.suspended_turns.claim(execution_id)
        if parked is None:
            await self._settle_unresumable_turn(
                execution_id,
                "",
                RunSettlement.ORPHANED,
                "the rest of this turn was not there to resume after its approval decision",
            )
            return
        turn = _TurnContinuation(
            prompt=str(parked["prompt"]),
            acting_as=str(parked["acting_as"]),
            observations=list(parked["observations"]),
        )
        if request is not None:
            response = self._tool_response(request)
            turn.observations.append(self._tool_observation(response["tool_request"]))
        if await self._park_if_attempts_spent(execution_id, turn.acting_as) is not None:
            return
        try:
            await self._continue_agent_turn(execution_id, turn)
        except AuthorizationError as refusal:
            log.info("Turn for %s was refused after its approval decision", execution_id)
            await self._settle_unresumable_turn(
                execution_id, turn.acting_as, RunSettlement.AUTHORITY_REVOKED, str(refusal)
            )
        except DomainError as failure:
            log.info("Turn for %s could not resume after its approval decision", execution_id)
            await self._settle_unresumable_turn(
                execution_id, turn.acting_as, RunSettlement.FAILED, str(failure)
            )

    async def _settle_unresumable_turn(
        self, execution_id: str, acting_as: str, settlement: RunSettlement, error: str
    ) -> None:
        """Say what became of a run whose continuation refused to run.

        A no-op when the step that raised already settled it, so the first true
        account of the run is the one that stands.
        """
        run = await self.repos.agent_runs.get_by_execution(execution_id)
        if run is None or run.harness_state is HarnessState.SETTLED:
            return
        await self._settle_run(run, settlement, acting_as or "system", error)
        await self._set_agent_status_safe(run.agent_id, AgentStatus.FAILED)

    async def reject_action(
        self,
        approval_id: str,
        reviewer_id: str,
        comment: str = "",
        *,
        require_member: bool = False,
        continue_turn: bool = False,
    ) -> Approval:
        require_human_boundary("approval.reject")
        """Refuse one gated tool call, and say what becomes of the run.

        Rejection used to resolve the request and stop, leaving the run
        AWAITING_APPROVAL: not settled, not leased, and unsweepable. It now ends in one
        of two named places inside the transaction that writes it — settled
        APPROVAL_REFUSED, or returned to STREAMING on a fresh lease when the reviewer
        refuses the tool but wants the turn continued. No third path leaves the run
        where it found it.
        """
        events: list[RoomEvent] = []
        async with self.db.transaction():
            approval = await self.repos.approvals.get(approval_id)
            if not approval:
                raise DomainError(f"approval not found: {approval_id}")
            if approval.status != ApprovalStatus.PENDING:
                raise DomainError(
                    f"approval {approval_id} is not pending (current: {approval.status.value})"
                )
            if require_member:
                await self._require_capability_in_transaction(
                    approval.room_id, reviewer_id, RoomCapability.ADMINISTER
                )
            approval = Approval(
                approval_id=approval.approval_id,
                room_id=approval.room_id,
                execution_id=approval.execution_id,
                agent_id=approval.agent_id,
                action_description=approval.action_description,
                authorized_by=approval.authorized_by,
                status=ApprovalStatus.REJECTED,
                reviewer_id=reviewer_id,
                review_comment=comment,
                requested_at=approval.requested_at,
                reviewed_at=utcnow(),
            )
            await self.repos.approvals.update(approval)
            events.append(
                await self.repos.events.append_with_next_sequence_in_transaction(
                    RoomEvent(
                        room_id=approval.room_id,
                        sequence=0,
                        event_type=EventType.APPROVAL_REJECTED,
                        payload={"approval_id": approval_id, "reviewer_id": reviewer_id},
                        actor_id=reviewer_id,
                        actor_type="user",
                    )
                )
            )
            gated = self._request_this_approval_gated(
                approval, await self.repos.tool_requests.get_by_approval(approval_id)
            )
            pending = gated
            if pending is not None:
                await self.repos.tool_requests.resolve_in_transaction(
                    pending.request_id, "REJECTED", "approval rejected", "{}"
                )
                events.append(
                    await self.repos.events.append_with_next_sequence_in_transaction(
                        RoomEvent(
                            room_id=pending.room_id,
                            sequence=0,
                            event_type=EventType.TOOL_CALL_REJECTED,
                            payload={
                                "request_id": pending.request_id,
                                "tool": pending.tool,
                                "reason": "approval rejected",
                            },
                            actor_id=pending.agent_id,
                            actor_type="agent",
                        )
                    )
                )
                pending = replace(pending, status="REJECTED", reason="approval rejected")
            events.extend(
                await self._end_refused_approval_in_transaction(
                    approval.execution_id, gated, reviewer_id, continue_turn
                )
            )
        await self._broadcast_persisted_events(events)
        if gated is not None:
            if continue_turn:
                # The fresh lease above is only honest if something is about to prompt
                # this run again. That is here.
                await self._resume_suspended_turn(approval.execution_id, pending)
            else:
                await self.repos.suspended_turns.discard(approval.execution_id)
        return approval

    @staticmethod
    def _request_this_approval_gated(
        approval: Approval, request: ToolRequest | None
    ) -> ToolRequest | None:
        """The undecided tool call this approval is actually holding, if it is holding one.

        An approval that gated nothing is a record of a question, and deciding it is a
        record of an answer. It is not an account of why a run ended, and it used to be
        allowed to write one: any member could open an approval against a live run
        through the approvals route, reject it, and settle that run APPROVAL_REFUSED —
        an untrue account of a run nobody had refused anything to. With
        ``continue_turn`` it put the run back on a fresh STREAMING lease with nothing
        suspended to prompt it, which is the state the turn loop promises cannot exist.
        """
        if request is None or request.status != "PENDING_APPROVAL":
            return None
        if request.execution_id != approval.execution_id:
            return None
        return request

    async def _end_refused_approval_in_transaction(
        self, execution_id: str, gated: ToolRequest | None, reviewer_id: str, continue_turn: bool
    ) -> list[RoomEvent]:
        """Settle the run this approval was holding, or put it back on a fresh lease.

        Never neither — and never a run this approval was not holding. ``gated`` is
        the undecided tool call the approval gated, and without one there is nothing
        here to end: refusing a question nobody's turn was waiting on leaves the run
        exactly where it was found.
        """
        if gated is None:
            return []
        run = await self.repos.agent_runs.get_by_execution(execution_id)
        if run is None or run.harness_state is HarnessState.SETTLED:
            return []
        if continue_turn:
            # The same reach as the approve path, and refused for a stronger reason:
            # this reviewer released nothing at all, so putting her name on the advance
            # would bound every remaining call of the run by somebody who said no to
            # one of them. The turn continues under the principal it was parked on.
            await self.repos.agent_runs.advance(
                run.run_id,
                HarnessState.STREAMING,
                utcnow() + _STREAMING_LEASE,
                gated.authorized_by or run.acting_user_id,
            )
            return []
        execution = await self.repos.executions.get(execution_id)
        if execution is not None and execution.status not in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }:
            return await self.repos.executions.terminalize_without_output_in_transaction(
                execution,
                ExecutionStatus.CANCELLED,
                "approval refused",
                [],
                RunSettlement.APPROVAL_REFUSED,
                reviewer_id,
            )
        return [
            await self.repos.events.append_with_next_sequence_in_transaction(event)
            for event in await self.repos.agent_runs.settle_in_transaction(
                execution_id, RunSettlement.APPROVAL_REFUSED, reviewer_id
            )
        ]

    async def list_pending_approvals(self, room_id: str) -> list[Approval]:
        return await self.repos.approvals.list_pending_by_room(room_id)

    async def _agent_run_to_steer(self, agent_id: str) -> Execution | None:
        """The run an agent-scoped steer reaches: the live one, else the recorded one.

        The bridge's map of agent to run is in-memory. It is empty after a restart
        and for a run another process is dispatching, so absence there says nothing
        about whether a run exists — the records do.
        """
        execution_id = await self.nexus.get_execution_for_agent(agent_id)
        execution = await self.repos.executions.get(execution_id) if execution_id else None
        if execution is not None:
            return execution
        return await self.repos.executions.latest_open_for_agent(agent_id)

    async def _require_agent_run_authority(self, agent_id: str, acting_as: str) -> None:
        """The agent-scoped doors reach whatever run the agent is serving, so the
        run's own authorization bounds them exactly as the run-scoped doors.

        With no run to bound them, they used to check nothing at all. An absent run
        is not an absent caller: steering an agent is making it act, so the caller
        still has to hold what it takes to make this agent act here.
        """
        execution = await self._agent_run_to_steer(agent_id)
        if execution is not None:
            await self._require_delegated_authority(execution, acting_as)
            return
        agent = await self.get_agent(agent_id)
        bounding = BoundingPrincipals(frozenset({acting_as}))
        if not (await self._lendable_terms(agent, agent.room_id, bounding)).lendable():
            raise AuthorizationError(
                f"{acting_as} may not steer agent {agent_id}: no effective capability"
            )

    async def interrupt_agent(
        self, agent_id: str, user_id: str, reason: str = "", *, require_member: bool = False
    ) -> None:
        require_human_boundary("agent.interrupt")
        agent = await self.get_agent(agent_id)
        if require_member:
            await self._require_agent_run_authority(agent_id, user_id)
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(agent.room_id, user_id)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=agent.room_id,
                    sequence=0,
                    event_type=EventType.HUMAN_INTERRUPTED_AGENT,
                    payload={"agent_id": agent_id, "reason": reason},
                    actor_id=user_id,
                    actor_type="user",
                )
            )
        execution_id = await self.nexus.get_execution_for_agent(agent_id)
        if execution_id:
            await self.nexus.pause_execution(execution_id)
        await self._set_agent_status_safe(agent_id, AgentStatus.PAUSED)
        await self._broadcast_persisted_events([event])

    async def redirect_agent(
        self, agent_id: str, user_id: str, instruction: str, *, require_member: bool = False
    ) -> None:
        require_human_boundary("agent.redirect")
        agent = await self.get_agent(agent_id)
        if require_member:
            await self._require_agent_run_authority(agent_id, user_id)
        # The agent-scoped door queues the same text into the same prompt as the
        # run-scoped one, so it persists the same bound.
        execution = await self._agent_run_to_steer(agent_id)
        intervention = (
            None if execution is None else self._intervention_for(execution, user_id, instruction)
        )
        async with self.db.transaction():
            if require_member:
                await self._require_mutate_in_transaction(agent.room_id, user_id)
            if intervention is not None:
                await self.repos.interventions.create(intervention)
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=agent.room_id,
                    sequence=0,
                    event_type=EventType.HUMAN_REDIRECTED_AGENT,
                    payload={"agent_id": agent_id, "instruction": instruction},
                    actor_id=user_id,
                    actor_type="user",
                )
            )
        if intervention is not None:
            await self.nexus.add_execution_intervention(intervention.execution_id, instruction)
        await self._broadcast_persisted_events([event])
