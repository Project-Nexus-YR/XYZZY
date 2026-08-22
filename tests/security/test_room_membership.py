"""Shared-channel membership: invite, access levels, role changes, removal, revocation."""

import asyncio
from collections.abc import Coroutine
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient, Response
from starlette.websockets import WebSocketDisconnect

from multiplayer.server import create_app

TOKENS = {
    "owner-token": "owner",
    "alex-token": "alex",
    "sam-token": "sam",
    "pat-token": "pat",
}
OWNER = {"Authorization": "Bearer owner-token"}
ALEX = {"Authorization": "Bearer alex-token"}
SAM = {"Authorization": "Bearer sam-token"}


def _bootstrap(client: TestClient, headers: dict[str, str], room_name: str) -> str:
    response = client.post(
        "/api/v1/me/bootstrap",
        headers=headers,
        json={"display_name": "Person", "room_name": room_name},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["room"]["room_id"])


def _invite(client: TestClient, room_id: str, user_id: str, role: str) -> None:
    response = client.post(
        f"/api/v1/rooms/{room_id}/members/invitations",
        headers=OWNER,
        json={"user_id": user_id, "role": role},
    )
    assert response.status_code == 200, response.text


def _context_room_ids(client: TestClient, headers: dict[str, str]) -> set[str]:
    context = client.get("/api/v1/me/context", headers=headers)
    assert context.status_code == 200
    return {room["room_id"] for room in context.json()["rooms"]}


def _roles(client: TestClient, room_id: str) -> dict[str, str]:
    members = client.get(f"/api/v1/rooms/{room_id}/members", headers=OWNER).json()
    return {member["user_id"]: member["role"] for member in members}


def _events(client: TestClient, room_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/v1/rooms/{room_id}/events", headers=OWNER)
    assert response.status_code == 200, response.text
    return list(response.json())


def _event_types(client: TestClient, room_id: str) -> list[str]:
    return [event["event_type"] for event in _events(client, room_id)]


def test_three_people_share_one_channel_with_two_access_levels() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, OWNER, "Auth migration")
        assert room_id not in _context_room_ids(client, ALEX)

        _invite(client, room_id, "alex", "editor")
        _invite(client, room_id, "sam", "viewer")
        assert room_id in _context_room_ids(client, ALEX)
        assert room_id in _context_room_ids(client, SAM)
        assert _roles(client, room_id) == {"owner": "admin", "alex": "editor", "sam": "viewer"}

        # Editors contribute to the shared context; viewers read it.
        sent = client.post(
            f"/api/v1/rooms/{room_id}/messages", headers=ALEX, json={"content": "From Alex"}
        )
        assert sent.status_code == 200, sent.text
        blocked = client.post(
            f"/api/v1/rooms/{room_id}/messages", headers=SAM, json={"content": "From Sam"}
        )
        assert blocked.status_code == 403
        state = client.get(f"/api/v1/rooms/{room_id}/state", headers=SAM)
        assert state.status_code == 200
        assert [message["content"] for message in state.json()["messages"]] == ["From Alex"]

        # Invitations are idempotent-safe: a second invite is rejected, not duplicated.
        duplicate = client.post(
            f"/api/v1/rooms/{room_id}/members/invitations",
            headers=OWNER,
            json={"user_id": "alex", "role": "viewer"},
        )
        assert duplicate.status_code == 400
        assert _roles(client, room_id)["alex"] == "editor"
        assert _event_types(client, room_id).count("user.invited_room") == 2


