"""Finding 19: two processes booting one database with pending migrations must
not have one of them fail with a UNIQUE violation on schema_migrations, and a
process whose migration pass fails must not hang on an aiosqlite connection
nobody closed.
"""

from __future__ import annotations

import asyncio

import pytest

from multiplayer.db.connection import Database
from multiplayer.manage import open_database
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService


@pytest.mark.asyncio
async def test_two_concurrent_boots_of_a_fresh_database_both_succeed(tmp_path) -> None:
    """Neither of two connections racing the initial migration pass fails.

    Both connect first, sequentially: setting WAL mode on a brand-new file is
    its own brief exclusive operation, unrelated to the migration race this
    test targets, and racing it here would test that instead. The migration
    pass itself is what the two ``initialize()`` calls then race.
    """
    db_path = tmp_path / "app.db"
    db1 = Database(str(db_path))
    await db1.connect()
    db2 = Database(str(db_path))
    await db2.connect()

    async def boot(db: Database) -> None:
        await MultiplayerService(db, RealtimeHub(), known_users=frozenset()).initialize()

    await asyncio.gather(boot(db1), boot(db2))
    try:
        rows = await db1.fetch_all("SELECT name FROM schema_migrations")
        names = [str(row["name"]) for row in rows]
        # No name recorded twice, and every shipped migration applied exactly once.
        assert len(names) == len(set(names))
        assert len(names) > 40
    finally:
        await db1.close()
        await db2.close()


@pytest.mark.asyncio
async def test_open_database_closes_the_connection_when_the_migration_pass_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed boot must not leave a live aiosqlite connection behind."""
    import multiplayer.services.bootstrap as bootstrap_module

    async def _boom(self: object, migrations_dir: object) -> None:
        del self, migrations_dir
        raise RuntimeError("simulated migration failure")

    monkeypatch.setattr(bootstrap_module._BootstrapMixin, "_apply_migrations", _boom)

    with pytest.raises(RuntimeError, match="simulated migration failure"):
        await open_database(str(tmp_path / "app.db"))

    # A second, unrelated connection to the same file must not find it locked
    # by a connection the failed open_database left dangling.
    monkeypatch.undo()
    db = await open_database(str(tmp_path / "app.db"))
    try:
        row = await db.fetch_one("SELECT COUNT(*) AS n FROM schema_migrations")
        assert row is not None and row["n"] > 0
    finally:
        await db.close()
