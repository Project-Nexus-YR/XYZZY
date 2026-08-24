-- Three records that were saying something untrue, and one cache that made a rule
-- into something a spend-point had to remember.
--
-- 1. agent_runs.settlement had no name for "a reviewer never answered". A run holding
--    at an approval whose lease ran out was settled ORPHANED — nothing was orphaned —
--    and the vocabulary is a CHECK constraint, so extending it is a table rebuild.
-- 2. suspended_turns.steerers was a copy of who had steered a turn. The steers
--    themselves live in execution_interventions with the identity that produced each
--    one, so the column was a second answer to a question that already had a durable
--    one, and a second place for that answer to go stale. Every copy of a capability
--    input is where the next relocation of this defect lives.
-- 3. agent_room_memberships let a rejoin name ITSELF. 026's guard asked for an insert
--    to name a departure of the same agent and room that is removed; an INSERT OR
--    REPLACE whose membership_id is an existing removed row satisfies that against
--    the row it is about to replace, and because the replacement is live, neither the
--    update guard nor the delete guard — both of which fire only on a removed row —
--    applies to it afterwards. Verified: self-naming REPLACE, then UPDATE removed_at
--    = NULL, then DELETE, leaving zero rows and no departure. It fails closed, so no
--    access was granted; what it destroyed was the audit record.

-- ── 1. A settlement for an approval nobody answered ─────────────────────────

-- SQLite cannot alter a CHECK, so agent_runs is rebuilt around the widened one. The
-- triggers go first: ALTER TABLE ... RENAME re-parses every trigger in the schema,
-- and each is restated verbatim at the end. Foreign keys are off across the rebuild
-- because resumed_from_run_id points back into this same table with ON DELETE
-- RESTRICT, and DROP TABLE performs an implicit delete that RESTRICT would refuse.
PRAGMA foreign_keys=OFF;

DROP TRIGGER IF EXISTS agent_runs_require_live_identity;
DROP TRIGGER IF EXISTS agent_runs_require_challenge_answer;
DROP TRIGGER IF EXISTS agent_runs_require_room_membership;
DROP TRIGGER IF EXISTS agent_runs_settlement_is_final;
DROP TRIGGER IF EXISTS agent_runs_reject_actor_update;
DROP TRIGGER IF EXISTS agent_runs_reject_key_update;
DROP TRIGGER IF EXISTS agent_runs_reject_delete;
DROP TRIGGER IF EXISTS agent_runs_reject_duplicate_insert;

CREATE TABLE agent_runs_v2 (run_id TEXT PRIMARY KEY,
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
    CHECK(attempts >= 1 AND max_attempts >= 1));

INSERT INTO agent_runs_v2 (run_id, execution_id, agent_id, identity_id, room_id,
    authorized_by, acting_user_id, harness_id, credential_hash, challenge_verified_at,
    harness_state, settlement, resumed_from_run_id, lease_expires_at, created_at,
    settled_at, attempts, max_attempts)
SELECT run_id, execution_id, agent_id, identity_id, room_id,
    authorized_by, acting_user_id, harness_id, credential_hash, challenge_verified_at,
    harness_state, settlement, resumed_from_run_id, lease_expires_at, created_at,
    settled_at, attempts, max_attempts
FROM agent_runs;

DROP TABLE agent_runs;
ALTER TABLE agent_runs_v2 RENAME TO agent_runs;

CREATE INDEX IF NOT EXISTS idx_runs_open ON agent_runs(lease_expires_at) WHERE harness_state <> 'SETTLED';
CREATE INDEX IF NOT EXISTS idx_runs_agent_room ON agent_runs(agent_id, room_id);

-- 016's fail-closed launch guards, restated unchanged.
CREATE TRIGGER agent_runs_require_live_identity BEFORE INSERT ON agent_runs
WHEN NOT EXISTS (SELECT 1 FROM agent_identities i WHERE i.identity_id = NEW.identity_id
    AND i.agent_id = NEW.agent_id AND i.revoked_at IS NULL)
BEGIN SELECT RAISE(ABORT, 'an agent without a live identity may not launch'); END;

