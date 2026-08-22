-- First-class isolated AI-work contexts. Rooms remain the durable channel boundary;
-- every new execution must now belong to exactly one Branch.

CREATE TABLE IF NOT EXISTS branches (
    branch_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK(mode IN ('TURN_LOCKED_SINGLE', 'PARALLEL')),
    status TEXT NOT NULL CHECK(
        status IN ('PENDING', 'RUNNING', 'COMPLETED', 'PARTIAL', 'FAILED', 'CANCELLED')
    ),
    initiated_by TEXT NOT NULL,
    initiating_prompt TEXT NOT NULL,
    context_event_sequence INTEGER NOT NULL CHECK(context_event_sequence >= 0),
    context_message_ids TEXT NOT NULL DEFAULT '[]',
    context_snapshot TEXT NOT NULL DEFAULT '{}',
    context_hash TEXT NOT NULL,
    lifecycle_managed INTEGER NOT NULL DEFAULT 1 CHECK(lifecycle_managed IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_branches_room_created
    ON branches(room_id, created_at, branch_id);

-- The legacy low-level session/execution API shares one compatibility branch per
-- room. New branch APIs never use this index/path.
CREATE UNIQUE INDEX IF NOT EXISTS idx_branches_one_legacy_room
    ON branches(room_id)
    WHERE lifecycle_managed = 0;

ALTER TABLE executions ADD COLUMN branch_id TEXT REFERENCES branches(branch_id);

-- Preserve historical runs honestly. Pre-migration context was never captured,
-- so the snapshot says so instead of fabricating a boundary.
INSERT INTO branches(
    branch_id, room_id, mode, status, initiated_by, initiating_prompt,
    context_event_sequence, context_message_ids, context_snapshot, context_hash,
    lifecycle_managed, created_at, updated_at, completed_at
)
SELECT
    'branch_legacy_' || e.execution_id,
    s.room_id,
    'PARALLEL',
    CASE
        WHEN e.status = 'COMPLETED' THEN 'COMPLETED'
        WHEN e.status = 'FAILED' THEN 'FAILED'
        WHEN e.status = 'CANCELLED' THEN 'CANCELLED'
        ELSE 'RUNNING'
    END,
    e.agent_id,
    'LEGACY_UNAVAILABLE',
    COALESCE((SELECT seq FROM room_sequences rs WHERE rs.room_id = s.room_id), 0),
    '[]',
    '{"boundary":"LEGACY_UNAVAILABLE"}',
    'LEGACY_UNAVAILABLE',
    1,
    e.started_at,
    COALESCE(e.completed_at, e.started_at),
    e.completed_at
FROM executions e
JOIN sessions s ON s.session_id = e.session_id
WHERE e.branch_id IS NULL;

UPDATE executions
SET branch_id = 'branch_legacy_' || execution_id
WHERE branch_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_executions_branch_started
    ON executions(branch_id, started_at, execution_id);

ALTER TABLE output_selections ADD COLUMN branch_id TEXT REFERENCES branches(branch_id);

UPDATE output_selections
SET branch_id = (
    SELECT e.branch_id
    FROM agent_outputs o
    JOIN executions e ON e.execution_id = o.execution_id
    WHERE o.output_id = output_selections.output_id
)
WHERE branch_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_output_selections_branch
    ON output_selections(branch_id, updated_at, output_id);

CREATE TRIGGER IF NOT EXISTS branches_reject_context_update
BEFORE UPDATE OF room_id, initiated_by, initiating_prompt, context_event_sequence,
    context_message_ids, context_snapshot, context_hash ON branches
BEGIN
    SELECT RAISE(ABORT, 'branch context boundary is immutable');
END;

CREATE TRIGGER IF NOT EXISTS executions_require_branch
BEFORE INSERT ON executions
WHEN NEW.branch_id IS NULL
BEGIN
    SELECT RAISE(ABORT, 'execution branch is required');
END;

CREATE TRIGGER IF NOT EXISTS executions_reject_branch_update
BEFORE UPDATE OF branch_id ON executions
BEGIN
    SELECT RAISE(ABORT, 'execution branch is immutable');
END;

CREATE TRIGGER IF NOT EXISTS executions_require_matching_branch_room
BEFORE INSERT ON executions
WHEN NOT EXISTS (
    SELECT 1
    FROM branches b
    JOIN sessions s ON s.session_id = NEW.session_id
    WHERE b.branch_id = NEW.branch_id AND b.room_id = s.room_id
)
BEGIN
    SELECT RAISE(ABORT, 'execution branch must belong to session room');
END;

CREATE TRIGGER IF NOT EXISTS output_selections_require_branch
BEFORE INSERT ON output_selections
WHEN NEW.branch_id IS NULL
BEGIN
    SELECT RAISE(ABORT, 'output selection branch is required');
END;

CREATE TRIGGER IF NOT EXISTS output_selections_reject_branch_update
BEFORE UPDATE OF branch_id ON output_selections
WHEN OLD.branch_id <> NEW.branch_id
BEGIN
    SELECT RAISE(ABORT, 'output selection branch is immutable');
END;

CREATE TRIGGER IF NOT EXISTS output_selections_require_output_branch
BEFORE INSERT ON output_selections
WHEN NOT EXISTS (
    SELECT 1
    FROM agent_outputs o
    JOIN executions e ON e.execution_id = o.execution_id
    WHERE o.output_id = NEW.output_id
      AND o.room_id = NEW.room_id
      AND e.branch_id = NEW.branch_id
)
BEGIN
    SELECT RAISE(ABORT, 'selection output must belong to branch');
END;

