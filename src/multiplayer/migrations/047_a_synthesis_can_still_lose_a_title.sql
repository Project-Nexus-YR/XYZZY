-- A branch synthesis's title is what a human typed (or, absent that, the first
-- 80 characters of their own initiating prompt) when they asked for a
-- Decision Brief or similar. 008's trigger blocks any update to a synthesis
-- row once it goes terminal (COMPLETED or FAILED), on purpose: the record of
-- what a model produced and under what provenance needs to stay trustworthy
-- once the run is over.
--
-- Erasure needs one narrow exception to that rule, the same shape as 046's
-- exception for a branch's context_snapshot. Unlike a message or a task
-- title, a synthesis's title never rides inside a chained room_events
-- payload (branch.synthesis.started carries only ids), so nothing else in
-- this schema can reach it: the title column is the only copy, and it is not
-- part of the provenance commitment this table otherwise protects (content,
-- provider_name, provider_model, provider_response_id, provider_evidence,
-- artifact_version_id, status, error, and every other column stay exactly as
-- immutable as 008/012 made them).

DROP TRIGGER IF EXISTS branch_syntheses_reject_completed_update;

CREATE TRIGGER IF NOT EXISTS branch_syntheses_reject_completed_update
BEFORE UPDATE OF
    branch_id, room_id, synthesis_type, status, initiated_by, provider_input,
    provider_name, provider_model, provider_response_id, provider_evidence,
    simulated, content, error, artifact_version_id, created_at, completed_at,
    token_usage
ON branch_syntheses
WHEN OLD.status IN ('COMPLETED', 'FAILED')
BEGIN
    SELECT RAISE(ABORT, 'terminal branch synthesis is immutable');
END;
