-- index idx_agent_outputs_room_created
CREATE INDEX idx_agent_outputs_room_created
    ON agent_outputs(room_id, created_at)

-- index idx_agent_outputs_session
CREATE INDEX idx_agent_outputs_session
    ON agent_outputs(session_id)

-- index idx_agent_room_memberships_agent_room
CREATE INDEX idx_agent_room_memberships_agent_room
    ON agent_room_memberships(agent_id, room_id)

-- index idx_agent_room_memberships_live
CREATE UNIQUE INDEX idx_agent_room_memberships_live
    ON agent_room_memberships(agent_id, room_id) WHERE removed_at IS NULL

-- index idx_agent_task_chain_agent
CREATE UNIQUE INDEX idx_agent_task_chain_agent ON agent_task_chain(task_id, agent_id)

-- index idx_agent_task_messages_task
CREATE INDEX idx_agent_task_messages_task ON agent_task_messages(task_id, sequence)

-- index idx_agent_tasks_context
CREATE INDEX idx_agent_tasks_context ON agent_tasks(context_id)

-- index idx_agent_tasks_execution
CREATE INDEX idx_agent_tasks_execution ON agent_tasks(execution_id)

-- index idx_agent_tasks_room
CREATE INDEX idx_agent_tasks_room ON agent_tasks(room_id)

-- index idx_agent_tasks_state_created
CREATE INDEX idx_agent_tasks_state_created
    ON agent_tasks(state, created_at)

-- index idx_agent_tasks_target
CREATE INDEX idx_agent_tasks_target ON agent_tasks(target_agent_id, state)

-- index idx_agent_templates_workspace
CREATE INDEX idx_agent_templates_workspace ON agent_templates(workspace_id)

-- index idx_agents_room
CREATE INDEX idx_agents_room ON agent_instances(room_id)

-- index idx_agents_template
CREATE INDEX idx_agents_template ON agent_instances(template_id)

-- index idx_approvals_room
CREATE INDEX idx_approvals_room ON approvals(room_id)

-- index idx_approvals_status
CREATE INDEX idx_approvals_status ON approvals(status)

-- index idx_artifact_claims_version
CREATE INDEX idx_artifact_claims_version
    ON artifact_claims(version_id, ordinal)

-- index idx_artifact_shares_artifact
CREATE INDEX idx_artifact_shares_artifact ON artifact_shares(artifact_id)

-- index idx_artifact_shares_token_hash
CREATE INDEX idx_artifact_shares_token_hash ON artifact_shares(token_hash)

-- index idx_artifact_versions_branch_synthesis
CREATE UNIQUE INDEX idx_artifact_versions_branch_synthesis
    ON artifact_versions(branch_synthesis_id)
    WHERE branch_synthesis_id IS NOT NULL

-- index idx_artifacts_room
CREATE INDEX idx_artifacts_room ON artifacts(room_id)

-- index idx_attachments_message
CREATE INDEX idx_attachments_message ON attachments(message_id)

-- index idx_attachments_room
CREATE INDEX idx_attachments_room ON attachments(room_id)

-- index idx_branch_syntheses_branch_created
CREATE INDEX idx_branch_syntheses_branch_created
    ON branch_syntheses(branch_id, created_at, synthesis_id)

-- index idx_branch_synthesis_inputs_output
CREATE INDEX idx_branch_synthesis_inputs_output
    ON branch_synthesis_inputs(output_id, synthesis_id)

-- index idx_branches_one_legacy_room
CREATE UNIQUE INDEX idx_branches_one_legacy_room
    ON branches(room_id)
    WHERE lifecycle_managed = 0

-- index idx_branches_room_created
CREATE INDEX idx_branches_room_created
    ON branches(room_id, created_at, branch_id)

-- index idx_decisions_room
CREATE INDEX idx_decisions_room ON decisions(room_id)

-- index idx_event_redactions_room
CREATE INDEX idx_event_redactions_room ON event_redactions(room_id)

-- index idx_execution_interventions_unconsumed
CREATE INDEX idx_execution_interventions_unconsumed
    ON execution_interventions(execution_id, consumed_at)

-- index idx_executions_agent_status_started
CREATE INDEX idx_executions_agent_status_started
    ON executions(agent_id, status, started_at DESC)

-- index idx_executions_agent_task
CREATE INDEX idx_executions_agent_task ON executions(agent_task_id)

-- index idx_executions_branch_started
CREATE INDEX idx_executions_branch_started
    ON executions(branch_id, started_at, execution_id)

-- index idx_executions_session
CREATE INDEX idx_executions_session ON executions(session_id)

-- index idx_executions_unclaimed_pending
CREATE INDEX idx_executions_unclaimed_pending
    ON executions(triggered_by, status, dispatch_claim)

-- index idx_memories_room
CREATE INDEX idx_memories_room ON memories(room_id)

-- index idx_memories_scope
CREATE INDEX idx_memories_scope ON memories(scope)

-- index idx_memories_workspace
CREATE INDEX idx_memories_workspace ON memories(workspace_id)

-- index idx_message_mentions_room
CREATE INDEX idx_message_mentions_room
    ON message_mentions(room_id, created_at)

-- index idx_message_mentions_target
CREATE INDEX idx_message_mentions_target
    ON message_mentions(target_type, target_id, created_at)

-- index idx_message_reactions_live
CREATE INDEX idx_message_reactions_live
    ON message_reactions(message_id, removed_at)

-- index idx_messages_parent
CREATE INDEX idx_messages_parent
    ON messages(parent_message_id)

-- index idx_messages_room
CREATE INDEX idx_messages_room ON messages(room_id)

-- index idx_messages_room_sequence
CREATE INDEX idx_messages_room_sequence
    ON messages(room_id, event_sequence, message_id)

-- index idx_messages_thread
CREATE INDEX idx_messages_thread
    ON messages(root_message_id, event_sequence, message_id)

-- index idx_notifications_user
CREATE INDEX idx_notifications_user ON notifications(user_id, status)

-- index idx_ontology_entities_room_kind
CREATE INDEX idx_ontology_entities_room_kind
    ON ontology_entities(room_id, kind, created_at)

-- index idx_ontology_entities_room_sequence
CREATE INDEX idx_ontology_entities_room_sequence
    ON ontology_entities(room_id, asserted_at_sequence)

-- index idx_ontology_relationships_room_kind
CREATE INDEX idx_ontology_relationships_room_kind
    ON ontology_relationships(room_id, kind, created_at)

-- index idx_ontology_relationships_room_sequence
CREATE INDEX idx_ontology_relationships_room_sequence
    ON ontology_relationships(room_id, asserted_at_sequence)

-- index idx_ontology_reviews_room_target_created
CREATE INDEX idx_ontology_reviews_room_target_created
    ON ontology_reviews(room_id, target_id, created_at DESC)

-- index idx_ontology_reviews_target_created
CREATE INDEX idx_ontology_reviews_target_created
    ON ontology_reviews(target_type, target_id, created_at)

-- index idx_output_selections_branch
CREATE INDEX idx_output_selections_branch
    ON output_selections(branch_id, updated_at, output_id)

-- index idx_output_selections_room
CREATE INDEX idx_output_selections_room
    ON output_selections(room_id, updated_at)

-- index idx_room_events_room_seq
CREATE INDEX idx_room_events_room_seq ON room_events(room_id, sequence)

-- index idx_room_events_room_seq_unique
CREATE UNIQUE INDEX idx_room_events_room_seq_unique ON room_events(room_id, sequence)

-- index idx_room_events_room_type_sequence
CREATE INDEX idx_room_events_room_type_sequence
    ON room_events(room_id, event_type, sequence)

