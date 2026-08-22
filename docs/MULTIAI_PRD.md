# MultiAI — Product Requirements Document

## 1. Product

MultiAI is a **multiplayer AI workspace** where teams collaborate with AI agents in shared channels, preserve individual AI work, selectively synthesize results, and automatically build a queryable organizational ontology from their work.

The core loop is:

```
Humans + Agents
      ↓
Channels
      ↓
AI Branches
      ↓
Individual Agent Outputs
      ↓
Selective Synthesis
      ↓
Artifacts / Decisions / Tasks
      ↓
Organizational Ontology
      ↓
Meta
```

MultiAI initially integrates with existing work systems rather than replacing Slack, GitHub, ClickUp/Linear, or document platforms.

---

## 2. Problem

Organizational knowledge is fragmented across:

- conversations;
- AI chats;
- tasks;
- documents;
- code;
- decisions;
- project-management systems.

AI reasoning is especially fragmented because employees increasingly work with AI independently.

Teams lack a shared system for:

- collaborating with AI;
- comparing AI reasoning;
- preserving important reasoning;
- connecting conclusions to evidence;
- understanding current organizational state;
- querying why something happened.

MultiAI connects:

**people → conversations → AI reasoning → decisions → tasks → artifacts → outcomes.**

---

## 3. Core Product Principles

1. **Shared state, not just group chat.**
2. **Individual agent outputs remain inspectable.**
3. **Humans choose what gets synthesized.**
4. **Agents operate with least privilege.**
5. **Permissions are enforced outside prompts.**
6. **Retrieve the minimum information necessary.**
7. **Ontology facts retain provenance.**
8. **AI-derived information is distinguishable from confirmed facts.**
9. **Important actions remain human-governed.**
10. **Do not build infrastructure before usage requires it.**

---

## 4. Workspace Model

```
Workspace
│
├── Channels
│   ├── Messages
│   ├── Threads
│   ├── Branches
│   │   ├── Agent Runs
│   │   └── Agent Outputs
│   └── Artifacts
│
├── Work
│   ├── Projects
│   ├── Tasks
│   ├── Decisions
│   └── Goals
│
├── Meta
│   ├── Ask
│   ├── Explore
│   ├── Ontology
│   └── Synthesis
│
└── Control Plane
    ├── Identity
    ├── Roles
    ├── Permissions
    ├── Skills
    ├── Policies
    └── Approvals
```

---

## 5. Channels

Channels are persistent collaboration and AI-context boundaries.

Examples:

```
#engineering
#research
#product
#general
```

Channels control:

- membership;
- conversation;
- available agents;
- retrieval scope;
- artifacts;
- permissions.

A **thread** is conversational.

A **branch** is an isolated AI-work context.

---

## 6. AI Branches

A branch captures AI work originating from a specific context.

```
Question
│
├── Research Agent → Output A
├── Security Agent → Output B
└── Engineering Agent → Output C
```

Every branch records:

- initiator;
- context boundary;
- agents;
- runs;
- outputs;
- interventions;
- provenance.

Individual outputs remain visible even after synthesis.

---

## 7. Execution Modes

### Turn-Locked

One agent operates against frozen context.

```
OPEN → AGENT_RUNNING → OPEN
```

Normal context-changing messages may be blocked while the agent runs.

### Interruptible

One agent runs while authorized users can:

- redirect;
- provide evidence;
- interrupt;
- cancel.

Interventions are recorded separately from normal messages.

### Parallel

Multiple agents independently analyze the same context.

Useful strategies include:

- different professional perspectives;
- independent verification;
- adversarial review;
- parallel subtasks.

Parallel execution is intentional, not the default.

---

## 8. Selective Synthesis

Users choose which outputs become synthesis inputs.

```
Research Agent       ✓
Security Agent       ✓
Engineering Agent    ✗
                    ↓
                Synthesis
```

Synthesis must retain exact provenance.

Initial synthesis types:

- General Synthesis
- Decision Brief
- Progress Report

Outputs become versioned Artifacts.

---

## 9. Organizational Ontology

MultiAI maintains a structured representation of organizational work.

Initial objects:

```
Person
Project
Task
Decision
Artifact
Claim
Agent
AgentOutput
```

Initial relationships:

