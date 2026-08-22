"""Idempotency-Key replay contract for retry-prone public writes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

import multiplayer.api.routes as routes_module
from multiplayer.domain.events import EventType
from multiplayer.server import create_app

TOKENS = {"owner-token": "owner", "peer-token": "peer"}
OWNER = {"Authorization": "Bearer owner-token"}
PEER = {"Authorization": "Bearer peer-token"}


def _keyed(headers: dict[str, str], key: str) -> dict[str, str]:
    return {**headers, "Idempotency-Key": key}


def _enter(client: TestClient, headers: dict[str, str]) -> str:
    bootstrap = client.post(
        "/api/v1/me/bootstrap",
        headers=headers,
        json={"display_name": "Owner", "room_name": "Decision"},
    )
    assert bootstrap.status_code == 200, bootstrap.text
    return str(bootstrap.json()["room"]["room_id"])


def _events(client: TestClient, room_id: str, event_type: str) -> list[dict[str, Any]]:
    events = client.get(f"/api/v1/rooms/{room_id}/events", headers=OWNER).json()
    return [event for event in events if event["event_type"] == event_type]


def _messages(client: TestClient, room_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/v1/rooms/{room_id}/messages", headers=OWNER)
    assert response.status_code == 200
    return list(response.json())


def _start_single_branch(client: TestClient, room_id: str, key: str) -> Any:
    template_id = client.get("/api/v1/agent-templates", headers=OWNER).json()[0]["template_id"]
    agent = client.post(
        f"/api/v1/rooms/{room_id}/agents",
        headers=OWNER,
        json={"template_id": template_id},
    ).json()
    body = {
        "mode": "TURN_LOCKED_SINGLE",
        "prompt": "Choose the migration sequence.",
        "agent_ids": [agent["agent_id"]],
    }
    first = client.post(f"/api/v1/rooms/{room_id}/branches", headers=_keyed(OWNER, key), json=body)
    assert first.status_code == 200, first.text
    replay = client.post(f"/api/v1/rooms/{room_id}/branches", headers=_keyed(OWNER, key), json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    return first.json()


def test_replayed_message_returns_original_and_appends_nothing() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _enter(client, OWNER)
        path = f"/api/v1/rooms/{room_id}/messages"
        first = client.post(path, headers=_keyed(OWNER, "send-1"), json={"content": "Ship it"})
        replay = client.post(path, headers=_keyed(OWNER, "send-1"), json={"content": "Ship it"})
        assert first.status_code == 200, first.text
        assert replay.status_code == 200, replay.text
        assert replay.json() == first.json()
        assert len(_messages(client, room_id)) == 1
        assert len(_events(client, room_id, "message.created")) == 1

        fresh = client.post(path, headers=_keyed(OWNER, "send-2"), json={"content": "Ship it"})
        assert fresh.status_code == 200
        assert fresh.json()["message_id"] != first.json()["message_id"]
        assert len(_events(client, room_id, "message.created")) == 2


def test_unkeyed_writes_keep_appending() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _enter(client, OWNER)
        path = f"/api/v1/rooms/{room_id}/messages"
        for _ in range(2):
            assert (
                client.post(path, headers=OWNER, json={"content": "Same text"}).status_code == 200
            )
        assert len(_messages(client, room_id)) == 2


def test_key_reuse_with_a_different_request_is_rejected() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _enter(client, OWNER)
        path = f"/api/v1/rooms/{room_id}/messages"
        assert (
            client.post(path, headers=_keyed(OWNER, "k"), json={"content": "A"}).status_code == 200
        )
        conflict = client.post(path, headers=_keyed(OWNER, "k"), json={"content": "B"})
        assert conflict.status_code == 409, conflict.text
        assert "different request" in conflict.text
        assert len(_messages(client, room_id)) == 1
        assert len(_events(client, room_id, "message.created")) == 1


def test_keys_are_scoped_to_the_principal() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _enter(client, OWNER)
        invited = client.post(
            f"/api/v1/rooms/{room_id}/members/invitations",
            headers=OWNER,
            json={"user_id": "peer", "role": "editor"},
        )
        assert invited.status_code == 200, invited.text
        path = f"/api/v1/rooms/{room_id}/messages"
        mine = client.post(path, headers=_keyed(OWNER, "shared"), json={"content": "Hello"})
        theirs = client.post(path, headers=_keyed(PEER, "shared"), json={"content": "Hello"})
        assert mine.status_code == 200 and theirs.status_code == 200
        assert mine.json()["message_id"] != theirs.json()["message_id"]
        assert len(_messages(client, room_id)) == 2


def test_blank_or_oversized_keys_are_rejected() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _enter(client, OWNER)
        path = f"/api/v1/rooms/{room_id}/messages"
        for key in (" ", "x" * 129):
            response = client.post(path, headers=_keyed(OWNER, key), json={"content": "Hello"})
            assert response.status_code == 400, response.text
        assert _messages(client, room_id) == []


def test_replayed_branch_start_holds_one_turn_lock() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _enter(client, OWNER)
        started = _start_single_branch(client, room_id, "branch-1")
        branch_id = started["branch"]["branch_id"]
        assert len(_events(client, room_id, "branch.started")) == 1
        assert len(_events(client, room_id, "turn_lock.acquired")) == 1
        assert len(_events(client, room_id, "agent.run.started")) == 1
        branches = client.get(f"/api/v1/rooms/{room_id}/branches", headers=OWNER).json()
        assert [item["branch_id"] for item in branches] == [branch_id]

        # The replay did not fail against its own lock; a genuinely new start still does.
        locked = client.post(
            f"/api/v1/rooms/{room_id}/branches",
            headers=_keyed(OWNER, "branch-2"),
            json={
                "mode": "TURN_LOCKED_SINGLE",
                "prompt": "Choose the migration sequence.",
                "agent_ids": [started["runs"][0]["agent_id"]],
            },
        )
        assert locked.status_code == 409, locked.text


def test_replayed_synthesis_returns_the_same_version() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _enter(client, OWNER)
        started = _start_single_branch(client, room_id, "branch-1")
        branch_id = started["branch"]["branch_id"]
        execution_id = started["runs"][0]["execution_id"]
        executed = client.post(
            f"/api/v1/branches/{branch_id}/runs/{execution_id}/execute", headers=OWNER
        )
        assert executed.status_code == 200, executed.text
        outputs = [
            output
            for output in client.get(f"/api/v1/rooms/{room_id}/outputs", headers=OWNER).json()
            if output["branch_id"] == branch_id
        ]
        assert len(outputs) == 1
        selected = client.put(
            f"/api/v1/branches/{branch_id}/output-selections/{outputs[0]['output_id']}",
            headers=OWNER,
            json={"disposition": "INCLUDED"},
        )
        assert selected.status_code == 200, selected.text

        path = f"/api/v1/branches/{branch_id}/syntheses/decision-brief"
        body = {"title": "Migration decision"}
        first = client.post(path, headers=_keyed(OWNER, "brief-1"), json=body)
        assert first.status_code == 200, first.text
        replay = client.post(path, headers=_keyed(OWNER, "brief-1"), json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json() == first.json()
        assert len(_events(client, room_id, "artifact.decision_brief_synthesized")) == 1
        artifacts = client.get(f"/api/v1/rooms/{room_id}/artifacts", headers=OWNER).json()
        assert [(artifact["name"], artifact["version"]) for artifact in artifacts] == [
            ("Decision Brief", 1)
        ]

        retitled = client.post(path, headers=_keyed(OWNER, "brief-1"), json={"title": "Other"})
        assert retitled.status_code == 409, retitled.text


@pytest.mark.asyncio
async def test_concurrent_replays_produce_one_message() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            bootstrap = await client.post(
                "/api/v1/me/bootstrap",
                headers=OWNER,
                json={"display_name": "Owner", "room_name": "Decision"},
            )
            room_id = bootstrap.json()["room"]["room_id"]
            path = f"/api/v1/rooms/{room_id}/messages"
            responses = await asyncio.gather(
                *[
                    client.post(path, headers=_keyed(OWNER, "burst"), json={"content": "Burst"})
                    for _ in range(20)
                ]
            )
            assert {response.status_code for response in responses} == {200}
            assert len({response.json()["message_id"] for response in responses}) == 1
            events = (await client.get(f"/api/v1/rooms/{room_id}/events", headers=OWNER)).json()
            created = [event for event in events if event["event_type"] == "message.created"]
            assert len(created) == 1
            listed = (await client.get(path, headers=OWNER)).json()
            assert len(listed) == 1


def test_idempotency_claims_survive_restart(tmp_path: Path) -> None:
    database = str(tmp_path / "idempotency.db")
    with TestClient(create_app(database, auth_tokens=TOKENS)) as client:
        room_id = _enter(client, OWNER)
        path = f"/api/v1/rooms/{room_id}/messages"
        first = client.post(path, headers=_keyed(OWNER, "durable"), json={"content": "Keep"})
        assert first.status_code == 200, first.text

    with TestClient(create_app(database, auth_tokens=TOKENS)) as client:
        assert _enter(client, OWNER) == room_id
        replay = client.post(path, headers=_keyed(OWNER, "durable"), json={"content": "Keep"})
        assert replay.status_code == 200, replay.text
        assert replay.json() == first.json()
        assert len(_messages(client, room_id)) == 1
        assert len(_events(client, room_id, "message.created")) == 1


def test_failed_synthesis_releases_its_key(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _enter(client, OWNER)
        started = _start_single_branch(client, room_id, "branch-1")
        branch_id = started["branch"]["branch_id"]
        execution_id = started["runs"][0]["execution_id"]
        executed = client.post(
            f"/api/v1/branches/{branch_id}/runs/{execution_id}/execute", headers=OWNER
        )
        assert executed.status_code == 200, executed.text
        outputs = [
            output
            for output in client.get(f"/api/v1/rooms/{room_id}/outputs", headers=OWNER).json()
            if output["branch_id"] == branch_id
        ]
        selected = client.put(
            f"/api/v1/branches/{branch_id}/output-selections/{outputs[0]['output_id']}",
            headers=OWNER,
            json={"disposition": "INCLUDED"},
        )
        assert selected.status_code == 200, selected.text

        async def broken_synthesis(**_: Any) -> dict[str, Any]:
            return {"document": "not a document", "simulated": True}

        service = routes_module._svc_or_404()
        monkeypatch.setattr(service.nexus, "synthesize_selected_outputs", broken_synthesis)
        path = f"/api/v1/branches/{branch_id}/syntheses/decision-brief"
        body = {"title": "Migration decision"}
        failed = client.post(path, headers=_keyed(OWNER, "brief-1"), json=body)
        assert failed.status_code == 400, failed.text
        assert len(_events(client, room_id, EventType.BRANCH_SYNTHESIS_FAILED.value)) == 1

        # The key is terminal, not stuck: a replay says so, and a new key succeeds.
        replay = client.post(path, headers=_keyed(OWNER, "brief-1"), json=body)
        assert replay.status_code == 409, replay.text
        assert "new idempotency key" in replay.text
        monkeypatch.undo()
        retried = client.post(path, headers=_keyed(OWNER, "brief-2"), json=body)
        assert retried.status_code == 200, retried.text
        assert len(_events(client, room_id, "artifact.decision_brief_synthesized")) == 1