-- index idx_room_participant_handles_unique
CREATE UNIQUE INDEX idx_room_participant_handles_unique
    ON room_participant_handles(room_id, handle)

-- index idx_room_postures_current
CREATE INDEX idx_room_postures_current
    ON room_postures(room_id, declared_at DESC)

-- index idx_room_templates_workspace
CREATE INDEX idx_room_templates_workspace ON room_templates(workspace_id)

-- index idx_rooms_workspace
CREATE INDEX idx_rooms_workspace ON rooms(workspace_id)

-- index idx_runs_agent_room
CREATE INDEX idx_runs_agent_room ON agent_runs(agent_id, room_id)

-- index idx_runs_open
CREATE INDEX idx_runs_open ON agent_runs(lease_expires_at) WHERE harness_state <> 'SETTLED'

-- index idx_search_documents_room
CREATE INDEX idx_search_documents_room
    ON search_documents(room_id, document_id)

-- index idx_session_refresh_session
CREATE INDEX idx_session_refresh_session ON session_refresh_tokens(session_id)

-- index idx_sessions_agent
CREATE INDEX idx_sessions_agent ON sessions(agent_id)

-- index idx_sessions_room
CREATE INDEX idx_sessions_room ON sessions(room_id)

-- index idx_tasks_assigned
CREATE INDEX idx_tasks_assigned ON tasks(assigned_agent_id)

-- index idx_tasks_room
CREATE INDEX idx_tasks_room ON tasks(room_id)

-- index idx_tasks_status
CREATE INDEX idx_tasks_status ON tasks(status)

-- index idx_tool_permissions_agent
CREATE INDEX idx_tool_permissions_agent ON tool_permissions(agent_id, room_id)

-- index idx_tool_requests_approval
CREATE INDEX idx_tool_requests_approval ON tool_requests(approval_id)

-- index idx_tool_requests_room
CREATE INDEX idx_tool_requests_room ON tool_requests(room_id, created_at)

-- index idx_turn_locks_branch
CREATE INDEX idx_turn_locks_branch
    ON turn_locks(branch_id, acquired_at, lock_id)

-- index idx_turn_locks_one_active_scope
CREATE UNIQUE INDEX idx_turn_locks_one_active_scope
    ON turn_locks(scope_type, scope_id)
    WHERE status = 'ACTIVE'

-- index idx_user_sessions_sid
CREATE INDEX idx_user_sessions_sid ON user_sessions(issuer, idp_session_id)

-- index idx_user_sessions_subject
CREATE INDEX idx_user_sessions_subject ON user_sessions(issuer, subject)

-- index idx_user_sessions_user
CREATE INDEX idx_user_sessions_user ON user_sessions(user_id)

-- index idx_user_tokens_session
CREATE INDEX idx_user_tokens_session ON user_tokens(session_id)

-- index idx_user_tokens_user
CREATE INDEX idx_user_tokens_user ON user_tokens(user_id)

-- table agent_address_allowlist
CREATE TABLE agent_address_allowlist (
    agent_id TEXT NOT NULL REFERENCES agent_addressing(agent_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL, added_by TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (agent_id, user_id))

-- table agent_addressing
CREATE TABLE agent_addressing (
    agent_id TEXT PRIMARY KEY REFERENCES agent_instances(agent_id) ON DELETE CASCADE,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK(mode IN ('OWNER_ONLY','ALLOWLIST','ANYONE','NOBODY')),
    owner_user_id TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL)

-- table agent_identities
CREATE TABLE agent_identities (
    identity_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, revoked_at TEXT,
    proof_mode TEXT NOT NULL CHECK(proof_mode IN ('IN_PROCESS','SIGNED_CHALLENGE')),
    public_key TEXT, key_fingerprint TEXT UNIQUE,
    agent_id TEXT NOT NULL UNIQUE REFERENCES agent_instances(agent_id) ON DELETE CASCADE,
    -- A key exists exactly when there is an untrusted transport to prove authorship across.
    CHECK((proof_mode = 'SIGNED_CHALLENGE') = (public_key IS NOT NULL)))

-- table agent_instances
CREATE TABLE agent_instances (
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
, harness_id TEXT NOT NULL DEFAULT 'nexus')

-- table agent_outputs
CREATE TABLE agent_outputs (
    output_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    execution_id TEXT NOT NULL UNIQUE REFERENCES executions(execution_id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    output_data TEXT NOT NULL DEFAULT '{}',
    source_prompt TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
, provider_input TEXT NOT NULL DEFAULT '', provider_name TEXT NOT NULL DEFAULT '', provider_model TEXT NOT NULL DEFAULT '', provider_response_id TEXT NOT NULL DEFAULT '', provider_interventions TEXT NOT NULL DEFAULT '[]', provider_evidence TEXT NOT NULL DEFAULT '')

-- table agent_room_memberships
CREATE TABLE "agent_room_memberships" (
    membership_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id) ON DELETE CASCADE,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    joined_at TEXT NOT NULL,
    removed_at TEXT,
    -- The departure this membership follows. NULL for a first join.
    rejoined_from_membership_id TEXT
)

-- table agent_runs
CREATE TABLE "agent_runs" (run_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL UNIQUE REFERENCES executions(execution_id) ON DELETE RESTRICT,
    agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id) ON DELETE RESTRICT,
    identity_id TEXT NOT NULL REFERENCES agent_identities(identity_id) ON DELETE RESTRICT,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE RESTRICT,
    authorized_by TEXT NOT NULL, acting_user_id TEXT NOT NULL,   -- initiator, then last caller
    harness_id TEXT NOT NULL, credential_hash TEXT NOT NULL,
    challenge_verified_at TEXT,
    harness_state TEXT NOT NULL CHECK(harness_state IN
        ('STARTING','STREAMING','AWAITING_APPROVAL','CANCEL_REQUESTED','SETTLED')),
    settlement TEXT CHECK(settlement IN ('END_TURN','CANCELLED','MAX_TOKENS','FAILED',
        'ORPHANED','AUTHORITY_REVOKED','AGENT_REMOVED','APPROVAL_REFUSED',
        'APPROVAL_EXPIRED','PARKED')),
    resumed_from_run_id TEXT REFERENCES agent_runs(run_id) ON DELETE RESTRICT,
    lease_expires_at TEXT NOT NULL, created_at TEXT NOT NULL, settled_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 1, max_attempts INTEGER NOT NULL DEFAULT 3,
    -- Settled with no settlement is terminal to the machine and invisible to the sweep: stuck.
    CHECK(harness_state <> 'SETTLED' OR settlement IS NOT NULL),
    CHECK(attempts >= 1 AND max_attempts >= 1))

-- table agent_task_chain
CREATE TABLE agent_task_chain (
    task_id TEXT NOT NULL REFERENCES agent_tasks(task_id),
    position INTEGER NOT NULL,
    agent_id TEXT NOT NULL,
    PRIMARY KEY (task_id, position)
)

-- table agent_task_messages
CREATE TABLE agent_task_messages (
    message_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES agent_tasks(task_id),
    sequence INTEGER NOT NULL,
    role TEXT NOT NULL,
    parts TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (task_id, sequence)
)

-- table agent_tasks
CREATE TABLE agent_tasks (
    task_id TEXT PRIMARY KEY,
    context_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    target_agent_id TEXT NOT NULL,
    delegating_agent_id TEXT,
    delegating_run_id TEXT,
    execution_id TEXT,
    state TEXT NOT NULL,
    accepted_output_modes TEXT NOT NULL DEFAULT '[]',
    depth INTEGER NOT NULL DEFAULT 0,
    authorized_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    terminal_at TEXT,
    refusal_reason TEXT NOT NULL DEFAULT ''
, requested_by TEXT NOT NULL DEFAULT '')

