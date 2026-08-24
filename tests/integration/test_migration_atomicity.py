"""A migration and the row recording it commit together, or neither does.

The old loop ran the script and recorded it as two separate autocommits: a
crash mid-script left partial DDL behind, and a crash between the two left an
applied migration unmarked, so the replay failed forever on the next boot.
These tests pin the atomic behaviour, including the rebuild recipe whose
PRAGMA foreign_keys=OFF a transaction would silently ignore.
"""

from pathlib import Path

import pytest

from multiplayer.db.connection import Database
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService


async def _service() -> tuple[Database, MultiplayerService]:
    db = Database(":memory:")
    await db.connect()
    return db, MultiplayerService(db, RealtimeHub(), known_users=frozenset())


async def test_a_failing_migration_leaves_no_trace_and_succeeds_on_retry(tmp_path):
    (tmp_path / "001_good.sql").write_text("CREATE TABLE first (id INTEGER PRIMARY KEY);")
    (tmp_path / "002_bad.sql").write_text(
        "CREATE TABLE second (id INTEGER PRIMARY KEY);\nINSERT INTO missing VALUES (1);"
    )
    db, svc = await _service()
    try:
        with pytest.raises(RuntimeError, match="002_bad.sql"):
            await svc._apply_migrations(tmp_path)

        applied = await db.fetch_all("SELECT name FROM schema_migrations")
        assert [row["name"] for row in applied] == ["001_good.sql"]
        half = await db.fetch_one(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'second'"
        )
        assert half is None, "failed migration left partial DDL behind"

        (tmp_path / "002_bad.sql").write_text("CREATE TABLE second (id INTEGER PRIMARY KEY);")
        await svc._apply_migrations(tmp_path)
        applied = await db.fetch_all("SELECT name FROM schema_migrations ORDER BY name")
        assert [row["name"] for row in applied] == ["001_good.sql", "002_bad.sql"]
    finally:
        await db.close()


async def test_an_applied_migration_is_never_replayed(tmp_path):
    (tmp_path / "001_once.sql").write_text("CREATE TABLE once (id INTEGER PRIMARY KEY);")
    db, svc = await _service()
    try:
        await svc._apply_migrations(tmp_path)
        await svc._apply_migrations(tmp_path)
        applied = await db.fetch_all("SELECT name FROM schema_migrations")
        assert [row["name"] for row in applied] == ["001_once.sql"]
    finally:
        await db.close()


async def test_the_rebuild_recipe_still_works_inside_the_transaction(tmp_path):
    (tmp_path / "001_tables.sql").write_text(
        "CREATE TABLE parent (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE child (id INTEGER PRIMARY KEY, "
        "parent_id INTEGER NOT NULL REFERENCES parent(id));\n"
        "INSERT INTO parent VALUES (1);\nINSERT INTO child VALUES (1, 1);"
    )
    # The sanctioned rebuild: drop and recreate a referenced table while rows
    # still point at it, legal only with foreign keys off for the duration.
    (tmp_path / "002_rebuild.sql").write_text(
        "PRAGMA foreign_keys=OFF;\n"
        "CREATE TABLE parent_rebuilt (id INTEGER PRIMARY KEY, note TEXT);\n"
        "INSERT INTO parent_rebuilt(id) SELECT id FROM parent;\n"
        "DROP TABLE parent;\n"
        "ALTER TABLE parent_rebuilt RENAME TO parent;\n"
        "PRAGMA foreign_keys=ON;"
    )
    db, svc = await _service()
    try:
        await svc._apply_migrations(tmp_path)
        row = await db.fetch_one("SELECT COUNT(*) AS n FROM child")
        assert row is not None and row["n"] == 1
        enforced = await db.fetch_one("PRAGMA foreign_keys")
        assert enforced is not None and enforced["foreign_keys"] == 1
    finally:
        await db.close()


async def test_the_real_migration_chain_applies_from_scratch():
    db, svc = await _service()
    try:
        await svc.initialize()
        expected = len(list(Path("src/multiplayer/migrations").glob("*.sql")))
        row = await db.fetch_one("SELECT COUNT(*) AS n FROM schema_migrations")
        assert row is not None and row["n"] == expected
    finally:
        await db.close()
