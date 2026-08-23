-- An intervention is bounded by the authority that produced it, and that bound
-- is a row rather than a local variable.
--
-- intervene_execution computed the intervener's terms, kept only "is it
-- non-empty", and threw the set away. The text then reached the provider prompt
-- verbatim and the next step ran under the run's own, wider terms: a member whose
-- effective set was {analysis} steered a run into calling a retrieval tool she
-- could not call herself. Recording the intersected set beside the instruction is
-- what makes discarding it impossible: the step that consumes an intervention
-- reads these rows and intersects every unconsumed one into its terms.
--
-- consumed_at is the only column a later write may touch. Who intervened, what
-- they held, and what they said are settled when the row is written.

CREATE TABLE IF NOT EXISTS execution_interventions (
    intervention_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL REFERENCES executions(execution_id),
    intervened_by TEXT NOT NULL,
    -- The intervener's effective set at the moment they steered, as a JSON array.
    capabilities TEXT NOT NULL,
    instruction TEXT NOT NULL,
    created_at TEXT NOT NULL,
    consumed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_execution_interventions_unconsumed
    ON execution_interventions(execution_id, consumed_at);

CREATE TRIGGER IF NOT EXISTS execution_interventions_reject_authority_update
BEFORE UPDATE OF intervened_by, capabilities, instruction ON execution_interventions
BEGIN
    SELECT RAISE(ABORT, 'an intervention keeps the authority that produced it');
END;

-- Deleting an unconsumed intervention would unbind the run it narrows, which is
-- the discard this table exists to prevent.
CREATE TRIGGER IF NOT EXISTS execution_interventions_reject_delete
BEFORE DELETE ON execution_interventions
BEGIN
    SELECT RAISE(ABORT, 'an intervention keeps the authority that produced it');
END;

-- ── The authorizing principal, closed for real ──────────────────────────────

-- Migration 014 rejected a plain UPDATE of authorized_by and nothing else, so
-- INSERT OR REPLACE and DELETE-then-INSERT both rewrote it. REPLACE fires delete
-- triggers only when recursive triggers are enabled, which is why the deletion
-- and the duplicate insertion are guarded separately, as in migration 005.

CREATE TRIGGER IF NOT EXISTS executions_reject_delete
BEFORE DELETE ON executions
BEGIN
    SELECT RAISE(ABORT, 'execution authorizing principal is immutable');
END;

CREATE TRIGGER IF NOT EXISTS executions_reject_duplicate_insert
BEFORE INSERT ON executions
WHEN EXISTS (SELECT 1 FROM executions e WHERE e.execution_id = NEW.execution_id)
BEGIN
    SELECT RAISE(ABORT, 'execution authorizing principal is immutable');
END;

-- A principal made of spaces names nobody, and 014's emptiness test accepted one.
DROP TRIGGER IF EXISTS executions_require_authorized_by;

CREATE TRIGGER executions_require_authorized_by
BEFORE INSERT ON executions
WHEN TRIM(NEW.authorized_by, ' ' || CHAR(9) || CHAR(10) || CHAR(13)) = ''
BEGIN
    SELECT RAISE(ABORT, 'execution authorizing principal is required');
END;