-- table agent_templates
CREATE TABLE agent_templates (
    template_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL,
    system_prompt TEXT NOT NULL DEFAULT '',
    capabilities TEXT NOT NULL DEFAULT '[]',
    preferred_tools TEXT NOT NULL DEFAULT '[]',
    avatar_url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
, workspace_id TEXT REFERENCES workspaces(workspace_id), created_by TEXT, deleted_at TEXT, shared_at TEXT)

-- table approvals
CREATE TABLE approvals (
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
, authorized_by TEXT NOT NULL DEFAULT '')

-- table artifact_claim_sources
CREATE TABLE artifact_claim_sources (
    claim_id TEXT NOT NULL REFERENCES artifact_claims(claim_id) ON DELETE CASCADE,
    output_id TEXT NOT NULL REFERENCES agent_outputs(output_id) ON DELETE RESTRICT,
    evidence TEXT NOT NULL, agent_id TEXT NOT NULL DEFAULT '', execution_id TEXT NOT NULL DEFAULT '', source_prompt TEXT NOT NULL DEFAULT '', provider_input TEXT NOT NULL DEFAULT '', provider_name TEXT NOT NULL DEFAULT '', provider_model TEXT NOT NULL DEFAULT '', provider_response_id TEXT NOT NULL DEFAULT '', provider_interventions TEXT NOT NULL DEFAULT '[]', provider_evidence TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (claim_id, output_id)
)

-- table artifact_claims
CREATE TABLE artifact_claims (
    claim_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES artifact_versions(version_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    is_ai_derived INTEGER NOT NULL DEFAULT 1,
    confidence REAL NOT NULL,
    UNIQUE(version_id, ordinal)
)

-- table artifact_shares
CREATE TABLE artifact_shares (
    share_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
)

-- table artifact_versions
CREATE TABLE artifact_versions (
    version_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, provenance_hash TEXT NOT NULL DEFAULT '', branch_synthesis_id TEXT REFERENCES branch_syntheses(synthesis_id),
    UNIQUE(artifact_id, version_number)
)

-- table artifacts
CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    current_version INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)

-- table attachments
CREATE TABLE attachments (
    attachment_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    uploader_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    message_id TEXT REFERENCES messages(message_id),
    data BLOB NOT NULL
)

