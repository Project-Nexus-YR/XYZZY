# Evaluating XYZZY in 30 minutes

This walks a team lead through the core loop end to end, with no backend
reading required beyond this page. It uses the web UI at
`http://localhost:8000`; every step also has a raw API call underneath if you
want to script it instead.

## Setup (5 minutes)

```bash
git clone https://github.com/Project-Nexus-YR/XYZZY.git
cd XYZZY
docker compose up
```

Open `http://localhost:8000` and sign in with the dev token
`change-me-dev-token` (from `docker-compose.yml` — replace it before
deploying anywhere real). Without an `OPENAI_API_KEY`, specialists run in
simulator mode: every AI output is clearly labeled
`SIMULATED WORKFLOW OUTPUT` rather than presented as real analysis, and the
whole workflow below still works. Set `OPENAI_API_KEY` in
`docker-compose.yml` first if you want live model output.

You'll need a second signed-in user for the invite step. XYZZY has no
self-serve signup or user directory — every account is created by an
operator. From another terminal, against the same container:

```bash
docker compose exec xyzzy python -m multiplayer.manage /data/multiplayer.db user add bob --email bob@example.com
docker compose exec xyzzy python -m multiplayer.manage /data/multiplayer.db token mint bob --label eval
```

The `token mint` output is the only copy of that token — it isn't stored.
Open a second browser (or a private window) and sign in as `bob` with it.

## The core loop (15 minutes)

1. **Channel.** As the first user, create a room from the workspace view.
   The UI calls this a channel; the API calls the same object a room
   (`POST /api/v1/workspaces/{workspace_id}/rooms`, body
   `{"name": "...", "description": "..."}`).
2. **Invite.** Invite `bob` into the room. This is where you need his user
   id, not his email — the invite call is
   `POST /api/v1/rooms/{room_id}/members/invitations` with body
   `{"user_id": "bob", "role": "viewer"}`. Have bob join from his own
   session so both of you are members with a live cursor in the room.
3. **Branch two specialists.** Spawn two agents from the agent-template
   list (`GET /api/v1/agent-templates`), then start a branch in `PARALLEL`
   mode naming both: `POST /api/v1/rooms/{room_id}/branches`, body
   `{"mode": "PARALLEL", "prompt": "...", "agent_ids": ["agent-1", "agent-2"]}`.
   Execute each run from the branch view; watch both stream into the room
   in real time.
4. **Compare and publish a brief.** Once both runs finish, include or
   exclude each output for synthesis (`PUT /api/v1/branches/{branch_id}/output-selections/{output_id}`,
   body `{"disposition": "INCLUDE"}` or `"EXCLUDE"`), then publish:
   `POST /api/v1/branches/{branch_id}/syntheses/decision-brief`, body
   `{"title": "..."}`. The resulting artifact version is immutable — a new
   synthesis always creates a new version.
5. **Accept the decision.** Create a decision tied to the brief
   (`POST /api/v1/rooms/{room_id}/decisions`), then move it to `ACTIVE`:
   `POST /api/v1/decisions/{decision_id}/status`, body
   `{"status": "ACTIVE"}`.
6. **Ask Meta why.** From the room's Meta panel, ask why the decision was
   made. Under the hood this is `GET /api/v1/rooms/{room_id}/meta?kind=WHY_DECISION`
   — `kind` is a closed set (`STATUS`, `BLOCKERS`, `CHANGES`,
   `DECISIONS_OPEN`, `DECISIONS_MADE`, `DISAGREEMENT`, `WHY_DECISION`,
   `DECISION_EVIDENCE`); free-text `question` works too and is recorded
   rather than parsed when a `kind` is also given. Meta answers only from
   what the asking user can already read in the room — it doesn't leak
   evidence bob can't see, and bob's own Meta question won't surface
   anything scoped to a room he isn't in.

## What to look at in the artifact provenance

`GET /api/v1/artifact-versions/{version_id}/provenance` is the drill-down.
It returns:

- `content_hash` and `provenance_hash`, plus `provenance_hash_verified` —
  whether the stored hash still matches the content, checked server-side on
  every read, not just trusted from the stored row.
- `branch_synthesis` — which synthesis produced this version, which model
  and provider ran it, whether it was `simulated`, and `selected_output_ids`
  — the exact agent outputs that fed it, in order.
- `claims` — the Decision → Claim → AgentOutput chain: each claim in the
  brief traced back to the specific output it came from, not just the
  branch it came from.

This is the artifact's whole paper trail: what went in, who ran it, and
whether the record has been altered since.

## Local models

Point specialists at any OpenAI-compatible chat-completions server —
Ollama, LM Studio, vLLM, llama.cpp — instead of the OpenAI API:

```bash
export XYZZY_LOCAL_MODEL_BASE_URL="http://localhost:11434/v1"
export XYZZY_OPENAI_MODEL="llama3"
```

This takes priority over `OPENAI_API_KEY` when both are set. If you set
`OPENAI_API_KEY` anyway, it's sent as a bearer token to the local base URL —
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
- **No tasks UI yet.** The task API is complete server-side
  (`POST/GET /rooms/{room_id}/tasks`, `/assign`, `/delegate`, `/complete`,
  `/cancel`) but has no client surface — see `docs/BACKLOG.md`. Reachable
  only by calling the API directly today.
- **Invitation needs the user id, not an email or username.** There's no
  user directory or lookup endpoint; the only way to learn another user's id
  today is for them to tell you, or for you to have created their account
  yourself with `manage.py user add`.
