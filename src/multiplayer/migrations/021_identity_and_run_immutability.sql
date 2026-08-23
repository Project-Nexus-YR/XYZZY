-- The two immutability claims migration 016 made in prose, made true in the schema.
--
-- 016 said agent identity was "one immutable row" and that a settled run was
-- terminal. Neither was enforced. agent_runs refused UPDATE, DELETE and UPSERT on a
-- settled run, but INSERT OR REPLACE reopened one as STREAMING with no settlement and
-- re-pointed it at another agent and identity: recursive_triggers is off in this
-- codebase's connection setup, so the delete REPLACE performs never reaches
-- agent_runs_reject_delete. Migration 018 met that same bypass on executions and
-- answered it with a duplicate-insert guard standing beside the delete guard; this is
-- that pair again, for the table 016 left half-closed.
--
-- agent_identities carried no trigger at all. A plain UPDATE un-revoked a revoked
-- identity and downgraded a SIGNED_CHALLENGE identity to IN_PROCESS, after which a
-- launch the service had just refused succeeded. The fail-closed story rests on that
-- row, so the row is what has to hold.

-- ── A run is written once ───────────────────────────────────────────────────

-- The counterpart to agent_runs_reject_delete, exactly as
-- executions_reject_duplicate_insert is to executions_reject_delete. execution_id is
-- tested as well as run_id because REPLACE resolves a conflict on any unique index,
-- and a fresh run_id landing on a settled run's execution_id launders the settlement
-- just as thoroughly as reusing the run_id does.
CREATE TRIGGER IF NOT EXISTS agent_runs_reject_duplicate_insert
BEFORE INSERT ON agent_runs
WHEN EXISTS (
    SELECT 1 FROM agent_runs r
    WHERE r.run_id = NEW.run_id OR r.execution_id = NEW.execution_id
)
BEGIN
    SELECT RAISE(ABORT, 'a run is an audit record and is never rewritten');
END;

-- REPLACE is not only an INSERT keyword. UPDATE OR REPLACE resolves its conflict the
-- same silent way: moving an open run onto a settled run's execution_id deleted the
-- settled row and left the open one wearing it, and no UPDATE trigger fired, because
-- the row being updated was the open one. A run's own two keys never change after
-- insert, so freezing them removes the only unique indexes such a collision can be
-- aimed at.
CREATE TRIGGER IF NOT EXISTS agent_runs_reject_key_update
BEFORE UPDATE OF run_id, execution_id ON agent_runs
BEGIN
    SELECT RAISE(ABORT, 'a run is an audit record and is never rewritten');
END;

-- ── An identity is settled when it is written ───────────────────────────────

-- revoked_at is the only column a later write may touch, as consumed_at is on an
-- intervention: who the identity belongs to, how it proves itself, and the key it
-- proves itself with are all decided at registration. Freezing agent_id and
-- key_fingerprint also closes UPDATE OR REPLACE here, since with identity_id they are
-- this table's three unique indexes.
CREATE TRIGGER IF NOT EXISTS agent_identities_reject_immutable_update
BEFORE UPDATE OF identity_id, created_at, proof_mode, public_key, key_fingerprint, agent_id
ON agent_identities
BEGIN
    SELECT RAISE(ABORT, 'an agent identity is settled when it is written');
END;

-- Revocation is a one-way door: NULL to a time, once. Moving the timestamp is barred
-- with clearing it, because a revocation backdated or postdated is a different claim
-- about when the agent stopped being allowed to act.
CREATE TRIGGER IF NOT EXISTS agent_identities_revocation_is_permanent
BEFORE UPDATE ON agent_identities
WHEN OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NOT OLD.revoked_at
BEGIN
    SELECT RAISE(ABORT, 'a revoked identity is never restored');
END;

-- Without this the UPDATE guards above are advisory: INSERT OR REPLACE on any of the
-- three unique indexes deletes the row and writes the attacker's in its place, and
-- the delete guard below never sees it.
CREATE TRIGGER IF NOT EXISTS agent_identities_reject_duplicate_insert
BEFORE INSERT ON agent_identities
WHEN EXISTS (
    SELECT 1 FROM agent_identities i
    WHERE i.identity_id = NEW.identity_id
       OR i.agent_id = NEW.agent_id
       OR i.key_fingerprint = NEW.key_fingerprint
)
BEGIN
    SELECT RAISE(ABORT, 'an agent identity is settled when it is written');
END;

-- Deletion is refused where deletion would launder something. A revoked row and a
-- key-bearing row carry the two facts an attacker wants gone, and losing either one
-- widens what the agent may do. A live keyless IN_PROCESS row carries neither: it is
-- already the weakest identity this schema can hold, deleting it only stops the agent
-- launching at all, and anything written in its place is equal or stricter. That
-- narrow window is also how an identity is upgraded to SIGNED_CHALLENGE, and it
-- closes on its own the moment the identity is used, because agent_runs.identity_id
-- is ON DELETE RESTRICT.
--
-- This covers the parent route as well: agent_identities.agent_id is ON DELETE
-- CASCADE, and unlike the delete REPLACE performs, a foreign key cascade does fire
-- the child's delete trigger with recursive_triggers off. Dropping the instance to
-- take the identity with it and re-register the same agent_id clean is refused here.
CREATE TRIGGER IF NOT EXISTS agent_identities_reject_delete
BEFORE DELETE ON agent_identities
WHEN OLD.revoked_at IS NOT NULL OR OLD.proof_mode = 'SIGNED_CHALLENGE'
BEGIN
    SELECT RAISE(ABORT, 'a revoked or key-bearing identity is never deleted');
END;
