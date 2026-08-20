-- Freeze the exact provenance visible at publication time and bind it to the
-- artifact version. Correlated updates backfill the best available snapshot
-- for versions published before this migration.
ALTER TABLE artifact_versions ADD COLUMN provenance_hash TEXT NOT NULL DEFAULT '';

ALTER TABLE artifact_claim_sources ADD COLUMN agent_id TEXT NOT NULL DEFAULT '';
ALTER TABLE artifact_claim_sources ADD COLUMN execution_id TEXT NOT NULL DEFAULT '';
ALTER TABLE artifact_claim_sources ADD COLUMN source_prompt TEXT NOT NULL DEFAULT '';
ALTER TABLE artifact_claim_sources ADD COLUMN provider_input TEXT NOT NULL DEFAULT '';
ALTER TABLE artifact_claim_sources ADD COLUMN provider_name TEXT NOT NULL DEFAULT '';
ALTER TABLE artifact_claim_sources ADD COLUMN provider_model TEXT NOT NULL DEFAULT '';
ALTER TABLE artifact_claim_sources ADD COLUMN provider_response_id TEXT NOT NULL DEFAULT '';
ALTER TABLE artifact_claim_sources ADD COLUMN provider_interventions TEXT NOT NULL DEFAULT '[]';
ALTER TABLE artifact_claim_sources ADD COLUMN provider_evidence TEXT NOT NULL DEFAULT '';

UPDATE artifact_claim_sources
SET agent_id = COALESCE((
        SELECT o.agent_id FROM agent_outputs o
        WHERE o.output_id = artifact_claim_sources.output_id
    ), ''),
    execution_id = COALESCE((
        SELECT o.execution_id FROM agent_outputs o
        WHERE o.output_id = artifact_claim_sources.output_id
    ), ''),
    source_prompt = COALESCE((
        SELECT o.source_prompt FROM agent_outputs o
        WHERE o.output_id = artifact_claim_sources.output_id
    ), ''),
    provider_input = COALESCE((
        SELECT o.provider_input FROM agent_outputs o
        WHERE o.output_id = artifact_claim_sources.output_id
    ), ''),
    provider_name = COALESCE((
        SELECT o.provider_name FROM agent_outputs o
        WHERE o.output_id = artifact_claim_sources.output_id
    ), ''),
    provider_model = COALESCE((
        SELECT o.provider_model FROM agent_outputs o
        WHERE o.output_id = artifact_claim_sources.output_id
    ), ''),
    provider_response_id = COALESCE((
        SELECT o.provider_response_id FROM agent_outputs o
        WHERE o.output_id = artifact_claim_sources.output_id
    ), ''),
    provider_interventions = COALESCE((
        SELECT o.provider_interventions FROM agent_outputs o
        WHERE o.output_id = artifact_claim_sources.output_id
    ), '[]'),
    provider_evidence = COALESCE((
        SELECT o.provider_evidence FROM agent_outputs o
        WHERE o.output_id = artifact_claim_sources.output_id
    ), '');

-- AgentOutput records are append-only evidence. This blocks accidental or
-- direct rewrites while retaining normal parent-room cascade deletion.
CREATE TRIGGER IF NOT EXISTS agent_outputs_reject_update
BEFORE UPDATE ON agent_outputs
BEGIN
    SELECT RAISE(ABORT, 'agent_outputs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS artifact_claims_reject_update
BEFORE UPDATE ON artifact_claims
BEGIN
    SELECT RAISE(ABORT, 'artifact claims are immutable');
END;

CREATE TRIGGER IF NOT EXISTS artifact_claim_sources_reject_update
BEFORE UPDATE ON artifact_claim_sources
BEGIN
    SELECT RAISE(ABORT, 'artifact claim provenance is immutable');
END;

-- Legacy upgrade code may fill a previously blank provenance_hash, but the
-- version identity and content commitment themselves are append-only.
CREATE TRIGGER IF NOT EXISTS artifact_versions_reject_content_update
BEFORE UPDATE OF artifact_id, version_number, content, content_hash ON artifact_versions
BEGIN
    SELECT RAISE(ABORT, 'artifact version content is immutable');
END;

CREATE TRIGGER IF NOT EXISTS artifact_versions_lock_provenance_hash
BEFORE UPDATE OF provenance_hash ON artifact_versions
WHEN OLD.provenance_hash <> '' OR NEW.provenance_hash = ''
BEGIN
    SELECT RAISE(ABORT, 'artifact provenance hash is immutable');
END;
