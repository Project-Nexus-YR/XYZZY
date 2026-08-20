-- SQLite's INSERT OR REPLACE may remove a conflicting row without invoking
-- delete triggers unless recursive triggers are enabled. Guard both deletion
-- and duplicate insertion so committed evidence cannot be replaced either way.

CREATE TRIGGER IF NOT EXISTS agent_outputs_reject_delete
BEFORE DELETE ON agent_outputs
BEGIN
    SELECT RAISE(ABORT, 'agent_outputs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS agent_outputs_reject_duplicate_insert
BEFORE INSERT ON agent_outputs
WHEN EXISTS (
    SELECT 1 FROM agent_outputs o
    WHERE o.output_id = NEW.output_id OR o.execution_id = NEW.execution_id
)
BEGIN
    SELECT RAISE(ABORT, 'agent_outputs are immutable');
END;

CREATE TRIGGER IF NOT EXISTS artifact_versions_reject_delete
BEFORE DELETE ON artifact_versions
BEGIN
    SELECT RAISE(ABORT, 'artifact versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS artifact_versions_reject_duplicate_insert
BEFORE INSERT ON artifact_versions
WHEN EXISTS (
    SELECT 1 FROM artifact_versions v
    WHERE v.version_id = NEW.version_id
       OR (v.artifact_id = NEW.artifact_id AND v.version_number = NEW.version_number)
)
BEGIN
    SELECT RAISE(ABORT, 'artifact versions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS artifact_claims_reject_delete
BEFORE DELETE ON artifact_claims
BEGIN
    SELECT RAISE(ABORT, 'artifact claims are immutable');
END;

CREATE TRIGGER IF NOT EXISTS artifact_claims_reject_duplicate_insert
BEFORE INSERT ON artifact_claims
WHEN EXISTS (
    SELECT 1 FROM artifact_claims c
    WHERE c.claim_id = NEW.claim_id
       OR (c.version_id = NEW.version_id AND c.ordinal = NEW.ordinal)
)
BEGIN
    SELECT RAISE(ABORT, 'artifact claims are immutable');
END;

CREATE TRIGGER IF NOT EXISTS artifact_claim_sources_reject_delete
BEFORE DELETE ON artifact_claim_sources
BEGIN
    SELECT RAISE(ABORT, 'artifact claim provenance is immutable');
END;

CREATE TRIGGER IF NOT EXISTS artifact_claim_sources_reject_duplicate_insert
BEFORE INSERT ON artifact_claim_sources
WHEN EXISTS (
    SELECT 1 FROM artifact_claim_sources s
    WHERE s.claim_id = NEW.claim_id AND s.output_id = NEW.output_id
)
BEGIN
    SELECT RAISE(ABORT, 'artifact claim provenance is immutable');
END;
