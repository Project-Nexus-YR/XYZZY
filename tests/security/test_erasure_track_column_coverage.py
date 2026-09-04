"""Round 4: stop relying on memory for "did we redact everything".

Rounds 1 to 3 found every gap this track has closed so far by a person
re-reading ``erase_user`` and guessing what else might carry personal text.
That works until it doesn't: nothing forces a fourth round, or a fifth, to
notice a brand new TEXT column a future migration adds. This file converts
"we hope we remembered everything" into "the test tells you when you didn't".

It introspects the live, fully-migrated schema with ``PRAGMA table_info`` and
cross-references every TEXT-affinity column it finds against
``erasure._COLUMN_CLASSIFICATION``, an explicit, hand-reviewed allowlist that
classifies every one of them as ``"redacted"``, ``"kept_by_ruling"``, or
``"not_user_authored"`` (see the constant's own docstring in
``services/erasure.py`` for what each bucket means and the detection rule
below). A column with no entry fails the test outright.

Detection rule: every table in ``sqlite_master`` except ``sqlite_%``
internals, FTS shadow tables (``%_fts``/``%_fts_%``), and
``schema_migrations`` (migration bookkeeping, not application data); every
column of one of those tables whose declared type is exactly ``TEXT``.
Checked once, empirically, against this schema: every column here declares an
explicit type (none is blank), and the only non-TEXT types in use are
INTEGER, REAL, and one BLOB (``attachments.data``, the attachment bytes
themselves, handled separately by ``AttachmentRepo.erase_in_transaction`` and
irrelevant to a TEXT-only sweep).
"""

from __future__ import annotations

from multiplayer.db.connection import Database
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.erasure import _COLUMN_CLASSIFICATION
from multiplayer.services.service import MultiplayerService

_VALID_CLASSIFICATIONS = {"redacted", "kept_by_ruling", "not_user_authored"}


async def _text_columns(db: Database) -> set[tuple[str, str]]:
    tables = await db.fetch_all(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    columns: set[tuple[str, str]] = set()
    for row in tables:
        name = str(row["name"])
        if name.endswith("_fts") or "_fts_" in name or name == "schema_migrations":
            continue
        for col in await db.fetch_all(f"PRAGMA table_info({name})"):
            if str(col["type"] or "").upper() == "TEXT":
                columns.add((name, str(col["name"])))
    return columns


async def test_every_text_column_is_classified():
    """The whole point: a column introspection finds with no allowlist entry
    fails here, rather than silently shipping unredacted."""
    db = Database(":memory:")
    await db.connect()
    try:
        svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset())
        await svc.initialize()
        found = await _text_columns(db)
        allowlisted = set(_COLUMN_CLASSIFICATION.keys())

        unclassified = found - allowlisted
        assert unclassified == set(), (
            f"schema has TEXT column(s) with no entry in "
            f"erasure._COLUMN_CLASSIFICATION: {sorted(unclassified)}. Add each one "
            f"as 'redacted', 'kept_by_ruling', or 'not_user_authored', with a "
            f"reason, before this test may pass."
        )

        # The reverse direction matters too: an allowlist entry for a column a
        # migration renamed or dropped is a stale claim about a schema that no
        # longer exists, not a harmless leftover.
        stale = allowlisted - found
        assert stale == set(), (
            f"erasure._COLUMN_CLASSIFICATION names column(s) the live schema no "
            f"longer has: {sorted(stale)}. Remove them or fix the rename."
        )
    finally:
        await db.close()


def test_every_classification_value_is_one_of_the_three_buckets():
    """Guards the allowlist itself against a typo'd bucket name silently
    turning into a fourth, unrecognised classification."""
    bad = {
        key: value
        for key, value in _COLUMN_CLASSIFICATION.items()
        if value not in _VALID_CLASSIFICATIONS
    }
    assert bad == {}, f"non-canonical classification value(s): {bad}"
