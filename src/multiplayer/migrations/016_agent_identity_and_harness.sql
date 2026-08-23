-- Agent identity, delegated authority, durable addressing, and the run envelope.
--
-- An agent instance could launch a run with nothing durable saying which agent
-- process it was, who it acted for, or who was allowed to point it. This migration
-- makes each of those a record: an immutable identity row, an authorizing human on
-- every gated write, an addressing mode owned by the workspace rather than by the
-- harness, and an agent_runs row that carries the transport state of one turn.
--
-- agent_runs is the envelope around an executions row, not a second state machine
-- over the same fact: executions.status stays the domain state and
-- agent_runs.harness_state the transport, each mapping to one domain status.

CREATE TABLE IF NOT EXISTS agent_identities (
    identity_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, revoked_at TEXT,
    proof_mode TEXT NOT NULL CHECK(proof_mode IN ('IN_PROCESS','SIGNED_CHALLENGE')),
    public_key TEXT, key_fingerprint TEXT UNIQUE,
    agent_id TEXT NOT NULL UNIQUE REFERENCES agent_instances(agent_id) ON DELETE CASCADE,
    -- A key exists exactly when there is an untrusted transport to prove authorship across.
    CHECK((proof_mode = 'SIGNED_CHALLENGE') = (public_key IS NOT NULL)));

CREATE TABLE IF NOT EXISTS agent_addressing (
    agent_id TEXT PRIMARY KEY REFERENCES agent_instances(agent_id) ON DELETE CASCADE,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK(mode IN ('OWNER_ONLY','ALLOWLIST','ANYONE','NOBODY')),
    owner_user_id TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS agent_address_allowlist (
    agent_id TEXT NOT NULL REFERENCES agent_addressing(agent_id) ON DELETE CASCADE,
    user_id TEXT NOT NULL, added_by TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (agent_id, user_id));

-- Every parent below is RESTRICT: a run must outlive what it names. Under CASCADE,
-- deleting an instance wiped its runs, so identity_id's RESTRICT never fired and the
-- trail went in one delete.
--
-- challenge_verified_at is how the launch guard below reads "the challenge was
-- answered". The service verifies the signature and stamps this column on the row it
-- inserts; a SIGNED_CHALLENGE identity with the column still NULL never launches.
--
-- attempts and max_attempts are the lease's companion: a run whose dispatcher dies is
-- swept, and a run that has been picked up max_attempts times and died every time is
-- parked rather than swept again, so a stuck run reaches a state a reader can name.
CREATE TABLE IF NOT EXISTS agent_runs (run_id TEXT PRIMARY KEY,
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
        'ORPHANED','AUTHORITY_REVOKED','AGENT_REMOVED','APPROVAL_REFUSED','PARKED')),
    resumed_from_run_id TEXT REFERENCES agent_runs(run_id) ON DELETE RESTRICT,
    lease_expires_at TEXT NOT NULL, created_at TEXT NOT NULL, settled_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 1, max_attempts INTEGER NOT NULL DEFAULT 3,
    -- Settled with no settlement is terminal to the machine and invisible to the sweep: stuck.
    CHECK(harness_state <> 'SETTLED' OR settlement IS NOT NULL),
    CHECK(attempts >= 1 AND max_attempts >= 1));

CREATE INDEX IF NOT EXISTS idx_runs_open ON agent_runs(lease_expires_at) WHERE harness_state <> 'SETTLED';
CREATE INDEX IF NOT EXISTS idx_runs_agent_room ON agent_runs(agent_id, room_id);

-- Fail-closed launch, below the service so a future code path cannot launch anonymously.
CREATE TRIGGER IF NOT EXISTS agent_runs_require_live_identity BEFORE INSERT ON agent_runs
WHEN NOT EXISTS (SELECT 1 FROM agent_identities i WHERE i.identity_id = NEW.identity_id
    AND i.agent_id = NEW.agent_id AND i.revoked_at IS NULL)
BEGIN SELECT RAISE(ABORT, 'an agent without a live identity may not launch'); END;

-- The second half of the same refusal: in SIGNED_CHALLENGE mode a live identity is not
-- enough, because the key proves nothing until this launch answered with it.
CREATE TRIGGER IF NOT EXISTS agent_runs_require_challenge_answer BEFORE INSERT ON agent_runs
WHEN NEW.challenge_verified_at IS NULL AND EXISTS (
    SELECT 1 FROM agent_identities i WHERE i.identity_id = NEW.identity_id
        AND i.proof_mode = 'SIGNED_CHALLENGE')