-- table branch_syntheses
CREATE TABLE "branch_syntheses" (
    synthesis_id TEXT PRIMARY KEY,
    branch_id TEXT NOT NULL REFERENCES branches(branch_id) ON DELETE RESTRICT,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    synthesis_type TEXT NOT NULL CHECK(
        synthesis_type IN ('GENERAL_SYNTHESIS', 'DECISION_BRIEF', 'PROGRESS_REPORT')
    ),
    status TEXT NOT NULL CHECK(status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')),
    title TEXT NOT NULL,
    initiated_by TEXT NOT NULL,
    provider_input TEXT NOT NULL DEFAULT '',
    provider_name TEXT NOT NULL DEFAULT '',
    provider_model TEXT NOT NULL DEFAULT '',
    provider_response_id TEXT NOT NULL DEFAULT '',
    provider_evidence TEXT NOT NULL DEFAULT '',
    simulated INTEGER NOT NULL DEFAULT 0 CHECK(simulated IN (0, 1)),
    content TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    artifact_version_id TEXT UNIQUE,
    created_at TEXT NOT NULL,
    completed_at TEXT
, token_usage INTEGER NOT NULL DEFAULT 0)

-- table branch_synthesis_inputs
CREATE TABLE branch_synthesis_inputs (
    synthesis_id TEXT NOT NULL REFERENCES branch_syntheses(synthesis_id) ON DELETE RESTRICT,
    output_id TEXT NOT NULL REFERENCES agent_outputs(output_id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK(ordinal > 0),
    PRIMARY KEY (synthesis_id, output_id),
    UNIQUE(synthesis_id, ordinal)
)

-- table branches
CREATE TABLE branches (
    branch_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK(mode IN ('TURN_LOCKED_SINGLE', 'PARALLEL')),
    status TEXT NOT NULL CHECK(
        status IN ('PENDING', 'RUNNING', 'COMPLETED', 'PARTIAL', 'FAILED', 'CANCELLED')
    ),
    initiated_by TEXT NOT NULL,
    initiating_prompt TEXT NOT NULL,
    context_event_sequence INTEGER NOT NULL CHECK(context_event_sequence >= 0),
    context_message_ids TEXT NOT NULL DEFAULT '[]',
    context_snapshot TEXT NOT NULL DEFAULT '{}',
    context_hash TEXT NOT NULL,
    lifecycle_managed INTEGER NOT NULL DEFAULT 1 CHECK(lifecycle_managed IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
)

-- table credentials
CREATE TABLE credentials (
    credential_id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES organizations(org_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    credential_type TEXT NOT NULL,
    encrypted_data TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
)

-- table decisions
CREATE TABLE decisions (
    decision_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PROPOSED',
    created_by TEXT NOT NULL DEFAULT '',
    reviewed_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
)

-- table event_redactions
CREATE TABLE event_redactions (
    redaction_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    original_event_hash TEXT NOT NULL,
    redacted_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor_id TEXT NOT NULL
, header_event_type TEXT NOT NULL DEFAULT '', header_actor_id TEXT NOT NULL DEFAULT '', header_actor_type TEXT NOT NULL DEFAULT '', header_timestamp TEXT NOT NULL DEFAULT '', header_schema_version INTEGER NOT NULL DEFAULT 0, header_sequence INTEGER NOT NULL DEFAULT 0, header_prev_hash TEXT NOT NULL DEFAULT '', header_hash TEXT NOT NULL DEFAULT '')

-- table execution_callers
CREATE TABLE execution_callers (
    execution_id TEXT NOT NULL REFERENCES executions(execution_id) ON DELETE RESTRICT,
    caller_id TEXT NOT NULL,
    first_acted_at TEXT NOT NULL,
    PRIMARY KEY (execution_id, caller_id)
)

-- table execution_interventions
CREATE TABLE execution_interventions (
    intervention_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL REFERENCES executions(execution_id),
    intervened_by TEXT NOT NULL,
    -- The intervener's effective set at the moment they steered, as a JSON array.
    instruction TEXT NOT NULL,
    created_at TEXT NOT NULL,
    consumed_at TEXT
)

-- table executions
CREATE TABLE executions (
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
, branch_id TEXT REFERENCES branches(branch_id), triggered_by TEXT NOT NULL DEFAULT 'DIRECT'
    CHECK(triggered_by IN ('MENTION', 'DIRECT', 'SCHEDULE')), authorized_by TEXT NOT NULL DEFAULT '', dispatch_claim TEXT, agent_task_id TEXT REFERENCES agent_tasks(task_id), token_usage INTEGER NOT NULL DEFAULT 0)

-- table idempotency_keys
CREATE TABLE idempotency_keys (
    scope_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope_id, user_id, idempotency_key)
)

-- table memories
CREATE TABLE memories (
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
)

-- table message_mentions
CREATE TABLE message_mentions (
    message_id TEXT NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    target_type TEXT NOT NULL CHECK(target_type IN ('USER', 'AGENT')),
    target_id TEXT NOT NULL,
    handle TEXT NOT NULL,
    invoked_execution_id TEXT REFERENCES executions(execution_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (message_id, target_type, target_id)
)

-- table message_reactions
CREATE TABLE message_reactions (
    message_id TEXT NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    actor_id TEXT NOT NULL,
    emoji TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    removed_at TEXT, actor_type TEXT NOT NULL DEFAULT 'USER'
    CHECK(actor_type IN ('USER', 'AGENT')),
    PRIMARY KEY (message_id, actor_id, emoji)
)

-- table messages
CREATE TABLE messages (
    message_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
, event_sequence INTEGER NOT NULL DEFAULT 0, parent_message_id TEXT REFERENCES messages(message_id), root_message_id TEXT REFERENCES messages(message_id), thread_depth INTEGER NOT NULL DEFAULT 0, broadcast_to_room INTEGER NOT NULL DEFAULT 1)

-- table notifications
CREATE TABLE notifications (
    notification_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    room_id TEXT,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    notification_type TEXT NOT NULL DEFAULT 'info',
    status TEXT NOT NULL DEFAULT 'UNREAD',
    created_at TEXT NOT NULL
)

-- table oidc_authorizations
CREATE TABLE oidc_authorizations (
    state TEXT PRIMARY KEY,
    nonce TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    -- The digest of a cookie set on the browser that began this login. Without
    -- it, state lives only on the server, and anyone who obtains a state value
    -- can finish a login somebody else started: the victim's browser ends up
    -- holding a session for the attacker's account. Login CSRF is the name.
    browser_binding_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
)

-- table oidc_logout_tokens
CREATE TABLE oidc_logout_tokens (
    jti TEXT NOT NULL,
    issuer TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (jti, issuer)
)

-- table ontology_entities
CREATE TABLE ontology_entities (
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
    updated_at TEXT NOT NULL, extractor TEXT NOT NULL DEFAULT 'IMMEDIATE', asserted_at_sequence INTEGER NOT NULL DEFAULT 0, evidence_event_sequences TEXT NOT NULL DEFAULT '[]', stale_at_sequence INTEGER,
    UNIQUE(room_id, kind, source_object_id)
)

-- table ontology_extraction_cursors
CREATE TABLE ontology_extraction_cursors (
    room_id       TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    extractor     TEXT NOT NULL CHECK(extractor IN ('IMMEDIATE', 'ASYNC', 'SCHEDULED')),
    last_sequence INTEGER NOT NULL DEFAULT 0 CHECK(last_sequence >= 0),
    last_run_at   TEXT NOT NULL,
    PRIMARY KEY (room_id, extractor)
)

-- table ontology_relationships
CREATE TABLE ontology_relationships (
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
    updated_at TEXT NOT NULL, extractor TEXT NOT NULL DEFAULT 'IMMEDIATE', asserted_at_sequence INTEGER NOT NULL DEFAULT 0, evidence_event_sequences TEXT NOT NULL DEFAULT '[]', stale_at_sequence INTEGER, source_object_kind TEXT NOT NULL DEFAULT '', source_object_id TEXT NOT NULL DEFAULT '',
    UNIQUE(room_id, kind, from_entity_id, to_entity_id)
)

-- table ontology_reviews
CREATE TABLE ontology_reviews (
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
)

-- table organization_members
CREATE TABLE organization_members (
    org_id TEXT NOT NULL REFERENCES organizations(org_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    created_at TEXT NOT NULL,
    PRIMARY KEY (org_id, user_id)
)

-- table organizations
CREATE TABLE organizations (
    org_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
)

-- table output_selections
CREATE TABLE output_selections (
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    output_id TEXT NOT NULL UNIQUE REFERENCES agent_outputs(output_id) ON DELETE CASCADE,
    disposition TEXT NOT NULL CHECK(disposition IN ('INCLUDED', 'EXCLUDED')),
    decided_by TEXT NOT NULL,
    updated_at TEXT NOT NULL, branch_id TEXT REFERENCES branches(branch_id),
    PRIMARY KEY (room_id, output_id)
)

-- table room_events
CREATE TABLE room_events (
    event_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    actor_id TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1
, prev_hash TEXT, event_hash TEXT)

-- table room_members
CREATE TABLE room_members (
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    joined_at TEXT NOT NULL, allowed_capabilities TEXT,
    PRIMARY KEY (room_id, user_id)
)

-- table room_participant_handles
CREATE TABLE room_participant_handles (
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    participant_type TEXT NOT NULL CHECK(participant_type IN ('USER', 'AGENT')),
    participant_id TEXT NOT NULL,
    handle TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (room_id, participant_type, participant_id)
)

-- table room_postures
CREATE TABLE room_postures (
    declaration_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE RESTRICT,
    posture TEXT NOT NULL CHECK (posture IN ('GUARDED', 'STRICT')),
    declared_by TEXT NOT NULL,
    declared_at TEXT NOT NULL
)

-- table room_read_cursors
CREATE TABLE room_read_cursors (
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    last_read_sequence INTEGER NOT NULL CHECK(last_read_sequence >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (room_id, user_id)
)

-- table room_sequences
CREATE TABLE room_sequences (
    room_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL DEFAULT 0
)

-- table room_templates
CREATE TABLE room_templates (
    template_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    agent_template_ids TEXT NOT NULL DEFAULT '[]',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    deleted_at TEXT
)

-- table rooms
CREATE TABLE rooms (
    room_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
, allowed_capabilities TEXT)

-- table schema_migrations
CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)

-- table search_documents
CREATE TABLE search_documents (
    document_id INTEGER PRIMARY KEY,
    object_kind TEXT NOT NULL REFERENCES search_indexed_kinds(object_kind) ON DELETE RESTRICT,
    object_id TEXT NOT NULL,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    author_id TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    created_at TEXT NOT NULL, container_id TEXT NOT NULL DEFAULT '',
    UNIQUE (object_kind, object_id)
)

-- table search_documents_fts
CREATE VIRTUAL TABLE search_documents_fts USING fts5(
    content,
    content='search_documents',
    content_rowid='document_id',
    tokenize='unicode61'
)

-- table search_documents_fts_config
CREATE TABLE 'search_documents_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID

-- table search_documents_fts_data
CREATE TABLE 'search_documents_fts_data'(id INTEGER PRIMARY KEY, block BLOB)

-- table search_documents_fts_docsize
CREATE TABLE 'search_documents_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB)

-- table search_documents_fts_idx
CREATE TABLE 'search_documents_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID

-- table search_indexed_kinds
CREATE TABLE search_indexed_kinds (
    object_kind TEXT PRIMARY KEY,
    indexed_at TEXT NOT NULL
)

-- table session_refresh_tokens
CREATE TABLE session_refresh_tokens (
    token_hash TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES user_sessions(session_id),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    replaced_by_hash TEXT
)

-- table sessions
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id) ON DELETE CASCADE,
    task_id TEXT,
    status TEXT NOT NULL DEFAULT 'CREATED',
    started_at TEXT NOT NULL,
    ended_at TEXT
)

-- table suspended_turns
CREATE TABLE "suspended_turns" (
    execution_id TEXT PRIMARY KEY REFERENCES executions(execution_id) ON DELETE CASCADE,
    prompt TEXT NOT NULL,
    acting_as TEXT NOT NULL,
    -- A JSON array: the gateway's own records of this turn's tool calls, in order.
    observations TEXT NOT NULL,
    suspended_at TEXT NOT NULL
)

-- table task_dependencies
CREATE TABLE task_dependencies (
    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    depends_on_task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, depends_on_task_id)
)

-- table tasks
CREATE TABLE tasks (
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
)

-- table tool_permissions
CREATE TABLE tool_permissions (
    permission_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    tool_name TEXT NOT NULL,
    allowed INTEGER NOT NULL DEFAULT 1,
    requires_approval INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(agent_id, room_id, tool_name)
)

-- table tool_request_reviewers
CREATE TABLE tool_request_reviewers (
    request_id TEXT NOT NULL REFERENCES tool_requests(request_id) ON DELETE RESTRICT,
    reviewer_id TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    PRIMARY KEY (request_id, reviewer_id)
)

-- table tool_requests
CREATE TABLE tool_requests (
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
, authorized_by TEXT NOT NULL DEFAULT '')

-- table turn_locks
CREATE TABLE turn_locks (
    lock_id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL CHECK(scope_type IN ('ROOM')),
    scope_id TEXT NOT NULL,
    branch_id TEXT NOT NULL REFERENCES branches(branch_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'RELEASED')),
    acquired_by TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    released_at TEXT,
    release_reason TEXT NOT NULL DEFAULT ''
)

-- table user_bootstrap_contexts
CREATE TABLE user_bootstrap_contexts (
    user_id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL UNIQUE REFERENCES organizations(org_id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL UNIQUE REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    room_id TEXT NOT NULL UNIQUE REFERENCES rooms(room_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
)

-- table user_sessions
CREATE TABLE user_sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    idp_session_id TEXT,
    created_at TEXT NOT NULL,
    idle_expires_at TEXT NOT NULL,
    absolute_expires_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_reason TEXT NOT NULL DEFAULT '',
    -- Kept only to be handed back as id_token_hint at RP-initiated logout, which
    -- Keycloak requires before it will honour a post-logout redirect. It is the
    -- provider's assertion that a login happened, not a credential for this API:
    -- nothing here ever accepts it as one.
    idp_id_token TEXT NOT NULL DEFAULT '',
    -- The provider's own refresh token, spent on every refresh of ours. Without
    -- it a session never speaks to the provider again after login, so a person
    -- disabled, locked out, or password-reset at the identity provider keeps a
    -- live session here until the absolute clock runs out. Keycloak's refresh
    -- grant re-checks the user session every time; this is how we do the same.
    idp_refresh_token TEXT NOT NULL DEFAULT ''
)

-- table user_tokens
CREATE TABLE user_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    label TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT
, session_id TEXT, expires_at TEXT)

-- table users
CREATE TABLE users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    avatar_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'OFFLINE',
    created_at TEXT NOT NULL
)

