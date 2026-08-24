-- A durable home for the one principal a run could not name.
--
-- 027 put the steerers on the authorization and filled them from durable rows, and
-- the claim was that there was nowhere left to put the mistake. There was one more
-- place: the acting caller. A run records who authorized it (executions.authorized_by,
-- immutable) and who has steered it (execution_interventions.intervened_by,
-- append-only), but the human who is DRIVING it had only agent_runs.acting_user_id —
-- a single mutable column documented as "initiator, then last caller". Every advance
-- overwrites it, so by the time a reviewer released a parked tool call it read as the
-- run's own principal, and the caller who actually asked for that call had left no
-- trace the authorization could read. Reproduced: a delegate holding only `writing`
-- steps somebody else's run, the gateway correctly parks task.create at approval, the
-- delegate is narrowed to nothing or removed from the room, the principal approves,
-- and the task is written on a grant nobody held any more.
--
-- A set cannot live in a column that holds the last writer, so the callers of a run
-- get a table of their own, append-only like the steers beside them. It is not a copy
-- of a capability input — 027 dropped suspended_turns.steerers for being exactly that
-- — it is the only record of a fact nothing else was keeping.
--
-- The triggers below are what make it a record rather than a discipline. A run cannot
-- be advanced without naming the human advancing it, and naming one writes it down in
-- the same statement, so a path written next year that moves a run on somebody's
-- behalf records that somebody whether or not its author has heard of any of this.

CREATE TABLE IF NOT EXISTS execution_callers (
    execution_id TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE RESTRICT,
    caller_id TEXT NOT NULL,
    first_acted_at TEXT NOT NULL,
    PRIMARY KEY (execution_id, caller_id)
);

-- A bound that can be edited away is not a bound, and a caller who can be deleted
-- from the record is a caller the next derivation will not intersect.
CREATE TRIGGER execution_callers_are_written_once
BEFORE UPDATE ON execution_callers
BEGIN
    SELECT RAISE(ABORT, 'a caller of a run is an audit record and is never rewritten');
END;

CREATE TRIGGER execution_callers_are_never_deleted
BEFORE DELETE ON execution_callers
BEGIN
    SELECT RAISE(ABORT, 'a caller of a run is an audit record and is never deleted');
END;

-- The best evidence available at upgrade time: whoever a run last recorded as its
-- caller, and whoever a turn already parked at a reviewer was suspended under. The
-- second is the one the defect turned on, so it is backfilled rather than lost.
INSERT OR IGNORE INTO execution_callers(execution_id, caller_id, first_acted_at)
SELECT r.execution_id, r.acting_user_id, r.created_at
FROM agent_runs r
WHERE r.acting_user_id <> ''
  AND EXISTS (SELECT 1 FROM executions e WHERE e.execution_id = r.execution_id);

INSERT OR IGNORE INTO execution_callers(execution_id, caller_id, first_acted_at)
SELECT s.execution_id, s.acting_as, s.suspended_at
FROM suspended_turns s
WHERE s.acting_as <> ''
  AND EXISTS (SELECT 1 FROM executions e WHERE e.execution_id = s.execution_id);

-- Launching a run is acting on it. The authorizing principal is read from
-- executions.authorized_by, which is immutable, so it is not copied here; only a
-- caller who is somebody else is a fact this table is the sole keeper of.
CREATE TRIGGER agent_runs_record_launch_caller
AFTER INSERT ON agent_runs
WHEN NEW.acting_user_id <> '' AND NEW.acting_user_id <> NEW.authorized_by
BEGIN
    INSERT OR IGNORE INTO execution_callers(execution_id, caller_id, first_acted_at)
    VALUES (NEW.execution_id, NEW.acting_user_id, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
END;

-- Advancing one is too, and this is the statement the old column lost its history to.
CREATE TRIGGER agent_runs_record_acting_caller
AFTER UPDATE OF acting_user_id ON agent_runs
WHEN NEW.acting_user_id <> ''
BEGIN
    INSERT OR IGNORE INTO execution_callers(execution_id, caller_id, first_acted_at)
    VALUES (NEW.execution_id, NEW.acting_user_id, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
END;
