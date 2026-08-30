-- An outward door keeps no key.
--
-- Every capability this schema has granted so far stays inside the room: a
-- membership row, an addressing grant, an approval — each one is checked
-- against a durable relationship the grantee already has to the workspace.
-- A share is the first capability meant to leave that boundary entirely and
-- be redeemed by whoever holds the link, with no membership check at all.
-- That is exactly why the credential itself is never the thing this table
-- keeps: token_hash is a sha256 of a secrets.token_urlsafe(32) minted once by
-- the server and handed back exactly once, in the create response. A door
-- that kept the key in the lock would make every later read of this table —
-- a backup, a debug dump, an admin's careless SELECT * — a way to mint a
-- fresh copy of every link ever issued. Keeping only the hash means the
-- table can leak in full and nobody outside the room gains a single read
-- they didn't already have; the actual bearer token lives only in the one
-- HTTP response that created it, and in whatever the recipient does with it
-- from there.
--
-- revoked_at is soft, matching the workspace's own audit discipline
-- (memberships end this way too, see 024 and 026): a share that leaks and
-- gets pulled back leaves a row that says a link existed and when it was
-- cut, rather than erasing the fact it was ever handed out. The public GET
-- checks revoked_at IS NULL and otherwise answers the same 404 it gives an
-- unknown token — revocation is an internal fact, never a status this route
-- distinguishes for whoever is holding the link.
--
-- room_id is denormalized off the artifact row it was created for rather than
-- joined at read time, because the capability check that gates creation and
-- revocation is a room-scoped ADMINISTER check like every other governance
-- write here (11, 018), and every other such check in this codebase takes a
-- room_id directly rather than resolving one through a second table first.
CREATE TABLE artifact_shares (
    share_id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

-- The public route's only lookup: a token hash to a live share.
CREATE INDEX idx_artifact_shares_token_hash ON artifact_shares(token_hash);
-- Listing an artifact's shares for its admin's own management view.
CREATE INDEX idx_artifact_shares_artifact ON artifact_shares(artifact_id);
