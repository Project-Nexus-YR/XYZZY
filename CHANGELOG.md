# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Governance is structurally outside the agent surface: while a model-driven
  turn executes, twenty-two governance methods - policies, postures, capability
  bounds, membership and roles, approvals, identity, agent and run control -
  refuse to run at all, whoever called them, through whatever path.
- Untrusted input is screened before a model reads it: invisible Unicode
  channels stripped, length bounded, member-authored and tool-returned
  content fenced as data with its origin named. Deterministic; cannot fail
  open.
- The benchmark baseline's concessions are now verified against pinned
  source commits of buzz and qm, with one claim withdrawn and two corrected.

- The deployment settings a server needs are configuration rather than source:
  `XYZZY_HOST`, `XYZZY_PORT`, `XYZZY_CORS_ORIGINS`, `XYZZY_RATE_LIMIT_PER_MINUTE`,
  `XYZZY_MAX_BODY_BYTES` and `XYZZY_LOG_LEVEL`. A `*` origin is refused rather
  than accepted, because paired with credentials it lets any site spend a
  signed-in session.
- A per-principal rate limit and a declared-body-size cap in front of every route
  but the health probe. Both count in one process: they bound one server's
  exposure, not a fleet's.
- `.github/workflows/ci.yml` runs the four gates on every push and pull request,
  on the 3.11 floor the project declares rather than the interpreter development
  happens on.
- A `Dockerfile` that runs as a non-root user, keeps the database on a mounted
  volume, and health-checks itself against the readiness probe.

- External sign-in over OpenID Connect: authorization code with PKCE against any
  provider, discovered from its issuer. XYZZY is a relying party and mints its
  own session rather than handing the provider's tokens to the browser, so a
  revoked session fails the very next request instead of surviving until a
  self-validating access token expires.
- Sessions carry two clocks — an idle clock that moves while the session is used
  and an absolute one that never moves — and both are read in the statement that
  authenticates.
- Refresh tokens rotate once. Presenting a spent one revokes the whole session
  rather than the token, because a replay means a copy exists somewhere it
  should not.
- Back-channel logout per OpenID Connect Back-Channel Logout 1.0, including the
  three checks it is easy to omit: the `events` claim, the refusal of a token
  carrying `nonce`, and the refusal of a replayed `jti`.
- Sign out here, or everywhere. Both are outside the agent surface, so no
  model-driven turn can mint, extend, or end a person's session.

- An agent may ask another agent for work. The asking is a task with Google's
  A2A lifecycle - the specification's own eight states, spelled the way it
  spells them, because these strings go on the wire. What a delegate may spend
  is its asker's authority intersected with its own, and it is re-derived from
  durable rows at the moment of spending rather than captured when the task
  opened: narrow the asker mid-task and the delegate narrows with it, and an
  asker that has left the room lends nothing.
- A delegation chain is rows rather than a claim. The parent is read from the
  delegating agent's own open run, never taken from the caller, so a cycle
  cannot be opened by declining to name one; the chain table cannot hold one
  agent twice; and depth is counted from the chain rather than stored beside
  it. A2A has no name for either refusal, so both carry the reason in an
  `UnsupportedOperationError` instead of minting a code in the range the
  specification reserves.
- The A2A wire surface: JSON-RPC 2.0 at `POST /a2a/v1` with the specification's
  eight methods and its named error codes, Server-Sent-Events streaming built
  on the room's existing event log rather than a second delivery path, and an
  Agent Card at `/.well-known/agent-card.json`. The public card advertises the
  door and no agents at all - a room's membership is the access decision, so a
  public list of agents would publish the shape of a private workspace to
  anyone who fetched a URL. The authenticated card shows each caller only the
  agents that caller could actually address, so no two callers share one
  document.
- Push notification is advertised as unsupported and then refused by name. A
  webhook fan-out would be a second delivery path with weaker guarantees than
  the durable ordered log clients already have, and a server that advertises
  false and then accepts the call is worse than one that declines.

### Changed

- `GET /api/v1/health` reads from the database instead of returning a constant,
  and answers 503 when it cannot. A process listening with a database it cannot
  open is no longer reported ready.
- The product is now XYZZY. The distribution is `xyzzy`, the environment
  variables are `XYZZY_AUTH_TOKENS`, `XYZZY_OPENAI_MODEL` and
  `XYZZY_MODEL_TIMEOUT_SECONDS`, and the WebSocket subprotocol is `xyzzy.v1`.
  Provenance envelopes and branch context snapshots written from here on name
  `xyzzy.artifact-provenance.v2` and `xyzzy.branch-context.v1`; envelopes
  written before the rename keep the old identifier and their original hash.

### Fixed

- `set_workspace_policy` bounds every room in the workspace but was gated on
  bare membership; it now requires the workspace admin role, re-read inside
  the transaction that writes. `remove_room_member` gets the same
  in-transaction re-check its route already implied.
- The migration transaction guard also catches a `COMMIT` that shares a line
  with another statement.
- Realtime subscription ids were the event loop's clock to six decimals plus
  `id(self)`, and `self` is the single hub, so the identifier was a timestamp
  and nothing else. Two sockets opening inside one clock tick took the same
  string, and the second replaced the first in the dictionary
  `revoke_room_access` searches - leaving the displaced socket unrevokable and
  still receiving a room whose access had been withdrawn.
- `__version__` looked itself up under the distribution's pre-rename name, so
  it had been reporting `0.0.0+uninstalled` since the rename.
- The README documented the sign-in endpoints at `/auth/...` when every one of
  them is mounted under `/api/v1`, which would have sent a deployment's
  `XYZZY_OIDC_REDIRECT_URI` somewhere that answers 404.

## [0.2.0] - 2026-08-24

### Added

- Tamper-evident hash chain over the room event log: every appended event
  commits to its predecessor, and `python -m multiplayer.manage <db> audit
  verify` recomputes the chains and names the first divergence per room.
- Database-backed credentials: hashed at rest, one row per token, revocable
  without a restart, managed by the operator CLI (`user add`, `token mint`,
  `token revoke`, `token list`).
- A WAL reader pool: a read outside a transaction no longer waits behind an
  unrelated write transaction (previously measured blocking over a second).
- The MIT `LICENSE` file the project metadata already claimed.

### Changed

- `MULTIAI_AUTH_TOKENS` is bootstrap-only. Authentication reads the
  `user_tokens` table on every request, so a revocation takes effect on the
  next call; a revoked bootstrap token stays revoked across restarts.
- A live WebSocket re-authenticates on its heartbeat and closes within about
  two beats of its credential being revoked.
- The server reports the installed package version instead of a hardcoded one.

### Fixed

- Migrations now apply atomically with the row that records them: a crash
  mid-migration leaves the database exactly at the previous migration, and a
  migration that manages its own transaction is refused outright.
- Removed the vendored `.agents` bundle: 125 unlicensed third-party files
  referenced by nothing, conflicting with the MIT claim.

## [0.1.0] - 2026-08-23

Initial workspace: event-sourced rooms with WebSocket sync, conversation
layer, five-way capability terms with a tool gateway and human approval,
agent identity and harness protocol, room postures, decision workflow, and
the lazy ontology with Meta answers.