```
OWNS
WORKS_ON
BLOCKS
DEPENDS_ON
SUPPORTS
CONTRADICTS
REFERENCES
DERIVED_FROM
ASSIGNED_TO
```

Example:

```
Alice
  ↓ OWNS
AUTH-42
  ↓ BLOCKS
Project Phoenix
  ↓ REFERENCES
architecture.md
```

Every inferred fact retains evidence.

---

## 10. Lazy Ontology

Ontology updates use a hybrid strategy.

### Immediate

Structured actions:

- task updated;
- decision recorded;
- artifact published;
- project changed.

### Asynchronous

Conversation/agent work:

- decisions;
- blockers;
- claims;
- risks;
- relationships.

### Scheduled

Periodic consolidation:

- deduplication;
- alias resolution;
- stale claims;
- contradictions;
- relationship updates.

### Query-Time

Meta may process a **bounded amount of relevant unprocessed information** when freshness is required.

The system must never routinely parse the entire workspace for one question.

---

## 11. Meta

Meta is the organizational intelligence interface, not another chat channel.

Users can ask:

- What is blocking Phoenix?
- What changed this week?
- Why did the launch slip?
- What decisions require attention?
- Where do Engineering and Security disagree?
- What evidence supports this decision?

Meta uses:

```
Ontology
+
Structured State
+
Relevant Raw Evidence
+
Permission-Aware Retrieval
+
AI Synthesis
```

Answers support drill-down:

```
Summary
  ↓
Ontology Object
  ↓
Agent Output
  ↓
Task / Message / Artifact
  ↓
Original Evidence
```

---

## 12. Skills

Skills describe reusable AI procedures.

Examples:

```
research
code_review
security_review
progress_report
decision_analysis
synthesis
```

A skill defines:

- instructions;
- required capabilities;
- tools;
- retrieval strategy;
- output format.

Skills do **not** enforce security.

---

## 13. Permissions

Agents operate under least privilege.

```
effective capabilities
=
user permissions
∩ agent capabilities
∩ skill capabilities
∩ channel policy
∩ workspace policy
```

Example:

A Coding Agent supports:

```
code.read
code.write
tests.execute
database.write
```

An HR user has:

```
code.read
```

When HR invokes the Coding Agent:

```
effective capability = code.read
```

The agent may recommend code/database changes but cannot execute them.

---

## 14. Tool Safety

All actions go through a Tool Gateway:

```
Agent
 ↓
Tool Request
 ↓
Permission Check
 ↓
Policy Check
 ↓
Approval if required
 ↓
Execute
 ↓
Audit Event
```

Security must never rely only on instructions such as:

> "Do not modify the database."

Unauthorized tools must actually be unavailable or rejected.

---

## 15. Retrieval

Default policy:

> Retrieve the minimum authorized information required to answer the question.

Flow:

```
Question
 ↓
Permissions
 ↓
Ontology lookup
 ↓
Relevant scopes
 ↓
Freshness check
 ↓
Bounded evidence retrieval
 ↓
Model
```

Agents do not receive the entire workspace by default.

---

## 16. Integrations

MultiAI should initially **consume existing work systems**, not replace them.

Priority:

1. GitHub/GitLab
2. ClickUp/Linear/Jira
3. Slack/Teams
4. Documents

Example:

```
PR #381
   ↓ IMPLEMENTS
AUTH-42
   ↓ BLOCKS
Project Phoenix
```

External activity becomes ontology evidence.

---

## 17. Management Intelligence

Meta should help management understand:

- project progress;
- blockers;
- dependencies;
- decisions;
- risks;
- ownership;
- important changes.

It should **not** initially generate simplistic employee productivity scores from commits, messages, or activity.

Focus on:

> What is happening, why, and what requires attention?

rather than:

> Which employee generated the most activity?

---

## 18. Events

Canonical actions generate ordered events:

```
message.created
branch.created
agent.run.started
agent.output.created
agent.run.completed
task.updated
decision.created
artifact.created
synthesis.completed
```

Events support:

- multiplayer synchronization;
- reconnect;
- ontology freshness;
- provenance;
- audit.

State mutation and event creation must be atomic.

---

## 19. MVP

The first product must prove one workflow:

```
3–5 humans
      ↓
Shared Channel
      ↓
Real Problem
      ↓
2–3 AI Agents
      ↓
Individual Outputs
      ↓
Human Selection
      ↓
Synthesis
      ↓
Artifact
      ↓
Basic Ontology
      ↓
Meta Query
```

