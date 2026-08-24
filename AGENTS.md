# XYZZY Repository Contract

This repository builds the multiplayer workspace for teams working with AI. The product is
successful only when 3–5 people can solve a consequential technical problem together more
effectively than they can in a ChatGPT shared Project with shared context and branched chats.

## Product loop

Protect this sequence in every design and implementation decision:

`collaborate -> branch -> reason -> compare -> select -> synthesize -> artifact -> ontology -> meta`

The first wedge is collaborative technical decisions for AI-native technical teams. Immediate
value must come from shared context, inspectable specialist outputs, deliberate selection, and a
useful synthesis artifact. Ontology and Meta compound that value; they must not be prerequisites
for experiencing it.

## Delivery order

Build and verify in this order. Do not skip ahead to later phases while earlier invariants are
unmet.

1. Correctness: authentication, authorization, atomic state/event writes, idempotency,
   concurrency control, reconnect, and auditability.
2. Multiplayer AI: shared channels, one functional agent, persistent runs and outputs, and
   turn-locked execution.
3. Branches: independent multi-agent work, individual output inspection, comparison, and exact
   provenance.
4. Synthesis: human-selected inputs, General Synthesis, Decision Brief, and Progress Report;
   publish results as versioned artifacts.
5. Minimal ontology: Person, Project, Task, Decision, Artifact, Claim, AgentOutput and only the
   relationships needed by proven workflows.
6. Meta: permission-aware, evidence-backed answers about status, blockers, changes, decisions,
   disagreement, and supporting evidence.
7. Control plane and integrations only when the core loop needs them.

## Non-negotiable product rules

- Channels are durable collaboration and context boundaries; threads are conversations and
  branches are isolated AI-work contexts.
- Preserve every individual agent output after synthesis. Humans explicitly choose synthesis
  inputs. Every synthesis claim must drill down to its exact inputs and original evidence.
- Label AI-derived claims separately from confirmed facts. Store confidence, provenance, and
  corrections; never turn uncertain extraction into silent organizational truth.
- Retrieve the minimum authorized, relevant information. Never routinely send an entire
  workspace to a model.
- Important or irreversible actions remain human-governed, visible, and auditable.
- Integrate with existing systems before attempting to replace them.
- Model work, decisions, dependencies, and outcomes; never create employee activity or
  productivity scores.

## Security invariants

Authorization is enforced in deterministic application code, never by prompt text. Effective
capabilities are the intersection of user, agent, skill, channel, and workspace permissions.
Every tool action flows through capability checks, policy checks, approval when required,
execution, and an audit event. Deny by default. Enforce workspace and channel isolation at every
read, mutation, retrieval, subscription, and replay boundary. State mutation and canonical event
creation are atomic. Reject invalid state transitions and stale, duplicated, or replayed writes.

Never expose secrets, credentials, hidden model context, cross-workspace data, or unauthorized
tools. Add regression tests for every security or concurrency defect.

## MVP acceptance workflow

A releasable vertical slice must demonstrate, with persisted and inspectable state:

1. Three authenticated humans enter one shared channel around a real technical question.
2. They invoke 2–3 specialist agents in single or parallel mode.
3. AgentRuns, interventions, outputs, state changes, and ordered events survive reconnect.
4. Humans inspect outputs and deliberately include or exclude each one.
5. The selected outputs become a versioned decision artifact with complete provenance.
6. Minimal ontology objects and relationships are extracted with evidence.
7. Meta answers at least one “why,” blocker, change, or disagreement question and supports
   drill-down to the source evidence.

The workflow must beat the recorded ChatGPT shared Project baseline on parallel specialist
comparison, deliberate selection, exact synthesis provenance, and deterministic governance while
matching its shared-context and branch discoverability. Selected outputs require 100% provenance
coverage. Reconnect and concurrent-write tests must show zero lost or duplicated events. The
complete test suite, lint, and type checks must pass. Local p95 acknowledgement latency for
benchmarked interactions must remain below 250 ms.

## Engineering rules

- Prefer the simplest architecture that proves the workflow. SQLite, a single process, and
  bounded in-memory coordination are valid until measured usage requires more.
- Keep domain rules independent of transport and persistence. Route handlers validate/translate;
  services authorize and orchestrate; repositories own durable data access.
- Use canonical ordered events for synchronization, reconnect, provenance, audit, and ontology
  freshness. Preserve transaction boundaries across mutation and event creation.
- Make public mutations idempotent where retries are possible. Use explicit state machines for
  runs, approvals, artifacts, and synthesis.
- Add or update focused tests with every behavior change. Run the smallest relevant suite while
  iterating, then the full quality gate before handoff.
- Keep setup reproducible from a clean checkout. Document commands actually exercised.
- Preserve unrelated user changes. Do not weaken tests, authorization, validation, or provenance
  to make a check pass.

## Scope guardrails

Do not build a Slack replacement, task-manager replacement, HRIS, autonomous management system,
unrestricted agent swarm, complex agent-to-agent society, graph database, custom model-serving
stack, Kafka, Kubernetes, CRDT layer, or broad skill marketplace before demonstrated need. Avoid
infrastructure work that does not strengthen the core multiplayer decision loop.

## Gauntlet workflow

Split work into independently testable pieces. Builders own implementation; separate critics with
fresh context inspect the actual artifact. Critics compare anonymized outcomes against the real
ChatGPT shared Projects workflow, make a binary choice, and name the single largest remaining gap.
No self-scoring. Feed that gap back to a builder and repeat until XYZZY wins or the user stops
the run.
