"""``manage.py db backup`` writes a consistent snapshot, unlike a plain file
copy of a WAL mode database."""

import sqlite3

import pytest

from multiplayer.db.connection import Database
from multiplayer.manage import backup_database, open_database


@pytest.mark.asyncio
async def test_db_backup_produces_a_readable_snapshot(tmp_path):
    src_path = tmp_path / "app.db"
    dest_path = tmp_path / "backup.db"

    db = await open_database(str(src_path))
    try:
        await backup_database(db, str(dest_path))
    finally:
        await db.close()

    assert dest_path.exists()
    backup = Database(str(dest_path))
    await backup.connect()
    try:
        row = await backup.fetch_one("SELECT COUNT(*) AS n FROM schema_migrations")
        assert row is not None and row["n"] > 0
    finally:
        await backup.close()

    # A backup taken this way must also be openable by plain sqlite3, the
    # tool an operator reaches for to inspect it.
    conn = sqlite3.connect(str(dest_path))
    try:
        cursor = conn.execute("SELECT COUNT(*) FROM schema_migrations")
        assert cursor.fetchone()[0] > 0
    finally:
        conn.close()
