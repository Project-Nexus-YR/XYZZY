-- Principal-owned identity for atomic, idempotent first-time setup.
-- Global organization slugs are deliberately not used as idempotency keys.

CREATE TABLE IF NOT EXISTS user_bootstrap_contexts (
    user_id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL UNIQUE REFERENCES organizations(org_id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL UNIQUE REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    room_id TEXT NOT NULL UNIQUE REFERENCES rooms(room_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);
