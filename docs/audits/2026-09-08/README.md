# XYZZY audit — 8 September 2026

## Verdict

XYZZY has a credible, unusually focused foundation: a shared technical decision can retain
each specialist's output, deliberate human selection, an immutable synthesis version, and
inspectable provenance. It is a self-hosted application and agent orchestration layer, not a
model library or a complete general-purpose agent platform.

This audit found and repaired concrete authorization, recovery, provider-context, and UI defects.
Separate backend and UI critics inspected the actual patches and passed their scoped changes
after feeding regressions back for repair. **This is not proof that XYZZY beats its competitors,
is production-ready for every deployment, or has no remaining defects.** The recorded baseline
still lacks a live three-to-five-person comparison. Under its own rules, an unverified win is a loss.

The highest-value next move is a real technical decision with three humans and live specialists,
not more infrastructure or an unrestricted agent swarm.

## Scope and method

- Fast-forwarded this checkout from `9ba1e8e` to origin/main `4606ccf` (v0.4.0).
  The existing `AGENTS.md` edit was preserved and excluded from the review changes. This report
  records local validation before PR publication; no production deployment was performed.
- Read current code rather than treating the September 1 backlog as current truth. Upstream had
  already changed 251 files, split the service, and moved the browser application into its package.
- Independent lanes inspected authorization/replay, task/provider/synthesis lifecycle,
  competitor evidence, and UI behavior. Reproducing failures preceded the substantive repairs.
- Exercised real local HTTP/WebSockets and Chromium, failure injection, cross-process behavior,
  and a file-backed acknowledgement benchmark. No real API credential or external NEXUS checkout
  was used. Simulator results establish mechanics, **not reasoning quality**.
- Current source inspection is authoritative here. The old knowledge graph from another checkout
  was not treated as a fresh graph of `4606ccf`.
- Market research covers **10 August–8 September 2026**, using official repositories, release
  notes, and product documentation. Competitors were not executed side by side. No Exa was used.

## What was wrong, and what changed

| Severity | Reproduced problem | Repair and evidence |
|---|---|---|
| P1 | Removing workspace membership left existing room subscriptions alive; a removed member could receive future content until heartbeat revalidation. | Revoke every affected local/remote subscription after the durable removal, notify clients, broadcast canonical events, then handle advisory presence cleanup. Failure-injected two-room regressions cover Redis presence outages. |
| P1 | Room creation authorized membership before its write transaction; a concurrent removal could make that decision stale. | Repeat the workspace-membership check inside the writer transaction. |
| P1 | A socket could pass its initial check, lose membership during registration/backfill, and still receive replayed or queued events. | Recheck durable read authorization after registration and before delivery. Database-check failures close with 1011 and terminate even a silent receiver; cleanup unsubscribes every room before advisory presence work. |
| P1 | An A2A task paused for approval was marked failed because no output existed yet; later approval could complete the run while the task stayed failed. | Task state now follows the matching execution atomically through authorization-required, working, completion, rejection, expiry, and cancellation. Cancellation prevents late output; restart recovery preserves an already completed answer. |
| P1 | Cancellation or a crash during synthesis could strand a RUNNING record and its idempotency key indefinitely. The start event was delayed until termination. | Persist start/state/key together; bound provider execution and publication by an immutable five-minute deadline; terminalize cancellation; recover expired work without failing another worker's fresh request; reject late publication. Failed keys retain their failure; retry with a new key. |
| P2 | The direct model-provider harness omitted the specialist's name, role, and instructions. | Both harnesses now use the same specialist prompt assembly, once, with the same frozen branch context and exact recorded provider input. |
| P2 | Redis subscriber backoff computed an unbounded exponent and overflowed after roughly 1,024 consecutive failures. | Bounded delay doubling/reset; a regression recovers after 1,100 failed attempts. |
| P2 | A launch started in one channel could continue using another channel's mutable UI identifiers after navigation. | Capture launch room, identity, and returned branch; avoid duplicate launch and stale navigation. Uncertain creation is reported as unconfirmed, not falsely declared absent. |
| P2 | Live snapshots replaced output cards, destroying keyboard focus and expanded provenance. Excluded cards dimmed already-muted text. | Keyed reconciliation preserves review state; explicit Included/Excluded/Needs review labels remain readable without opacity or strikethrough. |
| P2 | Human synthesis titles leaked between branches; branch cards counted only the currently selected branch; evidence lookup depended on that branch. | Branch-scoped title drafts, room-wide output/selection lookup, and room-wide evidence resolution. |
| P2 | Publication could double-submit, lose its busy state on refresh, or leave a previous artifact selected. Meta responses could repaint after channel navigation. | Single-flight publication with immediate visible/ARIA feedback, select the returned artifact, and bind asynchronous results to their initiating request/channel/session. Reset Meta on channel change/revocation. |
| P2 | Mobile header actions squeezed out the channel title; notifications were clickable but not keyboard buttons. | 44px mobile controls, secondary actions in an accessible menu, native notification buttons, visible synthesis labels/guidance, and the full branch question in the review view. Desktop menu Crtraversal explicitly skips mobile-only hidden items. |
| Gate | The 64 MiB body-cap test used Linux `/proc` on macOS. The test named “p95 below 250 ms” did not assert that limit. | Cross-platform lifetime peak-RSS measurement preserves all memory/body/time assertions. The acknowledgement test now enforces the actual 250 ms contract. |

