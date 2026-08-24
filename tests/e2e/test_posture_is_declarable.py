"""A posture nobody can declare is a posture nobody has.

P8 shipped a table, three triggers, an event type, a repository and a service method,
and no door: ``declare_room_posture`` had no route and no client, so the only channel
that could ever be ``STRICT`` was one a test reached into the service to declare. That
is building ahead of demonstrated need with the need already demonstrated — the rule
exists and the people it governs cannot see it or set it.

So there is a route, gated on ``ADMINISTER`` exactly as the channel policy beside it
is, and the snapshot every client already reloads carries the channel's posture and
the recorded cause of each parked call. Surfacing is the whole point: a rule that
pauses every tool call has to be readable by the person the pause is waiting on, and
a reviewer answering an approval has to be able to tell "this channel pauses
everything" from "this action always pauses".
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from multiplayer.server import create_app

ADMIN = {"Authorization": "Bearer admin-token"}
EDITOR = {"Authorization": "Bearer editor-token"}


def _app() -> TestClient:
    return TestClient(
        create_app(
            ":memory:",
            auth_tokens={"admin-token": "admin", "editor-token": "editor"},
        )
    )


def _room(client: TestClient) -> str:
    bootstrap = client.post(
        "/api/v1/me/bootstrap",
        headers=ADMIN,
        json={"display_name": "Admin", "room_name": "Decision"},
    ).json()
    room_id = str(bootstrap["room"]["room_id"])
    invited = client.post(
        f"/api/v1/rooms/{room_id}/members/invitations",
        headers=ADMIN,
        json={"user_id": "editor", "role": "editor"},
    )
    assert invited.status_code == 200, invited.text
    return room_id


def _posture(client: TestClient, room_id: str, headers: dict[str, str]) -> str:
    state = client.get(f"/api/v1/rooms/{room_id}/state", headers=headers)
    assert state.status_code == 200, state.text
    return str(state.json()["room"]["posture"])


def test_declaring_a_posture_needs_the_administering_capability() -> None:
    """An editor is refused and leaves nothing behind; the admin's declaration lands."""
    with _app() as client:
        room_id = _room(client)
        assert _posture(client, room_id, ADMIN) == "GUARDED"

        refused = client.patch(
            f"/api/v1/rooms/{room_id}/posture", headers=EDITOR, json={"posture": "STRICT"}
        )

        assert refused.status_code == 403, refused.text
        # An authorization refusal rather than a validation one. Which of the two
        # checks refused her is deliberately not asserted, because it is not
        # observable: the route asks for ADMINISTER and the service asks again
        # inside the transaction that writes, and both refuse in the same words.
        # Deleting the route is what makes this test fail; weakening its gate is
        # caught by the write instead, which is where the guarantee lives.
        assert refused.json()["detail"] == "room access forbidden"
        assert _posture(client, room_id, ADMIN) == "GUARDED"
        types = [
            event["event_type"]
            for event in client.get(f"/api/v1/rooms/{room_id}/events", headers=ADMIN).json()
        ]
        assert "room.posture_declared" not in types

        accepted = client.patch(
            f"/api/v1/rooms/{room_id}/posture", headers=ADMIN, json={"posture": "STRICT"}
        )

        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["posture"] == "STRICT"
        assert accepted.json()["declaration_id"]
        # The editor could not declare it and can still read what governs her.
        assert _posture(client, room_id, EDITOR) == "STRICT"
        declared = [
            event
            for event in client.get(f"/api/v1/rooms/{room_id}/events", headers=ADMIN).json()
            if event["event_type"] == "room.posture_declared"
        ]
        assert len(declared) == 1
        assert declared[0]["payload"]["declaration_id"] == accepted.json()["declaration_id"]

        # Loosening is a declaration too, and it goes through the same door.
        loosened = client.patch(
            f"/api/v1/rooms/{room_id}/posture", headers=ADMIN, json={"posture": "GUARDED"}
        )
        assert loosened.status_code == 200, loosened.text
        assert _posture(client, room_id, ADMIN) == "GUARDED"


def test_a_posture_the_enum_does_not_name_is_refused() -> None:
    """There are two values and no third. A tier that guarantees nothing is not offered."""
    with _app() as client:
        room_id = _room(client)

        rejected = client.patch(
            f"/api/v1/rooms/{room_id}/posture", headers=ADMIN, json={"posture": "DANGEROUS"}
        )

        assert rejected.status_code == 400, rejected.text
        assert _posture(client, room_id, ADMIN) == "GUARDED"


def test_the_client_shows_the_posture_and_lets_an_admin_change_it() -> None:
    """Read by everyone in the channel, changed by an admin, and never stored anywhere.

    The panel reads ``state.room.posture`` — the value the snapshot derives from the
    declaration rows on that read — rather than any value the page kept, so a posture
    a colleague declared is what the next reload shows.
    """
    ui = (Path(__file__).parents[2] / "web" / "index.html").read_text(encoding="utf-8")

    assert 'id="posture-panel"' in ui
    assert "renderPosture(state.room.posture)" in ui
    assert 'data-posture="${escHtml(current)}"' in ui
    assert "escHtml(POSTURE_COPY[current])" in ui
    # Both values are nameable in the control, and it is the admin's alone.
    assert 'onchange="declarePosture(this.value)"' in ui
    assert "currentRoomRole === 'admin'" in ui
    for posture in ("GUARDED", "STRICT"):
        assert f'<option value="{posture}"' in ui, posture
    # The one route, and a reload afterwards rather than a value the page remembers.
    assert "await api('PATCH', `/rooms/${roomId}/posture`, {posture})" in ui
    assert "case 'room.posture_declared':" in ui


def test_the_client_says_which_rule_parked_a_call() -> None:
    """A pause a reader cannot account for is a pause they cannot answer honestly.

    The approval card carries the cause recorded on the call's own row, so "this
    channel pauses everything" and "this action always pauses" read differently to
    the person the call is waiting on.
    """
    ui = (Path(__file__).parents[2] / "web" / "index.html").read_text(encoding="utf-8")

    assert "<div class=\"reason\">${escHtml(a.reason || 'awaiting a human')}</div>" in ui