-- table workspace_members
CREATE TABLE workspace_members (
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, user_id)
)

-- table workspaces
CREATE TABLE workspaces (
    workspace_id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES organizations(org_id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    created_at TEXT NOT NULL, allowed_capabilities TEXT,
    UNIQUE(org_id, slug)
)

-- trigger agent_identities_reject_delete
CREATE TRIGGER agent_identities_reject_delete
BEFORE DELETE ON agent_identities
WHEN OLD.revoked_at IS NOT NULL OR OLD.proof_mode = 'SIGNED_CHALLENGE'
BEGIN
    SELECT RAISE(ABORT, 'a revoked or key-bearing identity is never deleted');
END

-- trigger agent_identities_reject_duplicate_insert
CREATE TRIGGER agent_identities_reject_duplicate_insert
BEFORE INSERT ON agent_identities
WHEN EXISTS (
    SELECT 1 FROM agent_identities i
    WHERE i.identity_id = NEW.identity_id
       OR i.agent_id = NEW.agent_id
       OR i.key_fingerprint = NEW.key_fingerprint
)
BEGIN
    SELECT RAISE(ABORT, 'an agent identity is settled when it is written');
END

-- trigger agent_identities_reject_immutable_update
CREATE TRIGGER agent_identities_reject_immutable_update
BEFORE UPDATE OF identity_id, created_at, proof_mode, public_key, key_fingerprint, agent_id
ON agent_identities
BEGIN
    SELECT RAISE(ABORT, 'an agent identity is settled when it is written');
END

-- trigger agent_identities_revocation_is_permanent
CREATE TRIGGER agent_identities_revocation_is_permanent
BEFORE UPDATE ON agent_identities
WHEN OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at
BEGIN
    SELECT RAISE(ABORT, 'a revoked identity is never restored');
END

-- trigger agent_memberships_ids_are_written_once
CREATE TRIGGER agent_memberships_ids_are_written_once
BEFORE INSERT ON agent_room_memberships
WHEN EXISTS (
    SELECT 1 FROM agent_room_memberships m WHERE m.membership_id = NEW.membership_id
)
BEGIN
    SELECT RAISE(ABORT, 'a membership id is written once and never replaced');
END

-- trigger agent_memberships_reject_delete
CREATE TRIGGER agent_memberships_reject_delete
BEFORE DELETE ON agent_room_memberships
WHEN OLD.removed_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'a removal is a durable record and may not be deleted');
END

-- trigger agent_memberships_reject_duplicate_insert
CREATE TRIGGER agent_memberships_reject_duplicate_insert
BEFORE INSERT ON agent_room_memberships
WHEN EXISTS (
    SELECT 1 FROM agent_room_memberships m
    WHERE m.agent_id = NEW.agent_id AND m.room_id = NEW.room_id
      AND (m.removed_at IS NULL OR NEW.rejoined_from_membership_id IS NULL)
)
BEGIN
    SELECT RAISE(ABORT,
        'an agent that has left this room rejoins through a new membership naming its departure');
END

-- trigger agent_memberships_reject_key_update
CREATE TRIGGER agent_memberships_reject_key_update
BEFORE UPDATE OF membership_id, agent_id, room_id ON agent_room_memberships
WHEN NEW.membership_id <> OLD.membership_id
  OR NEW.agent_id <> OLD.agent_id
  OR NEW.room_id <> OLD.room_id
BEGIN
    SELECT RAISE(ABORT, 'an agent membership may not be re-pointed');
END

-- trigger agent_memberships_rejoin_names_a_departure
CREATE TRIGGER agent_memberships_rejoin_names_a_departure
BEFORE INSERT ON agent_room_memberships
WHEN NEW.rejoined_from_membership_id IS NOT NULL
  AND (NEW.rejoined_from_membership_id = NEW.membership_id
       OR NOT EXISTS (
    SELECT 1 FROM agent_room_memberships m
    WHERE m.membership_id = NEW.rejoined_from_membership_id
      AND m.membership_id <> NEW.membership_id
      AND m.agent_id = NEW.agent_id
      AND m.room_id = NEW.room_id
      AND m.removed_at IS NOT NULL
))
BEGIN
    SELECT RAISE(ABORT, 'a rejoin names the departure it follows');
END

-- trigger agent_memberships_removal_is_permanent
CREATE TRIGGER agent_memberships_removal_is_permanent
BEFORE UPDATE OF removed_at ON agent_room_memberships
WHEN OLD.removed_at IS NOT NULL
  AND (NEW.removed_at IS NULL OR NEW.removed_at <> OLD.removed_at)
BEGIN
    SELECT RAISE(ABORT, 'an agent removal may not be reversed or restamped in place');
END

-- trigger agent_outputs_reject_delete
CREATE TRIGGER agent_outputs_reject_delete
BEFORE DELETE ON agent_outputs
BEGIN
    SELECT RAISE(ABORT, 'agent_outputs are immutable');
END

-- trigger agent_outputs_reject_duplicate_insert
CREATE TRIGGER agent_outputs_reject_duplicate_insert
BEFORE INSERT ON agent_outputs
WHEN EXISTS (
    SELECT 1 FROM agent_outputs o
    WHERE o.output_id = NEW.output_id OR o.execution_id = NEW.execution_id
)
BEGIN
    SELECT RAISE(ABORT, 'agent_outputs are immutable');
END

-- trigger agent_outputs_reject_update
CREATE TRIGGER agent_outputs_reject_update
BEFORE UPDATE ON agent_outputs
BEGIN
    SELECT RAISE(ABORT, 'agent_outputs are immutable');
END