Regression sources:
[workspace boundaries](../../../tests/security/test_sept8_workspace_boundaries.py),
[replay and cleanup](../../../tests/security/test_sept8_replay_revocation.py),
[task lifecycle](../../../tests/security/test_sept8_task_approval_lifecycle.py),
[synthesis recovery](../../../tests/failure/test_sept8_synthesis_recovery.py),
[harness parity](../../../tests/regression/test_sept8_harness_prompt_context.py),
[Redis recovery](../../../tests/unit/test_sept8_fanout_recovery.py), and
[browser operation context](../../../tests/e2e/test_operation_context.py).

## How it works

```text
Browser modules ── REST / WebSocket ── FastAPI routes
                                          │
                         domain services + deterministic policy
                          │                                │
                  SQLite transaction                authorized AgentRun
                state + ordered event                      │
                          │                        selected harness
                     realtime hub                    │           │
                local / optional Redis            NEXUS       direct model
                          │                          └─────┬─────┘
                  snapshot + replay                 configured provider
                                                           │
                retained outputs → human selection → synthesis version
                                                           │
                                evidence / ontology → bounded Meta answers
```

The browser is a build-free ES-module client, with shared state and a small event bus connecting
renderers and synchronization. The server composes fourteen domain service mixins. Routes
translate requests; services enforce domain rules; repositories own persistence. SQLite WAL and
explicit transactions bind state changes to canonical, monotonically ordered events. WebSockets
are delivery, not the source of truth: snapshots and replay rebuild persisted collaboration.

A branch freezes a bounded channel snapshot (up to 50 messages and 100 events), its initiating
prompt, and a context hash. It starts one turn-locked specialist or two-to-three parallel
specialists. Each run retains identity, authority, interventions, output, and provider provenance.
Humans review every output and choose synthesis inputs; parallel synthesis requires at least two
included outputs, single-agent synthesis at least one. General Synthesis, Decision Brief, and
Progress Report produce versioned artifacts. Ontology marks derivation/review status and evidence;
Meta is a bounded, permission-aware query layer, not unrestricted chat over the whole organization.

Source entry points: [service composition](../../../src/multiplayer/services/service.py),
[branch lifecycle](../../../src/multiplayer/services/branches.py),
[step execution](../../../src/multiplayer/services/steps.py),
[authorization](../../../src/multiplayer/security/authorization.py),
[storage](../../../src/multiplayer/db/connection.py),
[socket boundary](../../../src/multiplayer/realtime/websocket.py), and
[browser wiring](../../../src/multiplayer/web/js/app.js).

## All provider paths — implementations versus compatibility

