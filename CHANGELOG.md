# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Six correctness findings from the project's first external audit, each
  verified against the tree before it was fixed. Reconnect now pages past
  the repository's 500 row limit, so a room's full history reaches a
  returning client (measured: 512 seeded, 512 returned, previously 500).
  The WebSocket revalidation heartbeat rechecks room membership as well as
  credentials, so a removed member's socket closes within one cycle even
  when the cross process revocation message is lost. An agent task's move
  to WORKING, its run creation, and its execution attachment commit as one
  transaction, as do a tool request's terminal state and the event that
  records it, so a crash can no longer strand half a fact. A2A message/send
  now actually dispatches the accepted task in a supervised background
  task, and a startup drain heals tasks stranded by a crash between accept
  and dispatch, one at a time so a backlog cannot stampede a restart. The
  rate limiter caps how many token buckets one address can mint (rotating
  junk credentials now shares the address budget instead of buying fresh
  ones) and the bucket store is a hard capped LRU, so its memory is bounded
  always.

- A spawn's per agent model_provider and model_name are no longer accepted
  and then ignored. Each provider now carries a verified identity read off
  what it already puts in a response, spawn refuses a caller supplied
  identity that disagrees with the one this process actually runs, and an
  empty field is stored as that configured identity so the agent row
  describes itself. Provenance no longer falls back to an agent declared
  string when a response omits its own identity; it falls back to the
  configured provider instead, so an unverified caller string can no longer
  enter the audit trail.

- The client round of the first external audit. The publish flow now asks
  for a synthesis title instead of hard coding one for every branch, prefilled
  from the branch prompt and editable. A branch launch that failed halfway
  used to leave the agents it had already spawned behind and strand pending
  runs on navigation; the launch now removes what it spawned before
  reporting the failure, says honestly when that removal itself failed, and
  a visible control offers to resume runs found pending on load rather than
  doing so silently. Decisions can be created where the evaluation guide
  says they can, and the demo seeds a third human so the seeded room matches
  the three to five person flow the product describes. An accessibility
  pass covers branch actions reachable at 390 pixels wide, search hits
  focusable and Enter activated, dialogs named and returning focus to their
  opener with focus entering on open, the mobile drawer carrying modal
  semantics, small controls grown to a 44 pixel target without letting
  neighbouring targets overlap, and the Ask Meta input labelled.

- A "subscribe" message for an extra room dropped the returned subscription
  on the floor: nothing drained its queue, so events into that room never
  reached the socket, and nothing released it on close, so the hub kept it
  for the process lifetime. "unsubscribe" also released every subscription
  the user held to that room across every one of their sockets, not just
  the one that asked. A socket now keeps every subscription it creates,
  primary and extras, delivers events from all of them through one send
  loop, releases only its own subscription on "unsubscribe", and every exit
  path (a clean close, a dropped connection, a revocation) releases all of
  them.

- Message idempotency ignored attachments: a retry with the same key and
  different attachment_ids replayed the original message as if it were
  identical. The hashed request now folds in the sorted attachment ids
  whenever the send carries any, so a retry with different attachments
  raises the existing "key already used for a different request" conflict
  and a retry with the same attachments in any order still replays; a send
  with no attachments hashes exactly as it did before.

- The room state payload's runs left out branch_id, so the client's four
  branch panels that filter runs by it never rendered. Each run in the
  state payload now carries its branch_id.

### Added

- The realtime hub scales past one process: with `XYZZY_REDIS_URL` set
  (install `xyzzy[redis]`), room events, session revocations, and user
  notifications fan out across processes through Redis pub/sub, and presence
  stays correct cluster-wide through keys that expire on silence. Transport
  is deliberately best-effort: the sequence-numbered log with reconnect
  replay remains the single source of truth, so a dropped message costs
  latency, never correctness. When Redis is down each process degrades to
  single-process behavior on its own, and local delivery never waits on a
  publish.
- A `live-provider` workflow, triggered by hand, spends one real API call to
  prove the genuine provider path produces model-written output. The gate
  suite keeps its fake transport; this is the opt-in other half, and the
  test behind it skips loudly when no `OPENAI_API_KEY` is present.

## [0.3.0] - 2026-08-31

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

