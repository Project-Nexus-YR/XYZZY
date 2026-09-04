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

from ..db.connection import Database

GENESIS_HASH = ""


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
        for row in rows:
            sequence = int(row["sequence"])
            event_id = str(row["event_id"])
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
            expected = event_chain_hash(
                prev_hash,
                event_id,
                room_id,
                sequence,
                str(row["event_type"]),
                str(row["payload"]),
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
            prev_hash = expected
            last_sequence = sequence
            verified += 1
        if broken:
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