| Layer | Implemented path | Important qualification |
|---|---|---|
| Model | `OpenAIResponsesProvider` | Selected with `OPENAI_API_KEY` when no local base URL is configured. Source default is `gpt-5.4-mini`; operator can change it. |
| Model | `OpenAIChatCompletionsProvider` | `XYZZY_LOCAL_MODEL_BASE_URL` takes precedence. Ollama, LM Studio, vLLM, and llama.cpp are compatible-server examples, **not four native adapters**. Structured-output support depends on the selected server/model. |
| Model | `WorkflowOnlyModelProvider` | No configured endpoint/key means conspicuously labeled simulated output. It is not an analytical fallback model. |
| Execution harness | `NexusHarness` / `NexusAgentBridge` | Default harness. External runtime is opt-in through `XYZZY_NEXUS_PATH`; without it, the bridge calls the configured model directly. A configured but unimportable checkout logs a warning and falls back. |
| Execution harness | `ModelProviderHarness` | Direct asynchronous provider contract. A harness abstraction is not a registry of separate model vendors. |
| Memory | `StubMemoryProvider` at the NEXUS bridge | Default recall returns nothing. Durable room/workspace/org memory records elsewhere do not make this placeholder a semantic retrieval implementation. |
| Identity | Operator bearer credentials and configurable OIDC | Protocol integration, not separate baked-in adapters for each identity vendor. End-user email invitation/onboarding is still limited. |
| Storage / delivery | SQLite; local hub or optional Redis pub/sub/presence | Redis is not the durable event store. There is no Postgres or graph-database backend. |

Selection code: [environment factory](../../../src/multiplayer/model_providers/openai_responses.py),
[compatible client](../../../src/multiplayer/model_providers/openai_chat_completions.py),
[harnesses](../../../src/multiplayer/harness/adapters.py),
[bridge](../../../src/multiplayer/nexus_bridge/agent_bridge.py).

Provider caveats: configuration is process-wide rather than per-user/per-specialist routing.
The compatible endpoint receives `OPENAI_API_KEY` as bearer auth if present; use a deliberate
endpoint-specific credential or unset it. Default requests have a 45-second timeout and a
4,096-token output cap; `XYZZY_RUN_TOKEN_BUDGET` defaults to 500,000 tokens and checks already
recorded usage before another step (`<=0` disables it). An in-flight step can overshoot that
threshold. These are not a hard monetary ceiling, complete per-workspace budget, or per-call ledger.
The adapters do not implement streaming or rate-aware retry/backoff. Intermediate provider calls
lack the same durable per-call record as the terminal output. Cancellation/late-output fencing
does not prove that an upstream provider stopped work or stopped charging. Native Anthropic,
Gemini, xAI, and Hermes adapters are not implemented by this inventory.

## Competitive comparison — current evidence, not a manufactured leaderboard

