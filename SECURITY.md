# Security Policy

## Supported Versions

XYZZY is pre-1.0. The `0.4.x` line is the only one that receives fixes.

| Version | Supported |
| --- | --- |
| 0.4.x | yes |
| < 0.4 | no |

## Reporting a Vulnerability

Use GitHub's private vulnerability reporting on this repository (Security tab
→ "Report a vulnerability"). Do not open a public issue for a security
finding, and do not email a maintainer: private reporting is the only
channel this project monitors for security reports. Include the endpoint or
code path, a reproduction, and the impact you believe it has. Expect an
acknowledgement within a few days; there is no fixed SLA yet at this stage of
the project.

## Security Model

This section is a factual summary with pointers into the code, not a claim
that the system is unbreakable. Read the linked files before relying on any
of it.

**Capability intersection on every tool call.** What an agent may do for a
given tool call is the intersection of five grants (user, agent, skill,
channel, workspace), recomputed from durable rows at the moment of spending,
never cached from an earlier check. `CapabilityTerms.effective` and
`spend_under()` in `src/multiplayer/security/capabilities.py` compute it;
`decide()` in the same file is the gateway that every tool call passes
through. A delegated run is bound the same way: what a delegate may spend is
its asker's authority intersected with its own, re-read live, so narrowing
the asker mid-task narrows the delegate with it.

**Structurally-excluded governance surface.** Certain actions, the ones that
change who can do what rather than do work inside those bounds, are not
reachable from an agent turn at all, independent of any permission a policy
config might grant. `agent_turn()` and `require_human_boundary()` in
`src/multiplayer/security/boundary.py` enforce this on ambient execution
context: a governance method call made while inside an agent turn raises,
full stop.

**Hash-chained room event log with a verify CLI.** Every room event is
written with a hash over the previous event's hash plus its own fields
(`event_chain_hash()` in `src/multiplayer/security/audit.py`), so the log is
tamper-evident: altering or deleting a row breaks every hash after it.
`verify_event_chain()` in the same file recomputes a room's chain and reports
sequence gaps or hash mismatches. Run it with
`python -m multiplayer.manage <db-path> audit verify`.

**Hashed credential rows with revocation.** Bearer tokens minted by the
operator CLI are stored as a hash, never plaintext; the token itself is
printed once at mint time and is not recoverable from the database.
`python -m multiplayer.manage <db-path> token revoke <token-or-hash>` marks a
row revoked immediately, without a restart. See `src/multiplayer/manage.py`
and the `user add` / `token mint` / `token revoke` / `token list` commands in
the README's Running section.

**OIDC sessions with rotation and back-channel logout.** A refresh token is
spendable once; presenting a spent one revokes the whole session rather than
just that token, since a replay means a copy exists somewhere it should not.
Every access-token refresh also spends the identity provider's own refresh
token (`refresh_at_provider()` in `src/multiplayer/security/oidc.py`), so a
person disabled or password-reset upstream loses the session at the next
rotation instead of only at the absolute session clock.
`POST /api/v1/auth/backchannel-logout` accepts the provider's own logout
token and ends the session from the provider's side
(`src/multiplayer/api/routes.py`).

**HttpOnly-cookie browser sessions with header and Origin CSRF gates.** The
browser's session cookie carries the access token only, HttpOnly, and is
never readable from page script. A cookie authenticates an HTTP request only
when the request also carries the `X-XYZZY-Client: web` header
(`WEB_CLIENT_HEADER` and `_current_user()` in `src/multiplayer/api/routes.py`):
a cross-origin request cannot attach a custom header without a CORS
preflight that `XYZZY_CORS_ORIGINS` refuses, and a plain top-level navigation
cannot attach one at all. A cookie-authed WebSocket cannot carry that header
either, so its handshake is gated on the `Origin` header matching the
configured allowlist exactly instead
(`src/multiplayer/realtime/websocket.py`), since a script cannot forge that
header on a WebSocket handshake.

**Pre-model screening of untrusted input with per-call provenance fences.**
Text that did not originate from the authenticated caller (another agent's
output, a fetched document, anything from outside this request) is run
through `screen()` before it reaches a model call: control and formatting
characters are stripped and length is bounded
(`MAX_UNTRUSTED_CHARS` in `src/multiplayer/security/screening.py`). `fenced()`
then wraps it in a per-call delimiter that names its source, so the model
sees it labeled as data from a specific origin rather than as an instruction.
This is a deterministic string transform, not a model call, so it cannot fail
open the way a model-based classifier can.

## Data Lifecycle

