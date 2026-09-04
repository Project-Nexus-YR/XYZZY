"""Agent tasks: the A2A surface an asker opens, delegates, escalates, and settles."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import timedelta
from typing import Any

from ..domain.agent_card import DEFAULT_OUTPUT_MODES
from ..domain.agent_tasks import (
    AgentTask,
    AgentTaskMessage,
    AgentTaskState,
    Part,
    PartKind,
    TaskMessageRole,
    TaskNotCancelableError,
    TaskNotFoundError,
    negotiate_output_modes,
    new_context_id,
    require_delegable,
    require_transition,
)
from ..domain.events import EventType, RoomEvent
from ..domain.models import (
    AgentTrigger,
    DomainError,
    Execution,
    Session,
    SessionStatus,
    new_id,
    utcnow,
)
from ..security.authorization import (
    AuthorizationError,
    RoomCapability,
)
from ..security.capabilities import (
    AGENT_PRINCIPAL_PREFIX,
    BoundingPrincipals,
    agent_principal,
)
from ..security.screening import fenced, screen
from ._shared import (
    _NO_SUCH_AGENT_TASK,
    _ROOM_ACCESS_FORBIDDEN,
    _SharedMixin,
)

log = logging.getLogger(__name__)


class _AgentTasksMixin(_SharedMixin):
    """Mixin providing the agent tasks surface of MultiplayerService."""

    _STALE_SUBMITTED_TASK_SECONDS = 30

    async def _require_agent_task(self, task_id: str) -> AgentTask:
        """The row, or the specification's name for its absence."""
        task = await self.repos.agent_tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(f"no agent task {task_id}")
        return task

    async def _append_agent_task_event(
        self, task: AgentTask, actor_id: str, actor_type: str, **extra: Any
    ) -> RoomEvent:
        """One event type for the whole lifecycle, with the state in the payload.

        A reader that has to match one event type per state is a reader that will
        miss the ninth one the day somebody adds it. TASK_DELEGATED is the room
        log's existing name for "an agent was asked to do something", and every
        move of that task is the same fact changing, not a different kind of fact.
        """
        return await self._append_room_event(
            task.room_id,
            EventType.TASK_DELEGATED,
            {
                "task_id": task.task_id,
                "context_id": task.context_id,
                "state": task.state.value,
                "target_agent_id": task.target_agent_id,
                "delegating_agent_id": task.delegating_agent_id,
                "updated_at": task.updated_at.isoformat(),
                **extra,
            },
            actor_id,
            actor_type,
        )

    @staticmethod
    def _require_asker(task: AgentTask, requested_by: str) -> None:
        """Only the party that asked may say more about the task or take it back.

        The asker is a human on a task somebody opened by hand and an agent on a
        delegated one, so this compares against both rather than reaching for the
        room's membership table — an agent holds no membership there, and gating
        on one would have made every delegated task uncontinuable by the only
        party with anything to add.

        The refusal is the one a task nobody has ever heard of gets. Anything else
        would answer a question the caller was not entitled to ask: a stranger who
        can tell "that is not yours" from "there is no such thing" can enumerate
        every task id in the deployment one guess at a time.
        """
        if requested_by not in {task.authorized_by, task.requested_by, task.delegating_agent_id}:
            raise TaskNotFoundError(_NO_SUCH_AGENT_TASK)

    async def _asker_task(self, task_id: str, requested_by: str) -> AgentTask:
        """The task, if this caller is the one that asked for it. One answer if not.

        Asking is authorized once, at task creation, and never rechecked here for
        the agent case, because an agent holds no room membership to recheck. A
        human asker does hold membership, and it can change after the task opened
        (removal, demotion), so a human's continued standing is reread against the
        room every time, not trusted from the original ask.
        """
        task = await self.repos.agent_tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError(_NO_SUCH_AGENT_TASK)
        self._require_asker(task, requested_by)
        if requested_by != task.delegating_agent_id:
            try:
                await self.authorization.require(task.room_id, requested_by, RoomCapability.MUTATE)
            except AuthorizationError as exc:
                raise TaskNotFoundError(_NO_SUCH_AGENT_TASK) from exc
        return task

    async def _visible_agent_task(
        self, task_id: str, user_id: str, capability: RoomCapability
    ) -> AgentTask:
        """The task, if this person may act on the room holding it. One answer if not.

        Resolution has to come first here, because the row is what says which room
        to ask about — so instead of authorizing first, the two failures are made
        one. A task id that was never minted and a task in a room this person
        cannot see are the same refusal, byte for byte and with no id echoed back.
        Asserting that each of them refuses is what let the difference between them
        survive: both were refusals, and the pair was an index of what exists.
        """
        task = await self.repos.agent_tasks.get(task_id)
        if task is not None:
            try:
                await self.authorization.require(task.room_id, user_id, capability)
            except AuthorizationError:
                task = None
        if task is None:
            raise TaskNotFoundError(_NO_SUCH_AGENT_TASK)
        return task

    async def _require_may_ask_here(
        self, room_id: str, requested_by: str, delegating_agent_id: str | None
    ) -> None:
        """May this caller ask anything in this room, decided before anything is looked up.

        Authorize first, resolve second. Resolution refusals describe what exists —
        an agent that is real but filed under another room, an id that was never
        minted — and a caller who may not act here is entitled to neither. Three
        distinguishable answers let somebody who is a member of nothing confirm
        that an agent id is real and then find the room it lives in, by walking ids
        against the differences.

        So the only thing consulted before the gate is the caller and the room, and
        the caller is a person every time — including on a delegated ask, where the
        route holds the delegating run and can name the human it is authorized by.
        An ``agent:`` principal has no membership row and so is refused here like
        any other stranger, which is the same wall that stops one from becoming a
        chain's root authority further down.

        The delegating agent has to be in the room it is asking in. That lookup
        answers False for an agent filed elsewhere and for an agent that was never
        spawned alike, and its refusal is worded as the gate's, so getting past the
        gate is the only thing any of these answers can be read for.
        """
        await self.authorization.require(room_id, requested_by, RoomCapability.MUTATE)
        if delegating_agent_id is not None and not await self.repos.agents.has_room_membership(
            delegating_agent_id, room_id
        ):
            raise AuthorizationError(_ROOM_ACCESS_FORBIDDEN)

    @staticmethod
    def _require_human_asker(requested_by: str) -> None:
        """A chain's root authority is a person, and the prefix is what proves it.

        An ``agent:`` principal arriving here would be written into ``authorized_by``
        and from there into ``executions.authorized_by``, which is one arm of the
        union every spend-point bounds by. The chain would then be authorized by an
        agent: removing the human from the room would change nothing about what it
        could spend, because the human was never in the record to begin with.
        """
        if requested_by.startswith(AGENT_PRINCIPAL_PREFIX):
            raise AuthorizationError(
                f"{requested_by} is an agent and cannot be the human a task is authorized by"
            )

    async def _delegating_task(
        self,
        room_id: str,
        delegating_agent_id: str,
        delegating_run_id: str | None,
        parent_task_id: str | None,
    ) -> AgentTask:
        """The task the delegating agent is itself running under, read from its run.

        Derived, never accepted. A parent the asker may decline to mention is a chain
        the asker may decline to have: A asking B asking A asking B arrived as twelve
        separate roots, every one of them depth zero with no chain rows at all, and
        ``require_delegable`` was handed an empty ancestry to look for a cycle in. The
        cycle was real; the evidence of it was simply never written down.

        So the delegator's own open run is what says which task this delegation
        descends from, and a delegation whose parent cannot be established is refused
        rather than rooted afresh. An agent that is not running under a task has
        nothing to delegate from — a person can open it one.
        """
        running = await self.repos.executions.latest_open_for_agent(delegating_agent_id)
        if running is None:
            raise TaskNotFoundError(
                f"agent {delegating_agent_id} has no open run and may not delegate"
            )
        if delegating_run_id is not None and delegating_run_id != running.execution_id:
            raise AuthorizationError(
                f"agent {delegating_agent_id} is running under {running.execution_id}, "
                f"not {delegating_run_id}"
            )
        parent = next(
            (
                task
                for task in await self.repos.agent_tasks.list_open_for_agent(delegating_agent_id)
                if task.execution_id == running.execution_id
            ),
            None,
        )
        if parent is None:
            raise TaskNotFoundError(
                f"the run agent {delegating_agent_id} is serving answers no task, "
                "so a delegation from it descends from nothing"
            )
        # And that task has to be in the room this delegation is being made in. An
        # agent placed in two rooms is running under one task at a time, so without
        # this an agent working in R1 could delegate in R2 and carry R1's context,
        # authority, chain and depth across a boundary the workspace draws to keep
        # one room's authority out of another's.
        if parent.room_id != room_id:
            raise AuthorizationError(
                f"agent {delegating_agent_id} is running under a task in another room"
            )
        if parent_task_id is not None and parent_task_id != parent.task_id:
            raise AuthorizationError(
                f"agent {delegating_agent_id} is running under task {parent.task_id}, "
                f"not {parent_task_id}"
            )
        return parent

    async def open_agent_task(
        self,
        room_id: str,
        target_agent_id: str,
        parts: tuple[Part, ...],
        *,
        requested_by: str,
        delegating_agent_id: str | None = None,
        delegating_run_id: str | None = None,
        parent_task_id: str | None = None,
        accepted_output_modes: tuple[str, ...] = (),
    ) -> AgentTask:
        """Ask an agent to do something, whether the asker is a person or an agent.

        The gates are the mention path's gates in the mention path's order, and
        delegation adds nothing to them but one more principal in the bounding
        set. That is the whole design: an agent asking is a principal like any
        other, so a delegate is ceilinged by every spend-point that already
        ceilings a mentioned agent, without any of them having learned a new name.
        """
        await self._require_may_ask_here(room_id, requested_by, delegating_agent_id)

        if delegating_agent_id is None:
            self._require_human_asker(requested_by)
            if parent_task_id is not None:
                raise AuthorizationError(
                    "a task a person opens is the root of its own chain; inheriting "
                    "another chain's authority is not something an asker may ask for"
                )
            parent = None
            ancestry: tuple[str, ...] = ()
            context_id = new_context_id()
            authorized_by = requested_by
        else:
            parent = await self._delegating_task(
                room_id, delegating_agent_id, delegating_run_id, parent_task_id
            )
            ancestry = (
                *await self.repos.agent_tasks.ancestry(parent.task_id),
                parent.target_agent_id,
            )
            context_id = parent.context_id
            # The human at the root of the chain, always, and read off the parent
            # rather than off the caller. A delegated task that re-rooted authority
            # on the delegating agent would be a way to launder a grant the human
            # never made: each hop would be authorized by the previous hop, and the
            # person who started it would drop out of the record on the first one.
            authorized_by = parent.authorized_by

        # Resolved after the gate, never before it. Which room an agent is filed
        # under, and whether the id names one at all, are answers this method used
        # to give away forty lines before it asked whether the caller could act here.
        agent = await self.get_agent(target_agent_id)
        if agent.room_id != room_id:
            raise DomainError("the agent being asked is not in this room")

        # Built whole rather than added to. ``also_bounded_by`` is reached by one
        # function in this class and takes no principal from its caller, which is
        # what keeps it from being the widening door fourteen rounds looked for;
        # this method does take one from its caller, so it names its set outright
        # and stays a construction site like the mention gate beside it.
        #
        # Both humans, because both of their ceilings apply. The root is who the
        # chain is authorized by and the caller is who asked for this hop, and on a
        # delegated task they are different people. Naming only the root was
        # relocation fifteen: an editor narrowed to one capability asked through a
        # delegation and the delegate spent three, because the narrowed member was
        # swapped out of this set rather than added to it. A principal whose ceiling
        # applies is added; one that is not durable is a row to write, which is why
        # ``requested_by`` is stored below and read back at every spend.
        asking = {authorized_by, requested_by}
        if delegating_agent_id is not None:
            asking.add(agent_principal(delegating_agent_id))
        bounding = BoundingPrincipals(frozenset(asking))
        if not (await self._lendable_terms(agent, room_id, bounding)).lendable():
            # No principal and no target in the wording: the caller knows who they
            # are, and this string ends up in logs and in an error body that crosses
            # an organisational boundary.
            raise AuthorizationError("no effective capability to open a task for this agent")
        await self._require_addressable(agent, room_id, authorized_by)

        depth = require_delegable(ancestry, target_agent_id)
        agreed = negotiate_output_modes(accepted_output_modes, DEFAULT_OUTPUT_MODES)
        task = AgentTask(
            task_id=new_id("a2atask"),
            context_id=context_id,
            room_id=room_id,
            target_agent_id=target_agent_id,
            authorized_by=authorized_by,
            requested_by=requested_by,
            delegating_agent_id=delegating_agent_id,
            # The run the delegation was made under, read off the parent task rather
            # than believed from the argument, so the row says which turn actually
            # asked instead of which turn the asker claimed.
            delegating_run_id=parent.execution_id if parent is not None else None,
            accepted_output_modes=agreed,
            depth=depth,
        )
        actor_type = "agent" if delegating_agent_id is not None else "user"
        async with self.db.transaction():
            created = await self.repos.agent_tasks.create_in_transaction(task, ancestry)
            await self.repos.agent_tasks.append_message_with_next_sequence_in_transaction(
                created.task_id, TaskMessageRole.ASKER, parts
            )
            event = await self.repos.events.append_with_next_sequence_in_transaction(
                RoomEvent(
                    room_id=room_id,
                    sequence=0,
                    event_type=EventType.TASK_DELEGATED,
                    payload={
                        "task_id": created.task_id,
                        "context_id": created.context_id,
                        "state": created.state.value,
                        "target_agent_id": created.target_agent_id,
                        "delegating_agent_id": created.delegating_agent_id,
                        "updated_at": created.updated_at.isoformat(),
                        "authorized_by": created.authorized_by,
                        "depth": created.depth,
                        "requested_by": requested_by,
                    },
                    actor_id=delegating_agent_id or requested_by,
                    actor_type=actor_type,
                )
            )
        await self._broadcast_persisted_events([event])
        # NOT dispatched here yet. :meth:`_dispatch_agent_task_run` below is the
        # entry point the feature lacked, and calling it from here is what turns a
        # submitted task into a running one — but a turn dispatched from this line
        # runs to a terminal state before this method returns, which means a
        # delegating agent's run is closed by the time anything could delegate from
        # it. Delegation happens mid-turn, from inside the harness, so the trigger
        # belongs where the delegate's turn can hold itself open. Wiring it here
        # without that ends every chain at depth one. Named in the report, not
        # papered over.
        return created

    def dispatch_agent_task_in_background(self, task: AgentTask) -> None:
        """Schedule a submitted task's turn off the request path, supervised.

        A2A's message/send is non-blocking by contract (see `_accept_message`
        in a2a.py): the caller is owed a SUBMITTED task back immediately, not
        the wall time of a provider call. `_dispatch_agent_task_run` never
        raises anything but its own cancellation, propagated rather than
        swallowed, and every other exit resolves the task or logs, so this
        only needs to keep the asyncio.Task alive until it finishes; without
        that reference the loop is free to garbage-collect it mid-flight.
        """
        running = asyncio.create_task(self._dispatch_agent_task_run(task))
        self._background_tasks.add(running)
        running.add_done_callback(self._background_tasks.discard)

    async def sweep_stale_submitted_agent_tasks(self) -> int:
        """Drain every task stuck SUBMITTED past the point that can only mean
        a lost handoff, so a crash between accept and dispatch heals on its own.

        One task at a time, one batch per query: the drain is a marathon with
        a bounded stride, never a thundering herd of concurrent provider
        calls. `_dispatch_agent_task_run` never raises and resolves losing
        races through its compare-and-swap, so a task the post-accept path
        already grabbed is skipped here without ceremony.
        """
        drained = 0
        attempted: set[str] = set()
        while True:
            threshold = utcnow() - timedelta(seconds=self._STALE_SUBMITTED_TASK_SECONDS)
            stale = await self.repos.agent_tasks.list_stale_submitted(threshold)
            fresh = [task for task in stale if task.task_id not in attempted]
            if not fresh:
                # Anything still listed was already attempted this drain: a
                # task that will not leave SUBMITTED is a row to investigate,
                # not a reason to spin on it.
                return drained
            for task in fresh:
                attempted.add(task.task_id)
                await self._dispatch_agent_task_run(task)
                drained += 1

    async def sweep_stranded_working_agent_tasks(self) -> int:
        """Fail every task WORKING behind a run that has already settled.

        ``_dispatch_agent_task_run`` fails a task itself when its own turn
        ends badly or is cancelled, but a harder kill (SIGKILL, an OOM, the
        process dying) leaves no handler running to catch anything: the run
        it was driving is later settled by ``sweep_expired_run_leases``,
        ORPHANED or otherwise, and nothing else ever revisits the task,
        because ``sweep_stale_submitted_agent_tasks`` only looks at
        SUBMITTED. A task WORKING behind a settled run is a delegator waiting
        on an answer that will never come, which is exactly the failure mode
        the docstring on ``_dispatch_agent_task_run`` says a state machine
        exists to prevent; this is what makes that true after a restart too,
        not only while the same process is still running.
        """
        failed = 0
        for task in await self.repos.agent_tasks.list_working_with_settled_run():
            run = await self.repos.agent_runs.get_by_execution(task.execution_id or "")
            settlement = run.settlement.value if run is not None and run.settlement else "unknown"
            try:
                await self.fail_agent_task(
                    task.task_id,
                    f"the run driving this task settled ({settlement}) with nothing "
                    "left to carry it further",
                    by_agent_id=task.target_agent_id,
                )
                failed += 1
            except DomainError:
                # Something else moved the task on since the list was read; the
                # sweep's own compare-and-swap (inside transition()) is what
                # makes that race land on whoever actually won it, not on this.
                log.info("Agent task %s moved before the stranded sweep reached it", task.task_id)
        return failed

    async def _dispatch_agent_task_run(self, task: AgentTask) -> None:
        """Drive a submitted task to a terminal state, or say on the row why not.

        The invariant is the mention path's: no task is left in a state the system
        cannot describe. Starting it is the compare-and-swap that makes it this
        process's work; the claim on the execution then keeps the startup sweep from
        mistaking a live run for an orphan. Anything that escapes the turn fails the
        task rather than leaving it WORKING forever, because a delegator waiting on
        an answer that will never come is the failure mode a state machine exists to
        prevent.
        """
        asked = " ".join(part.content for part in await self._asker_parts(task.task_id))
        try:
            started = await self.start_agent_task(task.task_id)
        except DomainError:
            log.exception("Agent task %s could not be started", task.task_id)
            return
        execution_id = started.execution_id or ""
        if not await self.repos.executions.claim_for_dispatch(execution_id, self._dispatch_claim):
            log.info("Agent task run %s was already claimed; not dispatching", execution_id)
            return
        try:
            result = await self.execute_agent_step(
                execution_id, fenced(screen(asked, "agent task"))
            )
            output_id = str(result.get("output_id", ""))
            output = await self.repos.agent_outputs.get(output_id) if output_id else None
            if output is None:
                # A turn that ended without an answer is not a completed task. The
                # run's own settlement already says what happened to it.
                await self.fail_agent_task(
                    task.task_id,
                    "the turn ended without an answer",
                    by_agent_id=task.target_agent_id,
                )
                return
            await self.complete_agent_task(
                task.task_id,
                (Part(kind=PartKind.TEXT, content=output.content),),
                by_agent_id=task.target_agent_id,
            )
        except asyncio.CancelledError:
            # A shutdown cancels every fire-and-forget dispatch (server.py), and
            # CancelledError derives from BaseException, so it would otherwise
            # escape the Exception handler below and leave this task claiming to
            # be working forever, with nothing left running that could ever move
            # it. Failed here instead, then re-raised, so the cancellation still
            # propagates the way the rest of this process's shutdown expects.
            log.info("Agent task %s dispatch was cancelled", task.task_id)
            try:
                await self.fail_agent_task(
                    task.task_id, "dispatch was cancelled", by_agent_id=task.target_agent_id
                )
            except Exception:
                log.exception("Failed to fail agent task %s after cancellation", task.task_id)
            raise
        except Exception as exc:
            log.exception("Agent task %s did not complete", task.task_id)
            try:
                await self.fail_agent_task(
                    task.task_id, f"dispatch failed: {exc}", by_agent_id=task.target_agent_id
                )
            except Exception:
                # The task and its ask are already committed. Failing this write
                # would leave the row claiming to be working; the sweep is what
                # catches that, and it is told by logs rather than by a raise here.
                log.exception("Failed to fail agent task %s", task.task_id)

    async def _asker_parts(self, task_id: str) -> tuple[Part, ...]:
        """What the asker last said, which is the prompt the delegate answers."""
        messages = await self.repos.agent_tasks.list_messages(task_id)
        asks = [m for m in messages if m.role is TaskMessageRole.ASKER]
        return asks[-1].parts if asks else ()

    async def start_agent_task(self, task_id: str) -> AgentTask:
        """Open the turn that answers a submitted task.

        The execution is triggered DIRECT rather than by a fourth trigger value of
        its own. ``executions.triggered_by`` carries a CHECK constraint admitting
        MENTION, DIRECT and SCHEDULE, so a fourth would mean rebuilding the table —
        and it would restate there what one join already answers, because the fact
        that distinguishes a delegated turn, who asked and under which run, is a
        column on ``agent_tasks`` and not a shade of the reason a turn opened.

        The task moves before the turn is opened, because the move is the only
        mutual exclusion there is. Opening first meant two concurrent starts both
        built a session, an execution and a run envelope, and only the winner's got
        attached — leaving the loser's holding a live credential and a lease that
        nothing would ever come back to close. Losing the compare-and-swap now costs
        a refusal instead of an orphan.
        """
        task = await self._require_agent_task(task_id)
        agent = await self.get_agent(task.target_agent_id)
        run = await self._prepare_agent_run(agent, task.room_id, task.authorized_by)
        session = Session(
            session_id=new_id("sess"),
            room_id=task.room_id,
            agent_id=task.target_agent_id,
            status=SessionStatus.ACTIVE,
        )
        execution = Execution(
            execution_id=new_id("exec"),
            session_id=session.session_id,
            agent_id=task.target_agent_id,
            authorized_by=task.authorized_by,
            # The link the bound is derived through. A task opens a fresh run every
            # time it resumes, and each of them has to keep pointing at the task.
            agent_task_id=task.task_id,
            triggered_by=AgentTrigger.DIRECT,
            input_data={"agent_task_id": task.task_id, "context_id": task.context_id},
        )
        # The WORKING transition, the session/execution/run rows and the
        # attach that binds them back onto the task are one fact — a task
        # left WORKING with no run is an orphan the settler cannot see,
        # because it only scans MENTION triggers. All or nothing here.
        async with self.db.transaction():
            await self.repos.agent_tasks.transition_in_transaction(
                task_id, task.state, AgentTaskState.WORKING
            )
            await self.repos.sessions.create(session)
            execution = await self.repos.executions.create(execution)
            await self.repos.agent_runs.create_in_transaction(
                replace(run, execution_id=execution.execution_id)
            )
            await self.repos.agent_tasks.attach_execution_in_transaction(
                task_id, execution.execution_id
            )
            # The asker is a participant of this run, so the run carries a row
            # saying so and every spend reads it. ``authorized_by`` is already
            # an arm of the bound; on a delegated task the caller is somebody
            # else, and without this row their ceiling would apply at the door
            # and nowhere afterwards. In the same transaction as the run's own
            # creation, so a crash between the two cannot leave a WORKING task
            # whose bound omits its asker.
            await self.repos.executions.record_caller_in_transaction(
                execution.execution_id, task.requested_by
            )
        started = await self._require_agent_task(task_id)
        await self._append_agent_task_event(
            started, started.target_agent_id, "agent", execution_id=execution.execution_id
        )
        return started

    async def continue_agent_task(
        self, task_id: str, parts: tuple[Part, ...], *, requested_by: str
    ) -> AgentTask:
        """The asker answers what the delegate asked for, and the task resumes."""
        task = await self._asker_task(task_id, requested_by)
        # The message is what moves the task, so it is written first — and the move
        # is checked before it, or a refused transition would leave its message
        # standing in the task's log as a turn that never happened. Every method
        # here that writes before it transitions asks in this order for that reason.
        # Both writes are one transaction, so the loser of a race against another
        # caller of this same method never leaves its message standing without the
        # move it was written for.
        require_transition(task.state, AgentTaskState.WORKING)
        async with self.db.transaction():
            await self.repos.agent_tasks.append_message_with_next_sequence_in_transaction(
                task_id, TaskMessageRole.ASKER, parts
            )
            moved = await self.repos.agent_tasks.transition_in_transaction(
                task_id, task.state, AgentTaskState.WORKING
            )
        await self._append_agent_task_event(
            moved,
            requested_by,
            "agent" if requested_by == task.delegating_agent_id else "user",
        )
        return moved

    async def _delegate_task(self, task_id: str, by_agent_id: str) -> AgentTask:
        """The task, if this agent is the one being asked. One answer if not.

        Five verbs move a task from the delegate's side — it needs more, it needs a
        person, it answers, it fails, it declines — and all five took a task id and
        nothing else. Any principal that reached one could finish or fail somebody
        else's task, and the refusal is the unknown-task one for the same reason the
        asker's is: a caller who can tell "not yours" from "no such thing" can
        enumerate the deployment's task ids one guess at a time.
        """
        task = await self.repos.agent_tasks.get(task_id)
        if task is None or task.target_agent_id != by_agent_id:
            raise TaskNotFoundError(_NO_SUCH_AGENT_TASK)
        return task

    async def require_agent_task_input(
        self, task_id: str, parts: tuple[Part, ...], *, by_agent_id: str
    ) -> AgentTask:
        """The delegate needs more from whoever asked, and stops until it arrives."""
        task = await self._delegate_task(task_id, by_agent_id)
        require_transition(task.state, AgentTaskState.INPUT_REQUIRED)
        async with self.db.transaction():
            await self.repos.agent_tasks.append_message_with_next_sequence_in_transaction(
                task_id, TaskMessageRole.DELEGATE, parts
            )
            moved = await self.repos.agent_tasks.transition_in_transaction(
                task_id, task.state, AgentTaskState.INPUT_REQUIRED
            )
        await self._append_agent_task_event(moved, moved.target_agent_id, "agent")
        return moved

    async def escalate_agent_task(
        self, task_id: str, *, reason: str, by_agent_id: str
    ) -> AgentTask:
        """Nobody in the chain can lend what this task needs, so a person is asked.

        The reason rides on the event rather than on ``refusal_reason``: nothing has
        been refused yet, and a column that means "why it ended" would then be
        holding "what somebody is being asked for" on a task still very much alive.
        """
        task = await self._delegate_task(task_id, by_agent_id)
        moved = await self.repos.agent_tasks.transition(
            task_id, task.state, AgentTaskState.AUTH_REQUIRED
        )
        await self._append_agent_task_event(
            moved, moved.target_agent_id, "agent", escalation_reason=reason
        )
        return moved

    async def resolve_agent_task_escalation(
        self, task_id: str, *, granted: bool, by_user_id: str
    ) -> AgentTask:
        """A named person answers the one escalation, either way.

        "No" is a rejection of the task and not a failure of the agent that asked,
        which is why AUTH_REQUIRED has an edge to REJECTED at all.
        """
        task = await self._visible_agent_task(task_id, by_user_id, RoomCapability.MUTATE)
        target = AgentTaskState.WORKING if granted else AgentTaskState.REJECTED
        moved = await self.repos.agent_tasks.transition(
            task_id,
            task.state,
            target,
            refusal_reason="" if granted else f"{by_user_id} declined the escalation",
        )
        await self._append_agent_task_event(moved, by_user_id, "user", granted=granted)
        return moved

    async def complete_agent_task(
        self, task_id: str, parts: tuple[Part, ...], *, by_agent_id: str
    ) -> AgentTask:
        """The delegate answers, and the task ends where it was asked to end."""
        task = await self._delegate_task(task_id, by_agent_id)
        require_transition(task.state, AgentTaskState.COMPLETED)
        async with self.db.transaction():
            await self.repos.agent_tasks.append_message_with_next_sequence_in_transaction(
                task_id, TaskMessageRole.DELEGATE, parts
            )
            moved = await self.repos.agent_tasks.transition_in_transaction(
                task_id, task.state, AgentTaskState.COMPLETED
            )
        await self._append_agent_task_event(moved, moved.target_agent_id, "agent")
        return moved

    async def fail_agent_task(self, task_id: str, reason: str, *, by_agent_id: str) -> AgentTask:
        task = await self._delegate_task(task_id, by_agent_id)
        moved = await self.repos.agent_tasks.transition(
            task_id, task.state, AgentTaskState.FAILED, refusal_reason=reason
        )
        await self._append_agent_task_event(moved, moved.target_agent_id, "agent")
        return moved

    async def reject_agent_task(self, task_id: str, reason: str, *, by_agent_id: str) -> AgentTask:
        """The delegate declines before doing anything, which is not a failure."""
        task = await self._delegate_task(task_id, by_agent_id)
        moved = await self.repos.agent_tasks.transition(
            task_id, task.state, AgentTaskState.REJECTED, refusal_reason=reason
        )
        await self._append_agent_task_event(moved, moved.target_agent_id, "agent")
        return moved

    async def cancel_agent_task(self, task_id: str, *, requested_by: str) -> AgentTask:
        """The asker takes it back, unless it has already ended.

        A finished task that can be cancelled is not a task with a lifecycle, so the
        refusal is named rather than left to the transition table: a caller cancelling
        something that completed a second earlier is owed the difference between "too
        late" and "that was never allowed".
        """
        task = await self._asker_task(task_id, requested_by)
        if task.is_terminal:
            raise TaskNotCancelableError(
                f"task {task_id} is already {task.state.value} and cannot be canceled"
            )
        try:
            moved = await self.repos.agent_tasks.transition(
                task_id,
                task.state,
                AgentTaskState.CANCELED,
                refusal_reason=f"canceled by {requested_by}",
            )
        except DomainError:
            # It ended between the read above and the write. The caller is owed the
            # same name they would have got a moment earlier, not a description of
            # the compare-and-swap that lost.
            settled = await self.repos.agent_tasks.get(task_id)
            if settled is not None and settled.is_terminal:
                raise TaskNotCancelableError(
                    f"task {task_id} is already {settled.state.value} and cannot be canceled"
                ) from None
            raise
        await self._append_agent_task_event(
            moved,
            requested_by,
            "agent" if requested_by == task.delegating_agent_id else "user",
        )
        return moved

    async def get_agent_task(self, task_id: str, *, viewer_id: str) -> AgentTask:
        return await self._visible_agent_task(task_id, viewer_id, RoomCapability.READ)

    async def list_agent_task_messages(
        self, task_id: str, *, viewer_id: str
    ) -> list[AgentTaskMessage]:
        await self._visible_agent_task(task_id, viewer_id, RoomCapability.READ)
        return await self.repos.agent_tasks.list_messages(task_id)
