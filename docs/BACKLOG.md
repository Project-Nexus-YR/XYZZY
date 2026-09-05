# Backlog

## From the first external audit (2026-09-01, verified before filing)

An outside reviewer audited the launch commit and filed findings beyond the
correctness fixes shipped the same week. These are the verified remainder,
deferred with reasons rather than fixed in place:

- **Not built: Skills and Integrations from the PRD's five-way grant
  intersection.** The PRD (`docs/XYZZY_PRD.md`) defines a Skill as its own
  grant alongside user, agent, room and channel, but no code ever narrows an
  agent's capability set below its template's: every `AgentInstance` is
  spawned with `capabilities=template.capabilities` and nothing updates it
  afterward, so the skill term always equals the agent term today, making
  the intersection four-way in practice. Integrations (Slack, GitHub) have
  no code path beyond a static footer link. Neither is scheduled.
- **Per-provider-call ledger.** Only the terminal provider response is
  durably stored on an output; intermediate tool-requesting calls keep
  their tool payloads but lose response ids and per-call provenance. The
  fix is a ledger table keyed by run and call index. Real schema work, and
  it should land together with the provider registry.
- **NEXUS execution is process-affine and its synchronous reasoning call
  runs on the event loop.** Resumability across workers needs run state
  out of memory; the blocking call needs a thread. Substantial runtime
  work, scheduled with the multi-node epic.
- **Provider operations lack streaming, retries with backoff, Retry-After,
  active cancellation, and token budgets.** One coherent provider-runtime
  pass, not six patches.
- **Chunked request bodies bypass the declared-size cap** (the cap reads
  Content-Length). Bound it with a counting stream wrapper when the
  provider-runtime pass touches the transport layer.
- **Concurrent processes can race startup migrations.** Multi-process
  deployments should start one process first today; a migration lock rides
  with the scaling epic.
- **The live 3 to 5 human baseline run** against ChatGPT Projects and the
  open competitors, which the benchmark doc itself records as incomplete.

## Deferred epics (moved from the README roadmap, 2026-08-31)

Deliberately not scheduled: each is weeks of work whose payoff scales with
adoption the project does not yet have.

- **Multi-node deployment and horizontal scaling.** The server is one process
  on SQLite by design (SECURITY.md's threat model and the event-chain writer
  assume a single writer). Going multi-node means Postgres or a fan-out layer
  and a rethink of sequence allocation: do it when a real deployment hits the
  ceiling, not before.
- **Native desktop and mobile clients.** The web SPA is the only client. A
  Slack/Discord bridge is the cheaper adoption lever and should come first.
- **Template marketplace beyond the org.** Org-wide sharing shipped; public
  distribution needs trust machinery (provenance display, versioning,
  takedown) that stays parked until there are strangers to share with.

## Client backlog

Out of scope for the completeness pass ([spec](../web/index.html) round). Each
line: what the API already supports, and why the UI still waits.

- **Tasks UI.** `POST/GET /rooms/{room_id}/tasks`, `POST /tasks/{id}/assign`,
  `/delegate`, `/complete`, `/cancel` exist server-side with no client surface at
  all: a full CRUD + assignment workflow, not a small add, and this round's
  budget went to the channel lifecycle gap the owner actually hit.
- **Hand-authored decisions/artifacts.** `POST /rooms/{room_id}/decisions`,
  `POST /rooms/{room_id}/artifacts`, `POST /artifacts/{id}/versions` let a
  human log a decision or author/edit an artifact directly; today both are
  produced only by the synthesis pipeline. Authoring UI (rich text entry,
  draft/publish states) is its own design pass, not an extension of this one.
- **Memories UI.** `POST/GET /rooms/{room_id}/memories` records a durable room
  fact for agents to draw on; there is no panel to read or write one. Small
  API surface, but needs its own placement (People panel? a new nav item?)
  that this round didn't scope.
- **Multi-org/workspace UI.** `GET/POST /organizations/{org_id}/workspaces`,
  `POST /organizations` let an account grow beyond the auto-bootstrapped single
  workspace. The whole client currently assumes one workspace; surfacing more
  is a navigation-model change, not a screen.
- **Capability-allowlist editors.** `PATCH /rooms/{id}/policy`,
  `PATCH /workspaces/{id}/policy`, `PATCH /rooms/{id}/members/{uid}/capabilities`
  set fine-grained tool/action allowlists beneath the coarser posture control
  already exposed (Guarded/Strict). Building a real editor for that needs its
  own information design; posture covers the common case for now.
- **Room rename.** No `PATCH /rooms/{room_id}` route exists to change a
  channel's name after creation: nothing to wire up until the server adds it.
- **Request-to-join.** `POST /rooms/{room_id}/join` exists, but Browse
  channels shows non-member rooms as "by invitation" only; a self-serve
  request-to-join flow (with an admin approval step) is a workflow the server
  route alone doesn't define and this round left for a follow-up.
- **Launch-round server features with no client surface yet.** Custom agent
  templates (create/delete/share), file attachments (upload/bind/download),
  audit export (download button), room templates (picker at channel
  creation), and share-link management (create/list/revoke on an artifact)
  all ship server-side with tests; each needs its own UI slice against the
  existing route contracts.

## Asked and answered 2026-08-30 (owner roadmap questions)

- **File upload / binary artifacts.** Answered at the time: not built, no
  upload endpoint existed. Since shipped (migration 039, v0.3.0): multipart
  upload of images capped by `XYZZY_MAX_ATTACHMENT_BYTES`, bound to a message,
  served nosniff and filename-sanitized. Artifacts themselves remain
  versioned text; a client surface for attachments is tracked under
  "Launch-round server features with no client surface yet" below.
- **Room templates / quickstart configurations.** Not built; only agent
  templates exist, list-only. The near-term slice: a room template as name +
  description + preselected specialists, offered at channel creation. Best
  next-build candidate together with custom agent templates.
- **Custom agent creation / marketplace.** Agents spawn only from built-in
  templates; there is no create-template route. First slice worth building:
  user-defined specialist templates (name, role, system prompt) per workspace.
  A "marketplace" needs distribution and trust machinery that stays parked.
- **Audit log export / compliance reporting.** The foundation ships free and
  tested (hash-chained event log, `manage audit verify`); the formatted export
  is deliberately NOT free: COMMERCIALIZATION.md names audit/compliance exports
  as the first paid gate.
