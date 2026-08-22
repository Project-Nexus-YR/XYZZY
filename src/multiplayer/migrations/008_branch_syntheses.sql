-- Durable synthesis attempts and their exact selected input set. Provider work
-- happens outside the transaction; terminal publication is committed atomically.

CREATE TABLE IF NOT EXISTS branch_syntheses (
    synthesis_id TEXT PRIMARY KEY,
    branch_id TEXT NOT NULL REFERENCES branches(branch_id) ON DELETE RESTRICT,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    synthesis_type TEXT NOT NULL CHECK(synthesis_type IN ('DECISION_BRIEF')),
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

CREATE INDEX IF NOT EXISTS idx_branch_syntheses_branch_created
    ON branch_syntheses(branch_id, created_at, synthesis_id);

CREATE TABLE IF NOT EXISTS branch_synthesis_inputs (
    synthesis_id TEXT NOT NULL REFERENCES branch_syntheses(synthesis_id) ON DELETE RESTRICT,
    output_id TEXT NOT NULL REFERENCES agent_outputs(output_id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK(ordinal > 0),
    PRIMARY KEY (synthesis_id, output_id),
    UNIQUE(synthesis_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_branch_synthesis_inputs_output
    ON branch_synthesis_inputs(output_id, synthesis_id);

ALTER TABLE artifact_versions
    ADD COLUMN branch_synthesis_id TEXT REFERENCES branch_syntheses(synthesis_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_versions_branch_synthesis
    ON artifact_versions(branch_synthesis_id)
    WHERE branch_synthesis_id IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS branch_syntheses_require_matching_room
BEFORE INSERT ON branch_syntheses
WHEN NOT EXISTS (
    SELECT 1 FROM branches b
    WHERE b.branch_id = NEW.branch_id AND b.room_id = NEW.room_id
)
BEGIN
    SELECT RAISE(ABORT, 'synthesis branch must belong to room');
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

CREATE TRIGGER IF NOT EXISTS artifact_versions_reject_synthesis_update
BEFORE UPDATE OF branch_synthesis_id ON artifact_versions
BEGIN
    SELECT RAISE(ABORT, 'artifact synthesis provenance is immutable');
END;

CREATE TRIGGER IF NOT EXISTS branch_syntheses_reject_completed_update
BEFORE UPDATE ON branch_syntheses
WHEN OLD.status IN ('COMPLETED', 'FAILED')
BEGIN
    SELECT RAISE(ABORT, 'terminal branch synthesis is immutable');
END;

CREATE TRIGGER IF NOT EXISTS branch_synthesis_inputs_reject_update
BEFORE UPDATE ON branch_synthesis_inputs
BEGIN
    SELECT RAISE(ABORT, 'branch synthesis inputs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS branch_synthesis_inputs_reject_delete
BEFORE DELETE ON branch_synthesis_inputs
BEGIN
    SELECT RAISE(ABORT, 'branch synthesis inputs are immutable');
END;

