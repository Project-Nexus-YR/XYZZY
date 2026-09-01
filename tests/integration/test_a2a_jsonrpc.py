"""The A2A surface, driven the way another implementation would drive it.

What is asserted here is conformance rather than behaviour: that the card says
0.3.0 and discloses nothing, that the envelope is JSON-RPC in both directions
including the id, that every named refusal comes back as its own code, and that
a part survives a round trip through the wire format unchanged. A client written
against the specification and never against this codebase is the reader these
tests stand in for.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from multiplayer.api import routes
from multiplayer.api.a2a import part_from_a2a, part_to_a2a
from multiplayer.domain.agent_tasks import Part, PartKind
from multiplayer.server import create_app
from multiplayer.services.service import MultiplayerService

TOKENS = {"owner-token": "user_1", "stranger-token": "user_2"}
AUTH = {"Authorization": "Bearer owner-token"}


@pytest.fixture
async def client():
    app = create_app(":memory:", auth_tokens=TOKENS)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c


async def _room_with_agent(client: AsyncClient) -> tuple[str, str]:
    org = (
        await client.post(
            "/api/v1/organizations", json={"name": "Acme", "slug": "acme"}, headers=AUTH
        )
    ).json()
    workspace = (
        await client.post(
            f"/api/v1/organizations/{org['org_id']}/workspaces",
            json={"name": "Main", "slug": "main"},
            headers=AUTH,
        )
    ).json()
    room = (
        await client.post(
            f"/api/v1/workspaces/{workspace['workspace_id']}/rooms",
            json={"name": "Auth Migration"},
            headers=AUTH,
        )
    ).json()
    await client.post(f"/api/v1/rooms/{room['room_id']}/join", headers=AUTH)
    templates = (await client.get("/api/v1/agent-templates", headers=AUTH)).json()
    agent = (
        await client.post(
            f"/api/v1/rooms/{room['room_id']}/agents",
            json={"template_id": templates[0]["template_id"], "name": "Forge"},
            headers=AUTH,
        )
    ).json()
    return room["room_id"], agent["agent_id"]


def _hold_the_task_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep message/send's real accept-and-background-dispatch, but never let
    the dispatch itself run: these tests drive a task's lifecycle by hand to
    exercise stream/resubscribe mechanics, and the SIMULATED provider is fast
    enough to race that manual driving to a terminal state on its own.
    """
    monkeypatch.setattr(
        MultiplayerService, "dispatch_agent_task_in_background", lambda self, task: None
    )


def _send_params(room_id: str, agent_id: str) -> dict[str, Any]:
    return {
        "message": {
            "kind": "message",
            "messageId": "msg-1",
            "role": "user",
            "parts": [{"kind": "text", "text": "port the auth service"}],
            "metadata": {"roomId": room_id, "targetAgentId": agent_id},
        }
    }


async def _call(
    client: AsyncClient, method: str, params: dict[str, Any] | None = None, call_id: Any = 1
) -> Any:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": call_id, "method": method}
    if params is not None:
        body["params"] = params
    return await client.post("/a2a/v1", json=body, headers=AUTH)


@pytest.mark.asyncio
async def test_the_card_is_reachable_with_no_credential_and_names_the_version(client):
    answer = await client.get("/.well-known/agent-card.json")
    assert answer.status_code == 200
    # Three components. A client string-comparing this would read "0.3" as a
    # version it has never heard of.
    assert answer.json()["protocolVersion"] == "0.3.0"


@pytest.mark.asyncio
async def test_the_public_card_discloses_no_agents(client):
    await _room_with_agent(client)
    card = (await client.get("/.well-known/agent-card.json")).json()
    # The room's membership is the access-control decision. A public list of
    # skills would publish the shape of a private workspace to anyone with a URL.
    assert card["skills"] == []


