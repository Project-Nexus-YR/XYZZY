-- Cross-object search: one index over the objects a room member can already read.
--
-- Nothing about the mechanism changes. object_kind stays a foreign key into
-- search_indexed_kinds, so a kind nobody listed cannot be written to the index at
-- all, and the matching query still joins room_members with the roles that carry
-- READ, so a non-member gets zero rows out of SQLite rather than rows a later
-- filter is trusted to drop. What widens is the allowlist, not the path.
--
-- Each kind below was admitted on one test: a member of the room can already read
-- the indexed text through an existing endpoint, gated by the same
-- RoomCapability.READ the search join requires.
--
--   ARTIFACT_VERSION  GET /artifacts/{artifact_id}/versions returns content
--   TASK              GET /rooms/{room_id}/tasks returns the title
--   AGENT_OUTPUT      GET /rooms/{room_id}/outputs returns content
--   DECISION          GET /rooms/{room_id}/decisions returns title and content
--
-- Kinds whose authorization is not room membership stay out rather than getting a
-- second query path: a memory can be workspace- or org-scoped and has no room to
-- join against, and an approval or tool request carries the arguments of a
-- pending action rather than a record the room has read.
--
-- What each row indexes is narrower than what its object holds. The write paths in
-- repositories.py name the excluded field and the reason beside each call.

ALTER TABLE search_documents ADD COLUMN container_id TEXT NOT NULL DEFAULT '';

INSERT OR IGNORE INTO search_indexed_kinds(object_kind, indexed_at) VALUES
    ('ARTIFACT_VERSION', '2026-08-23T00:00:00+00:00'),
    ('TASK', '2026-08-23T00:00:00+00:00'),
    ('AGENT_OUTPUT', '2026-08-23T00:00:00+00:00'),
    ('DECISION', '2026-08-23T00:00:00+00:00');

-- search_documents cannot carry a foreign key to the object it indexes, because
-- object_id names a different table for every kind. These triggers are what stops
-- an index row outliving its object: without them a deleted row leaves a hit that
-- resolves to nothing, and the room_id cascade only covers deleting a whole room.
--
-- There is no trigger for artifact_versions or agent_outputs because 005 makes
-- both append-only: a row of either cannot be deleted at all, so an index entry
-- for one cannot be orphaned by a delete. An artifact version leaves the index
-- when a newer version supersedes it, which the write path does explicitly.
CREATE TRIGGER IF NOT EXISTS search_documents_forget_deleted_message
AFTER DELETE ON messages
BEGIN
    DELETE FROM search_documents
    WHERE object_kind = 'MESSAGE' AND object_id = OLD.message_id;
END;

CREATE TRIGGER IF NOT EXISTS search_documents_forget_deleted_task
AFTER DELETE ON tasks
BEGIN
    DELETE FROM search_documents
    WHERE object_kind = 'TASK' AND object_id = OLD.task_id;
END;

CREATE TRIGGER IF NOT EXISTS search_documents_forget_deleted_decision
AFTER DELETE ON decisions
BEGIN
    DELETE FROM search_documents
    WHERE object_kind = 'DECISION' AND object_id = OLD.decision_id;
END;
