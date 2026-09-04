"""Finding 27: creating an org or workspace and its admin membership is atomic.

``create_organization`` and ``create_workspace`` used to commit the row and its
admin membership as two separate transactions, so a failure between them left
a memberless row: invisible to ``list_for_user``, unadministrable, and (for an
org) holding its globally unique slug forever. This proves each is now one
transaction by failing the membership write and asserting the row it would
have belonged to never landed either.
"""

from __future__ import annotations

import sqlite3

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService

OWNER = "owner"


class _FailsOnTheNthExecute(Database):
    """Raises on the Nth call to execute(), simulating a mid transaction fault."""

    def __init__(self, path: str, fail_on_call: int) -> None:
        super().__init__(path)
        self._fail_on_call = fail_on_call
        self._calls = 0

    async def execute(self, sql: str, params: tuple = ()):  # type: ignore[override]
        self._calls += 1
        if self._calls == self._fail_on_call:
            raise sqlite3.OperationalError("database or disk is full")
        return await super().execute(sql, params)


@pytest.mark.asyncio
async def test_create_organization_is_atomic_across_a_mid_write_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = _FailsOnTheNthExecute(":memory:", fail_on_call=0)
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({OWNER}))
    await svc.initialize()

    # Fail the second write create_organization makes: the admin membership
    # insert, after the organization row's own insert already ran.
    db._fail_on_call = db._calls + 2
    with pytest.raises(sqlite3.OperationalError):
        await svc.create_organization("Finding27 org", "finding27-org", OWNER)

    orgs = await svc.db.fetch_all(
        "SELECT org_id FROM organizations WHERE slug = ?", ("finding27-org",)
    )
    assert orgs == [], "a memberless org must not survive the failed membership write"
    await db.close()


@pytest.mark.asyncio
async def test_create_workspace_is_atomic_across_a_mid_write_fault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = _FailsOnTheNthExecute(":memory:", fail_on_call=0)
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({OWNER}))
    await svc.initialize()
    org = await svc.create_organization("Finding27 org", "finding27-ws-org", OWNER)

    db._fail_on_call = db._calls + 2
    with pytest.raises(sqlite3.OperationalError):
        await svc.create_workspace(org.org_id, "Main", "main", OWNER)

    workspaces = await svc.db.fetch_all(
        "SELECT workspace_id FROM workspaces WHERE slug = ?", ("main",)
    )
    assert workspaces == [], "a memberless workspace must not survive the failed write"
    await db.close()