@pytest.mark.asyncio
async def test_an_unauthenticated_call_is_refused_by_the_transport(client):
    answer = await client.post(
        "/a2a/v1", json={"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "x"}}
    )
    assert answer.status_code == 401
    assert answer.headers["www-authenticate"] == "Bearer"
    # Authentication is below the protocol: there is no JSON-RPC envelope here.
    assert "error" not in answer.json()


@pytest.mark.asyncio
async def test_a_body_that_is_not_json_is_a_parse_error_with_a_null_id(client):
    answer = await client.post(
        "/a2a/v1", content=b"{not json", headers={**AUTH, "content-type": "application/json"}
    )
    assert answer.status_code == 200
    body = answer.json()
    assert body["error"]["code"] == -32700
    # Nothing was parsed, so nothing is known about the id.
    assert body["id"] is None


@pytest.mark.asyncio
async def test_an_unknown_method_is_a_method_not_found(client):
    body = (await _call(client, "tasks/teleport", {})).json()
    assert body["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_a_well_formed_call_with_missing_params_is_an_invalid_params(client):
    assert (await _call(client, "tasks/get")).json()["error"]["code"] == -32602
    assert (await _call(client, "message/send")).json()["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_message_send_opens_a_task(client):
    room_id, agent_id = await _room_with_agent(client)
    body = (await _call(client, "message/send", _send_params(room_id, agent_id))).json()

    task = body["result"]
    assert task["kind"] == "task"
    # The specification's own lowercase spelling, straight off the enum.
    assert task["status"]["state"] == "submitted"
    assert task["id"] and task["contextId"]
    assert task["history"][0]["role"] == "user"
    assert task["history"][0]["parts"][0]["text"] == "port the auth service"


@pytest.mark.asyncio
async def test_message_send_actually_dispatches_the_task(client):
    """message/send used to persist the task and return, with nothing left to
    ever run it: the dispatcher (`_dispatch_agent_task_run`) had zero
    production callers, so an A2A task sat SUBMITTED forever. The accept
    schedules the dispatch as a background task now — non-blocking, so the
    immediate response is still SUBMITTED, but the task must leave that state
    on its own shortly after, against the SIMULATED provider tests run
    under (no model provider is configured for this fixture).
    """
    room_id, agent_id = await _room_with_agent(client)
    opened = (await _call(client, "message/send", _send_params(room_id, agent_id))).json()["result"]
    assert opened["status"]["state"] == "submitted"

    for _ in range(200):
        got = (await _call(client, "tasks/get", {"id": opened["id"]})).json()["result"]
        if got["status"]["state"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError(f"task never reached a terminal state: {got['status']['state']}")

    assert got["status"]["state"] in ("completed", "failed")


@pytest.mark.asyncio
async def test_tasks_get_on_an_unknown_id_is_a_task_not_found(client):
    body = (await _call(client, "tasks/get", {"id": "a2atask_nobody"})).json()
    assert body["error"]["code"] == -32001


@pytest.mark.asyncio
async def test_cancelling_a_task_that_already_ended_is_a_task_not_cancelable(client, monkeypatch):
    _hold_the_task_open(monkeypatch)
    room_id, agent_id = await _room_with_agent(client)
    opened = (await _call(client, "message/send", _send_params(room_id, agent_id))).json()["result"]

    first = (await _call(client, "tasks/cancel", {"id": opened["id"]})).json()
    assert first["result"]["status"]["state"] == "canceled"

    again = (await _call(client, "tasks/cancel", {"id": opened["id"]})).json()
    # Too late is a different answer from never allowed, and the caller is owed
    # the difference.
    assert again["error"]["code"] == -32002


@pytest.mark.asyncio
async def test_push_notification_configuration_is_refused_by_name(client):
    for method in ("tasks/pushNotificationConfig/set", "tasks/pushNotificationConfig/get"):
        body = (await _call(client, method, {"taskId": "a2atask_x"})).json()
        assert body["error"]["code"] == -32003


@pytest.mark.asyncio
async def test_a_data_part_is_refused_rather_than_dropped(client):
    room_id, agent_id = await _room_with_agent(client)
    params = _send_params(room_id, agent_id)
    params["message"]["parts"] = [{"kind": "data", "data": {"rows": [1, 2]}}]

    body = (await _call(client, "message/send", params)).json()
    assert body["error"]["code"] == -32005


def test_every_internal_part_kind_survives_the_round_trip():
    parts = (
        Part(kind=PartKind.TEXT, content="plain words"),
        Part(kind=PartKind.TEXT, content="# heading", media_type="text/markdown"),
        Part(kind=PartKind.RAW, content="ZmFrZQ==", media_type="image/png"),
        Part(kind=PartKind.URL, content="https://example.test/x.pdf", media_type="application/pdf"),
    )
    for part in parts:
        assert part_from_a2a(part_to_a2a(part)) == part

    # The two file kinds are one A2A type wearing two shapes, and each shape has
    # to be the one that comes back.
    assert part_to_a2a(parts[2])["file"]["bytes"] == "ZmFrZQ=="
    assert part_to_a2a(parts[3])["file"]["uri"] == "https://example.test/x.pdf"


@pytest.mark.asyncio
async def test_message_stream_is_an_event_stream_that_ends_when_the_task_does(client, monkeypatch):
    _hold_the_task_open(monkeypatch)
    room_id, agent_id = await _room_with_agent(client)
    opened = (await _call(client, "message/send", _send_params(room_id, agent_id))).json()["result"]

    svc = routes._svc
    assert svc is not None

    async def cancel_once_the_stream_is_listening() -> None:
        # Cancelling before the stream subscribes would broadcast to nobody and
        # leave the test waiting on an event that already happened.
        while await svc.hub.room_subscriber_count(room_id) == 0:
            await asyncio.sleep(0.01)
        await svc.cancel_agent_task(opened["id"], requested_by="user_1")

    canceller = asyncio.create_task(cancel_once_the_stream_is_listening())
    params = _send_params(room_id, agent_id)
    params["message"]["taskId"] = opened["id"]
    answer = await asyncio.wait_for(_call(client, "message/stream", params, call_id="s-1"), 10)
    await canceller

    assert answer.status_code == 200
    assert answer.headers["content-type"].startswith("text/event-stream")
    # Without these a proxy may hold the whole stream and deliver it at the end,
    # which is the one thing a stream exists not to do.
    assert answer.headers["cache-control"] == "no-cache"
    assert answer.headers["x-accel-buffering"] == "no"

    events = [line for line in answer.text.splitlines() if line.startswith("data: ")]
    # The task as it stood, then the move that ended it.
    assert len(events) == 2
    assert '"canceled"' in events[-1]
    assert '"kind": "status-update"' in events[-1]
    # Required on a status update, and true on exactly the one that ends it.
    assert '"final": true' in events[-1]


@pytest.mark.asyncio
async def test_a_stream_carries_its_own_task_and_is_not_ended_by_another(client, monkeypatch):
    _hold_the_task_open(monkeypatch)
    room_id, agent_id = await _room_with_agent(client)
    watched = (await _call(client, "message/send", _send_params(room_id, agent_id))).json()[
        "result"
    ]
    other = (await _call(client, "message/send", _send_params(room_id, agent_id))).json()["result"]

    svc = routes._svc
    assert svc is not None

    async def end_the_other_task_first() -> None:
        while await svc.hub.room_subscriber_count(room_id) == 0:
            await asyncio.sleep(0.01)
        # Both tasks share one room, so both land in the one subscription this
        # stream drains. A rejection is a terminal state, so a stream that failed
        # to tell the two tasks apart would report it and close here.
        await svc.reject_agent_task(other["id"], "not mine to answer", by_agent_id=agent_id)
        await svc.cancel_agent_task(watched["id"], requested_by="user_1")

    ending = asyncio.create_task(end_the_other_task_first())
    params = _send_params(room_id, agent_id)
    params["message"]["taskId"] = watched["id"]
    answer = await asyncio.wait_for(_call(client, "message/stream", params, call_id="s-2"), 10)
    await ending

    events = [line for line in answer.text.splitlines() if line.startswith("data: ")]
    assert len(events) == 2
    # The other task's terminal state reached the queue first. It must be in
    # neither event, under its own id or relabelled with this task's.
    assert "rejected" not in answer.text
    assert other["id"] not in answer.text
    assert '"canceled"' in events[-1] and '"final": true' in events[-1]


@pytest.mark.asyncio
async def test_a_move_the_task_may_still_leave_is_not_marked_final(client, monkeypatch):
    _hold_the_task_open(monkeypatch)
    room_id, agent_id = await _room_with_agent(client)
    opened = (await _call(client, "message/send", _send_params(room_id, agent_id))).json()["result"]

    svc = routes._svc
    assert svc is not None

    async def move_then_end() -> None:
        while await svc.hub.room_subscriber_count(room_id) == 0:
            await asyncio.sleep(0.01)
        # The legal first move out of submitted. A stream that called this the
        # end would leave a client watching a task that had barely started.
        await svc.continue_agent_task(
            opened["id"], (Part(kind=PartKind.TEXT, content="more"),), requested_by="user_1"
        )
        await svc.cancel_agent_task(opened["id"], requested_by="user_1")

    moving = asyncio.create_task(move_then_end())
    # Resubscribe rather than message/stream: this task must still be submitted
    # when the stream opens, and continuing it is what moves it.
    answer = await asyncio.wait_for(
        _call(client, "tasks/resubscribe", {"id": opened["id"]}, call_id="s-3"), 10
    )
    await moving

    events = [line for line in answer.text.splitlines() if line.startswith("data: ")]
    assert len(events) == 3
    assert '"working"' in events[1] and '"final": false' in events[1]
    assert '"canceled"' in events[2] and '"final": true' in events[2]


@pytest.mark.asyncio
async def test_a_resubscribe_to_a_task_already_over_says_so_rather_than_stopping(
    client, monkeypatch
):
    _hold_the_task_open(monkeypatch)
    room_id, agent_id = await _room_with_agent(client)
    opened = (await _call(client, "message/send", _send_params(room_id, agent_id))).json()["result"]
    await _call(client, "tasks/cancel", {"id": opened["id"]})

    answer = await asyncio.wait_for(
        _call(client, "tasks/resubscribe", {"id": opened["id"]}, call_id="s-4"), 10
    )

    events = [line for line in answer.text.splitlines() if line.startswith("data: ")]
    # A snapshot then silence is indistinguishable from a dropped socket, which
    # is the confusion `final` exists to end.
    assert len(events) == 2
    assert '"final": true' in events[-1]
    assert '"closedBecause": "task-already-terminal"' in events[-1]


@pytest.mark.asyncio
async def test_an_id_that_json_rpc_does_not_allow_comes_back_null(client):
    # A string, a number or null. Echoing an object verbatim would be this
    # server agreeing it was a valid id.
    for bad in ({"a": 1}, [1, 2], True):
        body = (await _call(client, "tasks/get", {"id": "x"}, call_id=bad)).json()
        assert body["id"] is None


@pytest.mark.asyncio
async def test_a_malformed_call_is_an_invalid_request_not_a_missing_method(client):
    # Method-not-found is the answer for a well-formed call naming a method this
    # server does not have. A call with no method at all is not well formed.
    for body in ({"jsonrpc": "2.0", "id": 1}, {"jsonrpc": "2.0", "id": 2, "method": 7}):
        answer = await client.post("/a2a/v1", json=body, headers=AUTH)
        assert answer.json()["error"]["code"] == -32600


@pytest.mark.asyncio
async def test_a_forbidden_call_answers_in_the_envelope_without_naming_anyone(client):
    room_id, agent_id = await _room_with_agent(client)

    # A second authenticated principal, in none of the first one's rooms. Opening
    # a task is the call that refuses on the authorization itself; reading one is
    # not, because a read has to answer the same way for a task that does not
    # exist, and that is the test below rather than this one.
    answer = await client.post(
        "/a2a/v1",
        json={
            "jsonrpc": "2.0",
            "id": 9,
            "method": "message/send",
            "params": _send_params(room_id, agent_id),
        },
        headers={"Authorization": "Bearer stranger-token"},
    )

    assert answer.status_code == 403
    body = answer.json()
    # A client that only reads bodies must still be able to tell a refusal from
    # a server that fell over.
    assert body["jsonrpc"] == "2.0" and body["id"] == 9
    assert body["error"]["message"] == "forbidden"
    # Naming the principal that failed the check confirms the principal exists.
    assert "user_2" not in answer.text


@pytest.mark.asyncio
async def test_reading_a_task_you_may_not_see_answers_as_if_it_did_not_exist(client):
    # Refusing a real task differently from an imaginary one tells a stranger
    # which task ids were minted, which is the whole of the disclosure. The two
    # answers have to be one answer, byte for byte.
    room_id, agent_id = await _room_with_agent(client)
    opened = (await _call(client, "message/send", _send_params(room_id, agent_id))).json()["result"]
    stranger = {"Authorization": "Bearer stranger-token"}

    def read(task_id: str) -> dict[str, object]:
        return {"jsonrpc": "2.0", "id": 4, "method": "tasks/get", "params": {"id": task_id}}

    real = await client.post("/a2a/v1", json=read(opened["id"]), headers=stranger)
    imaginary = await client.post("/a2a/v1", json=read("a2atask_never_minted"), headers=stranger)

    assert real.status_code == imaginary.status_code
    assert real.json() == imaginary.json()
    assert real.json()["error"]["code"] == -32001
    assert opened["id"] not in real.text


@pytest.mark.asyncio
async def test_the_call_id_is_echoed_on_success_and_on_failure(client):
    room_id, agent_id = await _room_with_agent(client)

    ok = (
        await _call(client, "message/send", _send_params(room_id, agent_id), call_id="abc")
    ).json()
    assert ok["id"] == "abc" and ok["jsonrpc"] == "2.0"

    refused = (await _call(client, "tasks/get", {"id": "nope"}, call_id=77)).json()
    assert refused["id"] == 77 and refused["jsonrpc"] == "2.0"
