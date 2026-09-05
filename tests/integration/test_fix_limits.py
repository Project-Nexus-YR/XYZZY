"""The eight list routes fixed here now push `limit` into the SQL itself
(a real `LIMIT ?` after the existing `ORDER BY`) instead of reading every row
and slicing the Python list. Each test seeds `limit + 1` real rows through the
repository or the API, requests the route with a `limit` query param, and
checks that exactly `limit` rows come back and that they are the same first
rows, in the same order, as an unlimited call over the same data.

Reuses the `seeded_client` fixture and auth constants from
`test_fix_api_pagination.py`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from multiplayer.domain.models import (
    Branch,
    BranchMode,
    BranchStatus,
    Decision,
    Memory,
    MemoryScope,
    Notification,
    Task,
    utcnow,
)
from multiplayer.server import create_app

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


def _svc():
    import multiplayer.api.routes as routes

    svc = routes._svc
    assert svc is not None
    return svc


def _spy_row_counts(monkeypatch: pytest.MonkeyPatch, obj: Any, method_name: str) -> list[int]:
    """Wrap `obj.method_name` to record how many rows each call returned.

    A response-body comparison alone cannot tell a real SQL `LIMIT` from a
    route that still reads every row and slices the Python list afterwards:
    both produce an identical page. Recording what the repository itself
    handed back closes that gap - a slice-after-the-fact shows up here as the
    full row count, not `limit`.
    """
    original = getattr(obj, method_name)
    calls: list[int] = []

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        calls.append(len(result))
        return result

    monkeypatch.setattr(obj, method_name, wrapper)
    return calls


async def _assert_limit_and_order(
    client: AsyncClient, path: str, id_key: str, *, limit: int, repo_calls: list[int]
) -> None:
    unlimited = await client.get(path, headers=AUTH)
    assert unlimited.status_code == 200, unlimited.text
    limited = await client.get(path, headers=AUTH, params={"limit": limit})
    assert limited.status_code == 200, limited.text
    limited_body = limited.json()
    unlimited_body = unlimited.json()
    assert len(limited_body) == limit
    assert [row[id_key] for row in limited_body] == [row[id_key] for row in unlimited_body[:limit]]
    assert repo_calls[-1] == limit, (
        f"repository returned {repo_calls[-1]} rows for limit={limit}: the "
        "LIMIT must reach the SQL, not a Python slice taken after the read"
    )


async def _seed_agent(svc: Any, room_id: str, tag: str) -> str:
    template_id = f"tmpl-{tag}"
    agent_id = f"agent-{tag}"
    now = utcnow().isoformat()
    await svc.db.execute(
        "INSERT INTO agent_templates(template_id, name, role, created_at) VALUES (?, ?, ?, ?)",
        (template_id, "Agent", "Worker", now),
    )
    await svc.db.execute(
        "INSERT INTO agent_instances(agent_id, template_id, room_id, name, role, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (agent_id, template_id, room_id, "Agent", "Worker", now),
    )
    await svc.db.commit()
    return agent_id


async def _seed_branch(svc: Any, room_id: str, tag: str, created_at) -> str:
    branch = Branch(
        branch_id=f"branch-{tag}",
        room_id=room_id,
        mode=BranchMode.TURN_LOCKED_SINGLE,
        status=BranchStatus.COMPLETED,
        initiated_by="user_1",
        initiating_prompt="p",
        context_event_sequence=0,
        context_message_ids=(),
        context_snapshot={},
        context_hash="h",
        created_at=created_at,
        updated_at=created_at,
    )
    await svc.repos.branches.create(branch)
    return branch.branch_id


async def _seed_output(
    svc: Any, room_id: str, branch_id: str, agent_id: str, tag: str, created_at
) -> str:
    session_id = f"sess-{tag}"
    execution_id = f"exec-{tag}"
    output_id = f"out-{tag}"
    ts = created_at.isoformat()
    await svc.db.execute(
        "INSERT INTO sessions(session_id, room_id, agent_id, status, started_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, room_id, agent_id, "COMPLETED", ts),
    )
    await svc.db.execute(
        "INSERT INTO executions(execution_id, session_id, agent_id, branch_id, status, "
        "started_at, completed_at, authorized_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (execution_id, session_id, agent_id, branch_id, "COMPLETED", ts, ts, "user_1"),
    )
    await svc.db.execute(
        "INSERT INTO agent_outputs(output_id, room_id, session_id, execution_id, agent_id, "
        "content, source_prompt, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (output_id, room_id, session_id, execution_id, agent_id, f"content-{tag}", "prompt", ts),
    )
    await svc.db.commit()
    return output_id


async def _seed_output_selection(
    svc: Any, room_id: str, branch_id: str, output_id: str, updated_at
) -> None:
    await svc.db.execute(
        "INSERT INTO output_selections(room_id, output_id, disposition, decided_by, "
        "updated_at, branch_id) VALUES (?, ?, ?, ?, ?, ?)",
        (room_id, output_id, "INCLUDED", "user_1", updated_at.isoformat(), branch_id),
    )
    await svc.db.commit()


async def test_room_branches_are_paginated(seeded_client) -> None:
    client, room_id, monkeypatch = seeded_client
    svc = _svc()
    base = utcnow()
    for i in range(6):
        await _seed_branch(svc, room_id, str(i), base + timedelta(seconds=i))
    repo_calls = _spy_row_counts(monkeypatch, svc.repos.branches, "list_by_room")
    await _assert_limit_and_order(
        client, f"/api/v1/rooms/{room_id}/branches", "branch_id", limit=5, repo_calls=repo_calls
    )


async def test_room_outputs_are_paginated(seeded_client) -> None:
    client, room_id, monkeypatch = seeded_client
    svc = _svc()
    agent_id = await _seed_agent(svc, room_id, "a")
    branch_id = await _seed_branch(svc, room_id, "b", utcnow())
    base = utcnow()
    for i in range(6):
        await _seed_output(svc, room_id, branch_id, agent_id, str(i), base + timedelta(seconds=i))
    repo_calls = _spy_row_counts(monkeypatch, svc.repos.agent_outputs, "list_by_room")
    await _assert_limit_and_order(
        client, f"/api/v1/rooms/{room_id}/outputs", "output_id", limit=5, repo_calls=repo_calls
    )


async def test_room_output_selections_are_paginated(seeded_client) -> None:
    client, room_id, monkeypatch = seeded_client
    svc = _svc()
    agent_id = await _seed_agent(svc, room_id, "a")
    branch_id = await _seed_branch(svc, room_id, "b", utcnow())
    base = utcnow()
    for i in range(6):
        output_id = await _seed_output(
            svc, room_id, branch_id, agent_id, str(i), base + timedelta(seconds=i)
        )
        await _seed_output_selection(
            svc, room_id, branch_id, output_id, base + timedelta(seconds=i)
        )
    repo_calls = _spy_row_counts(monkeypatch, svc.repos.output_selections, "list_by_room")
    await _assert_limit_and_order(
        client,
        f"/api/v1/rooms/{room_id}/output-selections",
        "output_id",
        limit=5,
        repo_calls=repo_calls,
    )


async def test_room_tasks_are_paginated(seeded_client) -> None:
    client, room_id, monkeypatch = seeded_client
    svc = _svc()
    base = utcnow()
    for i in range(6):
        task = Task(
            task_id=f"task-{i}",
            room_id=room_id,
            title=f"t{i}",
            created_at=base + timedelta(seconds=i),
            updated_at=base + timedelta(seconds=i),
        )
        await svc.repos.tasks.create(task)
    repo_calls = _spy_row_counts(monkeypatch, svc.repos.tasks, "list_by_room")
    await _assert_limit_and_order(
        client, f"/api/v1/rooms/{room_id}/tasks", "task_id", limit=5, repo_calls=repo_calls
    )


async def test_room_decisions_are_paginated(seeded_client) -> None:
    client, room_id, monkeypatch = seeded_client
    svc = _svc()
    base = utcnow()
    for i in range(6):
        decision = Decision(
            decision_id=f"dec-{i}",
            room_id=room_id,
            title=f"d{i}",
            content="x",
            created_at=base + timedelta(seconds=i),
        )
        await svc.repos.decisions.create(decision)
    repo_calls = _spy_row_counts(monkeypatch, svc.repos.decisions, "list_by_room")
    await _assert_limit_and_order(
        client, f"/api/v1/rooms/{room_id}/decisions", "decision_id", limit=5, repo_calls=repo_calls
    )


async def test_room_memories_are_paginated(seeded_client) -> None:
    client, room_id, monkeypatch = seeded_client
    svc = _svc()
    base = utcnow()
    for i in range(6):
        memory = Memory(
            memory_id=f"mem-{i}",
            room_id=room_id,
            workspace_id=None,
            org_id=None,
            scope=MemoryScope.ROOM,
            content="x",
            created_at=base + timedelta(seconds=i),
        )
        await svc.repos.memories.create(memory)
    repo_calls = _spy_row_counts(monkeypatch, svc.repos.memories, "list_by_room")
    await _assert_limit_and_order(
        client, f"/api/v1/rooms/{room_id}/memories", "memory_id", limit=5, repo_calls=repo_calls
    )


async def test_notifications_are_paginated(seeded_client) -> None:
    client, _room_id, monkeypatch = seeded_client
    svc = _svc()
    base = utcnow()
    for i in range(6):
        notif = Notification(
            notification_id=f"notif-{i}",
            user_id="user_1",
            room_id=None,
            title="t",
            body="b",
            created_at=base + timedelta(seconds=i),
        )
        await svc.repos.notifications.create(notif)
    repo_calls = _spy_row_counts(monkeypatch, svc.repos.notifications, "list_unread")
    await _assert_limit_and_order(
        client, "/api/v1/notifications", "notification_id", limit=5, repo_calls=repo_calls
    )


async def test_artifact_versions_are_paginated(seeded_client) -> None:
    client, room_id, monkeypatch = seeded_client
    svc = _svc()
    artifact = (
        await client.post(
            f"/api/v1/rooms/{room_id}/artifacts",
            json={"name": "Doc", "artifact_type": "DOCUMENT", "content": "v0"},
            headers=AUTH,
        )
    ).json()
    artifact_id = artifact["artifact_id"]
    for i in range(1, 6):
        resp = await client.post(
            f"/api/v1/artifacts/{artifact_id}/versions",
            json={"content": f"v{i}"},
            headers=AUTH,
        )
        assert resp.status_code == 200, resp.text
    repo_calls = _spy_row_counts(monkeypatch, svc.repos.artifacts, "list_versions")
    await _assert_limit_and_order(
        client,
        f"/api/v1/artifacts/{artifact_id}/versions",
        "version_id",
        limit=5,
        repo_calls=repo_calls,
    )
