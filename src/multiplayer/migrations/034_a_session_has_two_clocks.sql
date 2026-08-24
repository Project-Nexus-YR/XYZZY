-- A session has two clocks, and a refresh token is spent once.
--
-- user_tokens made a credential a row so a revocation could be a written fact.
-- A session is the same argument one level up: a human who signed in through an
-- identity provider holds a credential that must expire on its own, be
-- refreshable without a second trip to the provider, and die everywhere the
-- moment it is revoked.
--
-- Two clocks, because one cannot express both rules. idle_expires_at moves
-- forward while the session is used and is what makes an abandoned laptop stop
-- being a way in; absolute_expires_at never moves and is what stops a session
-- that is used constantly from living for ever. A session is alive only while
-- both hold, and both are read in the same statement that authenticates, so
-- neither can be true in one place and false in another.
--
-- session_refresh_tokens holds the digest, never the token, and records the
-- token each one replaced. A refresh is one-time use: consuming it stamps
-- consumed_at, and presenting a token that already carries one is either a theft
-- or a bug. Both deserve the session, so reuse revokes the whole row rather than
-- the one token, which is what Keycloak does and for the same reason.
--
-- oidc_authorizations is the pending half of a login: the state that carries CSRF
-- protection, the nonce that carries replay protection, and the PKCE verifier,
-- all bound to one attempt and consumable once. Nothing about a login is trusted
-- from the browser's copy of it.
--
-- oidc_logout_tokens remembers the jti of every back-channel logout already
-- acted on, because a logout token replayed is a request to kill a session that
-- may since have been legitimately re-established.

CREATE TABLE user_sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    idp_session_id TEXT,
    created_at TEXT NOT NULL,
    idle_expires_at TEXT NOT NULL,
    absolute_expires_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_reason TEXT NOT NULL DEFAULT '',
    -- Kept only to be handed back as id_token_hint at RP-initiated logout, which
    -- Keycloak requires before it will honour a post-logout redirect. It is the
    -- provider's assertion that a login happened, not a credential for this API:
    -- nothing here ever accepts it as one.
    idp_id_token TEXT NOT NULL DEFAULT '',
    -- The provider's own refresh token, spent on every refresh of ours. Without
    -- it a session never speaks to the provider again after login, so a person
    -- disabled, locked out, or password-reset at the identity provider keeps a
    -- live session here until the absolute clock runs out. Keycloak's refresh
    -- grant re-checks the user session every time; this is how we do the same.
    idp_refresh_token TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_user_sessions_user ON user_sessions(user_id);
CREATE INDEX idx_user_sessions_sid ON user_sessions(issuer, idp_session_id);
CREATE INDEX idx_user_sessions_subject ON user_sessions(issuer, subject);

CREATE TABLE session_refresh_tokens (
    token_hash TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES user_sessions(session_id),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    replaced_by_hash TEXT
);

CREATE INDEX idx_session_refresh_session ON session_refresh_tokens(session_id);

CREATE TABLE oidc_authorizations (
    state TEXT PRIMARY KEY,
    nonce TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    -- The digest of a cookie set on the browser that began this login. Without
    -- it, state lives only on the server, and anyone who obtains a state value
    -- can finish a login somebody else started: the victim's browser ends up
    -- holding a session for the attacker's account. Login CSRF is the name.
    browser_binding_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT
);

CREATE TABLE oidc_logout_tokens (
    jti TEXT NOT NULL,
    issuer TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (jti, issuer)
);

-- An access credential minted for a session dies with it. A credential an
-- operator minted has no session and keeps working exactly as before, so a
-- deployment with no identity provider configured is untouched by all of this.
ALTER TABLE user_tokens ADD COLUMN session_id TEXT;

-- And it dies on its own, which is the half a revocation cannot cover. Immediate
-- revocation only helps somebody who knows to revoke; a stolen credential nobody
-- has noticed is bounded by nothing else. A short life forces the thief through
-- the refresh rotation, where reuse detection is waiting.
--
-- NULL means no expiry, which is exactly what an operator-minted credential had
-- before this column existed and still has.
ALTER TABLE user_tokens ADD COLUMN expires_at TEXT;

CREATE INDEX idx_user_tokens_session ON user_tokens(session_id);