def test_membership_management_follows_access_levels() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, OWNER, "Auth migration")
        _invite(client, room_id, "alex", "editor")
        _invite(client, room_id, "sam", "viewer")

        # Viewers cannot invite; editors can (ChatGPT's edit level can too).
        viewer_invite = client.post(
            f"/api/v1/rooms/{room_id}/members/invitations",
            headers=SAM,
            json={"user_id": "pat", "role": "editor"},
        )
        assert viewer_invite.status_code == 403
        editor_invite = client.post(
            f"/api/v1/rooms/{room_id}/members/invitations",
            headers=ALEX,
            json={"user_id": "pat", "role": "viewer"},
        )
        assert editor_invite.status_code == 200, editor_invite.text
        invited = [e for e in _events(client, room_id) if e["event_type"] == "user.invited_room"]
        assert invited[-1]["actor_id"] == "alex"
        assert invited[-1]["payload"] == {"user_id": "pat", "role": "viewer"}

        # Only admins change access or remove people.
        for headers in (ALEX, SAM):
            change = client.patch(
                f"/api/v1/rooms/{room_id}/members/pat", headers=headers, json={"role": "editor"}
            )
            assert change.status_code == 403
            removal = client.delete(f"/api/v1/rooms/{room_id}/members/pat", headers=headers)
            assert removal.status_code == 403
        assert _roles(client, room_id) == {
            "owner": "admin",
            "alex": "editor",
            "sam": "viewer",
            "pat": "viewer",
        }

        # Admin membership is immutable through these routes; leave is the only exit.
        for path, method, body in (
            (f"/api/v1/rooms/{room_id}/members/owner", "patch", {"role": "viewer"}),
            (f"/api/v1/rooms/{room_id}/members/owner", "delete", None),
        ):
            response = client.request(method.upper(), path, headers=OWNER, json=body)
            assert response.status_code == 400, response.text
        assert _roles(client, room_id)["owner"] == "admin"


def test_invitations_name_known_accounts() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, OWNER, "Auth migration")
        for user_id in ("nobody-does-not-exist", "Alex", "a/b"):
            response = client.post(
                f"/api/v1/rooms/{room_id}/members/invitations",
                headers=OWNER,
                json={"user_id": user_id, "role": "editor"},
            )
            assert response.status_code == 400, (user_id, response.text)
        assert _roles(client, room_id) == {"owner": "admin"}
        assert "user.invited_room" not in _event_types(client, room_id)


def test_role_change_takes_effect_immediately() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, OWNER, "Auth migration")
        _invite(client, room_id, "sam", "viewer")
        promoted = client.patch(
            f"/api/v1/rooms/{room_id}/members/sam", headers=OWNER, json={"role": "editor"}
        )
        assert promoted.status_code == 200, promoted.text
        assert promoted.json() == {"user_id": "sam", "role": "editor"}
        sent = client.post(
            f"/api/v1/rooms/{room_id}/messages", headers=SAM, json={"content": "Now I can"}
        )
        assert sent.status_code == 200, sent.text

        demoted = client.patch(
            f"/api/v1/rooms/{room_id}/members/sam", headers=OWNER, json={"role": "viewer"}
        )
        assert demoted.status_code == 200
        blocked = client.post(
            f"/api/v1/rooms/{room_id}/messages", headers=SAM, json={"content": "Blocked"}
        )
        assert blocked.status_code == 403
        assert _event_types(client, room_id).count("user.role_changed") == 2

        # Re-applying the current access is a no-op, not a new history entry.
        same = client.patch(
            f"/api/v1/rooms/{room_id}/members/sam", headers=OWNER, json={"role": "viewer"}
        )
        assert same.status_code == 200
        assert same.json() == {"user_id": "sam", "role": "viewer"}
        assert _event_types(client, room_id).count("user.role_changed") == 2

        invalid = client.patch(
            f"/api/v1/rooms/{room_id}/members/sam", headers=OWNER, json={"role": "admin"}
        )
        assert invalid.status_code == 400
        unknown = client.patch(
            f"/api/v1/rooms/{room_id}/members/nobody", headers=OWNER, json={"role": "editor"}
        )
        assert unknown.status_code == 400


