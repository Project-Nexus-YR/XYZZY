-- Multiplayer AI Workspace - Initial Schema
-- All tables use TEXT primary keys (prefixed UUIDs).
-- Table order respects FK dependencies.

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ── Users (base table, no FKs) ──────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    avatar_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'OFFLINE',
    created_at TEXT NOT NULL
);

-- ── Organizations ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS organizations (
    org_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS organization_members (
    org_id TEXT NOT NULL REFERENCES organizations(org_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    created_at TEXT NOT NULL,
    PRIMARY KEY (org_id, user_id)
);

-- ── Workspaces ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES organizations(org_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(org_id, slug)
);

CREATE TABLE IF NOT EXISTS workspace_members (
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, user_id)
);

-- ── Rooms ───────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS rooms (
    room_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rooms_workspace ON rooms(workspace_id);

CREATE TABLE IF NOT EXISTS room_members (
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    joined_at TEXT NOT NULL,
    PRIMARY KEY (room_id, user_id)
);

-- ── Room Event Log ──────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS room_events (
    event_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    actor_id TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_room_events_room_seq ON room_events(room_id, sequence);
CREATE UNIQUE INDEX IF NOT EXISTS idx_room_events_room_seq_unique ON room_events(room_id, sequence);

-- ── Room Sequence Counter (atomic event ordering) ───────────────────────────

CREATE TABLE IF NOT EXISTS room_sequences (
    room_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL DEFAULT 0
);

-- ── Agents ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS agent_templates (
    template_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL,
    system_prompt TEXT NOT NULL DEFAULT '',
    capabilities TEXT NOT NULL DEFAULT '[]',
    preferred_tools TEXT NOT NULL DEFAULT '[]',
    avatar_url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_instances (
    agent_id TEXT PRIMARY KEY,
    template_id TEXT NOT NULL REFERENCES agent_templates(template_id),
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'IDLE',
    system_prompt TEXT NOT NULL DEFAULT '',
    capabilities TEXT NOT NULL DEFAULT '[]',
    model_provider TEXT NOT NULL DEFAULT '',
    model_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agents_room ON agent_instances(room_id);
CREATE INDEX IF NOT EXISTS idx_agents_template ON agent_instances(template_id);

CREATE TABLE IF NOT EXISTS agent_room_memberships (
    agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id) ON DELETE CASCADE,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    joined_at TEXT NOT NULL,
    PRIMARY KEY (agent_id, room_id)
);

-- ── Sessions ────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id) ON DELETE CASCADE,
    task_id TEXT,
    status TEXT NOT NULL DEFAULT 'CREATED',
    started_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_room ON sessions(room_id);
CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent_id);

-- ── Executions ──────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    run_id TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    input_data TEXT NOT NULL DEFAULT '{}',
    output_data TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_executions_session ON executions(session_id);

-- Immutable outputs are first-class records rather than opaque execution blobs.
-- One execution produces at most one terminal output in the Phase 1 run model.
CREATE TABLE IF NOT EXISTS agent_outputs (
    output_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    execution_id TEXT NOT NULL UNIQUE REFERENCES executions(execution_id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    output_data TEXT NOT NULL DEFAULT '{}',
    source_prompt TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_outputs_room_created
    ON agent_outputs(room_id, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_outputs_session
    ON agent_outputs(session_id);

-- One shared review decision per output. The output remains immutable regardless
-- of disposition, and another authorized room member sees the same choice.
CREATE TABLE IF NOT EXISTS output_selections (
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    output_id TEXT NOT NULL UNIQUE REFERENCES agent_outputs(output_id) ON DELETE CASCADE,
    disposition TEXT NOT NULL CHECK(disposition IN ('INCLUDED', 'EXCLUDED')),
    decided_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (room_id, output_id)
);

CREATE INDEX IF NOT EXISTS idx_output_selections_room
    ON output_selections(room_id, updated_at);

-- ── Tasks ───────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'CREATED',
    priority TEXT NOT NULL DEFAULT 'NORMAL',
    assigned_agent_id TEXT,
    created_by TEXT NOT NULL DEFAULT '',
    parent_task_id TEXT,
    delegation_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_room ON tasks(room_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_assigned ON tasks(assigned_agent_id);

CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    depends_on_task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, depends_on_task_id)
);

-- ── Messages ────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_room ON messages(room_id);

-- ── Artifacts ───────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    current_version INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_room ON artifacts(room_id);

CREATE TABLE IF NOT EXISTS artifact_versions (
    version_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(artifact_id, version_number)
);

CREATE TABLE IF NOT EXISTS artifact_claims (
    claim_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES artifact_versions(version_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    is_ai_derived INTEGER NOT NULL DEFAULT 1,
    confidence REAL NOT NULL,
    UNIQUE(version_id, ordinal)
);

CREATE TABLE IF NOT EXISTS artifact_claim_sources (
    claim_id TEXT NOT NULL REFERENCES artifact_claims(claim_id) ON DELETE CASCADE,
    output_id TEXT NOT NULL REFERENCES agent_outputs(output_id) ON DELETE RESTRICT,
    evidence TEXT NOT NULL,
    PRIMARY KEY (claim_id, output_id)
);

CREATE INDEX IF NOT EXISTS idx_artifact_claims_version
    ON artifact_claims(version_id, ordinal);

-- ── Decisions ───────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PROPOSED',
    created_by TEXT NOT NULL DEFAULT '',
    reviewed_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decisions_room ON decisions(room_id);

-- ── Memory ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS memories (
    memory_id TEXT PRIMARY KEY,
    room_id TEXT,
    workspace_id TEXT,
    org_id TEXT,
    scope TEXT NOT NULL,
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL DEFAULT 'fact',
    is_authoritative INTEGER NOT NULL DEFAULT 0,
    superseded_by TEXT,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_room ON memories(room_id);
CREATE INDEX IF NOT EXISTS idx_memories_workspace ON memories(workspace_id);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope);

-- ── Approvals ───────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    execution_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    action_description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    reviewer_id TEXT,
    review_comment TEXT NOT NULL DEFAULT '',
    requested_at TEXT NOT NULL,
    reviewed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_approvals_room ON approvals(room_id);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);

-- ── Notifications ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS notifications (
    notification_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    room_id TEXT,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    notification_type TEXT NOT NULL DEFAULT 'info',
    status TEXT NOT NULL DEFAULT 'UNREAD',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, status);

-- ── Credentials ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS credentials (
    credential_id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES organizations(org_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    credential_type TEXT NOT NULL,
    encrypted_data TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- ── Tool Permissions ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tool_permissions (
    permission_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    tool_name TEXT NOT NULL,
    allowed INTEGER NOT NULL DEFAULT 1,
    requires_approval INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(agent_id, room_id, tool_name)
);

CREATE INDEX IF NOT EXISTS idx_tool_permissions_agent ON tool_permissions(agent_id, room_id);
