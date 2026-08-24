# MultiAI

A multiplayer AI workspace where multiple humans and AI agents collaborate in persistent shared rooms.

## Why

Modern AI tools are single-player: one human, one chat, one context. Real work happens in teams. MultiAI lets multiple humans and AI agents share a room with a common event history, artifacts, tasks, and decisions — all persisted in SQLite with WebSocket-driven real-time synchronization.

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
│   └── 0NN_*.sql           # 28 migrations, applied in order at startup
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

MultiAI includes an optional integration with [NEXUS](../NEXUS/), a lightweight agent runtime. The `NexusAgentBridge` adapts NEXUS into the multiplayer context:

- **AgentExecutor** manages agent run lifecycle (create, reason, pause, resume, cancel)
- **Budget** enforces token limits, wall time, and tool call limits
- **PolicyEngine** gates tool access per agent and room
- **StateStore** persists agent state for checkpoint/restart

When NEXUS is unavailable, the bridge runs the configured model provider directly. With an
`OPENAI_API_KEY`, specialists use the OpenAI Responses API. Without a credential, MultiAI emits a
conspicuously labelled `SIMULATED WORKFLOW OUTPUT` so collaboration mechanics remain testable
without presenting placeholder text as real analysis.

## Local Installation

```bash
git clone https://github.com/Yasser-Ameur/MultiAI.git
cd MultiAI
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -e ".[dev]"
```

## Running

Every route below `/api/v1` needs a bearer token; `/api/v1/health` is the
exception. Without `OPENAI_API_KEY` the server runs a credential-free
simulator, which is enough for the whole workflow.

Credentials live in the database, hashed, one row per token, revocable
without a restart. `MULTIAI_AUTH_TOKENS` is bootstrap only: its tokens are
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
# Start the server (serves API + web UI). POSIX shells:
export MULTIAI_AUTH_TOKENS='{"local-dev-token":"user_local"}'
export OPENAI_API_KEY="..."                 # optional; simulated when unset
export MULTIAI_OPENAI_MODEL="gpt-5.4-mini" # optional; this is the default
export MULTIAI_MODEL_TIMEOUT_SECONDS="45"  # optional
python -m multiplayer.server

# Open browser
# http://localhost:8000
```

```powershell
# Windows PowerShell: `export` is not a PowerShell verb.
$env:MULTIAI_AUTH_TOKENS = '{"local-dev-token":"user_local"}'
python -m multiplayer.server
```

The model credential is never accepted from an API request, written to SQLite, or included in an
agent output. Requests send only the selected specialist's name, role, template instructions, the
user decision prompt, and any explicit human intervention. Responses API storage is disabled with
`store: false`.

`MULTIAI_AUTH_TOKENS` is a server-owned JSON map from opaque Bearer tokens to user IDs. Empty or
missing configuration denies every non-health request. The browser keeps its token in memory only.
The server binds to `127.0.0.1:8000` and persists to `multiplayer.db` by default; pass an explicit
database path as the first CLI argument when needed.

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

The current repository gate is 673 passing tests plus Ruff format/check and strict `mypy src`.
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
- **Token allowlist authentication** — deterministic Bearer identity and role authorization are
  implemented, but external SSO/session lifecycle is not

## Roadmap

- [x] OpenAI Responses API model-provider integration
- [x] Persistent file-backed SQLite storage
- [x] Opaque Bearer identity and room authorization
- [ ] External SSO and production session lifecycle
- [ ] Agent-to-agent messaging and negotiation
- [ ] File upload and binary artifact support
- [ ] Room templates and quickstart configurations
- [ ] Agent marketplace and custom agent creation
- [ ] Audit log export and compliance reporting

## License

MIT