def test_removal_revokes_reads_and_closes_the_live_subscription() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, OWNER, "Auth migration")
        _invite(client, room_id, "alex", "editor")

        with client.websocket_connect(f"/ws?room_id={room_id}", headers=ALEX) as websocket:
            assert websocket.receive_json()["type"] == "connected"
            removed = client.delete(f"/api/v1/rooms/{room_id}/members/alex", headers=OWNER)
            assert removed.status_code == 200, removed.text
            try:
                while True:
                    frame = websocket.receive_json()
                    assert frame.get("event_type") != "user.removed_room", (
                        "removed member must not receive events after revocation"
                    )
            except WebSocketDisconnect as exc:
                assert exc.code == 4403

        assert room_id not in _context_room_ids(client, ALEX)
        assert client.get(f"/api/v1/rooms/{room_id}/state", headers=ALEX).status_code == 403
        assert (
            client.post(
                f"/api/v1/rooms/{room_id}/messages", headers=ALEX, json={"content": "Still here?"}
            ).status_code
            == 403
        )
        try:
            with client.websocket_connect(f"/ws?room_id={room_id}", headers=ALEX):
                raise AssertionError("removed member reconnected")
        except WebSocketDisconnect as exc:
            assert exc.code == 4403
        assert _roles(client, room_id) == {"owner": "admin"}
        assert _event_types(client, room_id).count("user.removed_room") == 1

        # Re-inviting after removal works and the room history still shows the removal.
        _invite(client, room_id, "alex", "viewer")
        assert _roles(client, room_id)["alex"] == "viewer"


def test_removal_reaches_the_member_on_their_other_sockets() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, OWNER, "Auth migration")
        alex_room_id = _bootstrap(client, ALEX, "Alex notes")
        _invite(client, room_id, "alex", "editor")
        with client.websocket_connect(f"/ws?room_id={alex_room_id}", headers=ALEX) as websocket:
            assert websocket.receive_json()["type"] == "connected"
            removed = client.delete(f"/api/v1/rooms/{room_id}/members/alex", headers=OWNER)
            assert removed.status_code == 200, removed.text
            assert websocket.receive_json() == {"type": "room_removed", "room_id": room_id}


def test_invitation_reaches_the_invitee_live() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, OWNER, "Auth migration")
        sam_room_id = _bootstrap(client, SAM, "Sam notes")
        with client.websocket_connect(f"/ws?room_id={sam_room_id}", headers=SAM) as websocket:
            assert websocket.receive_json()["type"] == "connected"
            _invite(client, room_id, "sam", "viewer")
            assert websocket.receive_json() == {
                "type": "room_invited",
                "room_id": room_id,
                "room_name": "Auth migration",
                "role": "viewer",
            }
        assert room_id in _context_room_ids(client, SAM)


def test_leaving_is_durable_and_open_to_viewers() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap(client, OWNER, "Auth migration")
        _invite(client, room_id, "alex", "editor")
        _invite(client, room_id, "sam", "viewer")

        left = client.post(f"/api/v1/rooms/{room_id}/leave", headers=SAM)
        assert left.status_code == 200, left.text
        assert "sam" not in _roles(client, room_id)
        assert room_id not in _context_room_ids(client, SAM)
        assert client.get(f"/api/v1/rooms/{room_id}/state", headers=SAM).status_code == 403
        assert client.post(f"/api/v1/rooms/{room_id}/leave", headers=SAM).status_code == 403
        left_events = [e for e in _events(client, room_id) if e["event_type"] == "user.left_room"]
        assert [e["payload"] for e in left_events] == [{"user_id": "sam", "role": "viewer"}]

        # The channel always keeps an admin while anyone else is still in it.
        last_admin = client.post(f"/api/v1/rooms/{room_id}/leave", headers=OWNER)
        assert last_admin.status_code == 400
        assert _roles(client, room_id)["owner"] == "admin"
        assert (
            client.delete(f"/api/v1/rooms/{room_id}/members/alex", headers=OWNER).status_code == 200
        )
        assert client.post(f"/api/v1/rooms/{room_id}/leave", headers=OWNER).status_code == 200
        assert client.get(f"/api/v1/rooms/{room_id}/state", headers=OWNER).status_code == 403


