-- A departure is a record, so a return is a new row rather than an edit to that one.
--
-- Migration 024 closed UPDATE and DELETE on agent_room_memberships and deliberately
-- left out the duplicate-insert guard that 018 and 021 both carry, on the reasoning
-- that the rejoin path was an INSERT OR IGNORE a guard would turn into an abort.
-- That reasoning was wrong twice.
--
-- It was exploitable. recursive_triggers is off, so the delete an INSERT OR REPLACE
-- performs never reaches agent_memberships_reject_delete: replacing the (agent_id,
-- room_id) primary key row put removed_at back to NULL, returned the agent to the
-- roster, and let start_agent_session write session.started after agent.left_room --
-- the false record 024 was written to prevent.
--
-- And the flow it was protecting did not exist. add_room_membership is INSERT OR
-- IGNORE, so it silently no-opped against a removed row, and no verb added an agent
-- back to a room at all. A removed agent could not rejoin by any path.
--
-- Both are answered by making this table hold a history rather than a state. A
-- membership is keyed by its own id, at most one membership per (agent_id, room_id)
-- is live at a time, and a rejoin is a new row naming the departure it follows,
-- exactly as a resumed run names the run it continues. The removal row stays.

-- ── The table, rebuilt around a per-membership key ───────────────────────────

-- 020's launch guard reads this table by name, and ALTER TABLE ... RENAME re-parses
-- every trigger in the schema; it is dropped here and restated unchanged at the end.
DROP TRIGGER IF EXISTS agent_runs_require_room_membership;

-- DROP TABLE would take these with it, but only after this script has referred to a
-- table they guard. Dropping them first keeps the rebuild independent of whether
-- SQLite fires a trigger on an implicit delete.
DROP TRIGGER IF EXISTS agent_memberships_removal_is_permanent;
DROP TRIGGER IF EXISTS agent_memberships_reject_key_update;
DROP TRIGGER IF EXISTS agent_memberships_reject_delete;

CREATE TABLE agent_room_memberships_v2 (
    membership_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id) ON DELETE CASCADE,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    joined_at TEXT NOT NULL,
    removed_at TEXT,
    -- The departure this membership follows. NULL for a first join.
    rejoined_from_membership_id TEXT
);

INSERT INTO agent_room_memberships_v2(
    membership_id, agent_id, room_id, joined_at, removed_at, rejoined_from_membership_id)
SELECT 'member_' || lower(hex(randomblob(8))), agent_id, room_id, joined_at, removed_at, NULL
FROM agent_room_memberships;

DROP TABLE agent_room_memberships;

ALTER TABLE agent_room_memberships_v2 RENAME TO agent_room_memberships;

-- One live membership per agent per room. A removed row sits outside this index,
-- which is also what puts it beyond an INSERT OR REPLACE: REPLACE only deletes the
-- rows it collides with on a unique index.
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_room_memberships_live
    ON agent_room_memberships(agent_id, room_id) WHERE removed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_agent_room_memberships_agent_room
    ON agent_room_memberships(agent_id, room_id);

-- ── 024's guards, restated on the rebuilt table ──────────────────────────────

CREATE TRIGGER agent_memberships_removal_is_permanent
BEFORE UPDATE OF removed_at ON agent_room_memberships
WHEN OLD.removed_at IS NOT NULL
  AND (NEW.removed_at IS NULL OR NEW.removed_at <> OLD.removed_at)
BEGIN
    SELECT RAISE(ABORT, 'an agent removal may not be reversed or restamped in place');
END;

CREATE TRIGGER agent_memberships_reject_key_update
BEFORE UPDATE OF membership_id, agent_id, room_id ON agent_room_memberships
WHEN NEW.membership_id <> OLD.membership_id
  OR NEW.agent_id <> OLD.agent_id
  OR NEW.room_id <> OLD.room_id
BEGIN
    SELECT RAISE(ABORT, 'an agent membership may not be re-pointed');
END;

CREATE TRIGGER agent_memberships_reject_delete
BEFORE DELETE ON agent_room_memberships
WHEN OLD.removed_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'a removal is a durable record and may not be deleted');
END;

-- ── The guard 024 left out ──────────────────────────────────────────────────

-- The counterpart to agent_memberships_reject_delete, as
-- executions_reject_duplicate_insert is to executions_reject_delete and
-- agent_runs_reject_duplicate_insert is to agent_runs_reject_delete. An insert may
-- not land on an agent already in this room, and may not put an agent that has left
-- back on the roster unless it says which departure it is returning from. A bare
-- INSERT OR REPLACE aimed at a removed membership fails here.
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
END;

-- And the departure it names has to be one: this agent's, this room's, and removed.
CREATE TRIGGER agent_memberships_rejoin_names_a_departure
BEFORE INSERT ON agent_room_memberships
WHEN NEW.rejoined_from_membership_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM agent_room_memberships m
    WHERE m.membership_id = NEW.rejoined_from_membership_id
      AND m.agent_id = NEW.agent_id
      AND m.room_id = NEW.room_id
      AND m.removed_at IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'a rejoin names the departure it follows');
END;

-- 020's third fail-closed launch guard, unchanged.
CREATE TRIGGER agent_runs_require_room_membership BEFORE INSERT ON agent_runs
WHEN NOT EXISTS (SELECT 1 FROM agent_room_memberships m
    WHERE m.agent_id = NEW.agent_id AND m.room_id = NEW.room_id AND m.removed_at IS NULL)
BEGIN SELECT RAISE(ABORT, 'an agent removed from a room may not launch in it'); END;

-- ── A suspended turn outlives the process that suspended it ─────────────────

-- The rest of a turn that stopped at a reviewer was held in a per-process dict, so an
-- approval decided on any second process found nothing to resume: approve_action put
-- the run back on a fresh STREAMING lease and then nobody prompted it, which is the
-- silence the lease exists to rule out. The continuation is durable here instead, so
-- whichever process decides the approval can carry the turn to its answer.
--
-- It holds no authority. The prompt, what this turn's tools already returned, and who
-- has steered it are records; every capability is re-derived from durable rows at
-- each prompt, exactly as it is for a turn that never suspended.
CREATE TABLE IF NOT EXISTS suspended_turns (
    execution_id TEXT PRIMARY KEY REFERENCES executions(execution_id) ON DELETE CASCADE,
    prompt TEXT NOT NULL,
    acting_as TEXT NOT NULL,
    -- JSON arrays: the gateway's own records of this turn's tool calls, in order,
    -- and the user ids whose steers shaped it.
    observations TEXT NOT NULL,
    steerers TEXT NOT NULL,
    suspended_at TEXT NOT NULL
);
