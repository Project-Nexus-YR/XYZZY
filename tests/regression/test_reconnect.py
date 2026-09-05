"""Reconnect correctness tests: verify state reconstruction."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import multiplayer.api.routes as routes_mod
from multiplayer.db.connection import Database
from multiplayer.domain.models import (
    AgentStatus,
    ArtifactType,
    MemoryScope,
    MessageRole,
    TaskStatus,
)
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.server import create_app
from multiplayer.services.service import MultiplayerService

TOKENS = {"owner-token": "owner"}
OWNER = {"Authorization": "Bearer owner-token"}


@pytest.fixture
async def service():
    db = Database(":memory:")
    await db.connect()
    hub = RealtimeHub()
    svc = MultiplayerService(db, hub)
    await svc.initialize()
    yield svc
    await db.close()


@pytest.mark.asyncio
async def test_full_room_state_after_activities(service):
    """Room state snapshot captures all activity types."""
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room = await service.create_room(ws.workspace_id, "R", "u1")
    templates = await service.list_agent_templates()

    # Build up state
    await service.send_message(room.room_id, MessageRole.HUMAN, "u1", "Hello")
    await service.spawn_agent(room.room_id, templates[0].template_id, "Coder")
    await service.create_task(room.room_id, "Build API", "REST endpoints")
    await service.create_artifact(
        room.room_id, "api.md", ArtifactType.DOCUMENT, "API doc", "u1", "# API"
    )
    await service.create_decision(room.room_id, "Use FastAPI", "It's async")
    await service.create_memory(
        room.room_id, None, None, MemoryScope.ROOM, "We use Python 3.13", "fact", "u1"
    )

    state = await service.get_room_state(room.room_id)

    assert len(state["members"]) >= 1
    assert len(state["agents"]) == 1
    assert state["agents"][0]["name"] == "Coder"
    assert state["agents"][0]["status"] == AgentStatus.IDLE.value
    assert len(state["tasks"]) == 1
    assert state["tasks"][0]["title"] == "Build API"
    assert len(state["messages"]) == 1
    assert state["messages"][0]["content"] == "Hello"
    assert len(state["artifacts"]) == 1
    assert state["artifacts"][0]["name"] == "api.md"
    assert len(state["decisions"]) == 1
    assert state["decisions"][0]["title"] == "Use FastAPI"
    assert len(state["memories"]) == 1
    assert state["memories"][0]["content"] == "We use Python 3.13"


@pytest.mark.asyncio
async def test_reconnect_with_sequence_filter(service):
    """Reconnecting with a last_sequence returns only newer events."""
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room = await service.create_room(ws.workspace_id, "R", "u1")

    await service.send_message(room.room_id, MessageRole.HUMAN, "u1", "msg1")
    await service.send_message(room.room_id, MessageRole.HUMAN, "u1", "msg2")
    await service.send_message(room.room_id, MessageRole.HUMAN, "u1", "msg3")

    events = await service.get_room_events(room.room_id)
    assert len(events) == 4  # room_created + 3 messages

    # Get only events after sequence 2
    recent = await service.get_room_events(room.room_id, after_sequence=2)
    assert len(recent) == 2
    assert recent[0].sequence == 3
    assert recent[1].sequence == 4


@pytest.mark.asyncio
async def test_reconnect_full_state_has_no_events_when_caught_up(service):
    """When last_sequence matches latest, events_since is empty."""
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room = await service.create_room(ws.workspace_id, "R", "u1")

    await service.send_message(room.room_id, MessageRole.HUMAN, "u1", "Hello")

    events = await service.get_room_events(room.room_id)
    last_seq = events[-1].sequence

    state = await service.get_room_state(room.room_id, last_seq)
    assert len(state["events_since"]) == 0


@pytest.mark.asyncio
async def test_reconnect_partial_state_has_only_new_events(service):
    """Reconnecting with a mid-range sequence returns only newer events."""
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room = await service.create_room(ws.workspace_id, "R", "u1")

    await service.send_message(room.room_id, MessageRole.HUMAN, "u1", "msg1")
    events_at_1 = await service.get_room_events(room.room_id)
    seq1 = events_at_1[-1].sequence  # Should be 2 (room_created=1, msg1=2)

    await service.send_message(room.room_id, MessageRole.HUMAN, "u1", "msg2")
    await service.send_message(room.room_id, MessageRole.HUMAN, "u1", "msg3")

    state = await service.get_room_state(room.room_id, seq1)
    assert len(state["events_since"]) == 2  # msg2 and msg3


@pytest.mark.asyncio
async def test_reconnect_preserves_agent_status(service):
    """Agent status should be reflected in reconnect state."""
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room = await service.create_room(ws.workspace_id, "R", "u1")
    templates = await service.list_agent_templates()
    agent = await service.spawn_agent(room.room_id, templates[0].template_id, "Worker")

    # Change agent status
    await service.update_agent_status(agent.agent_id, AgentStatus.WORKING)
    await service.update_agent_status(agent.agent_id, AgentStatus.THINKING)

    state = await service.get_room_state(room.room_id)
    assert state["agents"][0]["status"] == AgentStatus.THINKING.value


@pytest.mark.asyncio
async def test_reconnect_preserves_task_status(service):
    """Task status should be reflected in reconnect state."""
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room = await service.create_room(ws.workspace_id, "R", "u1")
    templates = await service.list_agent_templates()
    agent = await service.spawn_agent(room.room_id, templates[0].template_id)

    task = await service.create_task(room.room_id, "Task 1")
    task = await service.assign_task(task.task_id, agent.agent_id)

    state = await service.get_room_state(room.room_id)
    assert state["tasks"][0]["status"] == TaskStatus.ASSIGNED.value
    assert state["tasks"][0]["assigned_agent_id"] == agent.agent_id


@pytest.mark.asyncio
async def test_reconnect_pages_past_the_500_row_default_without_truncating(service):
    """get_room_events (and the events_since half of get_room_state) used to
    call EventRepo.list_since once and hand back its first 500-row page — the
    same defect class already fixed once for the audit export (see
    test_audit_export.py). A reconnecting client asking for everything past
    ``after_sequence`` must get everything, not one page of it.
    """
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room = await service.create_room(ws.workspace_id, "R", "u1")

    total = 512
    for index in range(total):
        await service.send_message(room.room_id, MessageRole.HUMAN, "u1", f"msg{index}")

    counter = await service.repos.events.get_sequence_counter(room.room_id)
    assert counter == total + 1  # room_created plus every message

    events = await service.get_room_events(room.room_id)
    assert len(events) == counter
    assert [e.sequence for e in events] == list(range(1, counter + 1))

    # The reconnect path (get_room_state's events_since) must not truncate
    # past ``after_sequence`` either, for a real cursor: last_sequence=0 is a
    # fresh connect with no cursor yet, and now windows events_since around
    # the recent room instead (see get_room_state's docstring and
    # test_room_state_events_window.py), so this exercises the same
    # no-silent-page-truncation guarantee through a real cursor instead.
    state = await service.get_room_state(room.room_id, last_sequence=1)
    assert len(state["events_since"]) == counter - 1

    # A mid-range after_sequence still returns every event past it, not one page.
    partial = await service.get_room_events(room.room_id, after_sequence=1)
    assert len(partial) == counter - 1
    assert partial[0].sequence == 2
    assert partial[-1].sequence == counter


@pytest.mark.asyncio
async def test_reconnect_sequence_zero_gets_all(service):
    """last_sequence=0 returns all events."""
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room = await service.create_room(ws.workspace_id, "R", "u1")
    await service.send_message(room.room_id, MessageRole.HUMAN, "u1", "Hello")

    state = await service.get_room_state(room.room_id, 0)
    assert len(state["events_since"]) > 0


def _drain_sequences(queue) -> set[int]:
    sequences: set[int] = set()
    while not queue.empty():
        message = queue.get_nowait()
        if "sequence" in message:
            sequences.add(message["sequence"])
    return sequences


@pytest.mark.asyncio
async def test_late_write_after_subscribe_has_no_gap_via_live_delivery(service) -> None:
    """Finding 39, the safe half of the seam: a room event that commits while
    a `GET /state` snapshot is already mid-read, but after the socket has
    already subscribed. The subscribe happens first and is fully complete
    before the snapshot's underlying repository read even starts, so this is
    not asserting on incidental timing: the interleaving is forced with an
    `asyncio.Event` so the write provably lands between the repository read
    returning its (necessarily stale) rows and `get_room_state` handing the
    snapshot back to its caller. The invariant this pins: a socket that
    subscribed before an event, even one whose broadcast races the snapshot
    fetch, always gets it live, so the union of snapshot plus socket has no
    gap regardless of what the snapshot itself captured.
    """
    org = await service.create_organization("O", "o", "u1")
    ws = await service.create_workspace(org.org_id, "W", "w", "u1")
    room = await service.create_room(ws.workspace_id, "R", "u1")

    sub = await service.hub.subscribe(room.room_id, "u1")  # subscribed before any race begins

    read_returned = asyncio.Event()
    write_committed = asyncio.Event()
    original_list_since = service.repos.events.list_since

    async def hooked_list_since(room_id: str, after_sequence: int, limit: int = 500):
        result = await original_list_since(room_id, after_sequence, limit)
        if room_id == room.room_id:
            # The read already has its rows in hand, stale by construction:
            # everything below happens only after this line.
            read_returned.set()
            await write_committed.wait()
        return result

    service.repos.events.list_since = hooked_list_since  # type: ignore[method-assign]
    try:

        async def do_snapshot() -> dict:
            return await service.get_room_state(room.room_id, 0)

        async def do_late_write() -> None:
            await read_returned.wait()
            await service.send_message(room.room_id, MessageRole.HUMAN, "u1", "concurrent")
            write_committed.set()

        state, _ = await asyncio.gather(do_snapshot(), do_late_write())
    finally:
        service.repos.events.list_since = original_list_since

    since_sequences = {e["sequence"] for e in state["events_since"]}
    socket_sequences = _drain_sequences(sub.queue)
    counter = await service.repos.events.get_sequence_counter(room.room_id)

    assert since_sequences | socket_sequences == set(range(1, counter + 1)), (
        "gap between the state snapshot and the socket, despite subscribing first"
    )


def test_write_before_late_subscribe_is_replayed() -> None:
    """Finding 39, the seam's open half, now closed server-side: a client
    that reads its snapshot and only later opens the socket no longer
    depends on subscribing before any third party's write. `last_sequence`
    on `/ws` (the query param `websocket_endpoint`'s docstring long promised
    as "informational" but never read) replays every event with a greater
    sequence, in order, before live delivery begins, so the write below,
    committed before this socket even exists, is not lost: the backfill
    reads the event log fresh at subscribe time and catches it regardless
    of when the socket happens to connect relative to it.
    """
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/me/bootstrap",
            headers=OWNER,
            json={"display_name": "Owner", "room_name": "Primary"},
        )
        assert response.status_code == 200, response.text
        room_id = response.json()["room"]["room_id"]
        svc = routes_mod._svc
        assert svc is not None

        # The snapshot a client would already have fetched, establishing
        # its cursor.
        state = client.get(f"/api/v1/rooms/{room_id}/state", headers=OWNER).json()
        cursor = state["events_since"][-1]["sequence"] if state["events_since"] else 0

        # Committed after the snapshot, before the socket ever opens: the
        # exact seam finding 39 named, since nothing has subscribed yet.
        asyncio.run(svc.send_message(room_id, MessageRole.HUMAN, "owner", "concurrent"))

        with client.websocket_connect(
            f"/ws?room_id={room_id}&last_sequence={cursor}", headers=OWNER
        ) as websocket:
            assert websocket.receive_json()["type"] == "connected"

            replayed = websocket.receive_json()
            assert replayed["type"] == "room_event"
            assert replayed["sequence"] == cursor + 1
            assert replayed["payload"]["content"] == "concurrent"

            # Live delivery picks up right where the replay left off: no
            # gap, and no repeat of what was just replayed.
            asyncio.run(svc.send_message(room_id, MessageRole.HUMAN, "owner", "live"))
            live = websocket.receive_json()
            assert live["sequence"] == cursor + 2
            assert live["payload"]["content"] == "live"


def test_resync_marker_closes_a_live_socket_with_the_gap_signal() -> None:
    """Finding 40's consumer side: hub.py's overflow handling (tested at the
    unit level in test_realtime_track_hub_overflow.py, without a live
    websocket, to avoid racing that socket's own send loop draining the
    queue concurrently) enqueues `{"type": "resync"}` in place of the oldest
    entry. This proves the other half deterministically: a real, live socket
    that finds a `resync` marker on its queue is closed with the gap signal
    (code 4408), not left to forward it as an ordinary message or silently
    swallow it.
    """
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/me/bootstrap",
            headers=OWNER,
            json={"display_name": "Owner", "room_name": "Primary"},
        )
        assert response.status_code == 200, response.text
        room_id = response.json()["room"]["room_id"]
        svc = routes_mod._svc
        assert svc is not None

        with client.websocket_connect(f"/ws?room_id={room_id}", headers=OWNER) as websocket:
            assert websocket.receive_json()["type"] == "connected"

            sub_ids = asyncio.run(svc.hub.get_subscriptions_for_user_room("owner", room_id))
            assert len(sub_ids) == 1
            sub = svc.hub._subscriptions[sub_ids[0]]
            asyncio.run(_put_resync(sub))

            with pytest.raises(WebSocketDisconnect) as exc_info:
                websocket.receive_json()
            assert exc_info.value.code == 4408


async def _put_resync(sub) -> None:
    sub.queue.put_nowait({"type": "resync"})


def test_socket_backfill_dedupes_an_event_committed_during_its_own_read() -> None:
    """Finding 73: the `replayed_event_ids` dedupe branch in
    `websocket_endpoint` exists for exactly one interleaving: a write that
    commits after `hub.subscribe` but while the backfill's own
    `get_room_events` read is still in flight, so the event could otherwise
    arrive twice (once from the backfill, once from live delivery). The hook
    below commits that write from inside `list_since` itself, on the same
    event loop the socket's backfill is running on, which is exactly the
    "committed after the read started, before it returned" interleaving
    without needing cross-thread signalling to force it.
    """
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/me/bootstrap",
            headers=OWNER,
            json={"display_name": "Owner", "room_name": "Primary"},
        )
        assert response.status_code == 200, response.text
        room_id = response.json()["room"]["room_id"]
        svc = routes_mod._svc
        assert svc is not None

        original_list_since = svc.repos.events.list_since
        fired = False

        async def hooked_list_since(target_room_id: str, after_sequence: int, limit: int = 500):
            nonlocal fired
            result = await original_list_since(target_room_id, after_sequence, limit)
            if target_room_id == room_id and not fired:
                fired = True
                await svc.send_message(room_id, MessageRole.HUMAN, "owner", "mid-backfill")
            return result

        svc.repos.events.list_since = hooked_list_since  # type: ignore[method-assign]
        try:
            with client.websocket_connect(
                f"/ws?room_id={room_id}&last_sequence=0", headers=OWNER
            ) as websocket:
                assert websocket.receive_json()["type"] == "connected"

                sequences: list[int] = []
                while True:
                    message = websocket.receive_json()
                    sequences.append(message["sequence"])
                    if message["payload"].get("content") == "mid-backfill":
                        break
        finally:
            svc.repos.events.list_since = original_list_since

        assert fired, "the hook never fired, so this did not exercise the race at all"
        assert sequences.count(sequences[-1]) == 1, (
            "the event committed mid-backfill must not be delivered twice"
        )
        assert sequences == sorted(set(sequences)), "no gap and no repeat in the delivered order"
