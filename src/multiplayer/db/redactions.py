"""What a redacted event's marker stands in for: the original hash, and the
header snapshot that binds the marker row to the record naming it.

A redaction record used to carry only ``original_event_hash``, the redacted
row's own stored hash from before its payload was touched. That is enough to
tell whether the row's *hash* still matches what it was, but the row's other
columns (event_type, actor_id, actor_type, timestamp, schema_version,
sequence, prev_hash) were never re-checked against anything, because a marker
row skips the ordinary recompute-and-compare rule entirely (see
``security/audit.py::verify_event_chain``). Recording those seven fields here,
plus a hash over them, gives the verifier something to recompute the row's
*live* header against: a rewrite of any one of them now shows up as a header
mismatch instead of verifying clean.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .connection import Database, serialize_datetime


class EventRedactionRepo:
    """What a redacted event's marker stands in for: the original hash, and why."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def create_in_transaction(
        self,
        redaction_id: str,
        event_id: str,
        room_id: str,
        original_event_hash: str,
        redacted_at: datetime,
        reason: str,
        actor_id: str,
        *,
        header_event_type: str,
        header_actor_id: str,
        header_actor_type: str,
        header_timestamp: str,
        header_schema_version: int,
        header_sequence: int,
        header_prev_hash: str,
        header_hash: str,
    ) -> None:
        if not self.db.owns_current_transaction:
            raise RuntimeError("redaction record requires transaction ownership")
        await self.db.execute(
            "INSERT INTO event_redactions(redaction_id, event_id, room_id, "
            "original_event_hash, redacted_at, reason, actor_id, "
            "header_event_type, header_actor_id, header_actor_type, header_timestamp, "
            "header_schema_version, header_sequence, header_prev_hash, header_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                redaction_id,
                event_id,
                room_id,
                original_event_hash,
                serialize_datetime(redacted_at),
                reason,
                actor_id,
                header_event_type,
                header_actor_id,
                header_actor_type,
                header_timestamp,
                header_schema_version,
                header_sequence,
                header_prev_hash,
                header_hash,
            ),
        )

    async def get_by_event_id(self, event_id: str) -> dict[str, Any] | None:
        return await self.db.fetch_one(
            "SELECT * FROM event_redactions WHERE event_id = ?", (event_id,)
        )

    async def list_by_room(self, room_id: str) -> list[dict[str, Any]]:
        return await self.db.fetch_all(
            "SELECT * FROM event_redactions WHERE room_id = ? ORDER BY redacted_at", (room_id,)
        )
