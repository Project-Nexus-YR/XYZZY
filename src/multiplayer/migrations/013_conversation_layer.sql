-- The conversation layer: threads, mentions, reactions, read cursors, and search.
--
-- Two shapes here are deliberate departures from the usual chat schema.
--
-- Reply counts are NOT stored. A counter maintained on the write path can drift
-- from the reply graph it claims to summarise, and nothing detects the drift.
-- Every count in this layer is a COUNT() over the durable reply rows at read time,
-- and so are the thread's last reply time and its participant count.
--
-- thread_depth is bounded by MAX_THREAD_DEPTH in domain/models.py, enforced by the
-- service on the write path. The bound lives there rather than in a CHECK here so
-- that one number governs it; the triggers below still hold the lineage exact.
--
-- Search indexes an explicit allowlist. search_documents.object_kind is a foreign
-- key into search_indexed_kinds, so an object kind that nobody has opted in is not
-- merely excluded from results, it cannot be written to the index at all. A new
-- sensitive kind is therefore unsearchable by default rather than searchable until
-- someone remembers to blocklist it.

-- ── Threading ───────────────────────────────────────────────────────────────

-- A message carries the sequence of the canonical event that created it. That is
-- an identity, not a summary: it is written once inside the message's own
-- transaction and never recomputed, and it lets a client resume a room listing
-- from the same cursor it resumes the event log from.
ALTER TABLE messages ADD COLUMN event_sequence INTEGER NOT NULL DEFAULT 0;
ALTER TABLE messages ADD COLUMN parent_message_id TEXT REFERENCES messages(message_id);
ALTER TABLE messages ADD COLUMN root_message_id TEXT REFERENCES messages(message_id);
ALTER TABLE messages ADD COLUMN thread_depth INTEGER NOT NULL DEFAULT 0;
ALTER TABLE messages ADD COLUMN broadcast_to_room INTEGER NOT NULL DEFAULT 1;

UPDATE messages SET event_sequence = COALESCE((
    SELECT e.sequence
    FROM room_events e
    WHERE e.room_id = messages.room_id
      AND e.event_type = 'message.created'
      AND json_extract(e.payload, '$.message_id') = messages.message_id
), 0)
WHERE event_sequence = 0;

CREATE INDEX IF NOT EXISTS idx_messages_room_sequence
    ON messages(room_id, event_sequence, message_id);
CREATE INDEX IF NOT EXISTS idx_messages_thread
    ON messages(root_message_id, event_sequence, message_id);
CREATE INDEX IF NOT EXISTS idx_messages_parent
    ON messages(parent_message_id);

CREATE TRIGGER IF NOT EXISTS messages_require_thread_lineage
BEFORE INSERT ON messages
WHEN NEW.parent_message_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM messages p
    WHERE p.message_id = NEW.parent_message_id
      AND p.room_id = NEW.room_id
      AND NEW.root_message_id = COALESCE(p.root_message_id, p.message_id)
      AND NEW.thread_depth = p.thread_depth + 1
)
BEGIN
    SELECT RAISE(ABORT, 'thread reply must extend a parent in the same room');
END;

CREATE TRIGGER IF NOT EXISTS messages_require_root_with_parent
BEFORE INSERT ON messages
WHEN NEW.parent_message_id IS NULL AND NEW.root_message_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'a root message has no parent and no root');
END;

CREATE TRIGGER IF NOT EXISTS messages_reject_thread_update
BEFORE UPDATE OF parent_message_id, root_message_id, thread_depth ON messages
BEGIN
    SELECT RAISE(ABORT, 'thread lineage is immutable');
END;

-- ── Mentions ────────────────────────────────────────────────────────────────

