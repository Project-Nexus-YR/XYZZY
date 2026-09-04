"""Finding 59: a rebuild migration (``PRAGMA foreign_keys=OFF``) that commits
an orphan row must be refused, not applied.

The SQLite rebuild recipe's own step 10 is ``PRAGMA foreign_key_check`` run
before the commit, at the one moment an orphan is still reversible; nothing
in ``_apply_migrations`` ran it, so a rebuild migration that drops a
referenced row, or copies with a wrong column list, committed the orphan and
recorded itself as applied.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from multiplayer.db.connection import Database
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService


async def _service() -> tuple[Database, MultiplayerService]:
    db = Database(":memory:")
    await db.connect()
    return db, MultiplayerService(db, RealtimeHub(), known_users=frozenset())


async def test_a_rebuild_migration_that_commits_an_orphan_is_refused(tmp_path: Path) -> None:
    (tmp_path / "001_bad_rebuild.sql").write_text(
        "PRAGMA foreign_keys=OFF;\n"
        "CREATE TABLE parent (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id));\n"
        "INSERT INTO parent VALUES (1);\n"
        "INSERT INTO child VALUES (100, 999);\n"
        "PRAGMA foreign_keys=ON;\n"
    )
    db, svc = await _service()
    try:
        with pytest.raises(RuntimeError, match="001_bad_rebuild.sql"):
            await svc._apply_migrations(tmp_path)

        applied = await db.fetch_all("SELECT name FROM schema_migrations")
        assert applied == []
        orphan = await db.fetch_one(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'child'"
        )
        assert orphan is None, "the refused rebuild left its orphan committed"
    finally:
        await db.close()


async def test_a_rebuild_migration_with_no_orphan_still_applies(tmp_path: Path) -> None:
    (tmp_path / "001_good_rebuild.sql").write_text(
        "PRAGMA foreign_keys=OFF;\n"
        "CREATE TABLE parent (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id));\n"
        "INSERT INTO parent VALUES (1);\n"
        "INSERT INTO child VALUES (100, 1);\n"
        "PRAGMA foreign_keys=ON;\n"
    )
    db, svc = await _service()
    try:
        await svc._apply_migrations(tmp_path)
        applied = await db.fetch_all("SELECT name FROM schema_migrations")
        assert [row["name"] for row in applied] == ["001_good_rebuild.sql"]
        row = await db.fetch_one("SELECT parent_id FROM child WHERE id = 100")
        assert row is not None and row["parent_id"] == 1
    finally:
        await db.close()