`python -m multiplayer.manage app.db user erase <user_id>` tombstones a
person and redacts what they authored, without ever deleting a
`room_events` row or rewriting its `event_hash` or `prev_hash`
(`src/multiplayer/services/erasure.py`). The hash-chained event log
(`event_chain_hash()` in `src/multiplayer/security/audit.py`) makes every
row load-bearing for every row after it, so a hard delete or an in-place
edit would break the chain for the whole room. The two positions in
tension (a tamper-evident log wants nothing ever removed, and an erasure
request wants a specific person's data gone) are both kept, this way:

- **The person's own row is tombstoned, not deleted.** `display_name`
  becomes `"Erased user"`, `email` becomes an unreadable placeholder, and
  every credential and session they hold is revoked
  (`UserRepo.tombstone_in_transaction`,
  `UserSessionRepo.revoke_all_for_user_in_transaction`). Their room
  membership rows and the handle other rows still name are left alone,
  since history still needs a slot to say who was there; the room's own
  address book releases their handle for reuse.
- **What they authored is redacted, not rewritten in place.** For every
  event they authored whose payload carries something a person typed
  (message text, a title, an attachment name), the stored `payload` is
  replaced with a marker (`{"redacted": true, "redaction_id": ...}`), and
  a new `event_redactions` row records the event's original `event_hash`,
  when this happened, and under whose authority. The row's `event_hash`
  and `prev_hash` are never touched, so they still equal what
  `event_redactions.original_event_hash` records. `verify_event_chain`
  (`src/multiplayer/security/audit.py`) treats a marker payload as a
  special case: it trusts the recorded original hash in place of
  recomputing one from the marker, requires a matching `event_redactions`
  row to exist, and requires a later `event.redacted` event in the same
  room to name the redaction: a hand-edited marker with no backing
  row, a tampered `original_event_hash`, or a deleted announcement each
  surface as a `ChainBreak` at the row responsible, rather than silently
  verifying clean.
- **Every copy the room made of a redacted message is swept in the same
  transaction, not just the message row.** The full-text search index
  (`search_documents`) holds its own copy of message text made at send
  time, and a branch's `context_snapshot` holds its own copy made at
  branch-start time; both are updated or dropped alongside the message
  and event they were copied from (`SearchRepo.forget_in_transaction`,
  `BranchRepo.redact_message_in_context_snapshots_in_transaction`), so
  neither goes on returning the original text after erasure.
- **A task, decision, room, or artifact the user typed into is redacted the
  same way a message is.** A `task.created`, `decision.created`,
  `room.created`, or hand-created `artifact.created` event's payload gets the
  same marker treatment, and the durable column it also lives in
  (`tasks.title`/`description`, `decisions.title`/`content`,
  `rooms.name`/`description`, `artifacts.name`/`description`) is overwritten
  in the same transaction, along with any `search_documents` row keyed to it.
  A synthesis-published artifact's fixed name (`"Decision Brief"` and
  siblings) is never touched, since nobody typed it. A branch synthesis's
  title never rides inside any chained event, so it is swept on its own,
  keyed by who started it, and a narrow trigger exception (migration 047,
  the same shape as 046) lets that one column change after the synthesis
  went terminal. An artifact version's own content stays out of reach: it is
  append-only evidence bound into a provenance hash (migration 003), and
  redacting it would mean rearchitecting that commitment, not extending this
  track.
- **A branch's initiating prompt, a memory's content, and a reviewer's
  approval comment are redacted the same way, even though none of them ride
  inside a chained event.** `branches.initiating_prompt` (what a person typed
  to open a branch), `memories.content`, and `approvals.review_comment` each
  have no counterpart key in any `RoomEvent` payload, so the per-room sweep
  above cannot reach them; each is instead swept on its own, keyed by the
  column that names who authored it (`initiated_by`, `created_by`,
  `reviewer_id`), the same shape as a branch synthesis's title. Every
  execution a redacted branch launched also keeps its own independent copy
  of the prompt in `executions.input_data`, stamped there at launch time and
  never protected by any immutability trigger; that copy is overwritten too,
  in the same pass. A decision's `reason` (the third free-text field
  `create_decision` accepts, beside `title`/`content`) is now scrubbed
  alongside the other two. A `human_redirected_agent` event's `instruction`
  key (a human's steer text, sent to a running agent) is treated as personal
  content the same way `content`/`body`/`title`/`filename` are; the durable
  `execution_interventions.instruction` column that also holds it is
  redacted too, see below.
- **An agent task's own asker-authored messages are redacted, keyed off the
  same event that already announces the task.** A person can open an agent
  task with free-text `parts` (`open_agent_task`, A2A message/send) and can
  continue one the same way (`continue_agent_task`); neither writes a
  chained event that carries the words themselves, only `task.delegated`
  events that name the task by id. Every such event a human authored gets
  the standard marker treatment, and the durable
  `agent_task_messages.parts` row it points at (every `asker`-role message
  for that task, never a `delegate`-role reply, which is agent-authored) is
  overwritten with a marker part in the same transaction. Nothing indexes
  an agent task's messages for search (no `SearchObjectKind` exists for
  them), so there is no second copy to sweep.