@pytest.mark.asyncio
async def test_access_changes_are_atomic_with_writes() -> None:
    """A demotion and a message racing each other never leave a write by a viewer in the log."""
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
            members = f"/api/v1/rooms/{room_id}/members"
            invited = await client.post(
                f"{members}/invitations", headers=OWNER, json={"user_id": "sam", "role": "editor"}
            )
            assert invited.status_code == 200, invited.text

            for trial in range(12):
                promoted = await client.patch(
                    f"{members}/sam", headers=OWNER, json={"role": "editor"}
                )
                assert promoted.status_code == 200, promoted.text
                content = f"Trial {trial}"
                write: Coroutine[Any, Any, Response] = client.post(
                    f"/api/v1/rooms/{room_id}/messages", headers=SAM, json={"content": content}
                )
                demote: Coroutine[Any, Any, Response] = client.patch(
                    f"{members}/sam", headers=OWNER, json={"role": "viewer"}
                )
                racers = (write, demote) if trial % 2 == 0 else (demote, write)
                results = await asyncio.gather(*racers)
                written, demoted = (
                    (results[0], results[1]) if trial % 2 == 0 else (results[1], results[0])
                )
                assert demoted.status_code == 200, demoted.text
                assert written.status_code in {200, 403}, written.text

                events = (await client.get(f"/api/v1/rooms/{room_id}/events", headers=OWNER)).json()
                demotions = [
                    e["sequence"]
                    for e in events
                    if e["event_type"] == "user.role_changed" and e["payload"]["role"] == "viewer"
                ]
                created = [
                    e["sequence"]
                    for e in events
                    if e["event_type"] == "message.created"
                    and e["payload"].get("content") == content
                ]
                if written.status_code == 200:
                    assert len(created) == 1, events
                    assert created[0] < max(demotions), (trial, created, demotions)
                else:
                    assert created == [], (trial, events)


@pytest.mark.asyncio
async def test_channel_writes_are_atomic_with_demotion() -> None:
    """Task, artifact and decision writes obey the same in-transaction fence as messages."""
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
            members = f"/api/v1/rooms/{room_id}/members"
            invited = await client.post(
                f"{members}/invitations", headers=OWNER, json={"user_id": "sam", "role": "editor"}
            )
            assert invited.status_code == 200, invited.text

            writes = (
                ("tasks", "task.created", "title", lambda m: {"title": m}),
                ("artifacts", "artifact.created", "name", lambda m: {"name": m, "content": "x"}),
                ("decisions", "decision.created", "title", lambda m: {"title": m, "content": "x"}),
            )
            plan = [w for _ in range(4) for w in writes]
            for trial, (path, event_type, field, make_body) in enumerate(plan):
                promoted = await client.patch(
                    f"{members}/sam", headers=OWNER, json={"role": "editor"}
                )
                assert promoted.status_code == 200, promoted.text
                marker = f"{path}-{trial}"
                write: Coroutine[Any, Any, Response] = client.post(
                    f"/api/v1/rooms/{room_id}/{path}", headers=SAM, json=make_body(marker)
                )
                demote: Coroutine[Any, Any, Response] = client.patch(
                    f"{members}/sam", headers=OWNER, json={"role": "viewer"}
                )
                racers = (write, demote) if trial % 2 == 0 else (demote, write)
                results = await asyncio.gather(*racers)
                written = results[0] if trial % 2 == 0 else results[1]
                assert written.status_code in {200, 403}, (path, written.text)

                events = (await client.get(f"/api/v1/rooms/{room_id}/events", headers=OWNER)).json()
                demotions = [
                    e["sequence"]
                    for e in events
                    if e["event_type"] == "user.role_changed" and e["payload"]["role"] == "viewer"
                ]
                created = [
                    e["sequence"]
                    for e in events
                    if e["event_type"] == event_type and e["payload"].get(field) == marker
                ]
                if written.status_code == 200:
                    assert len(created) == 1, (trial, path, events)
                    assert created[0] < max(demotions), (trial, path, created, demotions)
                else:
                    assert created == [], (trial, path, events)


@pytest.mark.asyncio
async def test_concurrent_duplicate_invites_resolve_to_one_member() -> None:
    """Sixteen racing invites of one new account yield a single member and event, never a 500."""
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
            path = f"/api/v1/rooms/{room_id}/members/invitations"
            responses = await asyncio.gather(
                *[
                    client.post(path, headers=OWNER, json={"user_id": "sam", "role": "editor"})
                    for _ in range(16)
                ]
            )
            codes = [response.status_code for response in responses]
            assert set(codes) <= {200, 400}, codes
            assert codes.count(200) == 1, codes
            members = (await client.get(f"/api/v1/rooms/{room_id}/members", headers=OWNER)).json()
            assert [member["user_id"] for member in members].count("sam") == 1
            events = (await client.get(f"/api/v1/rooms/{room_id}/events", headers=OWNER)).json()
            assert [event["event_type"] for event in events].count("user.invited_room") == 1


