# XYZZY

A team makes a hard technical decision with AI, and keeps the receipts.

[![gates](https://github.com/Project-Nexus-YR/XYZZY/actions/workflows/ci.yml/badge.svg)](https://github.com/Project-Nexus-YR/XYZZY/actions/workflows/ci.yml)
[![image](https://github.com/Project-Nexus-YR/XYZZY/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/Project-Nexus-YR/XYZZY/actions/workflows/docker-publish.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="site/assets/screenshot-hero-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="site/assets/screenshot-hero-light.png">
  <img alt="XYZZY workspace: a channel conversation beside an open thread, with the room sidebar" src="site/assets/screenshot-hero-dark.png">
</picture>

## Try it

```bash
docker run -p 8000:8000 -e XYZZY_DEMO=1 ghcr.io/project-nexus-yr/xyzzy
```

Opens a seeded demo workspace at `http://localhost:8000`, signed in with one click. No account,
no config. Prefer to run from source? `git clone` this repo and run `docker compose --profile
demo up` instead — see [Docker](#docker) below for the non-demo path.

## What it is

Modern AI tools are single-player: one human, one chat, one context. Real work happens in teams.
XYZZY lets multiple humans and AI agents share a room — a common event history, artifacts, tasks,
and decisions — persisted in SQLite with WebSocket-driven real-time sync. Agents branch out in
parallel, a human selects or excludes what comes back, and the room publishes an immutable
Decision Brief with the evidence chain behind it.

## Why it's different

**Governed.** Actions wait for human approval before they execute. What an agent may do is
re-read from the room's own state at the moment it acts, so leaving a room or losing access takes
effect immediately, mid-task.

**Provable.** Every room's event log is hash-chained: each event is hashed against the one
before it, so altering or deleting a row breaks every hash after it — tamper-evident by
construction, checkable with the audit CLI. Each Decision links to the Claims and AgentOutputs
behind it, so a synthesis is inspectable down to the run that produced it.

**Yours.** One Python process and a SQLite file, self-hosted. Point specialists at Ollama, LM
Studio, or any OpenAI-compatible server instead of a hosted API. MIT licensed, source included.

## Core Capabilities

- **Persistent rooms** with durable event sourcing (every action is an ordered event)
- **Multi-agent orchestration** — spawn, pause, resume, redirect, and delegate between agents
- **Human-in-the-loop** — request/approve/reject agent actions before execution
- **Artifact versioning** — create and version documents, code, and other artifacts
- **Selective synthesis** — explicitly include/exclude outputs and publish immutable Decision Briefs
- **Evidence ontology** — typed, reviewable Decision → Claim → AgentOutput relationships
- **Bounded Meta** — permission-aware “why” and decision-evidence answers with exact drill-down
- **Decision tracking** — record and audit architectural and product decisions
- **Shared memory** — room-scoped, workspace-scoped, and org-scoped memory
- **Real-time collaboration** — WebSocket broadcasting of all room events
- **Reconnect support** — full state snapshot + incremental event replay on reconnect

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Browser (web/index.html)              │
│                    WebSocket + REST API                  │
└──────────────────────────┬──────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────┐
│                 FastAPI Server (server.py)               │
│          REST endpoints (routes.py) + WS endpoint        │
├─────────────────────────────────────────────────────────┤
│              Service Layer (service.py)                  │
│     State machines · Input validation · Authorization   │
├──────────────────┬──────────────────────────────────────┤
│   RealtimeHub    │        NexusAgentBridge              │
│  Pub/sub lock    │  AgentExecutor · Budget · Events     │
│  Queue delivery  │  Pause/Resume/Cancel · Interventions  │
├──────────────────┴──────────────────────────────────────┤
│            Repository Layer (repositories.py)           │
│   16 typed repos · Atomic event sequencing              │
├─────────────────────────────────────────────────────────┤
│           Database Layer (connection.py)                │
│         aiosqlite · WAL mode · Transaction support       │
├─────────────────────────────────────────────────────────┤
│                NEXUS Runtime (optional)                  │
│    AgentExecutor · ModelProvider · PolicyEngine          │
│    ToolRegistry · SQLiteStateStore · EventBus            │
└─────────────────────────────────────────────────────────┘
```

## Project Structure

```
src/multiplayer/
├── domain/
│   ├── models.py          # 25+ domain models (frozen dataclasses)
│   └── events.py          # 40+ event types, RoomEvent, OrgEvent
├── db/
│   ├── connection.py      # aiosqlite wrapper with transaction support
│   └── repositories.py    # 16 typed repository classes
├── migrations/
│   └── 0NN_*.sql           # numbered migrations, applied in order at startup
├── services/
│   ├── service.py         # Core service layer with state machines
│   └── presence.py        # In-memory presence tracking
├── nexus_bridge/
│   └── agent_bridge.py    # NEXUS runtime adapter (asyncio.Lock protected)
├── realtime/
│   ├── hub.py             # Pub/sub with lock-protected mutations
│   └── websocket.py       # WebSocket endpoint handler
├── api/
│   └── routes.py          # 40+ REST endpoints
└── server.py              # Uvicorn entry point with lifespan
web/
└── index.html             # Single-page workspace UI
tests/
├── unit/                  # Domain model tests
├── integration/           # Repository, service, API tests
├── concurrency/           # Concurrent event generation, hub, bridge
├── security/              # State machines, approvals, scope isolation
├── failure/               # Error handling, validation, stub tests
└── regression/            # Reconnect correctness
```

## How It Uses NEXUS

XYZZY includes an optional integration with [NEXUS](../NEXUS/), a lightweight agent runtime. The `NexusAgentBridge` adapts NEXUS into the multiplayer context:

- **AgentExecutor** manages agent run lifecycle (create, reason, pause, resume, cancel)
- **Budget** enforces token limits, wall time, and tool call limits
- **PolicyEngine** gates tool access per agent and room
- **StateStore** persists agent state for checkpoint/restart

When NEXUS is unavailable, the bridge runs the configured model provider directly. With an
`OPENAI_API_KEY`, specialists use the OpenAI Responses API. Without a credential, XYZZY emits a
conspicuously labelled `SIMULATED WORKFLOW OUTPUT` so collaboration mechanics remain testable
without presenting placeholder text as real analysis.

## Local Installation

Python 3.11 or newer and nothing else. The database is a file, so there is no
service to stand up first.

macOS and Linux:

```bash
git clone https://github.com/Project-Nexus-YR/XYZZY.git
cd XYZZY
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Windows PowerShell:

```powershell
git clone https://github.com/Project-Nexus-YR/XYZZY.git
cd XYZZY
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

macOS has shipped no `python` command since 12.3, and `/usr/bin/python3` is a
stub that offers to install the Command Line Tools rather than an interpreter
worth building against, so create the virtualenv with a real `python3` —
`brew install python@3.13`, or the installer from python.org. Once `.venv` is
activated, plain `python` is that virtualenv's interpreter and every command
below works as written. Apple silicon needs nothing special: every dependency
resolves to an arm64 wheel.

## Running

Every route below `/api/v1` needs a bearer token; `/api/v1/health` is the
exception. Without `OPENAI_API_KEY` the server runs a credential-free
simulator, which is enough for the whole workflow.

Credentials live in the database, hashed, one row per token, revocable
without a restart. `XYZZY_AUTH_TOKENS` is bootstrap only: its tokens are
ingested at startup, and a token an operator revoked stays revoked across
restarts. Mint and revoke real credentials with the operator CLI — the token
is printed once at mint time and never stored:

```bash
python -m multiplayer.manage multiplayer.db user add alice --email alice@example.com
python -m multiplayer.manage multiplayer.db token mint alice --label laptop
python -m multiplayer.manage multiplayer.db token revoke <token-or-hash>
python -m multiplayer.manage multiplayer.db token list
```

```bash
# Start the server (serves API + web UI). POSIX shells - macOS zsh, Linux bash:
export XYZZY_AUTH_TOKENS='{"local-dev-token":"user_local"}'
export OPENAI_API_KEY="..."                 # optional; simulated when unset
export XYZZY_OPENAI_MODEL="gpt-5.4-mini" # optional; this is the default
export XYZZY_MODEL_TIMEOUT_SECONDS="45"  # optional
python -m multiplayer.server

# Open browser
# http://localhost:8000
```

### Local models

Set `XYZZY_LOCAL_MODEL_BASE_URL` to point specialists at any OpenAI-compatible
chat-completions server instead of the OpenAI API — Ollama, LM Studio, vLLM,
and llama.cpp's server all qualify. It takes priority over `OPENAI_API_KEY`
when both are set. `XYZZY_OPENAI_MODEL` still names the model; `OPENAI_API_KEY`
is optional here and, when set, is sent as a bearer token — to the host the base
URL names, so unset the key (or use a placeholder) when pointing at a local
runtime you do not want your OpenAI key sent to.

```bash
# Ollama
export XYZZY_LOCAL_MODEL_BASE_URL="http://localhost:11434/v1"
export XYZZY_OPENAI_MODEL="llama3"

# LM Studio
export XYZZY_LOCAL_MODEL_BASE_URL="http://localhost:1234/v1"
export XYZZY_OPENAI_MODEL="local-model"
```

```powershell
# Windows PowerShell: `export` is not a PowerShell verb.
$env:XYZZY_AUTH_TOKENS = '{"local-dev-token":"user_local"}'
python -m multiplayer.server
```

The model credential is never accepted from an API request, written to SQLite, or included in an
agent output. Requests send only the selected specialist's name, role, template instructions, the
user decision prompt, and any explicit human intervention. Responses API storage is disabled with
`store: false`.

`XYZZY_AUTH_TOKENS` is a server-owned JSON map from opaque Bearer tokens to user IDs. Empty or
missing configuration denies every non-health request. The browser keeps its token in memory only.
The server binds to `127.0.0.1:8000` and persists to `multiplayer.db` by default; pass an explicit
database path as the first CLI argument when needed.

### Deployment settings

Every one of these has a working default, so a local run needs none of them. A
deployment that terminates TLS in front of the server needs the first three.

| Variable | Default | What it decides |
| --- | --- | --- |
| `XYZZY_HOST` | `127.0.0.1` | Interface to bind. Loopback by default: binding everything because nobody configured it is a deployment decision made by omission. |
| `XYZZY_PORT` | `8000` | Port to bind. |
| `XYZZY_CORS_ORIGINS` | the two loopback origins | Comma-separated browser origins allowed to call the API. `*` is refused: paired with credentials it would let any site spend a signed-in session. |
| `XYZZY_RATE_LIMIT_PER_MINUTE` | `120` | Requests per minute per bearer token, or per peer address when there is no token. `/api/v1/health` is exempt so a monitor cannot spend a client's budget. |
| `XYZZY_MAX_BODY_BYTES` | `1048576` | Largest declared request body. A chunked request declares no length, so this caps the honest case only. |
| `XYZZY_LOG_LEVEL` | `INFO` | Root log level. |

The rate limiter counts in process memory. It bounds one server's exposure, not a
fleet's; two replicas behind a load balancer each allow the full budget.

`GET /api/v1/health` is a readiness probe, not a liveness one: it reads from the
database and answers 503 when it cannot, so a process holding an unopenable
database is never reported ready.

`GET /metrics` exposes this process's own counters and gauges in Prometheus
text format, exempt from auth and from the rate limiter like `/health`. It is
single-process: scrape each replica rather than expecting one to speak for a
fleet.

### Signing in through an identity provider

SSO is additive. With none of these set the server behaves exactly as before —
bootstrap tokens and `manage token mint` — so a deployment without a provider is
untouched.

| Variable | What it decides |
| --- | --- |
| `XYZZY_OIDC_ISSUER` | The provider's issuer URL. Its configuration is discovered from `{issuer}/.well-known/openid-configuration`. |
| `XYZZY_OIDC_CLIENT_ID` | This deployment's client id. |
| `XYZZY_OIDC_CLIENT_SECRET` | Optional; omit for a public client relying on PKCE alone. |
| `XYZZY_OIDC_REDIRECT_URI` | Where the provider sends the browser back. |
| `XYZZY_OIDC_SCOPES` | Space separated; `openid profile email` by default. |
| `XYZZY_OIDC_POST_LOGOUT_REDIRECTS` | Comma-separated allowlist. A redirect target taken from a request would be an open redirect. |
| `XYZZY_SESSION_IDLE_SECONDS` | Idle clock, 1800 by default — Keycloak's. |
| `XYZZY_SESSION_ABSOLUTE_SECONDS` | Absolute ceiling, 36000 by default — Keycloak's. |
| `XYZZY_SESSION_ACCESS_SECONDS` | How long one access credential lives before it must be refreshed, 300 by default — Keycloak's. |
| `XYZZY_OIDC_ALLOW_UNVERIFIABLE_SESSIONS` | Accept a login from a provider that issues no refresh token. Off by default, because such a session can never be re-checked; when on, it is capped at 15 minutes. |

`GET /api/v1/auth/login` starts the flow, `GET /api/v1/auth/callback` finishes it
and returns an access token and a refresh token, `POST /api/v1/auth/refresh`
rotates them, `POST /api/v1/auth/logout` ends this session,
`POST /api/v1/auth/logout-everywhere` ends all of them, and
`POST /api/v1/auth/backchannel-logout` accepts the provider's logout token.
Every one of them sits under the `/api/v1` prefix, so `XYZZY_OIDC_REDIRECT_URI`
must too.

Three things worth knowing before you deploy it. A refresh token is spendable
once, and presenting a spent one revokes the entire session rather than that
token — a replay means a copy exists somewhere it should not, and revoking only
the copy leaves whoever holds the original inside. And an SSO login is keyed on
the provider's issuer and subject, never on the email address, so it does **not**
attach to an operator-created account that happens to share an email. Linking
those is a deliberate act; inferring it from a string is how accounts get taken
over. And there is no reuse grace window: a refresh
whose answer is lost cannot be retried, and the person signs in again. A window
was tried and removed, because it let a thief presenting the stolen predecessor
take a working session and leave the victim's own next refresh to be judged the
replay. Keycloak's default is no reuse either.

Every refresh also spends the provider's own refresh token, so a person
disabled, locked out, or password-reset upstream loses this session at the next
rotation rather than at the absolute clock.

The browser itself never sees either token. `GET /api/v1/auth/callback` sets a
cookie only when the request prefers `text/html` (a browser arriving by
redirect); that cookie carries the access token alone, HttpOnly, `__Host-`
prefixed on an HTTPS deployment, and expires with the session's idle clock.
Every other caller — curl, an agent, `refresh`/`logout` — still gets the JSON
body with both tokens, unchanged. A cookie authenticates an HTTP request only
when it also carries header `X-XYZZY-Client: web`, on every method including
GET, which is what keeps a mutating GET like `/auth/end-session` out of CSRF
reach: a cross-origin request cannot attach a custom header without a CORS
preflight `XYZZY_CORS_ORIGINS` refuses, and a top-level navigation cannot
attach one at all. A cookie-authed WebSocket cannot carry that header either,
so it is gated on `Origin` matching `configured_origins()` exactly instead.

**Trying it locally:** `scripts/dev_idp.py` is a throwaway identity provider —
stdlib/FastAPI, one hardcoded user, a fresh RS256 key generated on every start.
It refuses to run unless its own issuer is a loopback host, because it trusts
every caller completely.

```bash
python scripts/dev_idp.py --port 9100
# in another shell
export XYZZY_OIDC_ISSUER="http://127.0.0.1:9100"
export XYZZY_OIDC_CLIENT_ID="dev-client"
export XYZZY_OIDC_REDIRECT_URI="http://127.0.0.1:8000/api/v1/auth/callback"
python -m multiplayer.server
```

Open http://localhost:8000 and sign in through the provider; `XYZZY_DEV_IDP_SUB`,
`XYZZY_DEV_IDP_NAME`, and `XYZZY_DEV_IDP_EMAIL` change the one user's claims.

### Talking to other agents

XYZZY speaks Google's [A2A](https://a2a-protocol.org/) v0.3.0, so an agent built
against somebody else's runtime can be asked for work here, and one of ours can
ask it back.

`GET /.well-known/agent-card.json` is the discovery document and needs no
credential. It advertises the door and **no agents at all**: a room's membership
is the access-control decision, so a public list of agents and their skills
would publish the shape of a private workspace to anyone who fetched a URL. The
authenticated `agent/getAuthenticatedExtendedCard` shows each caller only the
agents that caller could actually address, which means no two callers share one
document.

`POST /a2a/v1` is the JSON-RPC 2.0 endpoint: `message/send`, `message/stream`,
`tasks/get`, `tasks/cancel`, `tasks/resubscribe`,
`agent/getAuthenticatedExtendedCard`, and the two `tasks/pushNotificationConfig`
methods. The card advertises `pushNotifications: false` and those two refuse by
name — a webhook fan-out would be a second delivery path with weaker guarantees
than the durable ordered log clients already have. Streaming is
Server-Sent-Events over that same log, not a parallel one.

A2A addresses one agent per URL and this server fronts many rooms, so
`message.metadata` carries `roomId` and `targetAgentId`. A caller who may not act
in a room gets the same refusal whether the agent is real, filed elsewhere, or
imaginary; a task you may not read answers exactly as a task that does not exist.

Two rules about delegation are worth knowing before you wire agents to each
other. What a delegate may spend is its asker's own authority intersected with
its own, re-read from durable rows at the moment of spending — narrow the asker
mid-task and the delegate narrows with it, and an asker that has left the room
lends nothing. And the chain a delegation belongs to is read from the delegating
agent's own open run rather than taken from the request, so an agent cannot
start a fresh chain by declining to name its parent: a cycle is refused by name,
and a chain deeper than four delegations is too.

### Docker

**Quickstart:**

```bash
git clone <this repo> && cd xyzzy
docker compose up
```

Open http://localhost:8000 and sign in with the dev token
`change-me-dev-token`. Replace that token in `docker-compose.yml` before
deploying anywhere real.

Without `docker compose`, the equivalent is:

```bash
docker build -t xyzzy .
docker run -p 8000:8000 -v xyzzy-data:/data -e XYZZY_AUTH_TOKENS='{"local-dev-token":"user_local"}' xyzzy
```

No account, no config, nothing to try alone: `docker compose --profile demo up` (or
`docker run -p 8000:8000 -e XYZZY_DEMO=1 ghcr.io/project-nexus-yr/xyzzy`, the published image —
see [Try it](#try-it) above) opens a seeded demo workspace at http://localhost:8000, signed in
with one click.

The database is a file under `/data`. Without the volume the room history dies
with the container.

## Running Tests

```bash
# All tests
python -m pytest tests/ -v

# Specific suites
python -m pytest tests/unit/ -v
python -m pytest tests/concurrency/ -v
python -m pytest tests/security/ -v
python -m pytest tests/failure/ -v
python -m pytest tests/regression/ -v
```

## Current Status

The current repository gate is 857 passing tests plus Ruff format/check and strict `mypy src`,
run on every push and pull request by `.github/workflows/ci.yml`.
The suite covers:
- Unit tests for domain models
- Integration tests for repositories, services, and API endpoints
- Concurrency tests for event sequencing, hub pub/sub, and agent bridge locks
- Security tests for state machines, approval workflows, and room isolation
- Failure injection tests for error handling and validation
- Regression tests for reconnect correctness
- File-backed acknowledgement latency and exact zero-loss event persistence

## Known Limitations

- **Live model credentials are not exercised in CI** — provider behavior is verified with a fake
  HTTP transport; a real Responses API run requires a server-side `OPENAI_API_KEY`
- **Single-process** — no Redis/Postgres for horizontal scaling

## Roadmap

- [x] OpenAI Responses API model-provider integration
- [x] Local model support (Ollama, LM Studio, any OpenAI-compatible endpoint)
- [x] Persistent file-backed SQLite storage
- [x] Opaque Bearer identity and room authorization
- [x] External SSO and production session lifecycle
- [x] Agent-to-agent messaging and negotiation
- [x] File attachments on messages, with a hard model boundary
- [x] Custom agent templates per workspace
- [x] Streaming audit export (ndjson, chain-verified)
- [x] Zero-config demo mode
- [x] Public read-only share links for decision briefs
- [ ] Room templates and quickstart configurations
- [ ] Agent template sharing across workspaces
- [ ] Multi-node deployment and horizontal scaling
- [ ] Native desktop and mobile clients

## License

MIT
