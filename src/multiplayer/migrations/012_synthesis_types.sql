-- PRD §8 names three synthesis types; the original table admitted only one. SQLite cannot
-- alter a CHECK constraint, so this is the sanctioned table rebuild: copy into a table with
-- the widened constraint, then restore the indexes and triggers the drop removes. Existing
-- rows keep their DECISION_BRIEF type.

PRAGMA foreign_keys=OFF;

CREATE TABLE branch_syntheses_rebuilt (
    synthesis_id TEXT PRIMARY KEY,
    branch_id TEXT NOT NULL REFERENCES branches(branch_id) ON DELETE RESTRICT,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    synthesis_type TEXT NOT NULL CHECK(
        synthesis_type IN ('GENERAL_SYNTHESIS', 'DECISION_BRIEF', 'PROGRESS_REPORT')
    ),
    status TEXT NOT NULL CHECK(status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')),
    title TEXT NOT NULL,
    initiated_by TEXT NOT NULL,
    provider_input TEXT NOT NULL DEFAULT '',
    provider_name TEXT NOT NULL DEFAULT '',
    provider_model TEXT NOT NULL DEFAULT '',
    provider_response_id TEXT NOT NULL DEFAULT '',
    provider_evidence TEXT NOT NULL DEFAULT '',
    simulated INTEGER NOT NULL DEFAULT 0 CHECK(simulated IN (0, 1)),
    content TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    artifact_version_id TEXT UNIQUE,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

INSERT INTO branch_syntheses_rebuilt SELECT * FROM branch_syntheses;

-- This trigger lives on another table but reads branch_syntheses, and SQLite validates it
-- when the table is dropped. Take it down with the table and restore it below, unchanged.
DROP TRIGGER IF EXISTS branch_synthesis_inputs_require_selected_branch_output;

DROP TABLE branch_syntheses;

ALTER TABLE branch_syntheses_rebuilt RENAME TO branch_syntheses;

CREATE INDEX IF NOT EXISTS idx_branch_syntheses_branch_created
    ON branch_syntheses(branch_id, created_at, synthesis_id);

CREATE TRIGGER IF NOT EXISTS branch_syntheses_require_matching_room
BEFORE INSERT ON branch_syntheses
WHEN NOT EXISTS (
    SELECT 1 FROM branches b
    WHERE b.branch_id = NEW.branch_id AND b.room_id = NEW.room_id
)
BEGIN
    SELECT RAISE(ABORT, 'synthesis branch must belong to room');
END;

CREATE TRIGGER IF NOT EXISTS branch_syntheses_reject_completed_update
BEFORE UPDATE ON branch_syntheses
WHEN OLD.status IN ('COMPLETED', 'FAILED')
BEGIN
    SELECT RAISE(ABORT, 'terminal branch synthesis is immutable');
END;

CREATE TRIGGER IF NOT EXISTS branch_synthesis_inputs_require_selected_branch_output
BEFORE INSERT ON branch_synthesis_inputs
WHEN NOT EXISTS (
    SELECT 1
    FROM branch_syntheses s
    JOIN executions e ON e.branch_id = s.branch_id
    JOIN agent_outputs o ON o.execution_id = e.execution_id
    JOIN output_selections os
      ON os.output_id = o.output_id
     AND os.branch_id = s.branch_id
     AND os.disposition = 'INCLUDED'
    WHERE s.synthesis_id = NEW.synthesis_id AND o.output_id = NEW.output_id
)
BEGIN
    SELECT RAISE(ABORT, 'synthesis input must be a selected branch output');
END;

PRAGMA foreign_keys=ON;
