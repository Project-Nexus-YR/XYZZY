# Evaluating XYZZY in 30 minutes

This walks a team lead through the core loop end to end, with no backend
reading required beyond this page. It uses the web UI at
`http://localhost:8000`; API routes and request bodies are included for
scripting. API requests need the caller's `Authorization: Bearer <token>`
header and JSON bodies need `Content-Type: application/json`.

Reconciled against the source on 2026-09-08. This is a workflow walkthrough;
it does not establish the still-pending live 3–5 human comparison against
ChatGPT shared Projects.

## Setup (5 minutes)

```bash
git clone https://github.com/Project-Nexus-YR/XYZZY.git
cd XYZZY
docker compose up
```

Open `http://localhost:8000` and sign in with the dev token
`change-me-dev-token` (from `docker-compose.yml`: replace it before
deploying anywhere real). Without an `OPENAI_API_KEY`, specialists run in
simulator mode: every AI output is clearly labeled
`SIMULATED WORKFLOW OUTPUT` rather than presented as real analysis, and the
whole workflow below still works. Set `OPENAI_API_KEY` in
`docker-compose.yml` first if you want live model output.

You'll need a second signed-in user for the invite step. This token-based
walkthrough uses operator-created accounts. From another terminal, against
the same container:

```bash
docker compose exec xyzzy python -m multiplayer.manage /data/multiplayer.db user add bob --email bob@example.com
docker compose exec xyzzy python -m multiplayer.manage /data/multiplayer.db token mint bob --label eval
```

The `token mint` output is the only copy of that token: it isn't stored.
Open a second browser (or a private window) and sign in as `bob` with it.

## The core loop (15 minutes)

1. **Channel.** As the first user, create a room from the workspace view.
   The UI calls this a channel; the API calls the same object a room
   (`POST /api/v1/workspaces/{workspace_id}/rooms`, body
   `{"name": "...", "description": "..."}`).
2. **Invite.** Invite `bob` into the room. This is where you need his user
   id, not his email: the invite call is
   `POST /api/v1/rooms/{room_id}/members/invitations` with body
   `{"user_id": "bob", "role": "editor"}`. Have bob open the invited
   channel from his session; both of you can now contribute and see presence.
   `POST /api/v1/rooms/{room_id}/join` marks an existing member present;
   the invitation is what grants membership. Use `viewer` for read-only access.
3. **Branch two or three specialists.** Open Start AI work, enter a question,
   select two or three specialists, and launch. The UI spawns agents, creates
   the branch, and executes its runs. To script this, list templates with
   `GET /api/v1/agent-templates`, then spawn each with
   `POST /api/v1/rooms/{room_id}/agents`, body `{"template_id": "..."}`.
   Pass the returned agent IDs to `POST /api/v1/rooms/{room_id}/branches`,
   body `{"mode": "PARALLEL", "prompt": "...", "agent_ids": ["...", "..."]}`,
   and execute each returned run with
   `POST /api/v1/branches/{branch_id}/runs/{execution_id}/execute`.
   Run state and completed outputs arrive through room events; provider
   responses do not stream token by token.
4. **Compare and publish a brief.** Once all runs finish, include or
   exclude each output for synthesis (`PUT /api/v1/branches/{branch_id}/output-selections/{output_id}`,
   body `{"disposition": "INCLUDED"}` or `{"disposition": "EXCLUDED"}`).
   Every output needs a selection and parallel mode needs at least two
   included outputs, so use three specialists to exercise exclusion.
   After all runs are terminal, publish:
   `POST /api/v1/branches/{branch_id}/syntheses/decision-brief`, body
   `{"title": "..."}`. The first successful publication creates a Decision
   Brief artifact; later publications extend that room's synthesis lineage
   with immutable versions. Send an `Idempotency-Key` header on branch
   creation and synthesis requests when scripting retries. Reusing a completed
   synthesis key replays its original version.
5. **Review the evidence.** Open the published artifact and inspect its
   claim provenance and Decision → Claim → AgentOutput tree. Publication
   materializes a version-backed ontology Decision; its AI-derived assertions
   remain unconfirmed until reviewed. Confirm an assertion only after checking
   its evidence (`POST /api/v1/rooms/{room_id}/ontology/entities/{entity_id}/reviews`,
   body `{"action": "CONFIRM", "reason": "Checked against source evidence"}`),
   or correct it through the tree's correction controls.