async def _demotion_race(client, room_id, event_type, do_write):
    """Promote sam to editor, then barrier-start a write and a demotion; return whether the
    write committed and the sequences needed to check it never landed after the demotion."""
    members = f"/api/v1/rooms/{room_id}/members"
    results = []
    for trial in range(6):
        promoted = await client.patch(f"{members}/sam", headers=OWNER, json={"role": "editor"})
        assert promoted.status_code == 200, promoted.text
        before = [
            e["sequence"]
            for e in (await client.get(f"/api/v1/rooms/{room_id}/events", headers=OWNER)).json()
            if e["event_type"] == event_type
        ]
        write: Coroutine[Any, Any, Response] = do_write()
        demote: Coroutine[Any, Any, Response] = client.patch(
            f"{members}/sam", headers=OWNER, json={"role": "viewer"}
        )
        racers = (write, demote) if trial % 2 == 0 else (demote, write)
        got = await asyncio.gather(*racers)
        written = got[0] if trial % 2 == 0 else got[1]
        assert written.status_code in {200, 403}, written.text
        events = (await client.get(f"/api/v1/rooms/{room_id}/events", headers=OWNER)).json()
        demotions = [
            e["sequence"]
            for e in events
            if e["event_type"] == "user.role_changed" and e["payload"]["role"] == "viewer"
        ]
        after = [e["sequence"] for e in events if e["event_type"] == event_type]
        if written.status_code == 200:
            assert len(after) == len(before) + 1, (event_type, before, after)
            assert max(after) < max(demotions), (trial, event_type, after, demotions)
        else:
            assert after == before, (trial, event_type, before, after)
        results.append(written.status_code)
    return results


@pytest.mark.asyncio
async def test_agent_spawn_is_atomic_with_demotion() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            room_id = (
                await client.post(
                    "/api/v1/me/bootstrap",
                    headers=OWNER,
                    json={"display_name": "Owner", "room_name": "Decision"},
                )
            ).json()["room"]["room_id"]
            assert (
                await client.post(
                    f"/api/v1/rooms/{room_id}/members/invitations",
                    headers=OWNER,
                    json={"user_id": "sam", "role": "editor"},
                )
            ).status_code == 200
            templates = (await client.get("/api/v1/agent-templates", headers=OWNER)).json()
            template_id = templates[0]["template_id"]
            codes = await _demotion_race(
                client,
                room_id,
                "agent.joined_room",
                lambda: client.post(
                    f"/api/v1/rooms/{room_id}/agents",
                    headers=SAM,
                    json={"template_id": template_id},
                ),
            )
            assert set(codes) <= {200, 403}


@pytest.mark.asyncio
async def test_memory_write_is_atomic_with_demotion() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            room_id = (
                await client.post(
                    "/api/v1/me/bootstrap",
                    headers=OWNER,
                    json={"display_name": "Owner", "room_name": "Decision"},
                )
            ).json()["room"]["room_id"]
            assert (
                await client.post(
                    f"/api/v1/rooms/{room_id}/members/invitations",
                    headers=OWNER,
                    json={"user_id": "sam", "role": "editor"},
                )
            ).status_code == 200
            counter = {"n": 0}

            def write():
                counter["n"] += 1
                return client.post(
                    f"/api/v1/rooms/{room_id}/memories",
                    headers=SAM,
                    json={"content": f"note {counter['n']}", "scope": "ROOM"},
                )

            codes = await _demotion_race(client, room_id, "memory.created", write)
            assert set(codes) <= {200, 403}


