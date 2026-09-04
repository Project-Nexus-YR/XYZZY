"""Tamper-evident hash chain over the canonical room event log.

Every appended event commits to the one before it: event_hash covers the
event's stored fields plus the previous event's hash, so editing, removing or
reordering a row breaks every hash after it. Truncating the tail is caught by
the room's sequence counter, which the log must reach. The chain makes
tampering evident, not impossible — an attacker with the database can rewrite
history wholesale; they cannot rewrite it quietly.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ..db.connection import Database

GENESIS_HASH = ""


def _redaction_marker(payload_json: str) -> str | None:
    """The redaction id a marker payload names, or None for an ordinary payload.

    A marker is exactly ``{"redacted": true, "redaction_id": "..."}``: any other
    shape, including one that merely happens to carry a ``redacted`` key, is left
    to the normal hash check rather than treated as an erasure.
    """
    try:
        parsed: Any = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict) or parsed.get("redacted") is not True:
        return None
    redaction_id = parsed.get("redaction_id")
    return redaction_id if isinstance(redaction_id, str) and redaction_id else None


def _announced_redaction_ids(event_type: str, payload_json: str) -> set[str]:
    """The redaction ids one event.redacted event names, empty for any other event."""
    if event_type != "event.redacted":
        return set()
    try:
        parsed: Any = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError):
        return set()
    if not isinstance(parsed, dict):
        return set()
    ids = parsed.get("redaction_ids")
    if not isinstance(ids, list):
        return set()
    return {item for item in ids if isinstance(item, str)}


def event_chain_hash(
    prev_hash: str,
    event_id: str,
    room_id: str,
    sequence: int,
    event_type: str,
    payload: str,
    actor_id: str,
    actor_type: str,
    timestamp: str,
    schema_version: int,
) -> str:
    """Hash an event's stored fields onto the chain that precedes it.

    The payload is hashed exactly as stored, so verification never depends on
    re-serialising a dict the same way twice.
    """
    material = json.dumps(
        [
            prev_hash,
            event_id,
            room_id,
            sequence,
            event_type,
            payload,
            actor_id,
            actor_type,
            timestamp,
            schema_version,
        ],
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ChainBreak:
    room_id: str
    sequence: int
    event_id: str
    reason: str


async def verify_event_chain(
    db: Database, room_id: str | None = None
) -> tuple[int, list[ChainBreak]]:
    """Recompute every room's chain and report the first stored divergence.

    One break per room: everything after a divergence is untrustworthy anyway,
    so later mismatches in the same room are consequences, not findings. When
    ``room_id`` is given, only that room's chain is hashed and only that
    room's tail is checked; every other room is left alone.
    """
    breaks: list[ChainBreak] = []
    verified = 0
    if room_id is not None:
        counter_rows = await db.fetch_all(
            "SELECT room_id, seq FROM room_sequences WHERE room_id = ?", (room_id,)
        )
        event_room_rows = await db.fetch_all(
            "SELECT DISTINCT room_id FROM room_events WHERE room_id = ?", (room_id,)
        )
    else:
        counter_rows = await db.fetch_all(
            "SELECT room_id, seq FROM room_sequences ORDER BY room_id"
        )
        event_room_rows = await db.fetch_all("SELECT DISTINCT room_id FROM room_events")
    counters_by_room = {str(row["room_id"]): int(row["seq"]) for row in counter_rows}
    room_ids = sorted(set(counters_by_room) | {str(row["room_id"]) for row in event_room_rows})
    for room_id in room_ids:
        rows = await db.fetch_all(
            "SELECT event_id, sequence, event_type, payload, actor_id, actor_type, "
            "timestamp, schema_version, prev_hash, event_hash "
            "FROM room_events WHERE room_id = ? ORDER BY sequence",
            (room_id,),
        )
        prev_hash = GENESIS_HASH
        last_sequence = 0
        broken = False
        # A redaction row is announced by a later event.redacted event in the same
        # room; until that event is met, its redaction id waits here, keyed to the
        # row that named it so an unresolved redaction can be reported at that row.
        pending_redactions: dict[str, tuple[int, str]] = {}
        for row in rows:
            sequence = int(row["sequence"])
            event_id = str(row["event_id"])
            payload_json = str(row["payload"])
            if sequence != last_sequence + 1:
                reason = f"sequence {last_sequence + 1} is missing"
                breaks.append(ChainBreak(room_id, sequence, event_id, reason))
                broken = True
                break
            stored_prev = str(row["prev_hash"]) if row["prev_hash"] is not None else None
            if stored_prev != prev_hash:
                breaks.append(
                    ChainBreak(room_id, sequence, event_id, "stored prev_hash breaks the chain")
                )
                broken = True
                break
            marker_redaction_id = _redaction_marker(payload_json)
            if marker_redaction_id is not None:
                redaction_row = await db.fetch_one(
                    "SELECT original_event_hash FROM event_redactions "
                    "WHERE redaction_id = ? AND event_id = ?",
                    (marker_redaction_id, event_id),
                )
                if redaction_row is None:
                    breaks.append(
                        ChainBreak(
                            room_id,
                            sequence,
                            event_id,
                            "redaction marker has no matching event_redactions row",
                        )
                    )
                    broken = True
                    break
                original_hash = str(redaction_row["original_event_hash"])
                # The row's own event_hash and prev_hash were never rewritten, so the
                # recorded original hash must still be exactly what is stored here.
                # A mismatch means event_redactions was tampered with after the fact.
                if original_hash != str(row["event_hash"]):
                    breaks.append(
                        ChainBreak(
                            room_id,
                            sequence,
                            event_id,
                            "original_event_hash does not match the row's own stored hash",
                        )
                    )
                    broken = True
                    break
                expected = original_hash
                pending_redactions[marker_redaction_id] = (sequence, event_id)
            else:
                expected = event_chain_hash(
                    prev_hash,
                    event_id,
                    room_id,
                    sequence,
                    str(row["event_type"]),
                    payload_json,
                    str(row["actor_id"]),
                    str(row["actor_type"]),
                    str(row["timestamp"]),
                    int(row["schema_version"]),
                )
                if row["event_hash"] != expected:
                    reason = "stored hash does not match the recomputed chain"
                    breaks.append(ChainBreak(room_id, sequence, event_id, reason))
                    broken = True
                    break
            for announced in _announced_redaction_ids(str(row["event_type"]), payload_json):
                pending_redactions.pop(announced, None)
            prev_hash = expected
            last_sequence = sequence
            verified += 1
        if broken:
            continue
        if pending_redactions:
            first_sequence, first_event_id = min(pending_redactions.values())
            breaks.append(
                ChainBreak(
                    room_id,
                    first_sequence,
                    first_event_id,
                    "no later event.redacted event names this redaction",
                )
            )
            continue
        counter_seq = counters_by_room.get(room_id)
        if counter_seq is None:
            if last_sequence > 0:
                breaks.append(
                    ChainBreak(
                        room_id,
                        last_sequence,
                        rows[-1]["event_id"] if rows else "",
                        "the room's sequence counter is missing",
                    )
                )
        elif counter_seq != last_sequence:
            breaks.append(
                ChainBreak(
                    room_id,
                    last_sequence,
                    rows[-1]["event_id"] if rows else "",
                    f"log ends at {last_sequence} but the room counter reached {counter_seq}",
                )
            )
    return verified, breaks
