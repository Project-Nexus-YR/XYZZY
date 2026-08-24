-- A credential is a row, not a deployment.
--
-- Identity used to be MULTIAI_AUTH_TOKENS: a JSON blob of plaintext tokens that
-- lived in the process environment, could not be revoked without a restart, and
-- named users no table knew. user_tokens makes each credential one row: the
-- digest at rest (never the token), the user it binds to, and a revocation that
-- is a written fact rather than a redeploy. Authentication reads the table on
-- every request, so the moment revoked_at is set the next call fails.
--
-- Bootstrap tokens from the environment are ingested with DO NOTHING, so a row
-- an operator revoked stays revoked across restarts.

CREATE TABLE user_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    label TEXT,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE INDEX idx_user_tokens_user ON user_tokens(user_id);
