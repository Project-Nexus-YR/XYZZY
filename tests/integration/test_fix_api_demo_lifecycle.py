"""Finding 12: the demo bearer token used to survive into a real deployment
sharing its database file, and `--demo` against a database a real user had
already bootstrapped would ingest the public "demo" token into it directly.

A non-demo start now retires any leftover demo token as one case of the
general bootstrap-retirement rule (finding 11): the demo token is ingested
with label='bootstrap' like any other, so it goes when it is absent from a
non-demo start's configured map. A demo start separately refuses to run
against a database that already holds a live non-demo credential.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from multiplayer.server import create_app

DEMO = {"Authorization": "Bearer demo"}
REAL = {"Authorization": "Bearer real-token"}


def test_a_non_demo_start_retires_a_leftover_demo_token(tmp_path) -> None:
    """Fails before the fix: the demo token kept authenticating as user_demo
    on a later non-demo start of the same database file.
    """
    db_path = str(tmp_path / "shared.db")
    with TestClient(create_app(db_path, demo=True)) as client:
        assert client.get("/api/v1/me/context", headers=DEMO).status_code == 200

    with TestClient(
        create_app(db_path, demo=False, auth_tokens={"real-token": "user_real"})
    ) as client:
        assert client.get("/api/v1/auth/config").json()["demo"] is False
        assert client.get("/api/v1/me/context", headers=DEMO).status_code == 401
        assert client.get("/api/v1/me/context", headers=REAL).status_code == 200


def test_demo_refuses_a_database_a_real_user_already_bootstrapped(tmp_path) -> None:
    """Fails before the fix: `--demo` against a database a real user had
    already bootstrapped ingested the demo token into it directly instead of
    refusing to start.
    """
    db_path = str(tmp_path / "real-first.db")
    with TestClient(
        create_app(db_path, demo=False, auth_tokens={"real-token": "user_real"})
    ) as client:
        bootstrap = client.post(
            "/api/v1/me/bootstrap",
            headers=REAL,
            json={"display_name": "Real User", "room_name": "Work"},
        )
        assert bootstrap.status_code == 200

    with (
        pytest.raises(RuntimeError, match="XYZZY_DEMO"),
        TestClient(create_app(db_path, demo=True)),
    ):
        pass


def test_demo_still_starts_cleanly_against_a_fresh_database(tmp_path) -> None:
    db_path = str(tmp_path / "fresh.db")
    with TestClient(create_app(db_path, demo=True)) as client:
        assert client.get("/api/v1/me/context", headers=DEMO).status_code == 200


def test_a_repeated_demo_start_does_not_refuse_itself(tmp_path) -> None:
    """The demo token's own row must not count as "a real credential already
    here" on the next demo start of the same file.
    """
    db_path = str(tmp_path / "demo-twice.db")
    with TestClient(create_app(db_path, demo=True)):
        pass
    with TestClient(create_app(db_path, demo=True)) as client:
        assert client.get("/api/v1/me/context", headers=DEMO).status_code == 200
