"""Finding 32: the foreign_keys=OFF hoist is a case and whitespace insensitive
match against the migration body with comments stripped, not a raw substring.

The old check, ``"foreign_keys=OFF" in body``, missed a respelled pragma
(lower case, extra spaces) and fired on a comment that merely mentioned the
literal. Both are proven directly against ``_apply_migrations``: a migration
using the sanctioned rebuild recipe with a respelled pragma must still apply
(it needs foreign key enforcement lifted to survive the table swap under a
live foreign key), and a migration that only names the literal in a comment
must not have it lifted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService

# The sanctioned rebuild recipe: a table with a live foreign key reference from
# another table can only be dropped and replaced while foreign_keys is OFF.
_SETUP_SQL = (
    "CREATE TABLE parent(id INTEGER PRIMARY KEY, name TEXT);\n"
    "INSERT INTO parent(id, name) VALUES (1, 'a');\n"
    "CREATE TABLE child(id INTEGER PRIMARY KEY, "
    "parent_id INTEGER NOT NULL REFERENCES parent(id));\n"
    "INSERT INTO child(id, parent_id) VALUES (1, 1);\n"
)
_REBUILD_SQL = (
    "CREATE TABLE parent_new(id INTEGER PRIMARY KEY, name TEXT, extra TEXT DEFAULT '');\n"
    "INSERT INTO parent_new(id, name) SELECT id, name FROM parent;\n"
    "DROP TABLE parent;\n"
    "ALTER TABLE parent_new RENAME TO parent;\n"
)


@pytest.fixture
async def bare_service(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset())
    yield svc
    await db.close()


@pytest.mark.asyncio
async def test_a_respelled_pragma_still_lifts_enforcement(bare_service, tmp_path: Path) -> None:
    (tmp_path / "001_setup.sql").write_text(_SETUP_SQL)
    # Lower case, extra spaces around the equals sign: the old raw substring
    # match on the exact literal "foreign_keys=OFF" would have missed this.
    (tmp_path / "002_rebuild.sql").write_text("pragma  foreign_keys  =  off;\n" + _REBUILD_SQL)

    await bare_service._apply_migrations(tmp_path)

    row = await bare_service.db.fetch_one("SELECT extra FROM parent WHERE id = 1")
    assert row is not None and row["extra"] == ""


@pytest.mark.asyncio
async def test_a_comment_naming_the_literal_does_not_lift_enforcement(
    bare_service, tmp_path: Path
) -> None:
    (tmp_path / "001_setup.sql").write_text(_SETUP_SQL)
    # The pragma is only named in a comment, never issued. Enforcement must
    # stay on, so the rebuild that needs it lifted fails as it would for
    # anyone who forgot the pragma.
    (tmp_path / "002_rebuild.sql").write_text(
        "-- do not set foreign_keys=OFF here, this migration does not need it\n" + _REBUILD_SQL
    )

    with pytest.raises(RuntimeError, match="002_rebuild.sql failed"):
        await bare_service._apply_migrations(tmp_path)