| Project | Relevant evidence in the last 30 days | Implication for XYZZY |
|---|---|---|
| **QM** — direct | v0.1.9 on September 5; September 4 added email invitations with role/expiry and no-Slack directory resolution. September 3 expanded principal-aware search, per-user model accounts, and scoped memory. [Official releases](https://github.com/yc-software/qm/releases) | This is the sharpest adoption/integration benchmark: people must join and bring useful context without an operator translating IDs. Compete on inspectable decisions, not a larger integration checklist. |
| **Buzz** — direct | Shared rooms with agents, unified events/search, canvas, and development workflows; active September changes include information-flow work. Its own README marks approval-gate integration as unfinished. [Repository](https://github.com/block/buzz), [September 4 commit](https://github.com/block/buzz/commit/4d447b9c20a23fb33c94778e6cf309424abea6c8) | Study ambient collaboration and the bridge from conversation to real work. Do not infer security superiority from either project's feature list. |
| **Grok Bot** — adjacent | Enterprise announcement September 3; current collaboration docs describe groups of two-to-six bots. [Announcement](https://x.ai/news/grok-bot-for-enterprise), [collaboration docs](https://docs.x.ai/grok-bot/chat-and-collaboration) | Benchmark convenience and capable execution, but a bot group is not evidence of the same three-human, explicit-selection, versioned-decision workflow. |
| **Hermes Agent** — adjacent | v0.21.1, tagged `v2026.9.7`, released September 7; broader skills, memory, delegation, scheduling, provider/gateway ecosystem. [Release](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.9.7), [repository](https://github.com/NousResearch/hermes-agent) | Strong runtime breadth comparator or future governed integration. Rebuilding its entire agent runtime would distract from XYZZY's multiplayer decision wedge. |
| **Commonly** — direct/early | Recent repository activity around decision/mention attention, catch-up freshness, and Slack/Telegram relay; individual PR details were not all retrievable. [Repository history](https://github.com/Team-Commonly/commonly/commits/main) | An actionable “needs your decision”/“what changed” surface is a useful narrow next step. Activity is not proof of a shipped end-to-end feature. |
| **Patchwork** — adjacent/experimental | Latest release observed was v0.2.6 on August 17; emphasizes human decision inboxes, agent worktrees, previews, and PR feedback. [Release](https://github.com/vincelwt/patchwork/releases/tag/v0.2.6), [repository](https://github.com/vincelwt/patchwork) | Link a decision to an owned experiment, issue, or PR and its outcome. Do not turn XYZZY into a general worktree manager. |
| **Agor** — adjacent | Shared agent development workspace with branch environments, harnesses, permissions, and cost visibility; version metadata was inconsistent in inspected sources. [Repository](https://github.com/preset-io/agor) | Compare visible execution state and cost attribution. No precise release/version claim is made here. |

The [recorded shared-Project benchmark](../../benchmarks/chatgpt-shared-projects-baseline.md)
remains the acceptance test, not a completed experiment. No blind result was fabricated from
screenshots, simulated outputs, release notes, or a critic's opinion.

## What is still missing — ordered by payoff

1. **A live three-human evidence run.** Use one consequential question and identical source material
   in XYZZY and the reference. Record time to first useful comparison, setup friction, omissions,
   retained disagreement, selection intent, provenance coverage, recovery, and resulting decision
   quality. Remove product labels from artifacts; an independent critic chooses a winner and the
   largest gap. Live provider spend, reference access, and human participation are still needed.
2. **Independent skill authority and complete provider accounting.** Current skill capabilities
   come from the template rather than a separately governable skill grant. Add a narrowly scoped
   grant model and per-call ledger before claiming the full five-principal contract. Include
   input/output usage, retries, interruption, latency, and uncertainty about upstream cancellation.
3. **Original-source evidence, not only a model's text.** Current provenance can faithfully freeze
   an agent output without proving the output's factual claims. Capture authorized document/URL/code
   revisions and exact excerpts; link claims to them. Track corrections and invalidated sources.
   Do not quietly treat model-derived ontology as confirmed organizational knowledge.
4. **Operator-light onboarding.** A workspace-member directory and invite suggestions already
   exist; global/email discovery and an email invitation/acceptance path would remove more trial
   friction than a marketplace. Reuse the existing identity and
   membership boundaries; do not make invitation equal to unrestricted workspace access.
5. **Decision authoring and outcomes.** Manual New decision and Accept/Supersede controls already
   exist. Add artifact text draft/edit/review, artifact-version comparison,
   rationale for exclusions, and a minimal owner/action/issue/PR link after publication. Preserve
   the original specialist outputs and exact selected version throughout.
6. **Useful existing APIs in the UI.** Custom specialists, room templates, attachment binding,
   share-link management, and audit export have server-side pieces but incomplete client workflows.
   Expose the smallest slices that improve the decision loop, not another broad control plane.
7. **Mobile reading space and attention.** The sticky publication form consumes about 300px of an
   844px viewport. Collapse it to a compact review summary until requested. Add a focused queue for
   pending approvals, disagreements, changed evidence, and decisions awaiting a human; a narrow
   Slack relay may reduce adoption friction more than recreating chat software.
8. **Measured operational limits.** Five simultaneous 5,000-event synthetic replays with genuine
   file-backed authorization checks took 11.0–11.7 seconds per socket, versus 0.82–1.14 seconds with
   handshake-only authorization. This isolates query/serialization cost and excludes network time.
   A future cache/epoch or batching design must prove equivalent revocation fences. SQLite and
   shared local storage are still the deployment boundary; do not claim multi-node scale.
9. **Maintainability and test fidelity.** The service split improves navigation, but the shared
   core is still about 1,400 lines and the repository module about 6,500. Reduce cross-domain
   coupling incrementally, without splitting atomic workflows across services. Some tests under
   `e2e` assert source strings rather than executing a browser; one such stale Meta assertion
   surfaced in this audit. Keep executable behavior tests alongside those lightweight contracts.

There is also residual documentation and validation debt: not every supported interpreter,
container image, identity provider, local model, or optional runtime was exercised on this Mac.
The marketing page retains a long hero and technical proof strip, and externally hosted fonts;
these deserve measured copy/performance work, not an unsupported visual “win” claim.

## UI evidence and quality checks

The interface refinement deliberately preserved the existing brand and navigation model.
Impeccable directed the accessibility, state, and responsive audit; Design Taste informed the
marketing critique without applying a landing-page layout to a real-time workspace. A separate
critic checked the actual rendered interface and found the mobile form height to be remaining debt.

Representative captures:
[desktop review, light](evidence/branch-1440-light.png),
[desktop review, dark](evidence/branch-1440-dark.png),
[mobile publication](evidence/publish-390-dark.png),
[320px channel menu](evidence/menu-320-light.png),
[320px dark review](evidence/branch-320-dark.png).

Captured 1440×900, 390×844, and 320×844 in both themes. Observed no page errors or document-width
overflow in the completed captures; mobile header targets are 44×44px. On the excluded dark card,
computed body contrast was 7.33:1 and disposition contrast 5.25:1 with opacity 1. This is targeted
evidence, not a whole-application WCAG certification. The skill detector ran in degraded regex
mode because its optional HTML/CSS parser dependencies were unavailable; it could not certify
computed contrast. Its two layout-animation warnings were inspected and the height/margin
transitions removed. One capture needed retry after the demo's request limit; it was not treated
as a permanent missing-button UI defect.

## Verification

**Final full suite: 1,329 passed, one skipped, in 285.51 seconds.** The skipped case requires a
live `OPENAI_API_KEY`; no real provider was silently substituted. There are **46 new regression
cases**. The suite reports 28 existing transport/cookie deprecation warnings; these remain upgrade
maintenance work, not suppressed warnings.

- Initial upstream baseline: **1,282 passed, one failed, one skipped**. The failure was the macOS
  `/proc` assumption in the body-cap test, repaired without relaxing its memory/time limits.
- Latest focused browser regression run: **12 passed**; independent probes also preserved focus
  and expanded provenance during an actual selection change, not only an unchanged snapshot.
  The publication test was additionally strengthened to create a different artifact type, then
  passed in isolation, so retaining the seeded selection cannot satisfy its assertion accidentally.
- Backend critic: **PASS** on the final scoped diff after two fault-injection regressions were
  repaired. UI critic: **PASS** on its scoped patch, not a competitive/product-wide verdict.
- Ruff check/format, strict mypy across 61 source files, and 94 documentation anchors passed.
- All 24 local report links resolve; `pip check` found no broken requirements.
- `pipx run pip-audit -r constraints.txt`: **no known vulnerabilities found** in the pinned
  Python dependency set. This does not audit the container base image or establish absence of
  undisclosed vulnerabilities.
- Local file-backed selection acknowledgement p95: **1.343 ms**, 100 writes; all 100 distinct,
  contiguous events and the final selection survived reopening. This is a service benchmark,
  not Internet latency or the end-to-end reasoning time.

Commands exercised in the local Python 3.12 virtual environment:

```bash
git pull --ff-only origin main
pip install -c constraints.txt -e '.[dev,e2e,redis]'
python -m playwright install chromium
PYTHONPATH=src .venv/bin/python -m pytest
PYTHONPATH=src .venv/bin/python -m pytest tests/e2e/test_operation_context.py
PYTHONPATH=src .venv/bin/python -m pytest -s tests/performance/test_ack_latency.py
.venv/bin/ruff check .
.venv/bin/ruff format --check .
PYTHONPATH=src .venv/bin/mypy src
.venv/bin/python scripts/check_anchors.py
pipx run pip-audit -r constraints.txt
```

Loopback/browser checks required local execution outside the restricted socket sandbox.
No production database or real customer workspace was used. The audited changes are packaged
for review; merging and deployment remain separate actions.

During local validation, the patched demo ran at `127.0.0.1:8018`, using only the audit's temporary
database at `/private/tmp/xyzzy-audit-sept8.db`. Its health check returned `ok`. This address is
local to the audit machine, not a deployed review environment.