-- trigger agent_runs_record_acting_caller
CREATE TRIGGER agent_runs_record_acting_caller
AFTER UPDATE OF acting_user_id ON agent_runs
WHEN NEW.acting_user_id <> ''
BEGIN
    INSERT OR IGNORE INTO execution_callers(execution_id, caller_id, first_acted_at)
    VALUES (NEW.execution_id, NEW.acting_user_id, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
END

-- trigger agent_runs_record_launch_caller
CREATE TRIGGER agent_runs_record_launch_caller
AFTER INSERT ON agent_runs
WHEN NEW.acting_user_id <> '' AND NEW.acting_user_id <> NEW.authorized_by
BEGIN
    INSERT OR IGNORE INTO execution_callers(execution_id, caller_id, first_acted_at)
    VALUES (NEW.execution_id, NEW.acting_user_id, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
END

-- trigger agent_runs_reject_actor_update
CREATE TRIGGER agent_runs_reject_actor_update
BEFORE UPDATE OF agent_id, identity_id ON agent_runs
BEGIN SELECT RAISE(ABORT, 'a run may not be re-pointed at another agent or identity'); END

-- trigger agent_runs_reject_delete
CREATE TRIGGER agent_runs_reject_delete BEFORE DELETE ON agent_runs
BEGIN SELECT RAISE(ABORT, 'a run is an audit record and is never deleted'); END

-- trigger agent_runs_reject_duplicate_insert
CREATE TRIGGER agent_runs_reject_duplicate_insert
BEFORE INSERT ON agent_runs
WHEN EXISTS (
    SELECT 1 FROM agent_runs r
    WHERE r.run_id = NEW.run_id OR r.execution_id = NEW.execution_id
)
BEGIN
    SELECT RAISE(ABORT, 'a run is an audit record and is never rewritten');
END

-- trigger agent_runs_reject_key_update
CREATE TRIGGER agent_runs_reject_key_update
BEFORE UPDATE OF run_id, execution_id ON agent_runs
BEGIN
    SELECT RAISE(ABORT, 'a run is an audit record and is never rewritten');
END

-- trigger agent_runs_require_challenge_answer
CREATE TRIGGER agent_runs_require_challenge_answer BEFORE INSERT ON agent_runs
WHEN NEW.challenge_verified_at IS NULL AND EXISTS (
    SELECT 1 FROM agent_identities i WHERE i.identity_id = NEW.identity_id
        AND i.proof_mode = 'SIGNED_CHALLENGE')
BEGIN SELECT RAISE(ABORT, 'a signed-challenge agent must answer its launch challenge'); END

-- trigger agent_runs_require_live_identity
CREATE TRIGGER agent_runs_require_live_identity BEFORE INSERT ON agent_runs
WHEN NOT EXISTS (SELECT 1 FROM agent_identities i WHERE i.identity_id = NEW.identity_id
    AND i.agent_id = NEW.agent_id AND i.revoked_at IS NULL)
BEGIN SELECT RAISE(ABORT, 'an agent without a live identity may not launch'); END

-- trigger agent_runs_require_room_membership
CREATE TRIGGER agent_runs_require_room_membership BEFORE INSERT ON agent_runs
WHEN NOT EXISTS (SELECT 1 FROM agent_room_memberships m
    WHERE m.agent_id = NEW.agent_id AND m.room_id = NEW.room_id AND m.removed_at IS NULL)
BEGIN SELECT RAISE(ABORT, 'an agent removed from a room may not launch in it'); END

-- trigger agent_runs_settlement_is_final
CREATE TRIGGER agent_runs_settlement_is_final BEFORE UPDATE ON agent_runs
WHEN OLD.harness_state = 'SETTLED'
BEGIN SELECT RAISE(ABORT, 'a settled run is terminal'); END

-- trigger artifact_claim_sources_reject_delete
CREATE TRIGGER artifact_claim_sources_reject_delete
BEFORE DELETE ON artifact_claim_sources
BEGIN
    SELECT RAISE(ABORT, 'artifact claim provenance is immutable');
END

-- trigger artifact_claim_sources_reject_duplicate_insert
CREATE TRIGGER artifact_claim_sources_reject_duplicate_insert
BEFORE INSERT ON artifact_claim_sources
WHEN EXISTS (
    SELECT 1 FROM artifact_claim_sources s
    WHERE s.claim_id = NEW.claim_id AND s.output_id = NEW.output_id
)
BEGIN
    SELECT RAISE(ABORT, 'artifact claim provenance is immutable');
END

-- trigger artifact_claim_sources_reject_update
CREATE TRIGGER artifact_claim_sources_reject_update
BEFORE UPDATE ON artifact_claim_sources
BEGIN
    SELECT RAISE(ABORT, 'artifact claim provenance is immutable');
END

-- trigger artifact_claims_reject_delete
CREATE TRIGGER artifact_claims_reject_delete
BEFORE DELETE ON artifact_claims
BEGIN
    SELECT RAISE(ABORT, 'artifact claims are immutable');
END

-- trigger artifact_claims_reject_duplicate_insert
CREATE TRIGGER artifact_claims_reject_duplicate_insert
BEFORE INSERT ON artifact_claims
WHEN EXISTS (
    SELECT 1 FROM artifact_claims c
    WHERE c.claim_id = NEW.claim_id
       OR (c.version_id = NEW.version_id AND c.ordinal = NEW.ordinal)
)
BEGIN
    SELECT RAISE(ABORT, 'artifact claims are immutable');
END

-- trigger artifact_claims_reject_update
CREATE TRIGGER artifact_claims_reject_update
BEFORE UPDATE ON artifact_claims
BEGIN
    SELECT RAISE(ABORT, 'artifact claims are immutable');
END

-- trigger artifact_versions_lock_provenance_hash
CREATE TRIGGER artifact_versions_lock_provenance_hash
BEFORE UPDATE OF provenance_hash ON artifact_versions
WHEN OLD.provenance_hash <> '' OR NEW.provenance_hash = ''
BEGIN
    SELECT RAISE(ABORT, 'artifact provenance hash is immutable');
END

-- trigger artifact_versions_reject_content_update
CREATE TRIGGER artifact_versions_reject_content_update
BEFORE UPDATE OF artifact_id, version_number, content, content_hash ON artifact_versions
BEGIN
    SELECT RAISE(ABORT, 'artifact version content is immutable');
END

-- trigger artifact_versions_reject_delete
CREATE TRIGGER artifact_versions_reject_delete
BEFORE DELETE ON artifact_versions
BEGIN
    SELECT RAISE(ABORT, 'artifact versions are immutable');
END

-- trigger artifact_versions_reject_duplicate_insert
CREATE TRIGGER artifact_versions_reject_duplicate_insert
BEFORE INSERT ON artifact_versions
WHEN EXISTS (
    SELECT 1 FROM artifact_versions v
    WHERE v.version_id = NEW.version_id
       OR (v.artifact_id = NEW.artifact_id AND v.version_number = NEW.version_number)
)
BEGIN
    SELECT RAISE(ABORT, 'artifact versions are immutable');
END

-- trigger artifact_versions_reject_publication_identity_update
CREATE TRIGGER artifact_versions_reject_publication_identity_update
BEFORE UPDATE OF created_by, created_at ON artifact_versions
BEGIN
    SELECT RAISE(ABORT, 'artifact publication identity is immutable');
END

-- trigger artifact_versions_reject_synthesis_update
CREATE TRIGGER artifact_versions_reject_synthesis_update
BEFORE UPDATE OF branch_synthesis_id ON artifact_versions
BEGIN
    SELECT RAISE(ABORT, 'artifact synthesis provenance is immutable');
END

-- trigger branch_syntheses_reject_completed_update
CREATE TRIGGER branch_syntheses_reject_completed_update
BEFORE UPDATE OF
    branch_id, room_id, synthesis_type, status, initiated_by, provider_input,
    provider_name, provider_model, provider_response_id, provider_evidence,
    simulated, content, error, artifact_version_id, created_at, completed_at,
    token_usage
ON branch_syntheses
WHEN OLD.status IN ('COMPLETED', 'FAILED')
BEGIN
    SELECT RAISE(ABORT, 'terminal branch synthesis is immutable');
END

-- trigger branch_syntheses_require_matching_room
CREATE TRIGGER branch_syntheses_require_matching_room
BEFORE INSERT ON branch_syntheses
WHEN NOT EXISTS (
    SELECT 1 FROM branches b
    WHERE b.branch_id = NEW.branch_id AND b.room_id = NEW.room_id
)
BEGIN
    SELECT RAISE(ABORT, 'synthesis branch must belong to room');
END

-- trigger branch_synthesis_inputs_reject_delete
CREATE TRIGGER branch_synthesis_inputs_reject_delete
BEFORE DELETE ON branch_synthesis_inputs
BEGIN
    SELECT RAISE(ABORT, 'branch synthesis inputs are immutable');
END

-- trigger branch_synthesis_inputs_reject_update
CREATE TRIGGER branch_synthesis_inputs_reject_update
BEFORE UPDATE ON branch_synthesis_inputs
BEGIN
    SELECT RAISE(ABORT, 'branch synthesis inputs are immutable');
END

-- trigger branch_synthesis_inputs_require_selected_branch_output
CREATE TRIGGER branch_synthesis_inputs_require_selected_branch_output
BEFORE INSERT ON branch_synthesis_inputs
WHEN NOT EXISTS (
    SELECT 1
    FROM branch_syntheses s
    JOIN executions e ON e.branch_id = s.branch_id
    JOIN agent_outputs o ON o.execution_id = e.execution_id
    JOIN output_selections os
      ON os.output_id = o.output_id
     AND os.branch_id = s.branch_id
     AND os.disposition = 'INCLUDED'
    WHERE s.synthesis_id = NEW.synthesis_id AND o.output_id = NEW.output_id
)
BEGIN
    SELECT RAISE(ABORT, 'synthesis input must be a selected branch output');
END

-- trigger branches_reject_context_update
CREATE TRIGGER branches_reject_context_update
BEFORE UPDATE OF room_id, initiated_by, context_event_sequence,
    context_message_ids, context_hash ON branches
BEGIN
    SELECT RAISE(ABORT, 'branch context boundary is immutable');
END

-- trigger event_redactions_reject_delete
CREATE TRIGGER event_redactions_reject_delete
BEFORE DELETE ON event_redactions
BEGIN
    SELECT RAISE(ABORT, 'a redaction record is append-only');
END

-- trigger event_redactions_reject_update
CREATE TRIGGER event_redactions_reject_update
BEFORE UPDATE ON event_redactions
BEGIN
    SELECT RAISE(ABORT, 'a redaction record is append-only');
END

-- trigger execution_callers_are_never_deleted
CREATE TRIGGER execution_callers_are_never_deleted
BEFORE DELETE ON execution_callers
BEGIN
    SELECT RAISE(ABORT, 'a caller of a run is an audit record and is never deleted');
END

-- trigger execution_callers_are_written_once
CREATE TRIGGER execution_callers_are_written_once
BEFORE UPDATE ON execution_callers
BEGIN
    SELECT RAISE(ABORT, 'a caller of a run is an audit record and is never rewritten');
END

-- trigger execution_interventions_reject_authority_update
CREATE TRIGGER execution_interventions_reject_authority_update
BEFORE UPDATE OF intervened_by ON execution_interventions
BEGIN
    SELECT RAISE(ABORT, 'an intervention keeps the identity that produced it');
END

-- trigger execution_interventions_reject_delete
CREATE TRIGGER execution_interventions_reject_delete
BEFORE DELETE ON execution_interventions
BEGIN
    SELECT RAISE(ABORT, 'an intervention keeps the authority that produced it');
END

-- trigger executions_reject_authorized_by_update
CREATE TRIGGER executions_reject_authorized_by_update
BEFORE UPDATE OF authorized_by ON executions
BEGIN
    SELECT RAISE(ABORT, 'execution authorizing principal is immutable');
END

-- trigger executions_reject_branch_update
CREATE TRIGGER executions_reject_branch_update
BEFORE UPDATE OF branch_id ON executions
BEGIN
    SELECT RAISE(ABORT, 'execution branch is immutable');
END

-- trigger executions_reject_delete
CREATE TRIGGER executions_reject_delete
BEFORE DELETE ON executions
BEGIN
    SELECT RAISE(ABORT, 'execution authorizing principal is immutable');
END

-- trigger executions_reject_duplicate_insert
CREATE TRIGGER executions_reject_duplicate_insert
BEFORE INSERT ON executions
WHEN EXISTS (SELECT 1 FROM executions e WHERE e.execution_id = NEW.execution_id)
BEGIN
    SELECT RAISE(ABORT, 'execution authorizing principal is immutable');
END

-- trigger executions_require_authorized_by
CREATE TRIGGER executions_require_authorized_by
BEFORE INSERT ON executions
WHEN TRIM(NEW.authorized_by, ' ' || CHAR(9) || CHAR(10) || CHAR(13)) = ''
BEGIN
    SELECT RAISE(ABORT, 'execution authorizing principal is required');
END

-- trigger executions_require_branch
CREATE TRIGGER executions_require_branch
BEFORE INSERT ON executions
WHEN NEW.branch_id IS NULL
BEGIN
    SELECT RAISE(ABORT, 'execution branch is required');
END

-- trigger executions_require_matching_branch_room
CREATE TRIGGER executions_require_matching_branch_room
BEFORE INSERT ON executions
WHEN NOT EXISTS (
    SELECT 1
    FROM branches b
    JOIN sessions s ON s.session_id = NEW.session_id
    WHERE b.branch_id = NEW.branch_id AND b.room_id = s.room_id
)
BEGIN
    SELECT RAISE(ABORT, 'execution branch must belong to session room');
END

-- trigger idempotency_keys_reject_delete
CREATE TRIGGER idempotency_keys_reject_delete
BEFORE DELETE ON idempotency_keys
BEGIN
    SELECT RAISE(ABORT, 'idempotency records are immutable');
END

-- trigger idempotency_keys_reject_update
CREATE TRIGGER idempotency_keys_reject_update
BEFORE UPDATE ON idempotency_keys
BEGIN
    SELECT RAISE(ABORT, 'idempotency records are immutable');
END

-- trigger message_mentions_reject_user_invocation
CREATE TRIGGER message_mentions_reject_user_invocation
BEFORE INSERT ON message_mentions
WHEN NEW.invoked_execution_id IS NOT NULL AND NEW.target_type <> 'AGENT'
BEGIN
    SELECT RAISE(ABORT, 'only an agent mention can carry an invocation');
END

-- trigger message_reactions_reject_delete
CREATE TRIGGER message_reactions_reject_delete
BEFORE DELETE ON message_reactions
BEGIN
    SELECT RAISE(ABORT, 'a reaction is removed by removed_at, never by DELETE');
END

-- trigger messages_reject_content_update
CREATE TRIGGER messages_reject_content_update
BEFORE UPDATE OF content ON messages
WHEN NEW.content NOT GLOB '{"redacted": true, "redaction_id": "*"}'
BEGIN
    SELECT RAISE(ABORT, 'message content is immutable except through redaction');
END

-- trigger messages_reject_thread_update
CREATE TRIGGER messages_reject_thread_update
BEFORE UPDATE OF parent_message_id, root_message_id, thread_depth ON messages
BEGIN
    SELECT RAISE(ABORT, 'thread lineage is immutable');
END

-- trigger messages_require_root_with_parent
CREATE TRIGGER messages_require_root_with_parent
BEFORE INSERT ON messages
WHEN NEW.parent_message_id IS NULL AND NEW.root_message_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'a root message has no parent and no root');
END

-- trigger messages_require_thread_lineage
CREATE TRIGGER messages_require_thread_lineage
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
END

-- trigger ontology_extraction_cursors_reject_rewind
CREATE TRIGGER ontology_extraction_cursors_reject_rewind
BEFORE UPDATE ON ontology_extraction_cursors
WHEN NEW.last_sequence < OLD.last_sequence
BEGIN
    SELECT RAISE(ABORT, 'ontology extraction cursor must not rewind');
END

-- trigger ontology_reviews_reject_delete
CREATE TRIGGER ontology_reviews_reject_delete
BEFORE DELETE ON ontology_reviews
BEGIN
    SELECT RAISE(ABORT, 'ontology review history is immutable');
END

-- trigger ontology_reviews_reject_update
CREATE TRIGGER ontology_reviews_reject_update
BEFORE UPDATE ON ontology_reviews
BEGIN
    SELECT RAISE(ABORT, 'ontology review history is immutable');
END

-- trigger output_selections_reject_branch_update
CREATE TRIGGER output_selections_reject_branch_update
BEFORE UPDATE OF branch_id ON output_selections
WHEN OLD.branch_id <> NEW.branch_id
BEGIN
    SELECT RAISE(ABORT, 'output selection branch is immutable');
END

-- trigger output_selections_require_branch
CREATE TRIGGER output_selections_require_branch
BEFORE INSERT ON output_selections
WHEN NEW.branch_id IS NULL
BEGIN
    SELECT RAISE(ABORT, 'output selection branch is required');
END

-- trigger output_selections_require_output_branch
CREATE TRIGGER output_selections_require_output_branch
BEFORE INSERT ON output_selections
WHEN NOT EXISTS (
    SELECT 1
    FROM agent_outputs o
    JOIN executions e ON e.execution_id = o.execution_id
    WHERE o.output_id = NEW.output_id
      AND o.room_id = NEW.room_id
      AND e.branch_id = NEW.branch_id
)
BEGIN
    SELECT RAISE(ABORT, 'selection output must belong to branch');
END

-- trigger room_events_reject_delete
CREATE TRIGGER room_events_reject_delete
BEFORE DELETE ON room_events
BEGIN
    SELECT RAISE(ABORT, 'the event log is append-only');
END

-- trigger room_events_reject_hash_rewrite
CREATE TRIGGER room_events_reject_hash_rewrite
BEFORE UPDATE OF prev_hash, event_hash ON room_events
WHEN OLD.event_hash IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'a chained hash may not be rewritten once set');
END

