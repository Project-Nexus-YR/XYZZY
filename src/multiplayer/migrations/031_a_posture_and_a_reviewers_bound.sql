-- Two things a channel needs saying about it, and one reach taken back.
--
-- A channel could say what an agent may do — rooms.allowed_capabilities, one of the
-- five terms — and could not say what stops at a human. That was decided per tool at
-- write time by whoever registered it, and nothing above it could raise the bar for
-- one room. room_postures is that sentence: GUARDED is every room today, STRICT
-- pauses every call. It never touches what is permitted, only what pauses, so the
-- intersection is exactly as it was under either.
--
-- Declarations are rows, not a column on rooms, because "which rule governed this
-- action" has to be answerable afterwards from records that cannot have changed
-- since. A column would hold the current answer and no earlier one. Nothing derived
-- is stored anywhere: the posture in force for a room at any instant is the latest
-- declaration at or before it, read again at every decision.
--
-- The second table is the taking back. record_caller wrote a reviewer who released
-- one parked tool call into execution_callers, and everything in that table bounds
-- the whole run: an administrator scoped to retrieval who approved a single read
-- stripped writing from every later call of that run, turning calls that would have
-- paused into calls that were refused. It failed closed, so it was never a hole — it
-- was a reach, and one that teaches people not to answer approvals. A reviewer bounds
-- the call she released, so she is recorded against that call.

CREATE TABLE IF NOT EXISTS room_postures (
    declaration_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE RESTRICT,
    posture TEXT NOT NULL CHECK (posture IN ('GUARDED', 'STRICT')),
    declared_by TEXT NOT NULL,
    declared_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_room_postures_current
    ON room_postures(room_id, declared_at DESC);

-- A rule that can be edited after the fact is not evidence of what was in force.
CREATE TRIGGER room_postures_are_written_once
BEFORE UPDATE ON room_postures
BEGIN
    SELECT RAISE(ABORT, 'a posture declaration is an audit record and is never rewritten');
END;

CREATE TRIGGER room_postures_are_never_deleted
BEFORE DELETE ON room_postures
BEGIN
    SELECT RAISE(ABORT, 'a posture declaration is an audit record and is never deleted');
END;

-- INSERT OR REPLACE is a rewrite wearing an insert's clothes, and SQLite does not run
-- the delete trigger above for it unless recursive_triggers happens to be on. So the
-- refusal is stated on the insert, where the conflict is still visible.
CREATE TRIGGER room_postures_are_never_replaced
BEFORE INSERT ON room_postures
WHEN EXISTS (SELECT 1 FROM room_postures WHERE declaration_id = NEW.declaration_id)
BEGIN
    SELECT RAISE(ABORT, 'a posture declaration is an audit record and is never rewritten');
END;

-- Whoever released one parked call. Not a caller of the run: 029's table is read for
-- every call the run makes, and that is precisely the scope a reviewer does not have.
CREATE TABLE IF NOT EXISTS tool_request_reviewers (
    request_id TEXT NOT NULL REFERENCES tool_requests(request_id) ON DELETE RESTRICT,
    reviewer_id TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    PRIMARY KEY (request_id, reviewer_id)
);

CREATE TRIGGER tool_request_reviewers_are_written_once
BEFORE UPDATE ON tool_request_reviewers
BEGIN
    SELECT RAISE(ABORT, 'a reviewer of a call is an audit record and is never rewritten');
END;

CREATE TRIGGER tool_request_reviewers_are_never_deleted
BEFORE DELETE ON tool_request_reviewers
BEGIN
    SELECT RAISE(ABORT, 'a reviewer of a call is an audit record and is never deleted');
END;

-- No backfill, in either direction. No declaration rows means every existing channel
-- is GUARDED, which is byte-for-byte what it already was; and a reviewer already
-- written into execution_callers by the old path stays there, because narrowing a
-- historical run's bound after the fact would be rewriting what governed it.