-- One row per addressed target, derived server-side from the message text. A
-- mention notifies; it never runs anything on its own. invoked_execution_id is
-- populated only when the author explicitly asked for invocation and the
-- capability check passed, so the row itself states why an agent spoke.
CREATE TABLE IF NOT EXISTS message_mentions (
    message_id TEXT NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    target_type TEXT NOT NULL CHECK(target_type IN ('USER', 'AGENT')),
    target_id TEXT NOT NULL,
    handle TEXT NOT NULL,
    invoked_execution_id TEXT REFERENCES executions(execution_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (message_id, target_type, target_id)
);

CREATE INDEX IF NOT EXISTS idx_message_mentions_target
    ON message_mentions(target_type, target_id, created_at);
CREATE INDEX IF NOT EXISTS idx_message_mentions_room
    ON message_mentions(room_id, created_at);

CREATE TRIGGER IF NOT EXISTS message_mentions_reject_user_invocation
BEFORE INSERT ON message_mentions
WHEN NEW.invoked_execution_id IS NOT NULL AND NEW.target_type <> 'AGENT'
BEGIN
    SELECT RAISE(ABORT, 'only an agent mention can carry an invocation');
END;

-- ── Reactions ───────────────────────────────────────────────────────────────

-- Removing a reaction is a soft delete. The row survives so that re-adding the
-- same emoji is an update of durable history rather than a resurrection.
CREATE TABLE IF NOT EXISTS message_reactions (
    message_id TEXT NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    actor_id TEXT NOT NULL,
    emoji TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    removed_at TEXT,
    PRIMARY KEY (message_id, actor_id, emoji)
);

CREATE INDEX IF NOT EXISTS idx_message_reactions_live
    ON message_reactions(message_id, removed_at);

CREATE TRIGGER IF NOT EXISTS message_reactions_reject_delete
BEFORE DELETE ON message_reactions
BEGIN
    SELECT RAISE(ABORT, 'a reaction is removed by removed_at, never by DELETE');
END;

-- ── Read cursors ────────────────────────────────────────────────────────────

-- Per-user, per-room read position on the canonical room event sequence, so it
-- survives reconnect and describes the same ordering the event log does.
--
-- Setting a cursor appends no room event, and that is deliberate: a read position
-- is one member's private state, not shared room state, and every member of the
-- room replays the same event log, so an event here would publish to everyone
-- which member read what and when.
--
-- The cost is real and worth naming: with no event, a member's second device
-- learns nothing from the log when the cursor moves, and only sees the new
-- position when it next reads GET /rooms/{id}/read-cursor.
CREATE TABLE IF NOT EXISTS room_read_cursors (
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    last_read_sequence INTEGER NOT NULL CHECK(last_read_sequence >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (room_id, user_id)
);

-- ── Search ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS search_indexed_kinds (
    object_kind TEXT PRIMARY KEY,
    indexed_at TEXT NOT NULL
);

INSERT OR IGNORE INTO search_indexed_kinds(object_kind, indexed_at)
VALUES ('MESSAGE', '2026-08-23T00:00:00+00:00');

CREATE TABLE IF NOT EXISTS search_documents (
    document_id INTEGER PRIMARY KEY,
    object_kind TEXT NOT NULL REFERENCES search_indexed_kinds(object_kind) ON DELETE RESTRICT,
    object_id TEXT NOT NULL,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    author_id TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (object_kind, object_id)
);

CREATE INDEX IF NOT EXISTS idx_search_documents_room
    ON search_documents(room_id, document_id);

CREATE VIRTUAL TABLE IF NOT EXISTS search_documents_fts USING fts5(
    content,
    content='search_documents',
    content_rowid='document_id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS search_documents_after_insert
AFTER INSERT ON search_documents
BEGIN
    INSERT INTO search_documents_fts(rowid, content) VALUES (NEW.document_id, NEW.content);
END;

CREATE TRIGGER IF NOT EXISTS search_documents_after_delete
AFTER DELETE ON search_documents
BEGIN
    INSERT INTO search_documents_fts(search_documents_fts, rowid, content)
    VALUES ('delete', OLD.document_id, OLD.content);
END;

CREATE TRIGGER IF NOT EXISTS search_documents_after_update
AFTER UPDATE ON search_documents
BEGIN
    INSERT INTO search_documents_fts(search_documents_fts, rowid, content)
    VALUES ('delete', OLD.document_id, OLD.content);
    INSERT INTO search_documents_fts(rowid, content) VALUES (NEW.document_id, NEW.content);
END;

INSERT OR IGNORE INTO search_documents(object_kind, object_id, room_id, author_id, content,
    created_at)
SELECT 'MESSAGE', m.message_id, m.room_id, m.sender_id, m.content, m.created_at
FROM messages m;

-- ── Agent turn provenance ───────────────────────────────────────────────────

-- Every agent turn states why it happened. Runs that predate this column were
-- all started by a direct request, which is what DIRECT means.
ALTER TABLE executions ADD COLUMN triggered_by TEXT NOT NULL DEFAULT 'DIRECT'
    CHECK(triggered_by IN ('MENTION', 'DIRECT', 'SCHEDULE'));
