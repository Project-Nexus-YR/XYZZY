-- The authorizing human on the run itself, and the dispatcher that claimed it.
--
-- An AgentRun carries a human's authority, but the only record of that human was
-- input_data.requested_by: untyped metadata that execution time never consulted.
-- It re-derived the principal from branches.initiated_by instead, which for a
-- mention run is the agent's own id, so the run executed with the agent's terms
-- rather than the mentioner's. authorized_by is the authorization record itself:
-- required at insert, immutable afterwards, and the only input to the user term.
--
-- dispatch_claim names the dispatcher that took responsibility for a PENDING run.
-- It is the difference between a run orphaned by a crash and one another process
-- is actively dispatching, which the startup sweep could not tell apart and
-- settled either way.

ALTER TABLE executions ADD COLUMN authorized_by TEXT NOT NULL DEFAULT '';
ALTER TABLE executions ADD COLUMN dispatch_claim TEXT;

-- The best evidence available at upgrade time. A mention run recorded its
-- requester in metadata; any other run carries its branch's initiator, except
-- where that initiator is the agent itself, which names no human at all. A run
-- left empty here is honestly unknown, and an unknown principal lends nothing.
UPDATE executions
SET authorized_by = COALESCE(
    NULLIF(json_extract(input_data, '$.requested_by'), ''),
    (
        SELECT b.initiated_by
        FROM branches b
        WHERE b.branch_id = executions.branch_id
          AND b.initiated_by <> executions.agent_id
    ),
    ''
)
WHERE authorized_by = '';

CREATE INDEX IF NOT EXISTS idx_executions_unclaimed_pending
    ON executions(triggered_by, status, dispatch_claim);

CREATE TRIGGER IF NOT EXISTS executions_require_authorized_by
BEFORE INSERT ON executions
WHEN NEW.authorized_by = ''
BEGIN
    SELECT RAISE(ABORT, 'execution authorizing principal is required');
END;

CREATE TRIGGER IF NOT EXISTS executions_reject_authorized_by_update
BEFORE UPDATE OF authorized_by ON executions
BEGIN
    SELECT RAISE(ABORT, 'execution authorizing principal is immutable');
END;