@pytest.mark.asyncio
async def test_artifact_version_is_atomic_with_demotion() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            room_id = (
                await client.post(
                    "/api/v1/me/bootstrap",
                    headers=OWNER,
                    json={"display_name": "Owner", "room_name": "Decision"},
                )
            ).json()["room"]["room_id"]
            assert (
                await client.post(
                    f"/api/v1/rooms/{room_id}/members/invitations",
                    headers=OWNER,
                    json={"user_id": "sam", "role": "editor"},
                )
            ).status_code == 200
            artifact_id = (
                await client.post(
                    f"/api/v1/rooms/{room_id}/artifacts",
                    headers=OWNER,
                    json={"name": "Spec", "artifact_type": "DOCUMENT"},
                )
            ).json()["artifact_id"]
            counter = {"n": 0}

            def write():
                counter["n"] += 1
                return client.post(
                    f"/api/v1/artifacts/{artifact_id}/versions",
                    headers=SAM,
                    json={"content": f"v{counter['n']}"},
                )

            codes = await _demotion_race(client, room_id, "artifact.version_created", write)
            assert set(codes) <= {200, 403}


@pytest.mark.asyncio
async def test_interrupt_agent_is_atomic_with_demotion() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            room_id = (
                await client.post(
                    "/api/v1/me/bootstrap",
                    headers=OWNER,
                    json={"display_name": "Owner", "room_name": "Decision"},
                )
            ).json()["room"]["room_id"]
            assert (
                await client.post(
                    f"/api/v1/rooms/{room_id}/members/invitations",
                    headers=OWNER,
                    json={"user_id": "sam", "role": "editor"},
                )
            ).status_code == 200
            templates = (await client.get("/api/v1/agent-templates", headers=OWNER)).json()
            agent_id = (
                await client.post(
                    f"/api/v1/rooms/{room_id}/agents",
                    headers=OWNER,
                    json={"template_id": templates[0]["template_id"]},
                )
            ).json()["agent_id"]
            codes = await _demotion_race(
                client,
                room_id,
                "human.interrupted_agent",
                lambda: client.post(
                    f"/api/v1/agents/{agent_id}/interrupt",
                    headers=SAM,
                    json={"reason": "stop"},
                ),
            )
            assert set(codes) <= {200, 403}


@pytest.mark.asyncio
async def test_redirect_agent_is_atomic_with_demotion() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            room_id = (
                await client.post(
                    "/api/v1/me/bootstrap",
                    headers=OWNER,
                    json={"display_name": "Owner", "room_name": "Decision"},
                )
            ).json()["room"]["room_id"]
            assert (
                await client.post(
                    f"/api/v1/rooms/{room_id}/members/invitations",
                    headers=OWNER,
                    json={"user_id": "sam", "role": "editor"},
                )
            ).status_code == 200
            templates = (await client.get("/api/v1/agent-templates", headers=OWNER)).json()
            agent_id = (
                await client.post(
                    f"/api/v1/rooms/{room_id}/agents",
                    headers=OWNER,
                    json={"template_id": templates[0]["template_id"]},
                )
            ).json()["agent_id"]
            counter = {"n": 0}

            def write():
                counter["n"] += 1
                return client.post(
                    f"/api/v1/agents/{agent_id}/redirect",
                    headers=SAM,
                    json={"instruction": f"go {counter['n']}"},
                )

            codes = await _demotion_race(client, room_id, "human.redirected_agent", write)
            assert set(codes) <= {200, 403}


async def _seed_selected_output(client, room_id, template_id, prompt):
    """Owner-driven agent run producing one selectable output."""
    agent = (
        await client.post(
            f"/api/v1/rooms/{room_id}/agents", headers=OWNER, json={"template_id": template_id}
        )
    ).json()
    session = (
        await client.post(
            f"/api/v1/rooms/{room_id}/agents/{agent['agent_id']}/sessions", headers=OWNER
        )
    ).json()
    execution = (
        await client.post(f"/api/v1/sessions/{session['session_id']}/execute", headers=OWNER)
    ).json()
    result = (
        await client.post(
            f"/api/v1/executions/{execution['execution_id']}/step",
            headers=OWNER,
            json={"prompt": prompt},
        )
    ).json()
    return result["output_id"]