### Required

- authentication;
- shared channels;
- real-time synchronization;
- persistent AgentRuns;
- persistent AgentOutputs;
- single-agent mode;
- parallel-agent mode;
- branches;
- selective synthesis;
- artifacts;
- basic permissions;
- minimal ontology;
- evidence-backed Meta queries.

---

## 20. MVP Ontology

Start small.

Objects:

```
Person
Project
Task
Decision
Artifact
Claim
AgentOutput
```

Relationships:

```
OWNS
BLOCKS
DEPENDS_ON
SUPPORTS
CONTRADICTS
REFERENCES
DERIVED_FROM
```

Do not build a universal company ontology before validating extraction quality.

---

## 21. Non-Goals

Do **not** initially build:

- a Slack replacement;
- a ClickUp/Jira replacement;
- an HRIS;
- employee productivity scoring;
- autonomous management;
- autonomous agent swarms;
- complex agent-to-agent conversations;
- Kafka;
- Kubernetes;
- CRDTs;
- a graph database without demonstrated need;
- custom model serving;
- hundreds of skills;
- unrestricted autonomous production actions.

---

## 22. Product Risks

### Scope Explosion

The product can easily become Slack + ClickUp + Palantir + an agent framework.

**Response:** prioritize multiplayer AI collaboration.

### Ontology Noise

Bad extraction could institutionalize incorrect information.

**Response:** provenance, confidence, corrections, confirmation, evaluation.

### Agent Cost

Parallel agents multiply cost.

**Response:** explicit execution strategies, limits, cost visibility.

### Permission Complexity

Policy systems can become incomprehensible.

**Response:** simple capability model and deterministic enforcement.

### Collaboration Friction

Turn locking may interrupt natural collaboration.

**Response:** treat it as an MVP execution policy and evaluate smaller lock scopes later.

### Surveillance Drift

Management features could become employee-monitoring software.

**Response:** model work, dependencies, projects, decisions and outcomes rather than activity scores.

---

## 23. Success Criteria

The key product question is:

> Would a real team rather solve a complex problem in MultiAI than coordinate in Slack while everyone separately uses ChatGPT/Claude?

Signals:

- multiple humans regularly collaborate in the same AI session;
- users inspect individual agent outputs;
- users deliberately include/exclude outputs;
- synthesis artifacts are reused;
- ontology knowledge remains useful across sessions;
- Meta answers questions that previously required manual investigation;
- teams voluntarily return for subsequent work.

If those behaviors do not occur, broader management and enterprise features should not be built.

---

## 24. Development Roadmap

### Phase 0 — Correctness

- authentication;
- authorization;
- atomic events;
- reconnect;
- idempotency;
- concurrency control.

### Phase 1 — Multiplayer AI

- shared channels;
- one functional agent;
- persistent runs;
- shared outputs;
- turn-locked execution.

### Phase 2 — Branches

- multiple agents;
- independent outputs;
- comparison;
- provenance.

### Phase 3 — Synthesis

- output selection;
- synthesis profiles;
- versioned artifacts.

### Phase 4 — Ontology

- core objects;
- relationships;
- provenance;
- lazy extraction;
- freshness cursors.

### Phase 5 — Meta

- status;
- blockers;
- changes;
- decisions;
- evidence-backed queries.

### Phase 6 — Control Plane

- richer skills;
- tool gateway;
- approvals;
- configurable roles/policies.

### Phase 7 — Integrations

- GitHub;
- task manager;
- collaboration systems;
- documents.

### Phase 8 — Management Intelligence

- progress reports;
- decisions needed;
- risks;
- exceptions;
- project intelligence.

---

# 25. Core Product Thesis

MultiAI should not win because it contains more features than Slack, ClickUp, or existing AI assistants.

It should win because it introduces a better model for **collective human–AI work**:

```
Collaborate
     ↓
Branch
     ↓
Reason
     ↓
Compare
     ↓
Synthesize
     ↓
Structure
     ↓
Remember
     ↓
Query
     ↓
Act
```

The defensible product is the combination of:

**multiplayer AI + inspectable branches + selective synthesis + provenance-aware ontology + governed agents + organizational Meta intelligence.**

Everything else should be built only when it strengthens that loop.
