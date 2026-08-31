-- A recipe names its specialists, not their rows.
--
-- A workspace that stands up the same shape of room over and over — the
-- specialists it always wants in it — had no way to say that once. It could
-- only repeat the spawn calls by hand each time, room after room. A room
-- template is the workspace writing that shape down: a name, a description,
-- and the agent_template_ids a new room should preselect.
--
-- Consistent with 038's agent_templates: no built-ins here, only workspace
-- rows, so workspace_id is NOT NULL rather than nullable-means-global.
-- created_by names who wrote it, for the same creator-or-admin deletion check
-- agent_templates already uses. deleted_at is a written fact rather than a
-- removed row, and for the same reason 038 gave: a room created from this
-- recipe never reads the recipe again after creation, so nothing a DELETE
-- retires can break a room already spawned from it — the row only ever
-- backs a future create-room call, never a live one.
--
-- agent_template_ids is a JSON array, not a join table: the list is small,
-- ordered, and read whole exactly once per room creation, never queried by
-- membership. It names templates, not rows the DB enforces referentially —
-- the same reason agent_templates.capabilities is JSON rather than a table —
-- because a listed id can legitimately point at a template already deleted
-- by the time a room is created from this recipe, and that must be a refused
-- DomainError at use time, not a dangling FK at save time.
CREATE TABLE IF NOT EXISTS room_templates (
    template_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    agent_template_ids TEXT NOT NULL DEFAULT '[]',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    deleted_at TEXT
);

-- Listing a workspace's live recipes is the only query this table serves.
CREATE INDEX IF NOT EXISTS idx_room_templates_workspace ON room_templates(workspace_id);
