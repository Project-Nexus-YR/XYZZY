-- Generic turn ownership. Only ROOM scope is accepted in this milestone; scope
-- can narrow later without changing the lock identity model.

CREATE TABLE IF NOT EXISTS turn_locks (
    lock_id TEXT PRIMARY KEY,
    scope_type TEXT NOT NULL CHECK(scope_type IN ('ROOM')),
    scope_id TEXT NOT NULL,
    branch_id TEXT NOT NULL REFERENCES branches(branch_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK(status IN ('ACTIVE', 'RELEASED')),
    acquired_by TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    released_at TEXT,
    release_reason TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_turn_locks_one_active_scope
    ON turn_locks(scope_type, scope_id)
    WHERE status = 'ACTIVE';

CREATE INDEX IF NOT EXISTS idx_turn_locks_branch
    ON turn_locks(branch_id, acquired_at, lock_id);

CREATE TRIGGER IF NOT EXISTS turn_locks_require_room_branch
BEFORE INSERT ON turn_locks
WHEN NEW.scope_type <> 'ROOM'
  OR NOT EXISTS (
      SELECT 1 FROM branches b
      WHERE b.branch_id = NEW.branch_id AND b.room_id = NEW.scope_id
  )
BEGIN
    SELECT RAISE(ABORT, 'turn lock branch must own room scope');
END;

CREATE TRIGGER IF NOT EXISTS turn_locks_reject_identity_update
BEFORE UPDATE OF scope_type, scope_id, branch_id, acquired_by, acquired_at ON turn_locks
BEGIN
    SELECT RAISE(ABORT, 'turn lock identity is immutable');
END;
