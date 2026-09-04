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

    A marker is exactly ``{"redacted": true, "redaction_id": "..."}``, two keys
    and no more: any other shape, including one that merely happens to carry a
    ``redacted`` key, or the same two keys plus a third, is left to the normal
    hash check rather than treated as an erasure.
    """
    try:
        parsed: Any = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict) or set(parsed) != {"redacted", "redaction_id"}:
        return None
    if parsed.get("redacted") is not True:
        return None
    redaction_id = parsed.get("redaction_id")
    return redaction_id if isinstance(redaction_id, str) and redaction_id else None


@dataclass(frozen=True, slots=True)
class _RedactionAnnouncement:
    redaction_id: str
    header_hash: str
    original_event_hash: str


def _announced_redactions(event_type: str, payload_json: str) -> list[_RedactionAnnouncement]:
    """The redactions one event.redacted event names, empty for any other event.

    Each entry carries the header_hash and original_event_hash the announcer
    claims for that redaction id, so the caller can check the claim against
    the event_redactions row it should match, not just that some id was named.
    """
    if event_type != "event.redacted":
        return []
    try:
        parsed: Any = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, dict):
        return []
    entries = parsed.get("redactions")
    if not isinstance(entries, list):
        return []
    result: list[_RedactionAnnouncement] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        redaction_id = entry.get("redaction_id")
        header_hash = entry.get("header_hash")
        original_event_hash = entry.get("original_event_hash")
        if (
            isinstance(redaction_id, str)
            and redaction_id
            and isinstance(header_hash, str)
            and isinstance(original_event_hash, str)
        ):
            result.append(_RedactionAnnouncement(redaction_id, header_hash, original_event_hash))
    return result


def header_snapshot_hash(
    event_type: str,
    actor_id: str,
    actor_type: str,
    timestamp: str,
    schema_version: int,
    sequence: int,
    prev_hash: str,
) -> str:
    """Hash the seven header fields a redaction record snapshots at creation time.

    A marker row's payload can no longer be recomputed into anything (that is
    the point of redacting it), so nothing else in this module re-derives
    event_hash for a marker row. This gives verify_event_chain something else
    to recompute instead: the row's live header, checked against the snapshot
    ``db/redactions.py`` took the moment it replaced the payload. A rewrite of
    event_type, actor_id, actor_type, timestamp or schema_version on a marker
    row now changes this hash, so it stops matching the stored snapshot.
    """
    material = json.dumps(
        [event_type, actor_id, actor_type, timestamp, schema_version, sequence, prev_hash],
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


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
        # The header_hash/original_event_hash carried alongside let the
        # announcement itself be checked against the record it claims to name,
        # not just that some matching id showed up eventually.
        pending_redactions: dict[str, tuple[int, str, str, str]] = {}
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
                    "SELECT original_event_hash, header_event_type, header_actor_id, "
                    "header_actor_type, header_timestamp, header_schema_version, "
                    "header_sequence, header_prev_hash, header_hash FROM event_redactions "
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
                # The row's own hash never moving is not enough on its own: nothing
                # above recomputes a hash over event_type/actor_id/actor_type/
                # timestamp/schema_version for a marker row, so those could be
                # rewritten and event_hash would still equal original_event_hash.
                # Recompute the header snapshot from the row's LIVE columns and
                # compare it to what was recorded the moment this row was redacted.
                live_header_hash = header_snapshot_hash(
                    str(row["event_type"]),
                    str(row["actor_id"]),
                    str(row["actor_type"]),
                    str(row["timestamp"]),
                    int(row["schema_version"]),
                    sequence,
                    stored_prev if stored_prev is not None else GENESIS_HASH,
                )
                if live_header_hash != str(redaction_row["header_hash"]):
                    breaks.append(
                        ChainBreak(
                            room_id,
                            sequence,
                            event_id,
                            "the row's header no longer matches the snapshot taken at redaction",
                        )
                    )
                    broken = True
                    break
                expected = original_hash
                pending_redactions[marker_redaction_id] = (
                    sequence,
                    event_id,
                    str(redaction_row["header_hash"]),
                    original_hash,
                )
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
            announcement_break: str | None = None
            for announced in _announced_redactions(str(row["event_type"]), payload_json):
                pending = pending_redactions.get(announced.redaction_id)
                if pending is None:
                    # Names a redaction id this room never marked, or already
                    # resolved: not a defect this loop tracks (the redaction row
                    # itself is checked directly, above, whenever its marker row
                    # is reached).
                    continue
                _, _, expected_header_hash, expected_original_hash = pending
                if (
                    announced.header_hash != expected_header_hash
                    or announced.original_event_hash != expected_original_hash
                ):
                    announcement_break = (
                        "event.redacted names a redaction with a header hash or "
                        "original event hash that does not match its record"
                    )
                    break
                pending_redactions.pop(announced.redaction_id, None)
            if announcement_break is not None:
                breaks.append(ChainBreak(room_id, sequence, event_id, announcement_break))
                broken = True
                break
            prev_hash = expected
            last_sequence = sequence
            verified += 1
        if broken:
            continue
        if pending_redactions:
            first_sequence, first_event_id, _, _ = min(pending_redactions.values())
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
