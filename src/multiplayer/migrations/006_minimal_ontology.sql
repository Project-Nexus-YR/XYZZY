-- A deliberately small, room-scoped ontology. These tables project proven
-- workflow records; they do not replace artifacts, tasks, or AgentOutputs as
-- canonical sources of truth.

CREATE TABLE IF NOT EXISTS ontology_entities (
    entity_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN (
        'Person', 'Project', 'Task', 'Decision', 'Artifact', 'Claim', 'AgentOutput'
    )),
    source_object_id TEXT NOT NULL,
    label TEXT NOT NULL,
    properties TEXT NOT NULL DEFAULT '{}',
    derivation_kind TEXT NOT NULL CHECK(derivation_kind IN (
        'SYSTEM_MATERIALIZED', 'AI_DERIVED'
    )),
    confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
    evidence_ids TEXT NOT NULL,
    source_ids TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'UNCONFIRMED' CHECK(review_status IN (
        'UNCONFIRMED', 'CONFIRMED', 'CORRECTED'
    )),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(room_id, kind, source_object_id)
);

CREATE INDEX IF NOT EXISTS idx_ontology_entities_room_kind
    ON ontology_entities(room_id, kind, created_at);

CREATE TABLE IF NOT EXISTS ontology_relationships (
    relationship_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN (
        'OWNS', 'BLOCKS', 'DEPENDS_ON', 'SUPPORTS', 'CONTRADICTS',
        'REFERENCES', 'DERIVED_FROM'
    )),
    from_entity_id TEXT NOT NULL REFERENCES ontology_entities(entity_id) ON DELETE CASCADE,
    to_entity_id TEXT NOT NULL REFERENCES ontology_entities(entity_id) ON DELETE CASCADE,
    derivation_kind TEXT NOT NULL CHECK(derivation_kind IN (
        'SYSTEM_MATERIALIZED', 'AI_DERIVED'
    )),
    confidence REAL NOT NULL CHECK(confidence >= 0.0 AND confidence <= 1.0),
    evidence_ids TEXT NOT NULL,
    source_ids TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'UNCONFIRMED' CHECK(review_status IN (
        'UNCONFIRMED', 'CONFIRMED', 'CORRECTED'
    )),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(room_id, kind, from_entity_id, to_entity_id)
);

CREATE INDEX IF NOT EXISTS idx_ontology_relationships_room_kind
    ON ontology_relationships(room_id, kind, created_at);

CREATE TABLE IF NOT EXISTS ontology_reviews (
    review_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    target_type TEXT NOT NULL CHECK(target_type IN ('ENTITY', 'RELATIONSHIP')),
    target_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('CONFIRM', 'CORRECT')),
    before_value TEXT NOT NULL,
    after_value TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    reviewed_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ontology_reviews_target_created
    ON ontology_reviews(target_type, target_id, created_at);

CREATE TRIGGER IF NOT EXISTS ontology_reviews_reject_update
BEFORE UPDATE ON ontology_reviews
BEGIN
    SELECT RAISE(ABORT, 'ontology review history is immutable');
END;

CREATE TRIGGER IF NOT EXISTS ontology_reviews_reject_delete
BEFORE DELETE ON ontology_reviews
BEGIN
    SELECT RAISE(ABORT, 'ontology review history is immutable');
END;
