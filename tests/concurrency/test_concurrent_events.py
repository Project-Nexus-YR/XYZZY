"""Concurrency tests: verify atomic sequence generation under concurrent writes."""

import asyncio
import pytest
from multiplayer.db.connection import Database
from multiplayer.db.repositories import Repos
from multiplayer.domain.events import EventType, RoomEvent


@pytest.fixture
async def repos():
    db = Database(":memory:")
    await db.connect()
    await db.execute_script(open(
        str(__import__("pathlib").Path(__file__).parents[2] / "src" / "multiplayer" / "migrations" / "001_initial.sql")
    ).read())
    yield Repos(db)
    await db.close()


@pytest.fixture
async def seeded_room(repos):
    """Create a room + org + workspace so FK constraints are satisfied."""
    from multiplayer.domain.models import Organization, Workspace, Room, new_id
    org = Organization(org_id=new_id("org"), name="TestOrg", slug="testorg")
    await repos.orgs.create(org)
    ws = Workspace(workspace_id=new_id("ws"), org_id=org.org_id, name="TestWS", slug="testws")
    await repos.workspaces.create(ws)
    room = Room(room_id="conc_room", workspace_id=ws.workspace_id, name="ConcRoom", description="test", created_by="u1")
    await repos.rooms.create(room)
    return room.room_id


@pytest.mark.asyncio
async def test_concurrent_sequence_generation(repos, seeded_room):
    """50 concurrent appenders on the same room must produce unique, gap-free sequences."""
    room_id = seeded_room
    results: list[int] = []

    async def write_event(i: int):
        event = RoomEvent(
            room_id=room_id, sequence=0, event_type=EventType.MESSAGE_CREATED,
            payload={"index": i}, actor_id="u1", actor_type="user",
        )
        event = await repos.events.append_with_next_sequence(event)
        results.append(event.sequence)

    await asyncio.gather(*(write_event(i) for i in range(50)))

    assert len(results) == 50
    assert len(set(results)) == 50, "All sequences must be unique"
    assert min(results) == 1
    assert max(results) == 50

    events = await repos.events.list_since(room_id, 0)
    assert len(events) == 50


@pytest.mark.asyncio
async def test_concurrent_sequence_multi_room(repos):
    """Concurrent writes to different rooms must not interfere."""
    from multiplayer.domain.models import Organization, Workspace, Room, new_id
    org = Organization(org_id=new_id("org"), name="TestOrg", slug="testorg")
    await repos.orgs.create(org)
    ws = Workspace(workspace_id=new_id("ws"), org_id=org.org_id, name="TestWS", slug="testws")
    await repos.workspaces.create(ws)

    rooms = ["room_a", "room_b", "room_c"]
    for rid in rooms:
        room = Room(room_id=rid, workspace_id=ws.workspace_id, name=rid, description="", created_by="u1")
        await repos.rooms.create(room)

    results: dict[str, list[int]] = {r: [] for r in rooms}

    async def write_to_room(room_id: str, i: int):
        event = RoomEvent(
            room_id=room_id, sequence=0, event_type=EventType.MESSAGE_CREATED,
            payload={"index": i}, actor_id="u1", actor_type="user",
        )
        event = await repos.events.append_with_next_sequence(event)
        results[room_id].append(event.sequence)

    tasks = []
    for room_id in rooms:
        for i in range(20):
            tasks.append(write_to_room(room_id, i))
    await asyncio.gather(*tasks)

    for room_id in rooms:
        seqs = results[room_id]
        assert len(seqs) == 20
        assert len(set(seqs)) == 20, f"Duplicate sequences in {room_id}"
        assert min(seqs) == 1


@pytest.mark.asyncio
async def test_concurrent_sequence_with_existing_events(repos, seeded_room):
    """Sequencing must work correctly when events already exist."""
    room_id = seeded_room
    for i in range(5):
        event = RoomEvent(
            room_id=room_id, sequence=0, event_type=EventType.MESSAGE_CREATED,
            payload={"seed": i}, actor_id="u1", actor_type="user",
        )
        await repos.events.append_with_next_sequence(event)

    results: list[int] = []

    async def write_event(i: int):
        event = RoomEvent(
            room_id=room_id, sequence=0, event_type=EventType.MESSAGE_CREATED,
            payload={"concurrent": i}, actor_id="u1", actor_type="user",
        )
        event = await repos.events.append_with_next_sequence(event)
        results.append(event.sequence)

    await asyncio.gather(*(write_event(i) for i in range(30)))

    assert len(set(results)) == 30
    assert min(results) >= 6, "Concurrent sequences must start after existing"
    events = await repos.events.list_since(room_id, 0)
    assert len(events) == 35
