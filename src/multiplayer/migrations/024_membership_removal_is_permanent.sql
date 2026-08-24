-- Removal is a gate, so the row the gate reads has to hold.
--
-- Migration 020 made agent_room_memberships.removed_at the thing every launch door
-- consults, and 021 made revocation permanent on agent_identities for exactly this
-- reason. The membership table was left with no trigger at all, so a plain
-- UPDATE ... SET removed_at = NULL un-removed a removed agent and put it back on the
-- roster with its handle. No service path reaches that today; this is the same
-- defence-in-depth 021 gives identity, on the table 020's gate rests on.
--
-- Rejoining is a legitimate thing to want. It is a new membership, written by the
-- service through the join path, not an edit that erases the fact of a departure.

-- ── A removal is not reversible in place ────────────────────────────────────

CREATE TRIGGER IF NOT EXISTS agent_memberships_removal_is_permanent
BEFORE UPDATE OF removed_at ON agent_room_memberships
WHEN OLD.removed_at IS NOT NULL
  AND (NEW.removed_at IS NULL OR NEW.removed_at <> OLD.removed_at)
BEGIN
    SELECT RAISE(ABORT, 'an agent removal may not be reversed or restamped in place');
END;

-- The identifying columns settle when the row is written. Re-pointing either of them
-- moves a live membership onto another agent or another room, which is a grant, not
-- an edit.
CREATE TRIGGER IF NOT EXISTS agent_memberships_reject_key_update
BEFORE UPDATE OF agent_id, room_id ON agent_room_memberships
WHEN NEW.agent_id <> OLD.agent_id OR NEW.room_id <> OLD.room_id
BEGIN
    SELECT RAISE(ABORT, 'an agent membership may not be re-pointed');
END;

-- ── A removal is a durable record ───────────────────────────────────────────

-- Deliberately NOT added here: a duplicate-insert guard of the kind 018 and 021 use.
-- The rejoin path is `INSERT OR IGNORE` (db/repositories.py), so with a removed row
-- still on the primary key a rejoin silently does nothing today, and a guard here
-- would turn that silence into an abort - changing a flow rather than hardening one,
-- on a path no test covers. Both behaviours are wrong; the right answer is a real
-- rejoin that writes a new membership, which is a change worth making deliberately
-- rather than as a side effect of a trigger.

CREATE TRIGGER IF NOT EXISTS agent_memberships_reject_delete
BEFORE DELETE ON agent_room_memberships
WHEN OLD.removed_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'a removal is a durable record and may not be deleted');
END;
