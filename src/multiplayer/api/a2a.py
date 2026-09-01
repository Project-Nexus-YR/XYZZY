"""A2A v0.3.0 on the wire: one JSON-RPC endpoint, one card, and the mapping between.

The protocol is Google's Agent2Agent, pinned at v0.3.0 and read off that tag's
published JSON schema rather than from memory. Every field name in this file is
the schema's own spelling, which is why several of them are camelCase in a
codebase that is not.

It is a module of its own rather than more of ``routes.py`` because it answers to
a different authority. The REST surface next door is this product's, and may be
reshaped whenever the product wants it reshaped; this one is a specification's,
and a change here is a change to what two independent implementations agree a
word means.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from ..domain.agent_card import CardIdentity, build_extended_card, build_public_card
from ..domain.agent_tasks import (
    TERMINAL_STATES,
    AgentTask,
    AgentTaskMessage,
    DelegationCycleError,
    DelegationDepthExceededError,
    Part,
    PartKind,
    PushNotificationNotSupportedError,
    TaskMessageRole,
    TaskNotCancelableError,
    TaskNotFoundError,
    UnsupportedOperationError,
)
from ..domain.models import AgentAddressing, AgentInstance, DomainError, utcnow
from ..realtime.websocket import REAUTH_SECONDS
from ..security import AuthorizationError, RoomCapability
from ..services.service import MultiplayerService
from . import routes
from .routes import CurrentUser

A2A_PATH = "/a2a/v1"
CARD_PATH = "/.well-known/agent-card.json"

# The specification's own error codes, in its own reserved range. Two of them are
# deliberately absent: InvalidAgentResponseError (-32006) describes a client
# reading a malformed reply and is never this server's to raise, and
# AuthenticatedExtendedCardNotConfiguredError (-32007) describes a deployment
# without an extended card, which this one always has.
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
PUSH_NOTIFICATION_NOT_SUPPORTED = -32003
UNSUPPORTED_OPERATION = -32004
CONTENT_TYPE_NOT_SUPPORTED = -32005

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

# The media type an internal Part carries when nobody said otherwise.
PLAIN_TEXT = "text/plain"

_A2A_ROLES = {TaskMessageRole.ASKER: "user", TaskMessageRole.DELEGATE: "agent"}
_TERMINAL_VALUES = frozenset(state.value for state in TERMINAL_STATES)

router = APIRouter(tags=["a2a"])


class _WireError(Exception):
    """A refusal that belongs to the wire rather than to the domain.

    The domain names the refusals it can make. Everything the protocol refuses
    before the domain is reached — an unparseable part, a configuration this
    server will not honour — has no domain class to raise, and inventing one in
    ``domain/`` would put transport concerns in the layer that must not know
    about transports.
    """

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# Read in order, most specific first. The two delegation refusals have no code in
# the specification, because the specification has no notion of a chain of
# agents. They answer with its "this server will not do that" and carry the real
# reason in `message`; minting a private code inside the range the specification
# reserves for itself would collide the day it names one.
_DOMAIN_CODES: tuple[tuple[type[DomainError], int], ...] = (
    (TaskNotFoundError, TASK_NOT_FOUND),
    (TaskNotCancelableError, TASK_NOT_CANCELABLE),
    (PushNotificationNotSupportedError, PUSH_NOTIFICATION_NOT_SUPPORTED),
    (DelegationCycleError, UNSUPPORTED_OPERATION),
    (DelegationDepthExceededError, UNSUPPORTED_OPERATION),
    (UnsupportedOperationError, UNSUPPORTED_OPERATION),
)


def _code_for(exc: DomainError) -> int:
    for kind, code in _DOMAIN_CODES:
        if isinstance(exc, kind):
            return code
    # An unnamed domain refusal is a rule this call broke — an agent addressed in
    # the wrong room, a task told to do something its state machine forbids.
    # Reporting it as an internal error would blame the server for a refusal the
    # caller earned, and hide the sentence that says which rule it was.
    return INVALID_PARAMS


# ── Part mapping ─────────────────────────────────────────────────────────────


def part_to_a2a(part: Part) -> dict[str, Any]:
    """One internal part as the A2A part it is.

    The internal model has three kinds where A2A has two types, because A2A's
    ``FilePart`` is a single type holding one of two mutually exclusive shapes:
    bytes inline, or a URI to fetch them from. Keeping RAW and URL apart is what
    lets every reader in this codebase branch on ``kind`` alone. Collapsing them
    into one FILE kind would mean re-deriving which shape a part actually holds
    by testing for the presence of a key, and a reader that has to guess is a
    reader that will eventually guess wrong.
    """
    if part.kind is PartKind.TEXT:
        payload: dict[str, Any] = {"kind": "text", "text": part.content}
        if part.media_type != PLAIN_TEXT:
            # A ``TextPart`` has no mimeType in the schema, so a text part that
            # is not plain text would arrive at the far end having quietly become
            # plain text. ``metadata`` is the schema's own extension point, and
            # putting it there is what keeps the round trip exact.
            payload["metadata"] = {"mediaType": part.media_type}
        return payload
    file: dict[str, Any] = (
        {"bytes": part.content} if part.kind is PartKind.RAW else {"uri": part.content}
    )
    file["mimeType"] = part.media_type
    return {"kind": "file", "file": file}


def part_from_a2a(raw: Any) -> Part:
    """One A2A part as the internal part it is, or a named refusal."""
    if not isinstance(raw, dict):
        raise _WireError(INVALID_PARAMS, "each part must be an object")
    kind = raw.get("kind")
    if kind == "text":
        metadata = raw.get("metadata")
        carried = (
            metadata.get("mediaType", PLAIN_TEXT) if isinstance(metadata, dict) else PLAIN_TEXT
        )
        return Part(kind=PartKind.TEXT, content=str(raw.get("text", "")), media_type=str(carried))
    if kind == "file":
        file = raw.get("file")
        if not isinstance(file, dict):
            raise _WireError(INVALID_PARAMS, "a file part carries a file object")
        media_type = str(file.get("mimeType", PLAIN_TEXT))
        if "bytes" in file:
            return Part(kind=PartKind.RAW, content=str(file["bytes"]), media_type=media_type)
        if "uri" in file:
            return Part(kind=PartKind.URL, content=str(file["uri"]), media_type=media_type)
        raise _WireError(INVALID_PARAMS, "a file part carries either bytes or a uri")
    if kind == "data":
        # A2A's DataPart is structured JSON, and nothing internal holds it. The
        # specification has a name for exactly this, so the caller is told; a
        # server that accepted the part and dropped it would leave the caller
        # believing the data arrived.
        raise _WireError(
            CONTENT_TYPE_NOT_SUPPORTED,
            "this agent takes text and file parts; a data part has nowhere to go",
        )
    raise _WireError(INVALID_PARAMS, f"unknown part kind {kind!r}")


def _parts_from_a2a(raw: Any) -> tuple[Part, ...]:
    if not isinstance(raw, list) or not raw:
        raise _WireError(INVALID_PARAMS, "a message carries at least one part")
    return tuple(part_from_a2a(item) for item in raw)


# ── Task and message mapping ─────────────────────────────────────────────────


def _message_to_a2a(message: AgentTaskMessage, task: AgentTask) -> dict[str, Any]:
    return {
        "kind": "message",
        "messageId": message.message_id,
        "role": _A2A_ROLES[message.role],
        "parts": [part_to_a2a(part) for part in message.parts],
        "taskId": message.task_id,
        "contextId": task.context_id,
    }


def _task_to_a2a(
    task: AgentTask, messages: list[AgentTaskMessage], history_length: int | None
) -> dict[str, Any]:
    """The durable task as the document A2A describes.

    ``status.message`` is left off. The history below already carries every turn,
    and repeating the last one inside the status would hand a client two copies
    of one message with nothing to tell it they are the same message.
    """
    history = [_message_to_a2a(message, task) for message in messages]
    if history_length is not None:
        history = history[-history_length:] if history_length > 0 else []
    metadata: dict[str, Any] = {"roomId": task.room_id, "targetAgentId": task.target_agent_id}
    if task.refusal_reason:
        # The only place the reason a task ended survives at all.
        metadata["refusalReason"] = task.refusal_reason
    return {
        "id": task.task_id,
        "kind": "task",
        "contextId": task.context_id,
        "status": {"state": task.state.value, "timestamp": task.updated_at.isoformat()},
        "history": history,
        # A2A artifacts are the named outputs a task produced. This product keeps
        # agent outputs as room rows that outlive the task and are chosen by hand
        # afterwards, so the task record owns none: empty is the truth about it
        # rather than a field nobody got round to filling.
        "artifacts": [],
        "metadata": metadata,
    }


async def _task_payload(
    svc: MultiplayerService, task: AgentTask, viewer_id: str, history_length: int | None
) -> dict[str, Any]:
    messages = await svc.list_agent_task_messages(task.task_id, viewer_id=viewer_id)
    return _task_to_a2a(task, messages, history_length)


# ── Card ─────────────────────────────────────────────────────────────────────


def _identity(request: Request) -> CardIdentity:
    """The deployment as it describes itself on this request.

    The card has to name the endpoint a client should post to, and it is derived
    from the request rather than configured: a deployment that must restate its
    own URL in an environment variable is a deployment that will advertise the
    old one for a while after it moves.
    """
    return CardIdentity(url=str(request.base_url).rstrip("/") + A2A_PATH)


def _sso_configured() -> bool:
    sessions = routes._sessions
    return sessions is not None and sessions.provider.settings.configured


@router.get(CARD_PATH)
async def agent_card(request: Request) -> dict[str, Any]:
    """Discovery, unauthenticated by design: the door, never the room."""
    return build_public_card(_identity(request), sso_configured=_sso_configured())


async def _extended_card(
    svc: MultiplayerService, request: Request, viewer_id: str, params: dict[str, Any]
) -> dict[str, Any]:
    """The authenticated card: the agents this caller may actually address.

    The specification's request carries no parameters, because it was written for
    a server that fronts one agent. This one fronts every agent in every room,
    and nothing behind it answers "every agent this person could address
    anywhere" — so the room is named in ``params`` and the card describes that
    room. A call naming none gets the public card's empty skill list, which is
    still the truth: nothing was asked about.
    """
    room_id = params.get("roomId")
    agents: list[tuple[AgentInstance, AgentAddressing | None]] = []
    if isinstance(room_id, str) and room_id:
        await svc.authorization.require(room_id, viewer_id, RoomCapability.READ)
        for agent in await svc.list_room_agents(room_id):
            agents.append((agent, await svc.repos.agent_addressing.get(agent.agent_id)))
    return build_extended_card(
        _identity(request),
        sso_configured=_sso_configured(),
        viewer_id=viewer_id,
        agents=agents,
    )


# ── Methods ──────────────────────────────────────────────────────────────────


def _required_id(params: dict[str, Any]) -> str:
    task_id = params.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise _WireError(INVALID_PARAMS, "params.id names the task")
    return task_id


def _history_length(source: dict[str, Any]) -> int | None:
    length = source.get("historyLength")
    if length is None:
        return None
    if not isinstance(length, int) or isinstance(length, bool):
        raise _WireError(INVALID_PARAMS, "historyLength must be a whole number")
    return length


async def _accept_message(
    svc: MultiplayerService, user_id: str, params: dict[str, Any]
) -> tuple[AgentTask, int | None]:
    """Open a task or add to one, from a `MessageSendParams`.

    A2A addresses one agent per endpoint URL. This server fronts many agents
    inside many rooms, and the specification has no path segment to say which —
    so the room and the agent ride in the message's ``metadata``, which is the
    schema's own free-form extension point. A message that names an existing
    ``taskId`` needs neither: the task already knows both.
    """
    message = params.get("message")
    if not isinstance(message, dict):
        raise _WireError(INVALID_PARAMS, "params.message is required")
    parts = _parts_from_a2a(message.get("parts"))

    configuration = params.get("configuration") or {}
    if not isinstance(configuration, dict):
        raise _WireError(INVALID_PARAMS, "params.configuration must be an object")
    if configuration.get("pushNotificationConfig") is not None:
        raise PushNotificationNotSupportedError("this server does not push; the card says so")
    if configuration.get("blocking"):
        # Blocking means "hold the response until the task reaches a terminal
        # state". A task here is answered by a harness running outside this
        # request, so there is no moment in it at which that could be awaited:
        # honouring the flag would hold the connection until something else timed
        # it out. Refused by name, which the specification allows, rather than
        # answered non-blocking under a flag that asked for the opposite.
        raise _WireError(
            UNSUPPORTED_OPERATION,
            "this server does not block; poll tasks/get or open message/stream",
        )
    history_length = _history_length(configuration)
    modes = tuple(str(mode) for mode in configuration.get("acceptedOutputModes") or ())

    task_id = message.get("taskId")
    if isinstance(task_id, str) and task_id:
        return await svc.continue_agent_task(task_id, parts, requested_by=user_id), history_length

    metadata = message.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    room_id, agent_id = metadata.get("roomId"), metadata.get("targetAgentId")
    if not isinstance(room_id, str) or not isinstance(agent_id, str) or not room_id or not agent_id:
        raise _WireError(
            INVALID_PARAMS,
            "a new task names its room and agent in message.metadata as roomId and targetAgentId",
        )
    task = await svc.open_agent_task(
        room_id,
        agent_id,
        parts,
        requested_by=user_id,
        accepted_output_modes=modes,
    )
    # The accept already committed above; dispatching is scheduled rather than
    # awaited so this call returns the SUBMITTED task immediately instead of
    # blocking on the provider call the task/get and message/stream endpoints
    # exist to let the caller poll or watch instead.
    svc.dispatch_agent_task_in_background(task)
    return task, history_length


# ── Streaming ────────────────────────────────────────────────────────────────


def _sse(call_id: Any, result: dict[str, Any]) -> str:
    """One SSE event carrying one JSON-RPC response.

    A2A streams responses rather than bare results: every event is a complete
    envelope echoing the id of the call that opened the stream.
    """
    return "data: " + json.dumps({"jsonrpc": "2.0", "id": call_id, "result": result}) + "\n\n"


def _status_update(event: dict[str, Any], task: AgentTask) -> dict[str, Any] | None:
    """One room event as a `TaskStatusUpdateEvent`, or nothing if it is not ours.

    The hub carries the whole room's feed, and this stream promised one task.

    ``final`` is deliberately not set here. The schema defines it as "the final
    event in the stream for this interaction", which is a fact about the stream
    and not about the state — only the loop draining this knows whether it is
    about to send anything else.
    """
    if event.get("type") != "room_event":
        return None
    payload = event.get("payload")
    if not isinstance(payload, dict) or payload.get("task_id") != task.task_id:
        return None
    state = payload.get("state")
    if not isinstance(state, str):
        return None
    timestamp = payload.get("updated_at") or event.get("timestamp")
    return {
        "taskId": task.task_id,
        "contextId": str(payload.get("context_id") or task.context_id),
        "kind": "status-update",
        "status": {"state": state, "timestamp": str(timestamp)},
    }


def _closing_event(task: AgentTask, state: str, reason: str) -> dict[str, Any]:
    """The last thing a stream says when it ends for a reason of its own.

    ``final`` is the schema's "this is the final event in the stream for this
    interaction" — a fact about the stream, not about the task. A stream that
    simply stopped would leave a client unable to separate a finished task from
    a dropped socket, which is the single thing this flag exists to settle, so
    every way out of the loop below goes through here.

    The state carried is the last one this stream actually observed. It is not a
    claim that the task stopped there, and ``metadata.closedBecause`` is what
    says so: somebody losing access is owed the difference between "it is over"
    and "you may no longer watch".
    """
    return {
        "taskId": task.task_id,
        "contextId": task.context_id,
        "kind": "status-update",
        "status": {"state": state, "timestamp": utcnow().isoformat()},
        "final": True,
        "metadata": {"closedBecause": reason},
    }


async def _task_events(
    svc: MultiplayerService,
    authorization: str | None,
    viewer_id: str,
    task: AgentTask,
    snapshot: dict[str, Any],
    call_id: Any,
) -> AsyncIterator[str]:
    """The task as it stands, then every move it makes until it stops moving.

    The shape is the WebSocket send loop's, for the reasons that loop has it: the
    hub's queue is bounded and drained on a timeout so a quiet task still gets a
    heartbeat, and the credential is re-read on that same beat, because a
    revocation reaches a live stream from a process this one never hears from.

    ``TaskStatusUpdateEvent`` requires ``final``, and requires it on that event
    alone — ``TaskArtifactUpdateEvent`` has no such field. It is true exactly
    once per stream, on whatever event turns out to be the last one: the task
    reaching a state it can never leave, a resubscribe to something already
    over, or a credential that stopped being good. Never on a move the task is
    still expected to make.
    """
    yield _sse(call_id, snapshot)
    if task.is_terminal:
        # Nothing will ever arrive. The stream still has to say it is over: a
        # snapshot followed by silence reads exactly like a connection that died
        # a moment after opening.
        yield _sse(call_id, _closing_event(task, task.state.value, "task-already-terminal"))
        return

    subscription = await svc.hub.subscribe(task.room_id, viewer_id)
    authenticator = routes._authenticator
    last_state = task.state.value
    try:
        loop = asyncio.get_running_loop()
        next_reauth = loop.time() + REAUTH_SECONDS
        while True:
            if loop.time() >= next_reauth:
                if authenticator is None:
                    yield _sse(call_id, _closing_event(task, last_state, "server-shutting-down"))
                    return
                try:
                    await authenticator.authenticate(authorization)
                except Exception:
                    # Revoked, or unre-checkable because the database went away.
                    # Either way this stream has lost the right to continue.
                    yield _sse(call_id, _closing_event(task, last_state, "credential-ended"))
                    return
                next_reauth = loop.time() + REAUTH_SECONDS
            try:
                event = await asyncio.wait_for(subscription.queue.get(), timeout=REAUTH_SECONDS)
            except TimeoutError:
                # An SSE comment: it stops a proxy or a client calling a quiet
                # stream dead, and carries no event anybody has to parse.
                yield ": keep-alive\n\n"
                continue
            if event.get("type") == "access_revoked":
                # Told rather than dropped. Somebody whose membership was just
                # removed is going to reconnect otherwise, and keep reconnecting.
                yield _sse(call_id, _closing_event(task, last_state, "access-revoked"))
                return
            update = _status_update(event, task)
            if update is None:
                continue
            last_state = update["status"]["state"]
            # Here the two meanings coincide: a terminal state is the only thing
            # that ends this stream on its own, so it is the only move that can
            # be the last event. Every other ending is a `_closing_event`.
            update["final"] = last_state in _TERMINAL_VALUES
            yield _sse(call_id, update)
            if update["final"]:
                return
    finally:
        # Reached on a client disconnect too: closing the response closes this
        # generator, and a subscription nobody drains is a queue that fills.
        await svc.hub.unsubscribe(subscription.subscription_id)


def _stream(
    svc: MultiplayerService,
    request: Request,
    viewer_id: str,
    task: AgentTask,
    snapshot: dict[str, Any],
    call_id: Any,
) -> StreamingResponse:
    return StreamingResponse(
        _task_events(svc, request.headers.get("authorization"), viewer_id, task, snapshot, call_id),
        media_type="text/event-stream",
        # A stream held anywhere along the way is a stream that arrives all at
        # once at the end, which is the one thing it exists not to do. The cache
        # header is the standard instruction; `x-accel-buffering` is nginx's,
        # named here because nginx is what is usually in front.
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


# ── The endpoint ─────────────────────────────────────────────────────────────


def _echoable_id(raw: Any) -> str | int | float | None:
    """The id this call may be answered with.

    JSON-RPC allows a string, a number, or null and nothing else. An object or
    an array echoed back verbatim would be this server agreeing that it was a
    valid id, so anything else becomes null — the same answer a client gets when
    the id could not be read at all, which is the truth about it.
    """
    if isinstance(raw, bool):
        return None
    return raw if isinstance(raw, str | int | float) else None


def _failure(call_id: Any, code: int, message: str) -> JSONResponse:
    """A refused call is HTTP 200 with an `error` member.

    The transport succeeded; the call did not, and JSON-RPC says so in the body.
    Only authentication answers below the protocol, with the 401 A2A asks for.
    """
    return JSONResponse(
        {"jsonrpc": "2.0", "id": call_id, "error": {"code": code, "message": message}}
    )


@router.post(A2A_PATH)
async def json_rpc(request: Request, principal: CurrentUser) -> Response:
    """The whole A2A surface, dispatched by method name.

    Authentication answers below the protocol, with the 401 A2A asks for.
    Authorization answers at both levels at once: 403 on the status line, and a
    JSON-RPC envelope in the body carrying the specification's own name for "this
    server will not carry out that operation" — which is the true statement about
    that caller and that room. No private code is minted for it, for the reason
    the delegation refusals do not mint one either.
    """
    try:
        body = await request.json()
    except ValueError:
        return _failure(None, PARSE_ERROR, "the request body is not JSON")
    if not isinstance(body, dict):
        return _failure(None, INVALID_REQUEST, "a call is one JSON-RPC object; A2A has no batches")
    call_id = _echoable_id(body.get("id"))
    if body.get("jsonrpc") != "2.0":
        return _failure(call_id, INVALID_REQUEST, "jsonrpc must be '2.0'")
    method = body.get("method")
    if not isinstance(method, str):
        # Not method-not-found: that answer is for a well-formed call naming a
        # method this server does not have, and this call is not well formed.
        return _failure(call_id, INVALID_REQUEST, "method must be a string")
    params = body.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _failure(call_id, INVALID_PARAMS, "params must be an object")

    svc = routes._svc_or_404()
    user_id = principal.user_id
    try:
        if method == "message/send":
            task, history_length = await _accept_message(svc, user_id, params)
            result = await _task_payload(svc, task, user_id, history_length)
        elif method == "message/stream":
            task, history_length = await _accept_message(svc, user_id, params)
            snapshot = await _task_payload(svc, task, user_id, history_length)
            return _stream(svc, request, user_id, task, snapshot, call_id)
        elif method == "tasks/get":
            task = await svc.get_agent_task(_required_id(params), viewer_id=user_id)
            result = await _task_payload(svc, task, user_id, _history_length(params))
        elif method == "tasks/cancel":
            task = await svc.cancel_agent_task(_required_id(params), requested_by=user_id)
            result = await _task_payload(svc, task, user_id, None)
        elif method == "tasks/resubscribe":
            task = await svc.get_agent_task(_required_id(params), viewer_id=user_id)
            snapshot = await _task_payload(svc, task, user_id, None)
            return _stream(svc, request, user_id, task, snapshot, call_id)
        elif method in ("tasks/pushNotificationConfig/set", "tasks/pushNotificationConfig/get"):
            # The card advertises pushNotifications: false. A server that
            # advertises false and then takes the call is worse than one that
            # refuses, because the client leaves believing it has a webhook.
            raise PushNotificationNotSupportedError(
                "this server does not push; subscribe with message/stream or tasks/resubscribe"
            )
        elif method == "agent/getAuthenticatedExtendedCard":
            result = await _extended_card(svc, request, user_id, params)
        else:
            return _failure(call_id, METHOD_NOT_FOUND, f"no method {method!r}")
    except _WireError as exc:
        return _failure(call_id, exc.code, exc.message)
    except AuthorizationError:
        # Both answers, because each reader needs a different one. The 403 is the
        # transport-level refusal A2A asks for and the one a proxy or a browser
        # understands; the envelope is what a JSON-RPC client parses, and without
        # it "forbidden" and "this server fell over" are the same event.
        #
        # The message is the constant "forbidden" rather than the exception's
        # sentence, which names the principal and the room it was refused. That
        # sentence belongs in the audit log, not in a reply to whoever asked:
        # telling a stranger which principal id failed a check confirms the id.
        return JSONResponse(
            status_code=403,
            content={
                "jsonrpc": "2.0",
                "id": call_id,
                "error": {"code": UNSUPPORTED_OPERATION, "message": "forbidden"},
            },
        )
    except DomainError as exc:
        return _failure(call_id, _code_for(exc), str(exc))
    return JSONResponse({"jsonrpc": "2.0", "id": call_id, "result": result})
