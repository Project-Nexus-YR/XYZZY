"""Agent turns: capability terms, tool requests, and the step execution loop."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any

from ..domain.events import EventType, RoomEvent
from ..domain.models import (
    AgentInstance,
    AgentOutput,
    AgentStatus,
    ArtifactType,
    DomainError,
    Execution,
    ExecutionStatus,
    HarnessState,
    RunSettlement,
    Session,
    ToolRequest,
    new_id,
)
from ..harness import (
    KNOWN_HARNESS_IDS,
    HarnessError,
    PromptRequest,
    RunContext,
    SessionHandle,
    StopReason,
)
from ..security.authorization import (
    AuthorizationError,
)
from ..security.boundary import agent_turn
from ..security.capabilities import (
    BoundingPrincipals,
    CapabilityTerms,
    GatewayDecision,
    RunAuthorization,
    UnboundedTerms,
    allowed_tools,
    decide,
    delegating_agent_id,
    policy_capabilities,
    under_posture,
    user_capabilities,
)
from ..security.screening import fenced, screen
from ._shared import (
    _APPROVAL_LEASE,
    _STREAMING_LEASE,
    AgentLaunchRefused,
    RunAuthorityRevoked,
    _policy_list,
    _require_idle_entrance,
    _SharedMixin,
    _TurnContinuation,
)

log = logging.getLogger(__name__)


class _StepsMixin(_SharedMixin):
    """Mixin providing the steps surface of MultiplayerService."""

    async def _user_term(self, room_id: str, user_id: str) -> frozenset[str]:
        """What one human may lend an agent here, from durable membership alone."""
        member = await self.repos.room_members.get(room_id, user_id)
        granted = _policy_list(member.allowed_capabilities if member else None)
        return user_capabilities(member.role if member else None) & policy_capabilities(granted)

    async def _principal_term(self, room_id: str, principal: str) -> frozenset[str]:
        """What one principal may lend an agent here, whichever kind it is.

        A human lends from durable room membership. A delegating agent lends from
        its own capability row, and never more than it holds — which is the whole
        reason one agent asking another cannot become a way to obtain something
        the asker was itself refused.

        Both are read here rather than at the call sites. A call site that has to
        know which kind of principal it is holding is a call site that will get it
        wrong for the third kind, and being one participant short is how the same
        defect was relocated thirteen times.

        A delegator that has left the room lends nothing. That is re-read at every
        spend, so removing an agent mid-delegation stops the delegate too, rather
        than leaving it running on authority its asker no longer has.
        """
        delegator_id = delegating_agent_id(principal)
        if delegator_id is None:
            return await self._user_term(room_id, principal)
        delegator = await self.repos.agents.get_instance(delegator_id)
        if delegator is None or not await self.repos.agents.has_room_membership(
            delegator_id, room_id
        ):
            return frozenset()
        return frozenset(delegator.capabilities)

    async def _authorized_terms(self, authorization: RunAuthorization) -> CapabilityTerms:
        """The one derivation a spend-point spends. Nothing else produces spendable terms.

        Thirteen rounds relocated one defect because the bound was applied by
        remembering which identities to apply, and the list was one short every time:
        a spend-point re-derived the five terms from durable records — correctly, in
        itself — and did not know it also owed a steerer, or a caller who was not the
        run's own principal.

        Nothing is enumerated here now. The authorization carries every bounding
        principal as one set, this reads what each of them may lend right now, and
        :meth:`UnboundedTerms.spend_under` refuses to produce a spendable set for any
        run but the one they were read for. A spend-point written next year gets all
        of it by consuming the object it already had to consume.
        """
        agent = await self.get_agent(authorization.agent_id)
        unbounded = await self._lendable_terms(agent, authorization.room_id, authorization.bounding)
        return unbounded.spend_under(authorization)

    async def _authorization_for(
        self,
        execution_id: str,
        agent_id: str,
        room_id: str,
        required_capability: str = "",
    ) -> RunAuthorization:
        """Build the authority object every spend-point re-derives its terms from.

        The single place a ``RunAuthorization`` is made, and it takes no principal:
        there is no argument through which a caller could hand it a set that is
        missing one. Who bounds this run is a question the durable rows answer, in one
        read, and that read is the only thing that fills the set.
        """
        run = await self.repos.agent_runs.get_by_execution(execution_id)
        return RunAuthorization(
            run_id=run.run_id if run is not None else execution_id,
            agent_id=agent_id,
            room_id=room_id,
            bounding=BoundingPrincipals.read_from(
                await self.repos.executions.bounding_principals(execution_id)
            ),
            required_capability=required_capability,
        )

    @staticmethod
    def _step_schema(effective: frozenset[str]) -> dict[str, Any]:
        """Offer only the tools this run may call, so the rest are unavailable.

        And only the actions the server acts on. "delegate" and "wait" were offered
        and no branch handled either: a model that picked one ended its step having
        neither answered nor called a tool, and the run was left STREAMING for the
        lease sweep to mislabel a quarter of an hour later. Offering an action nobody
        implements is the same defect as leaving a tool unguarded, pointed the other
        way. A harness that answers outside this schema is still settled, below.
        """
        offered = allowed_tools(effective)
        properties: dict[str, Any] = {
            "action": {"type": "string", "enum": ["finish"]},
            "output": {"type": "object"},
        }
        if offered:
            properties["action"]["enum"] = ["tool", *properties["action"]["enum"]]
            properties["tool"] = {"type": "string", "enum": offered}
            properties["input"] = {"type": "object"}
        return {"type": "object", "properties": properties, "required": ["action"]}

    async def agent_capability_terms(
        self, room_id: str, agent_id: str, requested_by: str
    ) -> UnboundedTerms:
        """What this member could lend this agent, for a run they have not opened yet.

        A preview, not a spend: there is no run, so this member is the whole of it.

        The room is required and checked. Resolving the terms from the agent's own
        room while the caller was authorized against a different one let anyone who
        could read any room read any agent's channel and workspace policy anywhere -
        the caller passed a room they owned and named an agent belonging to someone
        else's workspace.
        """
        agent = await self.get_agent(agent_id)
        if agent.room_id != room_id:
            raise DomainError("agent is not in this room")
        return await self._lendable_terms(
            agent, room_id, BoundingPrincipals(frozenset({requested_by}))
        )

    async def _require_delegated_authority(self, execution: Execution, acting_as: str) -> None:
        """Guard every verb that advances or influences somebody else's run.

        Room MUTATE says the caller may act in this channel; it does not say what
        this run may do on their behalf. What the caller may lend is re-derived here
        from durable records, and a caller narrower than the authorizing principal is
        refused when the intersection is empty.

        What the run may then spend is not decided here. That is
        :meth:`_authorized_terms`, which narrows by every principal bounding the run
        at once; asking it here instead would let a steerer who has since been
        narrowed to nothing block the cancel that ends her own turn.

        A caller this gate admits is written down as one of the run's callers, because
        from here on their grant bounds it. A caller it refuses is not: a refusal is
        not participation, and recording one would let anybody narrow a run they were
        never allowed to touch.
        """
        if not acting_as:
            return
        session = await self.repos.sessions.get(execution.session_id)
        if session is None:
            raise DomainError("session not found")
        agent = await self.get_agent(execution.agent_id)
        # Interrupt, cancel and resume re-run the addressing check as well as the
        # authority one, so a caller who may not point this agent may not steer it
        # either — including the principal the run already names.
        try:
            await self._require_addressable(agent, session.room_id, acting_as)
        except AgentLaunchRefused as refusal:
            await self._record_launch_refusal(refusal)
            raise
        if acting_as == execution.authorized_by:
            return
        lendable = await self._lendable_terms(
            agent,
            session.room_id,
            BoundingPrincipals(frozenset({execution.authorized_by, acting_as})),
        )
        if not lendable.lendable():
            raise AuthorizationError(
                f"{acting_as} may not act on run {execution.execution_id}: no effective capability"
            )
        await self.repos.executions.record_caller(execution.execution_id, acting_as)

    async def _handle_tool_request(
        self,
        execution: Execution,
        session: Session,
        agent: AgentInstance,
        result: dict[str, Any],
        continuation: _TurnContinuation,
    ) -> dict[str, Any]:
        """Permission check, policy check, approval gate, execution, audit event.

        The terms are re-derived here rather than carried in from the step that
        offered the tool: a provider call sits between the two, and a grant withdrawn
        while the model was thinking must not still be spendable when it answers.

        Re-deriving is not the same as unbinding. The caller who drove this step and
        the steers that shaped it still bound what it may spend, and the authorization
        carries every one of them, so the derivation applies them without this door
        having to know any of them exist — or being able to name one if it wanted to.

        The channel's posture is read here too, and here only, because this is the one
        moment a call becomes a pause or an execution. It is read, never carried: the
        declaration rows are consulted beside the terms rather than a value resolved
        somewhere earlier being spent. What it may do to the decision is bounded by
        :func:`under_posture` rather than by this door's discipline — it raises the
        pause and cannot reach ``allowed``, so a channel's posture never changes what
        that channel permits.
        """
        authorization = await self._authorization_for(
            execution.execution_id, agent.agent_id, session.room_id
        )
        effective = (await self._authorized_terms(authorization)).effective
        tool = str(result.get("tool", ""))
        raw_input = result.get("input")
        tool_input = raw_input if isinstance(raw_input, dict) else {}
        decision = under_posture(
            decide(tool, effective), await self.repos.room_postures.current(session.room_id)
        )
        request = ToolRequest(
            request_id=new_id("toolreq"),
            room_id=session.room_id,
            execution_id=execution.execution_id,
            agent_id=agent.agent_id,
            # requested_by is the actor; authorized_by is the authority it acts under.
            requested_by=agent.agent_id,
            authorized_by=execution.authorized_by,
            tool=tool,
            input_json=json.dumps(tool_input, default=str),
            required_capability=decision.required_capability,
            effective_json=json.dumps(sorted(effective)),
            status="REJECTED" if not decision.allowed else "PENDING_APPROVAL",
            reason=decision.reason,
        )
        payload = {
            "request_id": request.request_id,
            "tool": tool,
            "agent_id": agent.agent_id,
            "execution_id": execution.execution_id,
            "required_capability": decision.required_capability,
            "effective": sorted(effective),
            "reason": decision.reason,
        }
        if not decision.allowed:
            await self.repos.tool_requests.create(request)
            await self._append_room_event(
                session.room_id,
                EventType.TOOL_CALL_REJECTED,
                payload,
                agent.agent_id,
                "agent",
            )
            return self._tool_response(request)
        if decision.requires_approval:
            # Deciding this approval puts the run back on a STREAMING lease, and that
            # lease is only honest if the rest of the turn is there to be prompted.
            # The two used to be separate writes — the approval committed here, the
            # continuation was saved by the turn loop afterwards — so a crash or a
            # race in between left an approval whose grant stranded the run: STREAMING,
            # NULL settlement, and a lease held by nobody. They are one transaction
            # now. Either the reviewer has a question and the turn is parked behind
            # it, or neither exists.
            async with self.db.transaction():
                approval, approval_event = await self._request_approval_in_transaction(
                    session.room_id,
                    execution.execution_id,
                    agent.agent_id,
                    f"{tool}: {decision.required_capability}",
                    execution.authorized_by,
                )
                request = replace(request, approval_id=approval.approval_id)
                await self.repos.tool_requests.create(request)
                # No harness work is in flight while a reviewer thinks, so the lease is
                # a long one. It is still a lease: an exemption is no deadline at all.
                await self._advance_run_for_execution(
                    execution.execution_id,
                    HarnessState.AWAITING_APPROVAL,
                    execution.authorized_by,
                    _APPROVAL_LEASE,
                )
                # Durably rather than in this process's memory: the decision that
                # releases it can be made on any process.
                await self.repos.suspended_turns.save(
                    execution.execution_id,
                    continuation.prompt,
                    continuation.acting_as,
                    continuation.observations,
                )
            await self._set_agent_status_safe(agent.agent_id, AgentStatus.WAITING_APPROVAL)
            await self._broadcast_persisted_events([approval_event])
            return self._tool_response(request)
        await self.repos.tool_requests.create(request)
        return self._tool_response(await self._execute_tool_request(request))

    async def _current_tool_decision(
        self, request: ToolRequest
    ) -> tuple[GatewayDecision, frozenset[str]]:
        """Decide a stored request again from the records as they stand right now.

        A twelve-hour approval window sits in front of this, and everything that can
        narrow a run can happen inside it — a steer reduced, the caller who asked for
        this tool narrowed, either of them taken out of the room. The authorization
        carries all of them, so the door a reviewer opens is bounded exactly as the
        gateway was, and takes no principal from its caller to be bounded by.

        The reviewer about to release it bounds it here as well, through the same
        helper the writer's own derivation uses, so this door refuses a call she may
        not answer for cleanly instead of leaving it to be revoked inside the write.

        The channel's posture is deliberately not applied. A posture decides whether a
        call pauses, and this call has already paused and been answered; re-pausing it
        would make an approval something a rule change could quietly revoke.
        """
        authorization = await self._bounded_by_this_calls_reviewers(
            request,
            await self._authorization_for(
                request.execution_id,
                request.agent_id,
                request.room_id,
                request.required_capability or "",
            ),
        )
        effective = (await self._authorized_terms(authorization)).effective
        return decide(request.tool, effective), effective

    async def _execute_tool_request(self, request: ToolRequest) -> ToolRequest:
        """Everything below runs inside the agent-turn boundary."""
        with agent_turn(request.execution_id):
            return await self._execute_tool_request_inner(request)

    async def _execute_tool_request_inner(self, request: ToolRequest) -> ToolRequest:
        """Run an authorised tool and audit the outcome. Never raises to the caller.

        The contract was not true. Only RunAuthorityRevoked and DomainError were
        caught, so add_agent_reaction's membership check — a bare AuthorizationError,
        which is a PermissionError and not a DomainError — escaped, leaving the
        request PENDING_APPROVAL under a tool.call_started event that never got a
        completion or a rejection: a call that started and, in the log, never ended.
        Every exit below resolves the row and appends a terminal event, and the last
        clause is a catch-all so an exception nobody anticipated cannot reopen the
        same hole.
        """
        await self._append_room_event(
            request.room_id,
            EventType.TOOL_CALL_STARTED,
            {"request_id": request.request_id, "tool": request.tool},
            request.agent_id,
            "agent",
        )
        try:
            output = await self._run_tool(request)
        except RunAuthorityRevoked as revoked:
            # The write already rolled back with the raise; the settlement is written
            # here, outside the transaction that could not have kept it.
            await self._append_room_event(
                request.room_id,
                EventType.AGENT_RUN_AUTHORITY_REVOKED,
                {
                    "run_id": revoked.authorization.run_id,
                    "bounded_by": sorted(revoked.authorization.bounding),
                    "stage": revoked.stage,
                    "missing_capability": revoked.authorization.required_capability,
                },
                request.agent_id,
                "agent",
            )
            await self._resolve_tool_request_terminal(
                request,
                "REJECTED",
                str(revoked),
                "{}",
                EventType.TOOL_CALL_REJECTED,
                {
                    "request_id": request.request_id,
                    "tool": request.tool,
                    "required_capability": request.required_capability,
                    "reason": str(revoked),
                },
            )
            run = await self.repos.agent_runs.get_by_execution(request.execution_id)
            if run is not None:
                await self._settle_run(
                    run,
                    RunSettlement.AUTHORITY_REVOKED,
                    run.acting_user_id,
                    str(revoked),
                )
            return replace(request, status="REJECTED", reason=str(revoked))
        except AuthorizationError as denied:
            # A refusal, not a failure: the tool was not permitted to this agent at
            # the moment it ran, which is what tool.call_rejected records.
            await self._resolve_tool_request_terminal(
                request,
                "REJECTED",
                str(denied),
                "{}",
                EventType.TOOL_CALL_REJECTED,
                {
                    "request_id": request.request_id,
                    "tool": request.tool,
                    "required_capability": request.required_capability,
                    "reason": str(denied),
                },
            )
            return replace(request, status="REJECTED", reason=str(denied))
        except DomainError as exc:
            await self._resolve_tool_request_terminal(
                request,
                "FAILED",
                str(exc),
                "{}",
                EventType.TOOL_CALL_FAILED,
                {"request_id": request.request_id, "tool": request.tool, "error": str(exc)},
            )
            return replace(request, status="FAILED", reason=str(exc))
        except Exception as exc:
            # Nothing gets to end a started tool call by escaping. An error nobody
            # named is still a failure, and it is recorded as one rather than
            # unwinding past the audit trail.
            log.exception(
                "Tool %s failed unexpectedly for request %s", request.tool, request.request_id
            )
            error = f"{type(exc).__name__}: {exc}"
            await self._resolve_tool_request_terminal(
                request,
                "FAILED",
                error,
                "{}",
                EventType.TOOL_CALL_FAILED,
                {"request_id": request.request_id, "tool": request.tool, "error": error},
            )
            return replace(request, status="FAILED", reason=error)
        result_json = json.dumps(output, default=str)
        await self._resolve_tool_request_terminal(
            request,
            "EXECUTED",
            "executed",
            result_json,
            EventType.TOOL_CALL_COMPLETED,
            {"request_id": request.request_id, "tool": request.tool},
        )
        return replace(request, status="EXECUTED", reason="executed", result_json=result_json)

    async def _run_tool(self, request: ToolRequest) -> dict[str, Any]:
        """The registry's executable side. Each tool is a small, auditable action."""
        tool_input = json.loads(request.input_json)
        # Authority is established before any branch, reads included. This used to be
        # derived after the read returned, so channel.read_context reached no re-check
        # at all: an agent removed from the room mid-turn still read the room's
        # messages back, including ones posted before it was ever mentioned. The
        # continuation loop turned that one-shot window into a per-prompt one.
        authorization = await self._run_authorization(request)
        if request.tool == "channel.read_context":
            # A read has no writer of its own to check inside, so the check and the
            # read are made one transaction here. Disclosure is the mutation a read
            # performs, and it is gated in the same place a write's would be.
            async with self.db.transaction():
                await self._require_run_authority_in_transaction(
                    authorization, "channel.read_context"
                )
                messages = await self.repos.messages.list_by_room(request.room_id, limit=20)
            return {
                "messages": [
                    {"message_id": m.message_id, "content": m.content, "role": m.role.value}
                    for m in messages
                ]
            }
        # Each writer below re-checks inside its own transaction rather than here:
        # those calls open their own, and Database.transaction() refuses to nest, so
        # a second check here would sit outside the write and relocate
        # check-then-use rather than end it.
        if request.tool == "message.react":
            # The channel the run belongs to is the boundary, checked here so a
            # cross-channel message id is refused as a domain error rather than as
            # an authorization one: the reaction's own membership check is about who
            # may react, not about which channel this run belongs to.
            message = await self.get_message(str(tool_input.get("message_id", "")))
            if message.room_id != request.room_id:
                raise DomainError("message is not in this channel")
            reaction = await self.add_agent_reaction(
                message.message_id,
                request.agent_id,
                str(tool_input.get("emoji", "")),
                authorization=authorization,
            )
            return {"message_id": message.message_id, "emoji": reaction.emoji}
        if request.tool == "task.create":
            task = await self.create_task(
                request.room_id,
                str(tool_input.get("title", "")),
                str(tool_input.get("description", "")),
                created_by=request.agent_id,
                authorization=authorization,
            )
            return {"task_id": task.task_id}
        if request.tool == "artifact.write":
            artifact = await self.create_artifact(
                request.room_id,
                str(tool_input.get("name", "Untitled")),
                ArtifactType.DOCUMENT,
                str(tool_input.get("description", "")),
                created_by=request.agent_id,
                authorization=authorization,
            )
            return {"artifact_id": artifact.artifact_id}
        raise DomainError(f"tool not executable: {request.tool}")

    async def _run_authorization(self, request: ToolRequest) -> RunAuthorization:
        """What a stored call is decided and written against, read from durable records.

        It used to read the acting caller off ``agent_runs.acting_user_id``, which is
        the last human to have moved the run rather than the set of humans whose grant
        bounds it. By the time a reviewer released a parked call that column had been
        overwritten with the run's own principal, and the delegate who asked for the
        call was bounding nothing.

        The run's own principals are read whole, by the one factory that reads them,
        and this call's reviewers are added to that set beside them.
        """
        return await self._bounded_by_this_calls_reviewers(
            request,
            await self._authorization_for(
                request.execution_id,
                request.agent_id,
                request.room_id,
                request.required_capability or "",
            ),
        )

    async def _bounded_by_this_calls_reviewers(
        self, request: ToolRequest, authorization: RunAuthorization
    ) -> RunAuthorization:
        """Put the humans who released this one call over it, and over nothing else.

        A reviewer answers for the call she released and for no other, so her grant
        belongs to that request's rows rather than to the run's. Recording her as a
        caller of the run instead — which is what releasing a call used to do — made
        an administrator scoped to ``retrieval`` strip ``writing`` from every later
        call of a run she had touched once, and made answering an approval something
        to avoid.

        Adding can only narrow: the terms are an intersection over the set, so a wider
        set is a smaller grant. There is no expression here that removes a principal
        the durable rows named, which is what keeps a per-call bound from becoming a
        way to spend more than the run's own principals hold. Both doors that decide a
        stored call — the reviewer's and the writer's — reach the reviewers through
        here, so neither can be the one that forgot them.
        """
        return replace(
            authorization,
            bounding=authorization.bounding.also_bounded_by(
                await self.repos.tool_requests.reviewers(request.request_id)
            ),
        )

    @staticmethod
    def _tool_response(request: ToolRequest) -> dict[str, Any]:
        return {
            "status": "ok",
            "action": "tool",
            "tool_request": {
                "request_id": request.request_id,
                "tool": request.tool,
                "status": request.status,
                "reason": request.reason,
                "required_capability": request.required_capability,
                "effective": json.loads(request.effective_json),
                "approval_id": request.approval_id,
                "result": json.loads(request.result_json),
            },
        }

    @staticmethod
    def _prompt_with_tool_results(provider_prompt: str, observations: list[str]) -> str:
        """The same turn, continued: what the tools this turn already called returned.

        A tool result can carry member-authored text - a channel read returns
        whatever was said in the room - so the block is screened and fenced.
        """
        results = "\n".join(f"- {observation}" for observation in observations)
        block = fenced(screen(results, "tool results"))
        return (
            f"{provider_prompt}\n\nTool results from this turn, in order:\n{block}\n\n"
            'Answer with action "finish" unless another tool call is genuinely required.'
        )

    async def _settle_turn_without_answer(
        self,
        execution: Execution,
        acting_as: str,
        result: dict[str, Any],
        settlement: RunSettlement,
        status: AgentStatus,
        error: str,
    ) -> dict[str, Any]:
        """End a step that produced no answer, now, in a state a reader can name.

        Two things reach here, and neither used to end anything. A turn cancelled
        mid-continuation came back with a cancelled stop reason and no tool request,
        so the loop returned and the run sat STREAMING with a NULL settlement until
        the lease sweep called it PARKED — "turn stopped without an answer", which is
        untrue of a run somebody cancelled, and non-resumable besides. And an action
        the server does not continue fell through the dispatch below to the same
        silence. Settling here is what makes the loop's promise true: nothing leaves
        this method with the run RUNNING and nobody about to prompt it.
        """
        run = await self.repos.agent_runs.get_by_execution(execution.execution_id)
        if run is not None and run.harness_state is not HarnessState.SETTLED:
            await self._settle_run(run, settlement, acting_as or "system", error)
            await self._set_agent_status_safe(execution.agent_id, status)
        return {**result, "error": error, "settlement": settlement.value}

    async def _execute_one_agent_step(
        self, execution_id: str, continuation: _TurnContinuation, *, require_idle: bool = False
    ) -> dict[str, Any]:
        """Everything below runs inside the agent-turn boundary.

        ``require_idle`` rides a contextvar rather than a parameter to the inner
        step, because several tests substitute that inner step outright and are
        entitled to keep its original two argument shape; a caller replacing it
        never sees this claim, and does not need to.
        """
        token = _require_idle_entrance.set(require_idle)
        try:
            with agent_turn(execution_id):
                return await self._execute_one_agent_step_inner(execution_id, continuation)
        finally:
            _require_idle_entrance.reset(token)

    async def _execute_one_agent_step_inner(
        self, execution_id: str, continuation: _TurnContinuation
    ) -> dict[str, Any]:
        """One prompt of a turn: authority, harness, and whatever the model chose.

        Every authority this spends is re-derived here rather than carried in from
        the prompt before it, so a grant withdrawn between two tool calls stops the
        second one.
        """
        prompt = continuation.prompt
        acting_as = continuation.acting_as
        execution = await self.repos.executions.get(execution_id)
        if not execution:
            raise DomainError(f"execution not found: {execution_id}")
        session = await self.repos.sessions.get(execution.session_id)
        if not session:
            raise DomainError("session not found")
        agent = await self.get_agent(execution.agent_id)
        branch = await self.get_branch(execution.branch_id)

        if execution.status in {
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        }:
            raise DomainError(
                f"execution {execution_id} is terminal (current: {execution.status.value})"
            )

        # The authority the run carries, re-derived from durable records now rather
        # than trusted from the request that opened it. A principal whose grant was
        # withdrawn between that write and this dispatch can no longer make the
        # agent speak, so the run is settled instead of run.
        principal = await self._lendable_terms(
            agent, session.room_id, BoundingPrincipals(frozenset({execution.authorized_by}))
        )
        if not principal.lendable():
            await self._settle_undispatched_run(
                execution_id,
                f"{execution.authorized_by or 'an unknown principal'} may no longer "
                f"invoke agent {execution.agent_id}: no effective capability",
                RunSettlement.AUTHORITY_REVOKED,
            )
            raise AuthorizationError(
                f"run {execution_id} is no longer authorized by {execution.authorized_by}"
            )
        # A caller who is not that principal is bounded by their own grant too, and so
        # is every steer the run is carrying. The gate above writes this caller into
        # the run's own records, and the authorization below reads every principal
        # back out of them. The steers still queued are read separately, because those
        # are what this prompt delivers rather than what bounds it.
        await self._require_delegated_authority(execution, acting_as)
        steers = await self.repos.interventions.list_unconsumed(execution_id)
        authorization = await self._authorization_for(execution_id, agent.agent_id, session.room_id)
        terms = await self._authorized_terms(authorization)
        if not terms.effective and execution.agent_task_id:
            # The gate above is a liveness check on the run's own authorizer, and it
            # passes while a *delegator* is revoked — leaving a run that dispatches,
            # derives an empty schema and sits there tooled with nothing.
            #
            # Only for a run answering a task. A run with an empty set is not always
            # finished: a mention run whose steerer was narrowed still answers in
            # words, and test_intervention_authority pins that an empty set de-tools
            # the step and audits a REJECTED request rather than killing the turn. A
            # delegated run is different because somebody is waiting on its answer —
            # a delegator blocked on a turn that can no longer do anything is the
            # state a task's terminal states exist to spare it.
            await self._settle_undispatched_run(
                execution_id,
                f"no principal bounding run {execution_id} can still lend "
                f"agent {execution.agent_id} anything",
                RunSettlement.AUTHORITY_REVOKED,
            )
            raise AuthorizationError(f"run {execution_id} is no longer authorized")

        source_prompt = prompt
        provider_prompt = prompt
        if branch.lifecycle_managed:
            if prompt != branch.initiating_prompt:
                raise DomainError("managed branch run must use its immutable initiating prompt")
            source_prompt = branch.initiating_prompt
            provider_prompt = self._branch_execution_prompt(branch)
        if continuation.observations:
            provider_prompt = self._prompt_with_tool_results(
                provider_prompt, continuation.observations
            )

        if agent.harness_id not in KNOWN_HARNESS_IDS:
            raise DomainError(f"no harness is registered as {agent.harness_id!r}")
        harness = self._harness(agent.harness_id)
        agent_run = await self.repos.agent_runs.get_by_execution(execution_id)
        handle = SessionHandle(
            run_id=agent_run.run_id if agent_run is not None else execution_id,
            harness_session_id=execution_id,
        )
        # The turn is in flight from here, on a lease the sweep can expire if the
        # process driving it dies. The entrance prompt of a call from outside makes
        # this a claim rather than an unconditional advance: a run already
        # streaming or already parked at a reviewer refuses instead of being
        # prompted again on top of a turn already in flight.
        require_idle = _require_idle_entrance.get()
        claimed = await self._advance_run_for_execution(
            execution_id,
            HarnessState.STREAMING,
            acting_as,
            _STREAMING_LEASE,
            expected=HarnessState.STARTING if require_idle else None,
        )
        if require_idle and not claimed:
            raise DomainError(
                f"execution {execution_id} is not awaiting a fresh turn, so this step is refused"
            )
        if not execution.run_id:
            if agent_run is not None:
                await harness.session_new(
                    RunContext(
                        run_id=agent_run.run_id,
                        agent_id=agent_run.agent_id,
                        identity_id=agent_run.identity_id,
                        room_id=agent_run.room_id,
                        run_credential=self._run_credentials.pop(agent_run.run_id, ""),
                        authorized_by=agent_run.authorized_by,
                        acting_user_id=acting_as or agent_run.acting_user_id,
                    )
                )
            else:
                await self.nexus.create_execution(agent, session, provider_prompt, execution)
            run_id = f"run_{execution.execution_id}"
            # replace(), not a rebuild: a rebuild silently reset triggered_by to
            # DIRECT, losing why the run was opened at the moment it starts.
            await self.repos.executions.mark_running(
                execution.execution_id, run_id, execution.status
            )
            execution = replace(execution, run_id=run_id, status=ExecutionStatus.RUNNING)

        effective = terms.effective
        try:
            turn = await harness.session_prompt(
                PromptRequest(
                    handle=handle,
                    prompt=provider_prompt,
                    response_schema=self._step_schema(effective),
                    offered_tools=tuple(allowed_tools(effective)),
                ),
                self._renew_run_lease,
            )
        except HarnessError as exc:
            # The steers stay unconsumed: a prompt that never reached the harness
            # did not spend them, and leaving them bounds the next step rather
            # than unbinding it.
            result: dict[str, Any] = {"status": "error", "error": str(exc)}
        else:
            result = dict(turn.output)
            self._record_model_tokens(result)
            # The NEXUS harness carries provenance inside the turn output; the model
            # provider harness returns it in the TurnResult's own field, and the reader
            # below looks only in the output. Without this an agent on that harness
            # records no provider input, model or evidence at all - and provenance is
            # the whole reason a synthesis claim can be drilled back to its source.
            if turn.provenance and not result.get("provenance"):
                result["provenance"] = dict(turn.provenance)
            if turn.stop_reason is StopReason.CANCELLED:
                # A cancelled turn returns before the harness drains its queue, so
                # the prompt never carried these steers. Leaving them unconsumed
                # bounds the next step rather than spending a delivery that did
                # not happen.
                result["status"] = "cancelled"
            else:
                # The prompt carried the queued steers, so they are spent here.
                await self.repos.interventions.mark_consumed(
                    [steer.intervention_id for steer in steers]
                )
        if result.get("status") == "error":
            error = str(result.get("error", ""))
            persisted_events = await self.repos.executions.terminalize_without_output(
                replace(execution, branch_id=branch.branch_id),
                ExecutionStatus.FAILED,
                error,
                [
                    RoomEvent(
                        room_id=session.room_id,
                        sequence=0,
                        event_type=EventType.EXECUTION_FAILED,
                        payload={
                            "branch_id": branch.branch_id,
                            "execution_id": execution.execution_id,
                            "error": error,
                        },
                        actor_id=execution.agent_id,
                        actor_type="agent",
                    )
                ],
            )
            await self._broadcast_persisted_events(persisted_events)
            await self._set_agent_status_safe(execution.agent_id, AgentStatus.FAILED)
            return result
        if result.get("status") == "cancelled":
            return await self._settle_turn_without_answer(
                execution,
                acting_as,
                result,
                RunSettlement.CANCELLED,
                AgentStatus.IDLE,
                "cancelled while the turn was in flight",
            )
        if result.get("action") == "tool":
            return await self._handle_tool_request(execution, session, agent, result, continuation)
        if result.get("action") == "finish":
            raw_output = result.get("result")
            output_data = raw_output if isinstance(raw_output, dict) else {"result": raw_output}
            raw_provenance = result.get("provenance")
            provenance = raw_provenance if isinstance(raw_provenance, dict) else {}
            raw_interventions = provenance.get("interventions")
            interventions = (
                tuple(str(item) for item in raw_interventions)
                if isinstance(raw_interventions, list)
                else ()
            )
            output = AgentOutput(
                output_id=new_id("out"),
                room_id=session.room_id,
                session_id=session.session_id,
                execution_id=execution.execution_id,
                agent_id=execution.agent_id,
                content=self._output_content(output_data),
                branch_id=branch.branch_id,
                output_data=output_data,
                source_prompt=source_prompt,
                provider_input=str(provenance.get("provider_input", "")),
                provider_name=str(provenance.get("provider_name", "")),
                provider_model=str(provenance.get("provider_model", "")),
                provider_response_id=str(provenance.get("provider_response_id", "")),
                provider_interventions=interventions,
                provider_evidence=str(provenance.get("provider_evidence", "")),
            )
            agent_message, agent_message_event = await self._agent_message_for_mention(
                execution, session, output
            )
            persisted_events = await self.repos.agent_outputs.complete_execution(
                output,
                [
                    RoomEvent(
                        room_id=session.room_id,
                        sequence=0,
                        event_type=EventType.AGENT_OUTPUT_CREATED,
                        payload={
                            "output_id": output.output_id,
                            "branch_id": branch.branch_id,
                            "execution_id": execution.execution_id,
                            "session_id": session.session_id,
                            "agent_id": execution.agent_id,
                        },
                        actor_id=execution.agent_id,
                        actor_type="agent",
                    ),
                    RoomEvent(
                        room_id=session.room_id,
                        sequence=0,
                        event_type=EventType.AGENT_RUN_COMPLETED,
                        payload={
                            "execution_id": execution.execution_id,
                            "session_id": session.session_id,
                            "agent_id": execution.agent_id,
                            "output_id": output.output_id,
                            "branch_id": branch.branch_id,
                        },
                        actor_id=execution.agent_id,
                        actor_type="agent",
                    ),
                ],
                execution.status,
                agent_message,
                agent_message_event,
                token_usage=result.get("token_usage", 0)
                if isinstance(result.get("token_usage", 0), int)
                else 0,
            )
            await self._broadcast_persisted_events(persisted_events)
            await self._set_agent_status_safe(execution.agent_id, AgentStatus.COMPLETED)
            await self._set_agent_status_safe(execution.agent_id, AgentStatus.IDLE)
            result["output_id"] = output.output_id
            return result
        return await self._settle_turn_without_answer(
            execution,
            acting_as,
            result,
            RunSettlement.FAILED,
            AgentStatus.FAILED,
            f"step returned {str(result.get('action', '')) or 'no action'}, "
            "which is not an answer and not a tool call",
        )
