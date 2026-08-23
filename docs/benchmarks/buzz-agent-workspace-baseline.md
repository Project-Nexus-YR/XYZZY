# Agent-workspace baseline: block/buzz and yc-software/qm

## Status

Active quality bar for the conversation layer, agent identity, and retrieval, alongside
`chatgpt-shared-projects-baseline.md`. The ChatGPT baseline remains the bar for the decision
workflow: shared context, branch discoverability, selection, and synthesis provenance. This
document is the bar for everything that makes a workspace a workspace.

Adopted 2026-08-23 at the user's direction.

## First reference: block/buzz

<https://github.com/block/buzz>

Verified directly from the repository page on 2026-08-23:

- tagline, verbatim: "A workspace where humans and agents build together, on a relay you own";
- 30.0k stars, 3.8k forks, Apache-2.0, Rust and TypeScript;
- complete: relay, channels, threads, direct messaging, canvases, media, full-text search,
  audit logging, desktop app, mobile clients, `buzz-cli`, ACP harness;
- in progress: workflow approval gates, push notifications, huddle lifecycle, YAML workflows,
  git events (NIP-34), git hosting;
- planned: web-of-trust reputation, culture features, advanced governance.

Secondary references, used only for interaction-design ideas and cited as such: Nous Research's
Hermes Agent (<https://github.com/NousResearch/hermes-agent>) and xAI's Grok bot
(<https://docs.x.ai/grok-bot/overview>, plus the mention-triggered `@grok` behaviour on X, which
is documented only in platform announcements and press).

## What Buzz does that MultiAI does not

Every item below is a capability MultiAI lacks entirely as of commit `c7836ab`.

| Buzz | Where |
|---|---|
| Threads as a first-class table: `parent_event_id`, `root_event_id`, `depth`, `reply_count`, `descendant_count`, `last_reply_at`, `broadcast` | `schema/schema.sql` |
| Mentions normalized into an `event_mentions` table indexed on `(pubkey_hex, created_at)`, driving both notification and agent routing | `schema/schema.sql`, `ARCHITECTURE.md` |
| Reactions with soft delete: `(event, pubkey, emoji)` primary key, `removed_at` rather than row deletion | `schema/schema.sql` |
| Full-text search over one kind-agnostic event table, so chat, patches, workflow runs and approvals are one index | `schema/schema.sql`, `README.md` |
| Agent identity as a keypair with a fail-closed launch invariant: an agent with no private key must refuse to start | `docs/remote-agents.md` |
| Delegated authority: a mutation requires a non-null `auth_tag` or `launch.owner_pubkey`, so every agent action names the human who authorised it | `docs/remote-agents.md` |
| Addressing modes on the harness: owner-only, allowlist, anyone, nobody | `crates/buzz-acp/README.md` |
| One harness contract, so any agent implementing ACP over stdio works: `initialize`, `session/new`, `session/prompt`, streaming `session/update`, `stopReason` in `end_turn`/`cancelled`/`max_tokens` | `crates/buzz-acp/README.md` |
| Owner-issued interrupt verbs validated against the owner's signature | `docs/remote-agents.md` |

## What Buzz concedes it does not do

These are quoted or paraphrased from the repository's own documentation, not inferred. They are
the ground MultiAI must hold.

1. **Search authorization is advisory.** `buzz-search` "returns candidate hits; the relay
   re-authorizes each one" afterwards, and private kinds are excluded by a hardcoded blocklist of
   kind IDs in a generated SQL column. A missed re-check anywhere on that path leaks private
   content, and any new sensitive kind is searchable by default until someone edits the list.
   (`ARCHITECTURE.md`, `schema/schema.sql`)
2. **Ordering is wall-clock.** Relay ordering relies on Postgres insertion order with no vector
   clock or sequence, across multiple nodes fanned out through Redis. (`ARCHITECTURE.md`)
3. **No read state exists.** Per-user, per-channel last-read position is not modelled
   server-side at all. (`ARCHITECTURE.md`)
4. **Thread counters are denormalized.** `reply_count` and `descendant_count` are maintained
   counters, not values derived from the append-only log, so a write-path bug desyncs them from
   the actual reply graph. (`schema/schema.sql`)
5. **Tool permission scoping is deferred.** "The specification does not comprehensively address
   tool permission scoping at the agent level"; identity proves who acted, not what they were
   allowed to do. Pod-level sandboxing substitutes for capability restriction.
   (`docs/remote-agents.md`)
6. **Approval gates are unfinished.** Listed as in progress, not complete. (`README.md`)
7. **Agent removal and crash semantics are unspecified.** Who may add or remove an agent from a
   channel, what happens to in-flight sessions on removal, and what survives a harness crash are
   not documented. (`docs/remote-agents.md`)

## Second reference: yc-software/qm

<https://github.com/yc-software/qm>

Added 2026-08-23 at the user's direction. Verified directly from the repository page and from
`package.json`: tagline, verbatim, "A multiplayer agent harness for work. In Slack and on the web";
14.1k stars, 1.7k forks, MIT, TypeScript; `package.json` describes it as the "Headless core for the
shared org agent (managed-agents architecture)", version 0.1.0.

qm is the closer competitor. Buzz is a workspace that agents can join; qm is an agent harness for a
whole organisation, which is the same product thesis as MultiAI.

### What to take from it

| Idea | Where | Why it matters here |
|---|---|---|
| Three declared security postures — Strict pauses every tool call for approval, Auto screens external content before it reaches the model, Dangerous does neither — inheritable by narrower scopes | `SECURITY.md` | MultiAI has per-tool `requires_approval` booleans and no posture above them. A room-level posture that a narrower scope may only tighten, never loosen, is the missing layer |
| An always-on command policy of hard denials and approval rules that applies even under the most permissive posture | `SECURITY.md` | A floor no policy can lower, independent of the capability intersection |
| Agents act with the authorising human's own permissions rather than a service identity | `README.md` | Independent confirmation of P6's delegated authority: attribution by construction rather than by logging |
| A run record carrying `leaseToken`, `leaseExpiresAt`, `workerId`, `attempts`, `maxAttempts`, with `leaseLapsed()` reclaiming stuck runs, `errorParks()` parking a run that exceeds its attempts, and a reaper process | `src/runs/run-store.ts` | This is the answer to "no run may be left in a state the system cannot describe". P6 has the lease and the sweep; it is missing a parked terminal state |
| Idempotent enqueue via `dedupKey` on the run store | `src/runs/run-store.ts` | Matches MultiAI's idempotency keys, applied to run submission rather than only to writes |
| Background work as first-class triggers — crons, webhooks, watches | repository overview | Confirms `triggered_by: SCHEDULE` is a real category, not speculation |
| Skills with their own access control, separate from the channel model | repository overview | MultiAI folds skills into the five-way intersection; qm gives them an ACL of their own |

Session concurrency converges with ours independently: `createOrchestrator()` takes a per-session
lease and refuses a second turn with "session busy (another turn is in progress)"
(`src/core/orchestrator.ts`) — MultiAI's turn lock, arrived at separately. Parallelism comes from
separate worker processes across sessions; no dependency graph or fan-out primitive was found in the
files read.

### What qm concedes it does not do

From its own `SECURITY.md` and open issues, not inferred:

1. **"Credential purposes are not enforced authorization."** Checks cover ownership and expiry, not
   whether a later command stays within the stated purpose.
2. **The command policy is "a speed bump against mistakes and injection, not a sandbox boundary"**,
   and is evadable by obfuscation or encoding.
3. **Browser actions do not re-enter command policy or human-in-the-loop approval**, relying on
   task-level consent only.
4. **Sandbox credentials are plaintext while in use** and readable by any process in that sandbox.
5. **"Model-context entries do not yet carry complete origin labels for every granted read, so
   mixed-permission filtering is incomplete."**
6. **Turn outcomes are not recorded faithfully** — issue #609 reports that "silent" means two
   different things and that suppressed turns are absent from metrics.
7. **A tracked task lives in only one session**, so qm "answers confidently and wrongly about it"
   elsewhere — issue #608. This is exactly the failure P7's refuse-rather-than-guess rule exists to
   prevent.
8. **Admins are privileged content readers** with scope-authorized access to transcripts and memory,
   audited but requiring no additional approval.

The sentence that states the whole difference, from `SECURITY.md`: **"audit records support
investigation; they do not prevent an action."** MultiAI's claim is the opposite one — the check is
inside the write transaction, so the action does not happen. Every piece in this gauntlet is judged
against that.

### Not copying

qm's `Dangerous` posture — a selectable tier with neither approval pauses nor content screening —
has no place in a governed workspace; a posture may tighten the floor, never remove it. Nor the
denylist-as-boundary pattern, which qm itself calls evadable, nor plaintext in-sandbox credentials.

---

## Seeded scenario

The same decision as the other baselines, so runs remain comparable:

> Should a 20-person AI-native SaaS startup migrate its authentication service from a
> self-managed PostgreSQL session store to a managed identity provider this quarter?

Run it as three humans and three specialist agents in one channel, with the conversation carried
in threads rather than one flat log, at least one agent invoked by mention, and the final brief
produced by selective synthesis.

## Binary comparison rules

Strip product labels before criticism. Ties and unverified conditions count as a MultiAI loss.
The critic returns a binary winner and the single largest remaining gap, nothing else.

### P5 — Conversation layer

MultiAI wins only if it matches Buzz on threaded replies, mention addressing, reactions, and
cross-object search, while clearly beating it on all of:

1. every thread reply is an ordered event with a room sequence, and reply counts are derived
   from that log rather than stored as a counter that can desync;
2. search authorizes inside the query, not after it, and a test proves a non-member retrieves
   zero rows for content they cannot read;
3. no content is searchable by default: indexing is opt-in per object kind, not excluded by a
   blocklist;
4. per-user, per-room read position is durable server-side state that survives reconnect;
5. a mention that addresses an agent records why the agent spoke, and cannot invoke an agent the
   mentioning user lacks the capability to invoke.

### P6 — Agent identity and harness

MultiAI wins only if it matches Buzz on durable agent identity and a pluggable harness, while
clearly beating it on all of:

1. every agent action names the human whose authority it runs under, and that authority is
   re-checked at execution time, not only when the run was requested;
2. effective permissions remain the five-way intersection, so identity alone never grants a tool;
3. addressing modes are enforced in application code against durable records, not harness
   configuration;
4. removing an agent from a room deterministically settles its in-flight runs, with an event
   recording the outcome;
5. a harness crash leaves no run in a state the system cannot describe.

### P7 — Ontology and Meta

Buzz has no equivalent, so the bar here stays the ChatGPT shared-Projects baseline plus:

1. extraction is lazy and cursored: re-running it does no duplicate work and no assertion is
   silently stale;
2. Meta answers status, blockers, changes, decisions, and disagreement, not a fixed string
   whitelist, and refuses rather than guesses when evidence is absent;
3. every Meta answer drills down to exact source evidence and is filtered by the asker's
   permissions.

## Measurements

Record evidence, not estimates: full suite result, ruff and mypy result, p95 acknowledgement
latency, the room event sequence range for the run, count of final claims traceable to exact
evidence, and any assertion whose freshness cursor was behind at read time.
