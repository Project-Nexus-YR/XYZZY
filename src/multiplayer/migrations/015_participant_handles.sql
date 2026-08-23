-- Durable handles for everyone a room can address, and an actor type on reactions.
--
-- A mention used to be matched against whatever the roster happened to look like:
-- an agent's id, or its display name lowercased. A display name is not an address.
-- It may contain spaces, so "Security Reviewer" was unmentionable by any spelling,
-- and it may change, which would silently retire an address people already use.
--
-- A handle is the address. It is derived from the display name once, at creation,
-- suffixed until it is unique in the room, and then it is durable: renaming the
-- participant does not move it. Members and agents share one table because they
-- share one namespace — @finance must mean exactly one participant in a room, and
-- the unique index below is what makes that true rather than a convention.
--
-- Existing rows are backfilled by MultiplayerService._backfill_participant_handles
-- immediately after this migration applies. The backfill is Python rather than SQL
-- so that one function derives every handle in the system; a second normaliser
-- written in SQL here would be free to disagree with the write path, and the first
-- symptom of the disagreement would be a mention that resolves to the wrong person.

CREATE TABLE IF NOT EXISTS room_participant_handles (
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    participant_type TEXT NOT NULL CHECK(participant_type IN ('USER', 'AGENT')),
    participant_id TEXT NOT NULL,
    handle TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (room_id, participant_type, participant_id)
);

-- Uniqueness within the room is the whole point of a handle, so the database
-- holds it. A collision suffix chosen on the write path is a race without this.
CREATE UNIQUE INDEX IF NOT EXISTS idx_room_participant_handles_unique
    ON room_participant_handles(room_id, handle);

-- A handle is an address, not a label. Repointing one at a different participant
-- would rewrite who past mentions addressed, so a row here is insert-only.
CREATE TRIGGER IF NOT EXISTS room_participant_handles_reject_update
BEFORE UPDATE OF handle, participant_id, participant_type ON room_participant_handles
BEGIN
    SELECT RAISE(ABORT, 'a handle is durable once issued');
END;

-- ── Reaction actors ─────────────────────────────────────────────────────────

-- Reactions predate agents being able to leave one, so every existing row is a
-- user's. The column says which kind of principal acted, because a reaction from
-- an agent is authorized against the agent's own room membership and a reaction
-- from a member against theirs, and a reader has to be told which one they see.
ALTER TABLE message_reactions ADD COLUMN actor_type TEXT NOT NULL DEFAULT 'USER'
    CHECK(actor_type IN ('USER', 'AGENT'));