- Custom agent templates (migration 038): a workspace writes its own
  specialists as rows beside the built-ins, create membership-gated with the
  capability re-checked inside the write transaction, names unique
  case-insensitively across built-ins and the workspace's own, delete
  creator-or-admin and soft because spawned agents hold an enforced foreign
  key against the row vanishing. A workspace-authored system prompt is
  untrusted member text and reaches the provider only through
  screen()+fenced(), unlike the trusted built-ins, and a deleted template's
  agents keep working because the template id is resolved at spawn time, not
  re-read afterward.
- File attachments (migration 039): multipart upload, capped by
  `XYZZY_MAX_ATTACHMENT_BYTES` with the body-cap middleware exempting exactly
  that route, binding to a message in-transaction and requiring same room,
  same uploader, unbound. Serving is nosniff and filename-sanitized, and only
  the png/jpeg/webp/gif allowlist keeps its real content type; attachment
  bytes never enter any model, screening, or synthesis path, so only fenced
  filename and size metadata can appear in message text.
- Streaming audit export at `GET /rooms/{id}/audit-export` streams a room's
  event log as ndjson with its stored hash-chain fields, paging past the
  repository layer's 500-row default so no event is silently dropped; the
  summary line reuses `verify_event_chain` and reports `chain_verified` false
  unless the exported count equals the room's own sequence counter, so the
  claim is checked against production, not just against a test.
- Zero-config demo mode: `XYZZY_DEMO=1`, `--demo`, or
  `docker compose --profile demo up` boots a seeded workspace — a real
  conversation with a thread and reactions, two specialists run in a parallel
  branch offline through the simulated provider, a published Decision Brief —
  behind one-click entry with no token, channel, or account. Demo mode
  refuses to coexist with OIDC or real auth tokens, so it can never be bolted
  onto a live deployment, and the seed is idempotent across restarts with
  message timestamps staggered across a plausible morning while the event
  chain keeps its true times and still verifies.
- Public read-only share links (migration 040): a room admin can publish an
  artifact to a public URL, create/list/revoke gated on the admin capability
  and re-checked inside the write transaction, and the token is returned
  exactly once with only its sha256 stored. The public page serves the
  artifact's escaped content and nothing else — no member names, no room
  name, no ids — and answers a constant 404 for unknown, revoked, and
  malformed tokens alike.
- The public landing page under `site/`: a single self-contained
  `site/index.html` with every claim on the page checked against the README
  and `SECURITY.md`, product screenshots that are real captures of the seeded
  demo workspace in both themes, and no invented logos, stars, or
  testimonials.
- The image workflow publishes `ghcr.io/project-nexus-yr/xyzzy` on every push
  to `main` and on version tags, so the quickstart in both the README and the
  landing page reduces to `docker run` with `XYZZY_DEMO=1` and nothing else.
- Room templates (migration 041): a workspace saves a recipe of name,
  description, and preselected specialists, and creating a room from one
  commits the room and every specialist spawn in a single transaction, so a
  recipe whose specialist vanished mid-flight creates nothing at all rather
  than a room with half its team. The recipe's id rides the room-creation
  event, so the audit trail shows where a room came from.
- Template sharing (migration 042): a workspace template can be shared
  org-wide, visible and spawnable from sibling workspaces, marked with its
  origin, and revocable at any moment with the revocation re-checked inside
  the spawn transaction. A shared prompt stays exactly as untrusted as it was
  at home, since screen()+fenced() applies identically wherever it is
  spawned; cross-org sharing stays refused, and built-ins have nothing to
  share.

### Changed

- `GET /api/v1/health` reads from the database instead of returning a constant,
  and answers 503 when it cannot. A process listening with a database it cannot
  open is no longer reported ready.
- The license changes from MIT to Apache 2.0, the same permissiveness plus an
  explicit patent grant, switched while the author is still the only
  contributor and the change needs nobody else's consent; the badge, landing
  page, project metadata, and the commercialization decision record all
  follow.
- The roadmap section leaves the README: everything on it that could be
  checked is shipped and tested, so it needs no list. The two
  deliberately-unscheduled epics and the beyond-the-org marketplace move to
  `docs/BACKLOG.md` with their reasoning, alongside a record of which
  launch-round server features still await a client surface.
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
- Custom agent templates seeded no capabilities, so an agent spawned from one
  could never execute a turn; template creation now seeds real capabilities
  alongside the row.
- Creating a room from a template spawned its specialists outside the room's
  creation transaction, so a specialist that vanished mid-flight could leave
  a room with half its team; room and every specialist spawn now commit in
  one transaction, caught in review before it shipped.

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
