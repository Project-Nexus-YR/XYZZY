-- Upgrade every prior provenance commitment to the v2 envelope that binds the
-- normalized publication author and timestamp. The application deterministically
-- recalculates blank hashes immediately after migrations complete.
DROP TRIGGER IF EXISTS artifact_versions_lock_provenance_hash;
UPDATE artifact_versions SET provenance_hash = '';

CREATE TRIGGER artifact_versions_lock_provenance_hash
BEFORE UPDATE OF provenance_hash ON artifact_versions
WHEN OLD.provenance_hash <> '' OR NEW.provenance_hash = ''
BEGIN
    SELECT RAISE(ABORT, 'artifact provenance hash is immutable');
END;

CREATE TRIGGER IF NOT EXISTS artifact_versions_reject_publication_identity_update
BEFORE UPDATE OF created_by, created_at ON artifact_versions
BEGIN
    SELECT RAISE(ABORT, 'artifact publication identity is immutable');
END;