@pytest.mark.asyncio
async def test_synthesis_is_atomic_with_demotion() -> None:
    """A member demoted while the synthesis model call runs must not author its events.

    The demotion is injected inside the model call itself, so the initiator passes the
    start-of-request check as an editor and is a viewer only by the time the completion
    transaction assigns the ordered event sequences — the exact window round 2 missed.
    """
    from multiplayer.api import routes as routes_mod

    app = create_app(":memory:", auth_tokens=TOKENS)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            room_id = (
                await client.post(
                    "/api/v1/me/bootstrap",
                    headers=OWNER,
                    json={"display_name": "Owner", "room_name": "Decision"},
                )
            ).json()["room"]["room_id"]
            assert (
                await client.post(
                    f"/api/v1/rooms/{room_id}/members/invitations",
                    headers=OWNER,
                    json={"user_id": "sam", "role": "editor"},
                )
            ).status_code == 200
            templates = (await client.get("/api/v1/agent-templates", headers=OWNER)).json()
            outputs = [
                await _seed_selected_output(client, room_id, t["template_id"], p)
                for t, p in zip(templates[:2], ("engineering", "security"), strict=True)
            ]
            for output_id in outputs:
                assert (
                    await client.put(
                        f"/api/v1/rooms/{room_id}/output-selections/{output_id}",
                        headers=OWNER,
                        json={"disposition": "INCLUDED"},
                    )
                ).status_code == 200

            svc = routes_mod._svc
            assert svc is not None
            original = svc.nexus.synthesize_selected_outputs

            async def demote_then_synthesize(**kwargs):
                demoted = await client.patch(
                    f"/api/v1/rooms/{room_id}/members/sam",
                    headers=OWNER,
                    json={"role": "viewer"},
                )
                assert demoted.status_code == 200, demoted.text
                return await original(**kwargs)

            svc.nexus.synthesize_selected_outputs = demote_then_synthesize  # type: ignore[method-assign]
            try:
                synth = await client.post(
                    f"/api/v1/rooms/{room_id}/syntheses/decision-brief",
                    headers=SAM,
                    json={"title": "Managed identity decision"},
                )
            finally:
                svc.nexus.synthesize_selected_outputs = original  # type: ignore[method-assign]

            assert synth.status_code == 403, synth.text
            events = (await client.get(f"/api/v1/rooms/{room_id}/events", headers=OWNER)).json()
            types = [e["event_type"] for e in events]
            # No synthesis contribution recorded for the demoted initiator.
            assert "branch.synthesis.completed" not in types, types
            assert "artifact.decision_brief_synthesized" not in types, types
            # The demotion is on the log; nothing user-attributed outranks it.
            demotions = [
                e["sequence"]
                for e in events
                if e["event_type"] == "user.role_changed" and e["payload"]["role"] == "viewer"
            ]
            assert demotions, types
            sam_events = [
                e["sequence"]
                for e in events
                if e["actor_type"] == "user" and e["actor_id"] == "sam"
            ]
            assert all(seq < max(demotions) for seq in sam_events), events


async def _managed_branch_runs(client, svc, room_id):
    """Spawn two agents and start a managed parallel branch; return its PENDING runs."""
    from multiplayer.domain.models import BranchMode

    templates = (await client.get("/api/v1/agent-templates", headers=OWNER)).json()
    agent_ids = []
    for template in templates[:2]:
        agent = (
            await client.post(
                f"/api/v1/rooms/{room_id}/agents",
                headers=OWNER,
                json={"template_id": template["template_id"]},
            )
        ).json()
        agent_ids.append(agent["agent_id"])
    await client.post(
        f"/api/v1/rooms/{room_id}/messages", headers=OWNER, json={"content": "context"}
    )
    _, runs = await svc.start_branch(
        room_id, BranchMode.PARALLEL, "Should we ship it?", "owner", agent_ids
    )
    return runs