CREATE TRIGGER agent_runs_require_challenge_answer BEFORE INSERT ON agent_runs
WHEN NEW.challenge_verified_at IS NULL AND EXISTS (
    SELECT 1 FROM agent_identities i WHERE i.identity_id = NEW.identity_id
        AND i.proof_mode = 'SIGNED_CHALLENGE')
BEGIN SELECT RAISE(ABORT, 'a signed-challenge agent must answer its launch challenge'); END;

-- 020's membership guard, restated as 026 last stated it.
CREATE TRIGGER agent_runs_require_room_membership BEFORE INSERT ON agent_runs
WHEN NOT EXISTS (SELECT 1 FROM agent_room_memberships m
    WHERE m.agent_id = NEW.agent_id AND m.room_id = NEW.room_id AND m.removed_at IS NULL)
BEGIN SELECT RAISE(ABORT, 'an agent removed from a room may not launch in it'); END;

-- 016's immutability guards, restated unchanged.
CREATE TRIGGER agent_runs_settlement_is_final BEFORE UPDATE ON agent_runs
WHEN OLD.harness_state = 'SETTLED'
BEGIN SELECT RAISE(ABORT, 'a settled run is terminal'); END;

CREATE TRIGGER agent_runs_reject_actor_update
BEFORE UPDATE OF agent_id, identity_id ON agent_runs
BEGIN SELECT RAISE(ABORT, 'a run may not be re-pointed at another agent or identity'); END;

CREATE TRIGGER agent_runs_reject_delete BEFORE DELETE ON agent_runs
BEGIN SELECT RAISE(ABORT, 'a run is an audit record and is never deleted'); END;

-- 021's guards, restated unchanged.
CREATE TRIGGER agent_runs_reject_duplicate_insert
BEFORE INSERT ON agent_runs
WHEN EXISTS (
    SELECT 1 FROM agent_runs r
    WHERE r.run_id = NEW.run_id OR r.execution_id = NEW.execution_id
)
BEGIN
    SELECT RAISE(ABORT, 'a run is an audit record and is never rewritten');
END;

CREATE TRIGGER agent_runs_reject_key_update
BEFORE UPDATE OF run_id, execution_id ON agent_runs
BEGIN
    SELECT RAISE(ABORT, 'a run is an audit record and is never rewritten');
END;

PRAGMA foreign_keys=ON;

-- ── 2. The steerers of a turn are read, never carried ───────────────────────

-- suspended_turns holds the prompt and what this turn's tools already returned. Who
-- steered it is answered by execution_interventions.intervened_by, which is where the
-- steer itself is, so the copy is dropped rather than kept in step.
CREATE TABLE suspended_turns_v2 (
    execution_id TEXT PRIMARY KEY REFERENCES executions(execution_id) ON DELETE CASCADE,
    prompt TEXT NOT NULL,
    acting_as TEXT NOT NULL,
    -- A JSON array: the gateway's own records of this turn's tool calls, in order.
    observations TEXT NOT NULL,
    suspended_at TEXT NOT NULL
);

INSERT INTO suspended_turns_v2(execution_id, prompt, acting_as, observations, suspended_at)
SELECT execution_id, prompt, acting_as, observations, suspended_at FROM suspended_turns;

DROP TABLE suspended_turns;
ALTER TABLE suspended_turns_v2 RENAME TO suspended_turns;

-- ── 3. A rejoin names a different row, and no insert lands on an existing one ──

-- The general form of the self-naming hole: INSERT OR REPLACE deletes the row it
-- collides with on the primary key, and with recursive_triggers off that delete never
-- reaches agent_memberships_reject_delete. Refusing an insert that reuses a
-- membership_id at all is what closes it — a removed membership can no longer be
-- deleted by being written over, whichever row the new one claims to follow.
CREATE TRIGGER agent_memberships_ids_are_written_once
BEFORE INSERT ON agent_room_memberships
WHEN EXISTS (
    SELECT 1 FROM agent_room_memberships m WHERE m.membership_id = NEW.membership_id
)
BEGIN
    SELECT RAISE(ABORT, 'a membership id is written once and never replaced');
END;

-- And the rejoin guard now asks for a departure that is a different row. Naming
-- itself was never a rejoin: there is no departure before a row that does not exist
-- yet, and the only thing such an insert can do is stand where a removal used to.
DROP TRIGGER IF EXISTS agent_memberships_rejoin_names_a_departure;
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
END;
