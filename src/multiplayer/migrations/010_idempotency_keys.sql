-- Durable idempotency records for retry-prone public writes. A record is
-- claimed inside the same transaction as the state mutation and its canonical
-- event, so a retried request replays the original result and appends nothing.
-- Keys are scoped to the authenticated principal and the room or branch the
-- write targets; two users may reuse the same key without colliding.

CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope_id, user_id, idempotency_key)
);

CREATE TRIGGER IF NOT EXISTS idempotency_keys_reject_delete
BEFORE DELETE ON idempotency_keys
BEGIN
    SELECT RAISE(ABORT, 'idempotency records are immutable');
END;

CREATE TRIGGER IF NOT EXISTS idempotency_keys_reject_update
BEFORE UPDATE ON idempotency_keys
BEGIN
    SELECT RAISE(ABORT, 'idempotency records are immutable');
END;