BEGIN SELECT RAISE(ABORT, 'a signed-challenge agent must answer its launch challenge'); END;

-- The INSERT trigger guards only the first write. These three close the ways a run was
-- otherwise rewritten: settled twice, re-pointed at another agent, or deleted and
-- reinserted to launder a settlement the UPDATE guard refused.
CREATE TRIGGER IF NOT EXISTS agent_runs_settlement_is_final BEFORE UPDATE ON agent_runs
WHEN OLD.harness_state = 'SETTLED'
BEGIN SELECT RAISE(ABORT, 'a settled run is terminal'); END;

CREATE TRIGGER IF NOT EXISTS agent_runs_reject_actor_update
BEFORE UPDATE OF agent_id, identity_id ON agent_runs
BEGIN SELECT RAISE(ABORT, 'a run may not be re-pointed at another agent or identity'); END;

CREATE TRIGGER IF NOT EXISTS agent_runs_reject_delete BEFORE DELETE ON agent_runs
BEGIN SELECT RAISE(ABORT, 'a run is an audit record and is never deleted'); END;

ALTER TABLE agent_instances ADD COLUMN harness_id TEXT NOT NULL DEFAULT 'nexus';
ALTER TABLE tool_requests ADD COLUMN authorized_by TEXT NOT NULL DEFAULT '';
ALTER TABLE approvals ADD COLUMN authorized_by TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_room_memberships ADD COLUMN removed_at TEXT;

-- ── Backfill ────────────────────────────────────────────────────────────────
--
-- Every harness today runs in this process, so an existing instance gets an
-- IN_PROCESS identity with no key: inventing a keypair would invent a secret to
-- guard a boundary that does not exist.
INSERT INTO agent_identities (identity_id, created_at, proof_mode, agent_id)
SELECT 'ident_' || lower(hex(randomblob(16))), a.created_at, 'IN_PROCESS', a.agent_id
FROM agent_instances a
WHERE NOT EXISTS (SELECT 1 FROM agent_identities i WHERE i.agent_id = a.agent_id);

-- OWNER_ONLY owned by the room's creator: the narrowest mode that keeps existing
-- rooms working, where ANYONE would silently widen them.
INSERT INTO agent_addressing (agent_id, room_id, mode, owner_user_id, updated_at, updated_by)
SELECT a.agent_id, a.room_id, 'OWNER_ONLY', COALESCE(r.created_by, ''), a.created_at, 'system'
FROM agent_instances a
JOIN rooms r ON r.room_id = a.room_id
WHERE NOT EXISTS (SELECT 1 FROM agent_addressing g WHERE g.agent_id = a.agent_id);

-- Historical runs get their envelope so the sweep and the settled-run refusal cover
-- them too. acting_user_id backfills to authorized_by, the only caller those runs are
-- known to have had, and a run left empty there is honestly unknown. The credential
-- hash is random: no credential was ever issued, so none may ever match. A run that is
-- not terminal gets a lease already in the past, because nothing is dispatching it.
INSERT INTO agent_runs (run_id, execution_id, agent_id, identity_id, room_id, authorized_by,
    acting_user_id, harness_id, credential_hash, harness_state, settlement,
    lease_expires_at, created_at, settled_at)
SELECT 'arun_' || lower(hex(randomblob(16))), e.execution_id, e.agent_id, i.identity_id,
    s.room_id, e.authorized_by, e.authorized_by, 'nexus', lower(hex(randomblob(32))),
    CASE WHEN e.status IN ('COMPLETED','FAILED','CANCELLED') THEN 'SETTLED' ELSE 'STARTING' END,
    CASE e.status WHEN 'COMPLETED' THEN 'END_TURN' WHEN 'FAILED' THEN 'FAILED'
        WHEN 'CANCELLED' THEN 'CANCELLED' ELSE NULL END,
    COALESCE(e.completed_at, e.started_at), e.started_at,
    CASE WHEN e.status IN ('COMPLETED','FAILED','CANCELLED')
        THEN COALESCE(e.completed_at, e.started_at) ELSE NULL END
FROM executions e
JOIN sessions s ON s.session_id = e.session_id
JOIN agent_identities i ON i.agent_id = e.agent_id
WHERE NOT EXISTS (SELECT 1 FROM agent_runs r WHERE r.execution_id = e.execution_id);
