# XYZZY

A team makes a hard technical decision with AI, and keeps the receipts.

Live page: [xyzzy.yasserameur-dev.workers.dev](https://xyzzy.yasserameur-dev.workers.dev/)

[![gates](https://github.com/Project-Nexus-YR/XYZZY/actions/workflows/ci.yml/badge.svg)](https://github.com/Project-Nexus-YR/XYZZY/actions/workflows/ci.yml)
[![image](https://github.com/Project-Nexus-YR/XYZZY/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/Project-Nexus-YR/XYZZY/actions/workflows/docker-publish.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="site/assets/screenshot-hero-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="site/assets/screenshot-hero-light.png">
  <img alt="XYZZY workspace: a channel conversation beside an open thread, with the room sidebar" src="site/assets/screenshot-hero-dark.png" width="960">
</picture>

## Try it

```bash
docker run -p 8000:8000 -e XYZZY_DEMO=1 ghcr.io/project-nexus-yr/xyzzy
```

Opens a seeded demo workspace at `http://localhost:8000`, signed in with one click. No account,
no config. Prefer to run from source? `git clone` this repo and run `docker compose --profile
demo up` instead (see [Docker](#docker) below for the non-demo path).

### What the demo opens

<img src="site/assets/demo.gif" alt="Tour of the seeded XYZZY demo workspace: one-click entry into the workspace, the General channel conversation between two teammates, an open thread attached to one of those messages, the branch view comparing two specialist outputs side by side, the top of the published Decision Brief, the Decision to Claim to AgentOutput evidence chain beneath it, and an Ask Meta answer summarizing where the room's decision stands." width="960">

One click drops you into a workspace already mid-decision: a channel conversation, a branch with
two specialist outputs to compare, and a published Decision Brief with its evidence chain intact.
No API key is configured for this recording, so the specialist outputs and the brief show the
conspicuously labelled SIMULATED workflow output described above; the collaboration mechanics
are the same either way.

## What it is

Modern AI tools are single-player: one human, one chat, one context. Real work happens in teams.
XYZZY lets multiple humans and AI agents share a room: a common event history, artifacts, tasks,
and decisions, persisted in SQLite with WebSocket-driven real-time sync. Agents branch out in
parallel, a human selects or excludes what comes back, and the room publishes an immutable
Decision Brief with the evidence chain behind it.

## Why it's different

**Governed.** Actions wait for human approval before they execute. What an agent may do is
re-read from the room's own state at the moment it acts, so leaving a room or losing access takes
effect immediately, mid-task.

**Provable.** Every room's event log is hash-chained: each event is hashed against the one
before it, so altering or deleting a row breaks every hash after it: tamper-evident by
construction, checkable with the audit CLI. Each Decision links to the Claims and AgentOutputs
behind it, so a synthesis is inspectable down to the run that produced it.

**Yours.** One Python process and a SQLite file, self-hosted. Point specialists at Ollama, LM
Studio, or any OpenAI-compatible server instead of a hosted API. Apache-2.0 licensed, source included.

## Core Capabilities

- **Persistent rooms** with durable event sourcing (every action is an ordered event)
- **Multi-agent orchestration:** spawn, pause, resume, redirect, and delegate between agents
- **Human-in-the-loop:** request/approve/reject agent actions before execution
- **Artifact versioning:** create and version documents, code, and other artifacts
- **Selective synthesis:** explicitly include/exclude outputs and publish immutable Decision Briefs
- **Evidence ontology:** typed, reviewable Decision → Claim → AgentOutput relationships
- **Bounded Meta:** permission-aware “why” and decision-evidence answers with exact drill-down
- **Decision tracking:** record and audit architectural and product decisions
- **Shared memory:** room-scoped, workspace-scoped, and org-scoped memory
- **Real-time collaboration:** WebSocket broadcasting of all room events
- **Reconnect support:** full state snapshot + incremental event replay on reconnect

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
│   44 typed repos · Atomic event sequencing              │
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
├── domain/        # models.py, events.py: frozen dataclasses, RoomEvent/OrgEvent
├── db/            # connection.py, repositories.py: 44 typed repository classes
├── migrations/    # numbered *.sql, applied in order at startup
├── services/      # service.py, presence.py: state machines, presence tracking
├── nexus_bridge/  # agent_bridge.py: NEXUS runtime adapter
├── realtime/      # hub.py, websocket.py: pub/sub, WebSocket endpoint
├── api/           # routes.py: REST endpoints
└── server.py      # Uvicorn entry point with lifespan
web/
└── index.html     # Single-page workspace UI
tests/             # unit, integration, concurrency, security, failure, regression
```

## How It Uses NEXUS

XYZZY includes an optional integration with [NEXUS](https://github.com/Project-Nexus-YR/NEXUS), a lightweight agent runtime. The `NexusAgentBridge` adapts NEXUS into the multiplayer context:

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
worth building against, so create the virtualenv with a real `python3`
(`brew install python@3.13`, or the installer from python.org). Once `.venv` is
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
restarts. Mint and revoke real credentials with the operator CLI: the token
is printed once at mint time and never stored:

```bash
python -m multiplayer.manage multiplayer.db user add alice --email alice@example.com
python -m multiplayer.manage multiplayer.db token mint alice --label laptop
python -m multiplayer.manage multiplayer.db token revoke <token-or-hash>
python -m multiplayer.manage multiplayer.db token list
```

```bash
# Start the server (serves API + web UI). POSIX shells: macOS zsh, Linux bash.
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
chat-completions server instead of the OpenAI API (Ollama, LM Studio, vLLM,
and llama.cpp's server all qualify). It takes priority over `OPENAI_API_KEY`
when both are set. `XYZZY_OPENAI_MODEL` still names the model; `OPENAI_API_KEY`
is optional here and, when set, is sent as a bearer token to the host the base
URL names, so unset the key (or use a placeholder) when pointing at a local
runtime you do not want your OpenAI key sent to.

**This is on purpose, not a leak:** a local base URL sitting behind a keyed
proxy needs a bearer credential too, and `OPENAI_API_KEY` is reused as that
credential precisely so a proxy setup like that works without a second
variable.

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
| `XYZZY_REDIS_URL` | unset | Fans room events, session revocations, and presence out across several server processes sharing one database; see [Scaling out](#scaling-out). |

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

SSO is additive: with none of `XYZZY_OIDC_*` set, the server behaves exactly as
before (bootstrap tokens and `manage token mint`). Set the issuer, client id,
and redirect URI to add OIDC login; `scripts/dev_idp.py` is a throwaway local
provider for trying it without a real one. Full variable table, endpoint list,
refresh-token and cookie security notes: [docs/SSO.md](docs/SSO.md).

### Talking to other agents

XYZZY speaks Google's [A2A](https://a2a-protocol.org/) v0.3.0 at `POST
/a2a/v1`, so an agent built against somebody else's runtime can be asked for
work here, and one of ours can ask it back. `GET
/.well-known/agent-card.json` is the discovery document; it advertises no
agents, since room membership is the access-control decision. Method list,
delegation-authority rules, and cycle limits: [docs/A2A.md](docs/A2A.md).

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
`docker run -p 8000:8000 -e XYZZY_DEMO=1 ghcr.io/project-nexus-yr/xyzzy`, the published image;
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

The current repository gate is 979 tests (978 passing, 1 skipped without `OPENAI_API_KEY`) plus Ruff format/check and strict `mypy src`,
run on every push and pull request by `.github/workflows/ci.yml`.
The suite covers:
- Unit tests for domain models
- Integration tests for repositories, services, and API endpoints
- Concurrency tests for event sequencing, hub pub/sub, and agent bridge locks
- Security tests for state machines, approval workflows, and room isolation
- Failure injection tests for error handling and validation
- Regression tests for reconnect correctness
- File-backed acknowledgement latency and exact zero-loss event persistence

## Scaling out

One process is the default and the recommendation until a real deployment
outgrows it. When one does, set `XYZZY_REDIS_URL` (install with
`pip install "xyzzy[redis]"`) and run several server processes against the
same database file: room events, session revocations, and user notifications
fan out across processes through Redis pub/sub, and presence stays correct
cluster-wide through keys that expire on silence. Redis carries no state
worth backing up. If it goes down, each process degrades to single-process
behavior and clients recover anything missed through the reconnect replay
path, because the event log stays the single source of truth.

Two boundaries to respect: all processes must share one real local
filesystem for the database (network filesystems such as NFS or SMB are
unsupported), and rate limits count per process, so divide the budget or
limit at the load balancer.

## Verifying the live provider path

CI verifies provider behavior against a fake HTTP transport on every push,
which keeps the gates free and deterministic. The `live-provider` workflow
is the opt-in other half: trigger it by hand (Actions tab) with an
`OPENAI_API_KEY` repository secret configured, and it spends one real API
call proving the genuine provider path produces model-written output.
Locally, the same test runs whenever the key is exported and skips loudly
when it is not.

## License

Apache 2.0, see [LICENSE](LICENSE).
