-- A redaction record used to trust one number: the redacted row's own
-- event_hash, carried forward unchanged as original_event_hash. That number
-- proves the row's hash column was never rewritten; it proves nothing about
-- the row's other columns, since a marker row skips the ordinary
-- recompute-and-compare rule verify_event_chain applies to every other row.
-- event_type, actor_id, actor_type, timestamp and schema_version could be
-- rewritten on a marker row and event_hash would still equal
-- original_event_hash, because nothing recomputes a hash over those fields
-- for a marker row at all.
--
-- This gives the record a header snapshot of those seven fields (event_type,
-- actor_id, actor_type, timestamp, schema_version, sequence, prev_hash) plus
-- a hash over them, so verify_event_chain can recompute that hash from the
-- row's live columns and compare it to what was recorded at redaction time. A
-- rewrite of any one of them now surfaces as a header mismatch. There are no
-- released databases with event_redactions rows to migrate: the feature
-- shipped unreleased, so every existing row (there are none) would need a
-- backfill this migration does not attempt.

ALTER TABLE event_redactions ADD COLUMN header_event_type TEXT NOT NULL DEFAULT '';
ALTER TABLE event_redactions ADD COLUMN header_actor_id TEXT NOT NULL DEFAULT '';
ALTER TABLE event_redactions ADD COLUMN header_actor_type TEXT NOT NULL DEFAULT '';
ALTER TABLE event_redactions ADD COLUMN header_timestamp TEXT NOT NULL DEFAULT '';
ALTER TABLE event_redactions ADD COLUMN header_schema_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE event_redactions ADD COLUMN header_sequence INTEGER NOT NULL DEFAULT 0;
ALTER TABLE event_redactions ADD COLUMN header_prev_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE event_redactions ADD COLUMN header_hash TEXT NOT NULL DEFAULT '';

-- Defence in depth: every other evidence table in this schema (003's
-- append-only tables, 004 through 049's narrowing triggers) refuses a plain
-- UPDATE or DELETE at the SQL level. room_events, room_sequences and
-- event_redactions never got that, so an application bug, a stray sqlite3
-- session, or a script running against the live file could rewrite or drop a
-- chained row with nothing noticing until the next manual `audit verify`.
-- The chain already detects each of these edits once made (a payload
-- rewrite fails the recomputed hash, a row delete leaves a sequence gap, a
-- counter delete or rewind is its own break); this closes the front door
-- rather than a gap in what verify_event_chain catches. A file-level
-- attacker (one who can open the database directly, outside this
-- connection) can still `DROP TRIGGER` first and tamper freely: this is
-- defence in depth against an in-process bug or a live session on this
-- connection, not a defence against that attacker, the same limit finding
-- #48's own audit named.
--
-- Two carve-outs, both already load-bearing elsewhere in this schema:
--   * payload is not in the immutable column list, so
--     EventRepo.redact_payload_in_transaction's UPDATE stays legal.
--   * prev_hash/event_hash may move from NULL to a value once (033's startup
--     backfill for rows written before the chain existed), but never again
--     once set.

CREATE TRIGGER room_events_reject_delete
BEFORE DELETE ON room_events
BEGIN
    SELECT RAISE(ABORT, 'the event log is append-only');
END;

CREATE TRIGGER room_events_reject_identity_update
BEFORE UPDATE OF event_id, room_id, sequence, event_type, actor_id, actor_type,
    timestamp, schema_version ON room_events
BEGIN
    SELECT RAISE(ABORT, 'an event''s identity and header are immutable');
END;

CREATE TRIGGER room_events_reject_hash_rewrite
BEFORE UPDATE OF prev_hash, event_hash ON room_events
WHEN OLD.event_hash IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'a chained hash may not be rewritten once set');
END;

CREATE TRIGGER event_redactions_reject_update
BEFORE UPDATE ON event_redactions
BEGIN
    SELECT RAISE(ABORT, 'a redaction record is append-only');
END;

CREATE TRIGGER event_redactions_reject_delete
BEFORE DELETE ON event_redactions
BEGIN
    SELECT RAISE(ABORT, 'a redaction record is append-only');
END;

CREATE TRIGGER room_sequences_reject_rewind
BEFORE UPDATE ON room_sequences
WHEN NEW.seq <= OLD.seq
BEGIN
    SELECT RAISE(ABORT, 'a room sequence counter only moves forward');
END;

CREATE TRIGGER room_sequences_reject_delete
BEFORE DELETE ON room_sequences
BEGIN
    SELECT RAISE(ABORT, 'a room sequence counter is never removed');
END;

-- messages.content has its own copy of a message's text, made at send time
-- and read by every listing that shows one; nothing before this narrowed who
-- may overwrite it. The one legitimate path is MessageRepo's own erasure
-- write, which always writes the exact two-key marker shape
-- json.dumps({"redacted": True, "redaction_id": ...}) produces (see
-- security/audit.py's _redaction_marker, which now requires that same exact
-- shape). Anything else is a plain edit of a person's words and is refused.
CREATE TRIGGER messages_reject_content_update
BEFORE UPDATE OF content ON messages
WHEN NEW.content NOT GLOB '{"redacted": true, "redaction_id": "*"}'
BEGIN
    SELECT RAISE(ABORT, 'message content is immutable except through redaction');
END;
