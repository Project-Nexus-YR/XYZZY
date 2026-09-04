"""Finding 22: a read-only manage.py verb (``db backup``, ``token list``,
``audit verify``) must not migrate the live database on its way to reading
it, and must refuse with a clear message when the schema is behind instead
of silently upgrading it. Only the server's own startup and the explicit
``db migrate`` verb apply a migration.

Finding 23: a database migrated by a newer build must refuse to open under
an older checkout rather than boot cleanly and run old code against schema
it never saw.
"""

from __future__ import annotations

import sqlite3

import pytest

from multiplayer.manage import (
    backup_database,
    open_database,
    open_database_readonly,
)


async def _old_checkout_db(tmp_path, stop_after: int) -> str:
    """A database migrated only partway through the real chain, as an older
    checkout would have left it."""
    from multiplayer.services import bootstrap as bootstrap_module

    real_glob = bootstrap_module.Path.glob

    def truncated_glob(self, pattern):  # type: ignore[no-untyped-def]
        files = sorted(real_glob(self, pattern))
        return files[:stop_after]

    db_path = tmp_path / "old.db"
    import multiplayer.manage as manage_module

    monkeypatch_target = bootstrap_module.Path
    original = monkeypatch_target.glob
    monkeypatch_target.glob = truncated_glob  # type: ignore[method-assign]
    try:
        db = await manage_module.open_database(str(db_path))
        await db.close()
    finally:
        monkeypatch_target.glob = original  # type: ignore[method-assign]
    return str(db_path)


@pytest.mark.asyncio
async def test_db_backup_refuses_to_migrate_an_old_checkouts_database(tmp_path) -> None:
    old_path = await _old_checkout_db(tmp_path, stop_after=41)
    before = sqlite3.connect(old_path)
    try:
        applied_before = before.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    finally:
        before.close()

    with pytest.raises(ValueError, match="behind this checkout"):
        await open_database_readonly(old_path)

    after = sqlite3.connect(old_path)
    try:
        applied_after = after.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    finally:
        after.close()
    # Refused before writing anything: the count a plain read verb would have
    # migrated past is exactly what it was before the refusal.
    assert applied_after == applied_before


@pytest.mark.asyncio
async def test_db_migrate_applies_pending_migrations_so_backup_then_succeeds(tmp_path) -> None:
    old_path = await _old_checkout_db(tmp_path, stop_after=41)

    migrated = await open_database(old_path)
    await migrated.close()

    db = await open_database_readonly(old_path)
    try:
        await backup_database(db, str(tmp_path / "backup.db"))
    finally:
        await db.close()
    assert (tmp_path / "backup.db").exists()


@pytest.mark.asyncio
async def test_a_database_migrated_by_a_newer_build_refuses_to_open(tmp_path) -> None:
    db_path = tmp_path / "app.db"
    db = await open_database(str(db_path))
    await db.execute(
        "INSERT INTO schema_migrations(name, applied_at) VALUES (?, datetime('now'))",
        ("099_from_a_newer_release.sql",),
    )
    await db.commit()
    await db.close()

    with pytest.raises(RuntimeError, match="newer build"):
        await open_database(str(db_path))


@pytest.mark.asyncio
async def test_open_database_readonly_lets_an_up_to_date_database_through(tmp_path) -> None:
    db_path = tmp_path / "app.db"
    migrated = await open_database(str(db_path))
    await migrated.close()

    db = await open_database_readonly(str(db_path))
    try:
        row = await db.fetch_one("SELECT COUNT(*) AS n FROM schema_migrations")
        assert row is not None and row["n"] > 0
    finally:
        await db.close()
