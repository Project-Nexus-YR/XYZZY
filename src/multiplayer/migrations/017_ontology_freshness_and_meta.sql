-- Freshness positioned on room_events.sequence, and the evidence hop a
-- relationship-centric Meta answer needs to terminate its drill-down chain.
--
-- A cursor here is a resume hint for one extractor and nothing else. Currency is
-- derived per read, by asking whether any event of an assertion's invalidation
-- class has landed since it was written, so no column below claims an assertion
-- is current.

CREATE TABLE IF NOT EXISTS ontology_extraction_cursors (
    room_id       TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    extractor     TEXT NOT NULL CHECK(extractor IN ('IMMEDIATE', 'ASYNC', 'SCHEDULED')),
    last_sequence INTEGER NOT NULL DEFAULT 0 CHECK(last_sequence >= 0),
    last_run_at   TEXT NOT NULL,
    PRIMARY KEY (room_id, extractor)
);

ALTER TABLE ontology_entities ADD COLUMN extractor TEXT NOT NULL DEFAULT 'IMMEDIATE';
ALTER TABLE ontology_entities ADD COLUMN asserted_at_sequence INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ontology_entities ADD COLUMN evidence_event_sequences TEXT NOT NULL DEFAULT '[]';
ALTER TABLE ontology_entities ADD COLUMN stale_at_sequence INTEGER;

ALTER TABLE ontology_relationships ADD COLUMN extractor TEXT NOT NULL DEFAULT 'IMMEDIATE';
ALTER TABLE ontology_relationships ADD COLUMN asserted_at_sequence INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ontology_relationships ADD COLUMN evidence_event_sequences TEXT NOT NULL DEFAULT '[]';
ALTER TABLE ontology_relationships ADD COLUMN stale_at_sequence INTEGER;

-- An edge had no hop from itself to the durable row whose content states the
-- relation, so every relationship-centric answer terminated early. Backfilled
-- from the edge's own from_entity: leaving '' would assert a chain that does not
-- exist, and SQLite cannot add a CHECK by ALTER, so non-emptiness is enforced in
-- the write path instead.
ALTER TABLE ontology_relationships ADD COLUMN source_object_kind TEXT NOT NULL DEFAULT '';
ALTER TABLE ontology_relationships ADD COLUMN source_object_id TEXT NOT NULL DEFAULT '';

UPDATE ontology_relationships
SET source_object_kind = COALESCE((
        SELECT e.kind FROM ontology_entities e
        WHERE e.entity_id = ontology_relationships.from_entity_id
    ), ''),
    source_object_id = COALESCE((
        SELECT e.source_object_id FROM ontology_entities e
        WHERE e.entity_id = ontology_relationships.from_entity_id
    ), '')
WHERE source_object_id = '';

CREATE INDEX IF NOT EXISTS idx_ontology_entities_room_sequence
    ON ontology_entities(room_id, asserted_at_sequence);
CREATE INDEX IF NOT EXISTS idx_ontology_relationships_room_sequence
    ON ontology_relationships(room_id, asserted_at_sequence);
-- Without this the per-read currency query scans the whole log once per answer.
CREATE INDEX IF NOT EXISTS idx_room_events_room_type_sequence
    ON room_events(room_id, event_type, sequence);

-- A cursor advance is a compare-and-swap. SQLite cannot add a CHECK by ALTER and
-- 012 shows the alternative is a table rebuild, so the guard is a trigger.
CREATE TRIGGER IF NOT EXISTS ontology_extraction_cursors_reject_rewind
BEFORE UPDATE ON ontology_extraction_cursors
WHEN NEW.last_sequence < OLD.last_sequence
BEGIN
    SELECT RAISE(ABORT, 'ontology extraction cursor must not rewind');
END;

-- Backfill, per 013's precedent for messages.event_sequence: an assertion is
-- positioned at the ontology.materialized event naming it, else at 0. No
-- backfilled row is claimed current; currency is derived, never stamped.
UPDATE ontology_entities
SET asserted_at_sequence = COALESCE((
    SELECT MAX(v.sequence) FROM room_events v
    WHERE v.room_id = ontology_entities.room_id
      AND v.event_type = 'ontology.materialized'
      AND json_extract(v.payload, '$.entity_ids') IS NOT NULL
      AND EXISTS (
          SELECT 1 FROM json_each(json_extract(v.payload, '$.entity_ids')) j
          WHERE j.value = ontology_entities.entity_id
      )
), 0)
WHERE asserted_at_sequence = 0;

UPDATE ontology_relationships
SET asserted_at_sequence = COALESCE((
    SELECT MAX(v.sequence) FROM room_events v
    WHERE v.room_id = ontology_relationships.room_id
      AND v.event_type = 'ontology.materialized'
      AND json_extract(v.payload, '$.relationship_ids') IS NOT NULL
      AND EXISTS (
          SELECT 1 FROM json_each(json_extract(v.payload, '$.relationship_ids')) j
          WHERE j.value = ontology_relationships.relationship_id
      )
), 0)
WHERE asserted_at_sequence = 0;

INSERT OR IGNORE INTO ontology_extraction_cursors(room_id, extractor, last_sequence, last_run_at)
SELECT room_id, 'IMMEDIATE', MAX(asserted_at_sequence), ''
FROM ontology_entities
GROUP BY room_id;
