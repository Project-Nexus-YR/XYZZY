-- A caller's first_acted_at was written in two shapes: strftime's own
-- '...Z' from the two triggers 029 added, and Python's isoformat '...+00:00'
-- from repositories.py's own record_caller. No reader orders by the column
-- today, but 'Z' sorts after '+' as plain text, so the column could not be
-- ordered or compared correctly the day a feature finally needed it, and
-- deserialize_datetime would parse the two at different sub-second
-- precision. The triggers are respelled to match the repository's own
-- shape exactly, offset included: strftime's own %f gives three digits of
-- millisecond precision, not the six-digit microseconds isoformat() writes,
-- and a shorter, unpadded fraction sorts before a longer one that shares
-- its own digits as a prefix ('...176+00:00' before '...176872+00:00', text
-- sorted, even though the first is the later of the two). Three zero
-- digits pad it to the same width without claiming a precision SQLite's
-- clock does not have.

DROP TRIGGER agent_runs_record_launch_caller;

CREATE TRIGGER agent_runs_record_launch_caller
AFTER INSERT ON agent_runs
WHEN NEW.acting_user_id <> '' AND NEW.acting_user_id <> NEW.authorized_by
BEGIN
    INSERT OR IGNORE INTO execution_callers(execution_id, caller_id, first_acted_at)
    VALUES (NEW.execution_id, NEW.acting_user_id, strftime('%Y-%m-%dT%H:%M:%f000', 'now') || '+00:00');
END;

DROP TRIGGER agent_runs_record_acting_caller;

CREATE TRIGGER agent_runs_record_acting_caller
AFTER UPDATE OF acting_user_id ON agent_runs
WHEN NEW.acting_user_id <> ''
BEGIN
    INSERT OR IGNORE INTO execution_callers(execution_id, caller_id, first_acted_at)
    VALUES (NEW.execution_id, NEW.acting_user_id, strftime('%Y-%m-%dT%H:%M:%f000', 'now') || '+00:00');
END;
