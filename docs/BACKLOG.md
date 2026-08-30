# Client backlog

Out of scope for the completeness pass ([spec](../web/index.html) round). Each
line: what the API already supports, and why the UI still waits.

- **Tasks UI.** `POST/GET /rooms/{room_id}/tasks`, `POST /tasks/{id}/assign`,
  `/delegate`, `/complete`, `/cancel` exist server-side with no client surface at
  all — a full CRUD + assignment workflow, not a small add, and this round's
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
  channel's name after creation — nothing to wire up until the server adds it.
- **Request-to-join.** `POST /rooms/{room_id}/join` exists, but Browse
  channels shows non-member rooms as "by invitation" only; a self-serve
  request-to-join flow (with an admin approval step) is a workflow the server
  route alone doesn't define and this round left for a follow-up.

## Asked and answered 2026-08-30 (owner roadmap questions)

- **File upload / binary artifacts.** Not built: artifacts are versioned text in
  SQLite, no upload endpoint exists, and the body cap is 1MB. Needs its own
  design pass - storage, content screening for the model path, download
  authorization - before any UI.
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