-- trigger room_events_reject_identity_update
CREATE TRIGGER room_events_reject_identity_update
BEFORE UPDATE OF event_id, room_id, sequence, event_type, actor_id, actor_type,
    timestamp, schema_version ON room_events
BEGIN
    SELECT RAISE(ABORT, 'an event''s identity and header are immutable');
END

-- trigger room_participant_handles_reject_update
CREATE TRIGGER room_participant_handles_reject_update
BEFORE UPDATE OF handle, participant_id, participant_type ON room_participant_handles
BEGIN
    SELECT RAISE(ABORT, 'a handle is durable once issued');
END

-- trigger room_postures_are_never_deleted
CREATE TRIGGER room_postures_are_never_deleted
BEFORE DELETE ON room_postures
BEGIN
    SELECT RAISE(ABORT, 'a posture declaration is an audit record and is never deleted');
END

-- trigger room_postures_are_never_replaced
CREATE TRIGGER room_postures_are_never_replaced
BEFORE INSERT ON room_postures
WHEN EXISTS (SELECT 1 FROM room_postures WHERE declaration_id = NEW.declaration_id)
BEGIN
    SELECT RAISE(ABORT, 'a posture declaration is an audit record and is never rewritten');
END

-- trigger room_postures_are_written_once
CREATE TRIGGER room_postures_are_written_once
BEFORE UPDATE ON room_postures
BEGIN
    SELECT RAISE(ABORT, 'a posture declaration is an audit record and is never rewritten');
