-- Removal is a gate, and a persisted capability set is not an authorization input.
--
-- Two defects with one shape: a fact was written down and then never read, or read
-- long after it stopped being true.
--
-- 1. remove_room_membership_in_transaction stamped agent_room_memberships.removed_at
--    and nothing consulted it. The roster read agent_instances with no join, and the
--    paths that open a run gated on identity, addressing, capability and harness but
--    never on membership, so a removed agent stayed on the roster, kept its handle,
--    and answered the next mention as if it had never left.
--
-- 2. execution_interventions.capabilities froze the intervener's effective set at the
--    moment she steered, on a row this migration's predecessor made immutable. The run
--    principal's authority is re-derived at spend time; hers was not, so narrowing her
--    — or removing her from the room — left the stale set bounding the step.

-- ── Membership is a gate ────────────────────────────────────────────────────

-- Every agent spawned since 001 gets a membership row, but an instance written
-- before the table would have none, and the trigger below refuses a run without
-- one. A removed agent already has its row, with removed_at set, so OR IGNORE
-- leaves the removal alone rather than readmitting it.
INSERT OR IGNORE INTO agent_room_memberships(agent_id, room_id, joined_at)
SELECT a.agent_id, a.room_id, a.created_at FROM agent_instances a;

-- The third fail-closed launch guard, beside the live-identity and challenge ones
-- from 016 and for the same reason: a future code path that forgets the service
-- check still cannot open a run for an agent that is not in the room.
CREATE TRIGGER IF NOT EXISTS agent_runs_require_room_membership BEFORE INSERT ON agent_runs
WHEN NOT EXISTS (SELECT 1 FROM agent_room_memberships m
    WHERE m.agent_id = NEW.agent_id AND m.room_id = NEW.room_id AND m.removed_at IS NULL)
BEGIN SELECT RAISE(ABORT, 'an agent removed from a room may not launch in it'); END;

-- ── A steer records who, never what they held ───────────────────────────────

-- 018 named the column immutable so nobody could discard the bound. Immutable is
-- exactly the problem: an authorization input that cannot be updated cannot be
-- narrowed either, and the column outlived every later change to its author's
-- grant. Dropping it is what stops the next reader treating it as authority; who
-- steered stays on the row, and the step re-derives that person's set when it
-- spends the text. The trigger is recreated without the column it named.
DROP TRIGGER IF EXISTS execution_interventions_reject_authority_update;

ALTER TABLE execution_interventions DROP COLUMN capabilities;

CREATE TRIGGER execution_interventions_reject_authority_update
BEFORE UPDATE OF intervened_by, instruction ON execution_interventions
BEGIN
    SELECT RAISE(ABORT, 'an intervention keeps the identity that produced it');
END;