6. **Ask Meta why.** From the room's Meta panel, ask why the decision was
   made. Under the hood this is
   `GET /api/v1/rooms/{room_id}/meta?kind=WHY_DECISION&version_id={version_id}`;
   omit `version_id` to use the latest Decision Brief, as the UI does.
   `kind` is a closed set (`STATUS`, `BLOCKERS`, `CHANGES`,
   `DECISIONS_OPEN`, `DECISIONS_MADE`, `DISAGREEMENT`, `WHY_DECISION`,
   `DECISION_EVIDENCE`). A free-text `question` alone accepts a bounded set of
   recognized phrases; when `kind` is given, the text is recorded without
   being parsed. Meta answers only from what the asking user can already
   read in the room: it doesn't leak
   evidence bob can't see, and bob's own Meta question won't surface
   anything scoped to a room he isn't in.

## What to look at in the artifact provenance

`GET /api/v1/artifact-versions/{version_id}/provenance` is the drill-down.
It returns:

- `content_hash` and `provenance_hash`, plus `provenance_hash_verified`:
  whether recomputing the content and provenance hashes matches the stored
  values, checked server-side by this endpoint.
- `branch_synthesis`: which synthesis produced this version, which model
  and provider ran it, whether it was `simulated`, and `selected_output_ids`,
  the exact agent outputs that fed it, in order.
- `claims`: frozen claim/source rows tracing each claim to its exact
  AgentOutput and provider evidence. The ontology and Meta views add the
  Decision → Claim → AgentOutput relationships.

This is the artifact's persisted provenance snapshot. Hash verification
detects inconsistency with that snapshot; it is not an external signature or
proof against someone able to rewrite the entire database.

## Local models

For the Docker walkthrough, uncomment `XYZZY_LOCAL_MODEL_BASE_URL` under
`xyzzy.environment` in `docker-compose.yml`, set it to your runtime's URL
(for example `http://host.docker.internal:11434/v1` on Docker Desktop), and
add `XYZZY_OPENAI_MODEL: "llama3"`. Recreate the service with
`docker compose up -d --build`. Host-shell exports alone do not configure
this Compose service.

When starting the Python server directly, set these in its shell to use an
OpenAI-compatible chat-completions server:

```bash
export XYZZY_LOCAL_MODEL_BASE_URL="http://localhost:11434/v1"
export XYZZY_OPENAI_MODEL="llama3"
```

This takes priority over `OPENAI_API_KEY` when both are set. If you set
`OPENAI_API_KEY` anyway, it's sent as a bearer token to the local base URL:
unset it, or use a placeholder, if you don't want your OpenAI key sent to a
local runtime. See the README's Local Installation section for the full
variable list, including OIDC and deployment settings not needed for this
walkthrough.

## Honest limits

- **SQLite is the only storage backend.** There's no Postgres option; the
  database is always one file. Multi-process fan-out for the realtime hub
  exists (`XYZZY_REDIS_URL`, Redis is pub/sub only, not a data store), but it
  still needs a shared local filesystem for that one database file, so this
  is not yet a multi-node deployment story. See the README's "Scaling out"
  section for what running more than one process actually requires.
- **Tasks are read-only in the UI.** The People panel lists task titles,
  status, priority, and assigned agents. Creation and lifecycle operations
  (`POST /rooms/{room_id}/tasks`, `/assign`, `/delegate`, `/complete`,
  `/cancel`) require API calls; see [the client backlog](BACKLOG.md#client-backlog).
- **Invitation uses a user id.** The picker suggests existing workspace
  members from `GET /api/v1/workspaces/{workspace_id}/members`. For an account
  outside that workspace, obtain the id from its owner or the operator who
  created it; there is no global email lookup in this flow.
- **Manual decisions have no explicit artifact link.** The New decision
  dialog creates a separate decision record and supports Accept/Supersede.
  Its API accepts `title`, `content`, and `reason`, with no artifact-version
  field. The brief's provenance-backed ontology Decision is created by
  synthesis itself. Hand-authored artifact creation and editing remain API-only.
