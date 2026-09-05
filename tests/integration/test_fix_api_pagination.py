"""Finding 10: eight list routes had no limit or cursor at all, so a room's
whole history of outputs, versions, branches, output selections, tasks,
decisions, memories, or a user's whole notification backlog came back in one
response. Each now takes the same `limit` (default 100, ceiling 500) the rest
of the API's list routes already use, and the repository call underneath it
is sliced in Python as an interim (see "Needs lead wiring: repositories" in
the report) since the repository methods themselves are the runtime track's
file.

Every case here monkeypatches the service call the route makes to return a
large synthetic list, rather than seeding hundreds of real rows through
several transactions per row: what is under test is `routes.py`'s own
`[:limit]`, not the repository or the domain objects it returns.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from multiplayer.domain.models import (
    AgentOutput,
    ArtifactVersion,
    Branch,
    BranchMode,
    BranchStatus,
    Decision,
    Memory,
    MemoryScope,
    Notification,
    OutputDisposition,
    OutputSelection,
    Task,
)
from multiplayer.server import create_app
from multiplayer.services.service import MultiplayerService

TOKENS = {"owner-token": "user_1"}
AUTH = {"Authorization": "Bearer owner-token"}


@pytest.fixture
async def seeded_client(monkeypatch: pytest.MonkeyPatch):
    app = create_app(":memory:", auth_tokens=TOKENS)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
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
                    json={"name": "Room"},
                    headers=AUTH,
                )
            ).json()
            yield client, room["room_id"], monkeypatch


async def _assert_paginated(
    client: AsyncClient, path: str, *, method_name: str, count: int
) -> None:
    default_page = await client.get(path, headers=AUTH)
    assert default_page.status_code == 200, default_page.text
    assert len(default_page.json()) == 100, f"{method_name}: default limit not applied"

    small_page = await client.get(path, headers=AUTH, params={"limit": 5})
    assert small_page.status_code == 200
    assert len(small_page.json()) == 5, f"{method_name}: explicit limit not applied"

    over_ceiling = await client.get(path, headers=AUTH, params={"limit": 5000})
    assert over_ceiling.status_code == 422, f"{method_name}: no ceiling enforced"


async def test_room_outputs_are_paginated(seeded_client) -> None:
    client, room_id, monkeypatch = seeded_client
    outputs = [
        AgentOutput(
            output_id=f"out-{i}",
            room_id=room_id,
            session_id="sess-1",
            execution_id=f"exec-{i}",
            agent_id="agent-1",
            content="hi",
        )
        for i in range(150)
    ]

    async def fake_list_room_outputs(self: MultiplayerService, rid: str) -> list[Any]:
        return outputs

    monkeypatch.setattr(MultiplayerService, "list_room_outputs", fake_list_room_outputs)
    await _assert_paginated(
        client, f"/api/v1/rooms/{room_id}/outputs", method_name="list_room_outputs", count=150
    )


async def test_room_output_selections_are_paginated(seeded_client) -> None:
    client, room_id, monkeypatch = seeded_client
    selections = [
        OutputSelection(
            room_id=room_id,
            output_id=f"out-{i}",
            disposition=OutputDisposition.INCLUDED,
            decided_by="user_1",
        )
        for i in range(150)
    ]

    async def fake_list_output_selections(self: MultiplayerService, rid: str) -> list[Any]:
        return selections

    monkeypatch.setattr(MultiplayerService, "list_output_selections", fake_list_output_selections)
    await _assert_paginated(
        client,
        f"/api/v1/rooms/{room_id}/output-selections",
        method_name="list_output_selections",
        count=150,
    )


async def test_room_tasks_are_paginated(seeded_client) -> None:
    client, room_id, monkeypatch = seeded_client
    tasks = [Task(task_id=f"task-{i}", room_id=room_id, title=f"t{i}") for i in range(150)]

    async def fake_list_room_tasks(self: MultiplayerService, rid: str) -> list[Any]:
        return tasks

    monkeypatch.setattr(MultiplayerService, "list_room_tasks", fake_list_room_tasks)
    await _assert_paginated(
        client, f"/api/v1/rooms/{room_id}/tasks", method_name="list_room_tasks", count=150
    )


async def test_room_decisions_are_paginated(seeded_client) -> None:
    client, room_id, monkeypatch = seeded_client
    decisions = [
        Decision(decision_id=f"dec-{i}", room_id=room_id, title=f"d{i}", content="x")
        for i in range(150)
    ]

    async def fake_list_room_decisions(self: MultiplayerService, rid: str) -> list[Any]:
        return decisions

    monkeypatch.setattr(MultiplayerService, "list_room_decisions", fake_list_room_decisions)
    await _assert_paginated(
        client, f"/api/v1/rooms/{room_id}/decisions", method_name="list_room_decisions", count=150
    )


async def test_room_memories_are_paginated(seeded_client) -> None:
    client, room_id, monkeypatch = seeded_client
    memories = [
        Memory(
            memory_id=f"mem-{i}",
            room_id=room_id,
            workspace_id=None,
            org_id=None,
            scope=MemoryScope.ROOM,
            content="x",
        )
        for i in range(150)
    ]

    async def fake_list_room_memories(self: MultiplayerService, rid: str) -> list[Any]:
        return memories

    monkeypatch.setattr(MultiplayerService, "list_room_memories", fake_list_room_memories)
    await _assert_paginated(
        client, f"/api/v1/rooms/{room_id}/memories", method_name="list_room_memories", count=150
    )


async def test_notifications_are_paginated(seeded_client) -> None:
    client, _room_id, monkeypatch = seeded_client
    notifs = [
        Notification(
            notification_id=f"notif-{i}", user_id="user_1", room_id=None, title="t", body="b"
        )
        for i in range(150)
    ]

    async def fake_list_notifications(self: MultiplayerService, uid: str) -> list[Any]:
        return notifs

    monkeypatch.setattr(MultiplayerService, "list_notifications", fake_list_notifications)
    await _assert_paginated(
        client, "/api/v1/notifications", method_name="list_notifications", count=150
    )


async def test_room_branches_are_paginated(seeded_client) -> None:
    client, room_id, monkeypatch = seeded_client
    branches = [
        Branch(
            branch_id=f"branch-{i}",
            room_id=room_id,
            mode=BranchMode.TURN_LOCKED_SINGLE,
            status=BranchStatus.COMPLETED,
            initiated_by="user_1",
            initiating_prompt="p",
            context_event_sequence=0,
            context_message_ids=(),
            context_snapshot={},
            context_hash="h",
        )
        for i in range(150)
    ]

    async def fake_list_room_branches(self: MultiplayerService, rid: str) -> list[Branch]:
        return branches

    async def fake_empty_list(self: MultiplayerService, ident: str) -> list[Any]:
        return []

    monkeypatch.setattr(MultiplayerService, "list_room_branches", fake_list_room_branches)
    monkeypatch.setattr(MultiplayerService, "list_room_outputs", fake_empty_list)
    monkeypatch.setattr(MultiplayerService, "list_output_selections", fake_empty_list)
    monkeypatch.setattr(MultiplayerService, "list_branch_runs", fake_empty_list)

    import multiplayer.api.routes as routes

    client_svc = routes._svc
    assert client_svc is not None

    async def fake_list_by_branch(branch_id: str) -> list[Any]:
        return []

    monkeypatch.setattr(client_svc.repos.branch_syntheses, "list_by_branch", fake_list_by_branch)

    await _assert_paginated(
        client, f"/api/v1/rooms/{room_id}/branches", method_name="list_room_branches", count=150
    )


async def test_artifact_versions_are_paginated_and_drop_content(seeded_client) -> None:
    client, room_id, monkeypatch = seeded_client
    artifact = (
        await client.post(
            f"/api/v1/rooms/{room_id}/artifacts",
            json={"name": "Doc", "artifact_type": "DOCUMENT", "content": "v1"},
            headers=AUTH,
        )
    ).json()
    artifact_id = artifact["artifact_id"]
    versions = [
        ArtifactVersion(
            version_id=f"ver-{i}",
            artifact_id=artifact_id,
            version_number=i,
            content="should not appear in the list",
        )
        for i in range(150)
    ]

    import multiplayer.api.routes as routes

    svc = routes._svc
    assert svc is not None

    # Targeted patches on the real repo object, not a wholesale replacement:
    # `_authorized_artifact` still needs the real `.get()` to find the room
    # this artifact belongs to.
    async def fake_list_versions(aid: str) -> list[ArtifactVersion]:
        return versions

    async def fake_get_version(version_id: str) -> ArtifactVersion:
        return versions[0]

    async def fake_get_version_provenance(version_id: str) -> list[Any]:
        return []

    monkeypatch.setattr(svc.repos.artifacts, "list_versions", fake_list_versions)
    monkeypatch.setattr(svc.repos.artifacts, "get_version", fake_get_version)
    monkeypatch.setattr(svc.repos.artifacts, "get_version_provenance", fake_get_version_provenance)

    listing = await client.get(f"/api/v1/artifacts/{artifact_id}/versions", headers=AUTH)
    assert listing.status_code == 200
    body = listing.json()
    assert len(body) == 100
    assert all("content" not in row for row in body), "finding 10: content must not be in the list"

    small = await client.get(
        f"/api/v1/artifacts/{artifact_id}/versions", headers=AUTH, params={"limit": 5}
    )
    assert len(small.json()) == 5

    provenance = await client.get(
        f"/api/v1/artifact-versions/{versions[0].version_id}/provenance", headers=AUTH
    )
    assert provenance.status_code == 200
    assert provenance.json()["content"] == "should not appear in the list"
