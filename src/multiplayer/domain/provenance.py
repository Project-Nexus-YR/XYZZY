"""Canonical hashing for immutable artifact-version provenance."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from unicodedata import normalize


def normalize_provenance_author(value: str) -> str:
    """Preserve case-sensitive identity while removing representation-only variance."""
    return normalize("NFC", value.strip())


def normalize_provenance_timestamp(value: datetime | str) -> str:
    """Canonicalize publication time to UTC with fixed microsecond precision."""
    timestamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC).isoformat(timespec="microseconds")


def calculate_artifact_provenance_hash(
    *,
    version_id: str,
    artifact_id: str,
    version_number: int,
    content_hash: str,
    created_by: str,
    created_at: datetime | str,
    claims: list[dict[str, Any]],
) -> str:
    """Bind a canonical provenance snapshot to one exact artifact version and content."""
    ordered_claims = sorted(
        claims,
        key=lambda claim: (int(claim["ordinal"]), str(claim["output_id"])),
    )
    envelope = {
        "artifact_id": artifact_id,
        "claims": ordered_claims,
        "content_hash": content_hash,
        "created_at": normalize_provenance_timestamp(created_at),
        "created_by": normalize_provenance_author(created_by),
        "schema": "xyzzy.artifact-provenance.v2",
        "version_id": version_id,
        "version_number": version_number,
    }
    canonical = json.dumps(
        envelope,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
