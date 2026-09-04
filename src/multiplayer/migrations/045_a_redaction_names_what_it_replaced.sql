-- A redaction names what it replaced.
--
-- The event log is hash chained and append only, by design: nothing here may
-- delete or rewrite a row without breaking every hash after it, which is the
-- whole point. Erasing a user's authored content therefore cannot mean editing
-- a row and moving on: it means replacing the row's payload with a marker, and
-- writing down, beside it, what the marker stands in for.
--
-- event_redactions is that record. original_event_hash is the room_events row's
-- own event_hash from before its payload was touched, so a verifier can still
-- account for that row's place in the chain without ever reading the content it
-- once carried: the row's event_hash and prev_hash columns are never rewritten,
-- only its payload. redacted_at, reason, and actor_id say when this happened,
-- why, and under whose authority, so an auditor sees that something was removed
-- and by whom, never what it was.
--
-- One redaction row exists per redacted event, and event_id is unique so a
-- second erasure pass over an already redacted event changes nothing: the
-- operator CLI treats a marker payload it meets again as already done.

CREATE TABLE event_redactions (
    redaction_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    original_event_hash TEXT NOT NULL,
    redacted_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor_id TEXT NOT NULL
);

CREATE INDEX idx_event_redactions_room ON event_redactions(room_id);
