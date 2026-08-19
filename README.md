# MultiAI

A multiplayer AI workspace where multiple humans and AI agents collaborate in persistent shared rooms.

## Why

Modern AI tools are single-player: one human, one chat, one context. Real work happens in teams. MultiAI lets multiple humans and AI agents share a room with a common event history, artifacts, tasks, and decisions — all persisted in SQLite with WebSocket-driven real-time synchronization.

## Core Capabilities

- **Persistent rooms** with durable event sourcing (every action is an ordered event)
- **Multi-agent orchestration** — spawn, pause, resume, redirect, and delegate between agents
- **Human-in-the-loop** — request/approve/reject agent actions before execution
- **Artifact versioning** — create and version documents, code, and other artifacts
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
│   └── 001_initial.sql    # 20-table schema with indexes
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

When NEXUS is unavailable, `StubModelProvider` returns `{action: "finish"}` immediately, allowing all multiplayer features to work without a real LLM.

## Local Installation

```bash
git clone https://github.com/Yasser-Ameur/MultiAI.git
cd MultiAI
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -e ".[dev]"
```

## Running

```bash
# Start the server (serves API + web UI)
python -m multiplayer.server

# Open browser
# http://localhost:8000
```

The server binds to `127.0.0.1:8000` by default. Uses in-memory SQLite (data resets on restart).

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

**92 tests passing** across 6 test suites:
- Unit tests for domain models
- Integration tests for repositories, services, and API endpoints
- Concurrency tests for event sequencing, hub pub/sub, and agent bridge locks
- Security tests for state machines, approval workflows, and room isolation
- Failure injection tests for error handling and validation
- Regression tests for reconnect correctness

## Known Limitations

- **No real LLM integration** — StubModelProvider returns a finish action immediately
- **No authentication** — user_id is passed in the request body (placeholder)
- **In-memory only** — SQLite is created in-memory; data resets on restart
- **Single-process** — no Redis/Postgres for horizontal scaling
- **No persistent storage option** — switching to file-based SQLite requires additional config

## Roadmap

- [ ] Real LLM provider integration (OpenAI, Anthropic, local models)
- [ ] Persistent storage (file-based SQLite or PostgreSQL)
- [ ] User authentication and session management
- [ ] Agent-to-agent messaging and negotiation
- [ ] File upload and binary artifact support
- [ ] Room templates and quickstart configurations
- [ ] Agent marketplace and custom agent creation
- [ ] Audit log export and compliance reporting

## License

MIT