END

-- trigger room_sequences_reject_delete
CREATE TRIGGER room_sequences_reject_delete
BEFORE DELETE ON room_sequences
BEGIN
    SELECT RAISE(ABORT, 'a room sequence counter is never removed');
END

-- trigger room_sequences_reject_rewind
CREATE TRIGGER room_sequences_reject_rewind
BEFORE UPDATE ON room_sequences
WHEN NEW.seq <= OLD.seq
BEGIN
    SELECT RAISE(ABORT, 'a room sequence counter only moves forward');
END

-- trigger search_documents_after_delete
CREATE TRIGGER search_documents_after_delete
AFTER DELETE ON search_documents
BEGIN
    INSERT INTO search_documents_fts(search_documents_fts, rowid, content)
    VALUES ('delete', OLD.document_id, OLD.content);
END

-- trigger search_documents_after_insert
CREATE TRIGGER search_documents_after_insert
AFTER INSERT ON search_documents
BEGIN
    INSERT INTO search_documents_fts(rowid, content) VALUES (NEW.document_id, NEW.content);
END

-- trigger search_documents_after_update
CREATE TRIGGER search_documents_after_update
AFTER UPDATE ON search_documents
BEGIN
    INSERT INTO search_documents_fts(search_documents_fts, rowid, content)
    VALUES ('delete', OLD.document_id, OLD.content);
    INSERT INTO search_documents_fts(rowid, content) VALUES (NEW.document_id, NEW.content);
END

-- trigger search_documents_forget_deleted_decision
CREATE TRIGGER search_documents_forget_deleted_decision
AFTER DELETE ON decisions
BEGIN
    DELETE FROM search_documents
    WHERE object_kind = 'DECISION' AND object_id = OLD.decision_id;
END

-- trigger search_documents_forget_deleted_message
CREATE TRIGGER search_documents_forget_deleted_message
AFTER DELETE ON messages
BEGIN
    DELETE FROM search_documents
    WHERE object_kind = 'MESSAGE' AND object_id = OLD.message_id;
END

-- trigger search_documents_forget_deleted_task
CREATE TRIGGER search_documents_forget_deleted_task
AFTER DELETE ON tasks
BEGIN
    DELETE FROM search_documents
    WHERE object_kind = 'TASK' AND object_id = OLD.task_id;
END

-- trigger tool_request_reviewers_are_never_deleted
CREATE TRIGGER tool_request_reviewers_are_never_deleted
BEFORE DELETE ON tool_request_reviewers
BEGIN
    SELECT RAISE(ABORT, 'a reviewer of a call is an audit record and is never deleted');
END

-- trigger tool_request_reviewers_are_written_once
CREATE TRIGGER tool_request_reviewers_are_written_once
BEFORE UPDATE ON tool_request_reviewers
BEGIN
    SELECT RAISE(ABORT, 'a reviewer of a call is an audit record and is never rewritten');
END

-- trigger turn_locks_reject_identity_update
CREATE TRIGGER turn_locks_reject_identity_update
BEFORE UPDATE OF scope_type, scope_id, branch_id, acquired_by, acquired_at ON turn_locks
BEGIN
    SELECT RAISE(ABORT, 'turn lock identity is immutable');
END

-- trigger turn_locks_require_room_branch
CREATE TRIGGER turn_locks_require_room_branch
BEFORE INSERT ON turn_locks
WHEN NEW.scope_type <> 'ROOM'
  OR NOT EXISTS (
      SELECT 1 FROM branches b
      WHERE b.branch_id = NEW.branch_id AND b.room_id = NEW.scope_id
  )
BEGIN
    SELECT RAISE(ABORT, 'turn lock branch must own room scope');
END

