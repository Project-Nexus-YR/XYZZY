-- Channel and workspace capability policies, and the tool gateway's durable decisions.
-- allowed_capabilities is a JSON list; NULL means the policy was never set and the
-- full vocabulary applies. Pre-existing rows therefore keep their behaviour.

ALTER TABLE rooms ADD COLUMN allowed_capabilities TEXT;
ALTER TABLE workspaces ADD COLUMN allowed_capabilities TEXT;
ALTER TABLE room_members ADD COLUMN allowed_capabilities TEXT;

CREATE TABLE IF NOT EXISTS tool_requests (
    request_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    execution_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    tool TEXT NOT NULL,
    input_json TEXT NOT NULL DEFAULT '{}',
    required_capability TEXT,
    effective_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('REJECTED', 'PENDING_APPROVAL', 'EXECUTED', 'FAILED')),
    reason TEXT NOT NULL DEFAULT '',
    approval_id TEXT,
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tool_requests_room ON tool_requests(room_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tool_requests_approval ON tool_requests(approval_id);