- **A suspended turn steered by the erased user is failed, not left
  waiting.** A turn parked behind a reviewer (`suspended_turns`) holds the
  human's own steer text in `prompt`, keyed by who it is being carried out
  for (`acting_as`). Rather than blank that text in place, the run it
  belongs to is settled `AUTHORITY_REVOKED` (the same settlement this
  codebase already uses whenever a run's bounding principal's authority no
  longer holds), which discards the `suspended_turns` row as part of
  settling: the pending prompt is gone because the row holding it is gone,
  the same way a released handle is gone rather than blanked. Nothing is
  left indefinitely waiting on a person who no longer exists.
- **An intervention's own steer text is redacted, and a run left waiting
  only on it is settled, not orphaned.** `execution_interventions.instruction`
  held a human reviewer's typed redirect behind an immutability trigger
  (migrations 018/020) meant to keep a run's authority record trustworthy,
  not to keep the words themselves forever. Migration 050 narrows that
  trigger the same way 047 and 048 narrowed theirs, naming only
  `intervened_by` as immutable from here on; `instruction` may now be
  overwritten. Every intervention this user authored, in every room the
  per-room sweep above already opens a transaction for, gets its
  `instruction` scrubbed in that same transaction, both a consumed
  intervention (history that still holds the words) and a pending one
  (still queued to steer a future step). When an execution's only
  *unconsumed* intervention was hers and no other member has one queued
  behind it, that execution is no longer waiting on anyone: the run is
  settled `AUTHORITY_REVOKED`, the same settlement a suspended turn above
  already uses for exactly this reason. The intervention row itself is not
  discarded, unlike a suspended turn's: it stays as the record of who
  steered and when, only its text is gone.
- **Shared configuration a user's name is attached to is kept, not
  redacted.** An organization's or workspace's `name`/`slug`, and an agent
  template's `name`/`description`/`system_prompt`, are what a group or a
  reusable specialist is called, not something belonging to the person who
  happened to create it. An agent template is live, shared, functional
  configuration: other workspaces can share it, and a room template can
  still name it for a future room's creation, so blanking a template's
  `system_prompt` on its author's erasure would silently break every
  workspace currently depending on it. An organization or workspace name is
  the same story one level up: it names a group other members keep
  belonging to, not a fact scoped to the one erased member who happened to
  create it, unlike a room's name (redacted above), which names one place
  inside that one person's own room-scoped history. These are
  `"kept_by_ruling"` in `_COLUMN_CLASSIFICATION`, not gaps.
- **The redaction is itself an event.** After every redaction in a room
  lands, one `event.redacted` event is appended, attributed to the
  operator identity that ran the erasure, naming every redaction id it
  covers. The erasure is therefore a fact in the chain, not an edit
  outside it.
- **Attachment bytes are actually removed**, not just unbound from a
  message: the blob, filename, and digest on every attachment the user
  uploaded are cleared (`AttachmentRepo.erase_in_transaction`).
- **The audit export shows the redaction, never the content.** A marker
  line in `export_room_audit` carries the matching `event_redactions`
  metadata (when it happened, why, and who did it) beside it, so an
  auditor can see that something was removed without ever recovering what
  it was.
- **Running it twice is a no-op.** An event whose payload is already a
  marker is left alone, so a second `user erase` against an already
  erased user reports zero new redactions.

- **Every TEXT column in the schema is classified, not remembered.**
  `services/erasure.py::_COLUMN_CLASSIFICATION` names every TEXT-affinity
  column this schema has as `"redacted"`, `"kept_by_ruling"`, or
  `"not_user_authored"`, and
  `tests/security/test_erasure_track_column_coverage.py` introspects the
  live, migrated schema and fails if a column exists with no entry. A
  migration that adds a new free-text column with nobody having decided
  whether erasure should touch it now fails a test instead of shipping
  silently unredacted.

## Known Gaps
- **Single-process rate limiting.** `XYZZY_RATE_LIMIT_PER_MINUTE` is enforced
  in process memory; it bounds one server's exposure, not a fleet's.
- **Single-node storage.** SQLite is the only supported backend today, so
  there is no independent second copy of the event log to cross-check
  against.
- **Live model calls are not exercised in CI.** Provider behavior is verified
  with a fake HTTP transport; a real run against the OpenAI Responses API
  needs a server-side credential and is not part of the automated gate.