@pytest.mark.asyncio
async def test_cancel_execution_is_atomic_with_demotion() -> None:
    """A member demoted while the cancel is dispatched to the runtime must not author the
    EXECUTION_CANCELLED event. The demotion is injected into the nexus await, so the member
    is an editor at the route check and a viewer by the terminalize transaction."""
    from multiplayer.api import routes as routes_mod

    app = create_app(":memory:", auth_tokens=TOKENS)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            room_id = (
                await client.post(
                    "/api/v1/me/bootstrap",
                    headers=OWNER,
                    json={"display_name": "Owner", "room_name": "Decision"},
                )
            ).json()["room"]["room_id"]
            assert (
                await client.post(
                    f"/api/v1/rooms/{room_id}/members/invitations",
                    headers=OWNER,
                    json={"user_id": "sam", "role": "editor"},
                )
            ).status_code == 200
            svc = routes_mod._svc
            assert svc is not None
            runs = await _managed_branch_runs(client, svc, room_id)

            original = svc.nexus.cancel_execution

            async def demote_then_cancel(execution_id):
                demoted = await client.patch(
                    f"/api/v1/rooms/{room_id}/members/sam",
                    headers=OWNER,
                    json={"role": "viewer"},
                )
                assert demoted.status_code == 200, demoted.text
                return await original(execution_id)

            svc.nexus.cancel_execution = demote_then_cancel  # type: ignore[method-assign]
            try:
                with pytest.raises(Exception) as excinfo:
                    await svc.cancel_execution(runs[0].execution_id, "sam", require_member=True)
            finally:
                svc.nexus.cancel_execution = original  # type: ignore[method-assign]
            assert "forbidden" in str(excinfo.value).lower()

            events = (await client.get(f"/api/v1/rooms/{room_id}/events", headers=OWNER)).json()
            assert "execution.cancelled" not in [e["event_type"] for e in events], events
            demotions = [
                e["sequence"]
                for e in events
                if e["event_type"] == "user.role_changed" and e["payload"]["role"] == "viewer"
            ]
            assert demotions
            sam_events = [
                e["sequence"]
                for e in events
                if e["actor_type"] == "user" and e["actor_id"] == "sam"
            ]
            assert all(seq < max(demotions) for seq in sam_events), events


@pytest.mark.asyncio
async def test_intervene_execution_is_atomic_with_demotion() -> None:
    """A member demoted while an execution intervention is prepared must not author the
    HUMAN_REDIRECTED_AGENT event. The demotion is injected into the agent lookup, before the
    transaction that re-checks membership and appends the event."""
    from multiplayer.api import routes as routes_mod

    app = create_app(":memory:", auth_tokens=TOKENS)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            room_id = (
                await client.post(
                    "/api/v1/me/bootstrap",
                    headers=OWNER,
                    json={"display_name": "Owner", "room_name": "Decision"},
                )
            ).json()["room"]["room_id"]
            assert (
                await client.post(
                    f"/api/v1/rooms/{room_id}/members/invitations",
                    headers=OWNER,
                    json={"user_id": "sam", "role": "editor"},
                )
            ).status_code == 200
            svc = routes_mod._svc
            assert svc is not None
            runs = await _managed_branch_runs(client, svc, room_id)

            original_get_agent = svc.get_agent

            async def demote_then_get_agent(agent_id):
                demoted = await client.patch(
                    f"/api/v1/rooms/{room_id}/members/sam",
                    headers=OWNER,
                    json={"role": "viewer"},
                )
                assert demoted.status_code == 200, demoted.text
                return await original_get_agent(agent_id)

            svc.get_agent = demote_then_get_agent  # type: ignore[method-assign]
            try:
                with pytest.raises(Exception) as excinfo:
                    await svc.intervene_execution(
                        runs[0].execution_id, "sam", "pivot", require_member=True
                    )
            finally:
                svc.get_agent = original_get_agent  # type: ignore[method-assign]
            assert "forbidden" in str(excinfo.value).lower()

            events = (await client.get(f"/api/v1/rooms/{room_id}/events", headers=OWNER)).json()
            demotions = [
                e["sequence"]
                for e in events
                if e["event_type"] == "user.role_changed" and e["payload"]["role"] == "viewer"
            ]
            assert demotions
            sam_events = [
                e["sequence"]
                for e in events
                if e["actor_type"] == "user" and e["actor_id"] == "sam"
            ]
            assert all(seq < max(demotions) for seq in sam_events), events
