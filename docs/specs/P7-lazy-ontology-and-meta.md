# P7 — Lazy Ontology and Meta

Design spec. Closes two verified gaps: no extraction freshness cursors exist anywhere in the schema, and
`_meta_question_kind` is a two-frozenset literal-string whitelist that raises on everything else. Covers PRD §9, §10, §15,
§17 and AGENTS.md delivery step 6. Room-scoped. Six decisions below were revised after an adversarial review measured
defects in the first draft; each names its evidence.

## 1. Freshness cursors and derived currency

**Decision.** Freshness is positioned on `room_events.sequence`, the monotonic per-room counter allocated from
`room_sequences` in the same transaction as the mutation it describes — already the repository's only total order, so
freshness, reconnect and provenance read one axis. Each assertion records the sequence it was written at; each extractor's
cursor is a **resume hint, nothing more**.

**Currency is derived, never stamped.** An assertion with `asserted_at_sequence = A` is **current** when no event in `(A,
head]` belongs to its **invalidation class** — computed by query at read time, per assertion, against one `head` snapshotted
for the answer; otherwise it is presented **as-of A** with the count of invalidating events. A global cursor cannot be the
oracle, because it and the allowlist sit on different axes: ASYNC reads only some event types while its cursor advances to
head, so a `task.updated` that closes a task is skipped yet the assertion still reports current. This is P5's rule for reply
counts — derive it, because a stored marker is a marker that can be wrong.

| Assertion kind | Invalidation class — event types in `(A, head]` |
|---|---|
| `Task` | the eight `task.*` members (`created`, `assigned`, `unassigned`, `started`, `completed`, `failed`, `cancelled`, `delegated`); `EventType` has no literal `task.updated` |
| `Decision` | `decision.created`, `decision.updated`, `decision.superseded`, `artifact.version_created`, `artifact.synthesis_published` |
| `Artifact` | `artifact.created`, `artifact.updated`, `artifact.version_created`, `artifact.synthesis_published` |
| `Claim`, `AgentOutput` | `agent.output.created`, `output.selection.updated`, `branch.synthesis.completed` |
| `Person` | `user.joined_room`, `user.left_room`, `user.role_changed`, `user.removed_room` |
| `Project` | `room.updated`, `room.archived` |
| any relationship | the union of its two endpoints' classes |

Every class also contains `ontology.assertion_superseded` and errs wide — a false "as-of" costs a caveat, a false "current"
a wrong answer — and is counted by one grouped `SELECT` per class per answer, never per claim. A reader gets `freshness =
{authorized_head, drain_lag_events, claims_as_of}`, each claim carrying `{asserted_at_sequence, current,
invalidating_events}`, all computed inside section 4's authorized scope; `drain_lag_events` is reported from the cursors so
a reader sees pending work but decides nothing, and `stale_at_sequence` stays a consolidation output.

**No duplicate work.** A pass snapshots `head`, reads only `sequence > last_sequence AND sequence <= head`, and writes
assertions under the existing deterministic IDs (`sha256(room_id:source_ids)`) with `ON CONFLICT DO NOTHING`. Assertions,
the `ontology.materialized` event and the cursor advance commit in one `BEGIN IMMEDIATE` transaction, so a crash rolls the
cursor back with the work — at-least-once delivery over idempotent writes is exactly-once in effect — and the advance is a
compare-and-swap that a trigger stops from regressing.

```sql
CREATE TABLE IF NOT EXISTS ontology_extraction_cursors (
    room_id       TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    extractor     TEXT NOT NULL CHECK(extractor IN ('IMMEDIATE', 'ASYNC', 'SCHEDULED')),
    last_sequence INTEGER NOT NULL DEFAULT 0 CHECK(last_sequence >= 0),
    last_run_at   TEXT NOT NULL,
    PRIMARY KEY (room_id, extractor)
);
-- on both ontology_entities and ontology_relationships:
ALTER TABLE <t> ADD COLUMN extractor TEXT NOT NULL DEFAULT 'IMMEDIATE';
ALTER TABLE <t> ADD COLUMN asserted_at_sequence INTEGER NOT NULL DEFAULT 0;
ALTER TABLE <t> ADD COLUMN evidence_event_sequences TEXT NOT NULL DEFAULT '[]';
ALTER TABLE <t> ADD COLUMN stale_at_sequence INTEGER;  -- NULL until consolidation marks it
```

`evidence_event_sequences` is a JSON list: an evidence ID names a row, a sequence names the moment in the log, and
drill-down needs both. **Events.** `ontology.extraction.advanced` — `{extractor, from_sequence, to_sequence,
entities_written, relationships_written}`; writes keep emitting `ontology.materialized`.

**Test** (`tests/integration/test_ontology_freshness.py`): run extraction twice with no intervening events and assert the
second pass writes zero rows and emits nothing; append events, re-run, assert every new assertion's `asserted_at_sequence`
equals `head`; force a mid-pass failure and assert cursor and assertions both rolled back. For currency: one event of an
assertion's invalidation class makes it not current **without** the cursor moving; one outside every class leaves it
current; and driving the ASYNC drain past an event type it does not read still reports that assertion not current — the
defect cursor-as-oracle allowed.

## 2. Lazy extraction — three timings, and why the fourth is deferred

**Reads never write.** Each surviving timing is a named extractor with its own cursor, all writing through one repository
method, so the transaction and event discipline is written once. **Immediate** — trigger: the committing transaction of a
structured action (`task.*`, `decision.created`, `artifact.version_created`, `artifact.synthesis_published`). Reads the
mutated row and its already-frozen provenance, nothing else; writes `SYSTEM_MATERIALIZED` assertions at `confidence = 1.0`
inside the caller's transaction, advancing the IMMEDIATE cursor. A structured record needs no inference, so deferring it
would only cost freshness.

**Asynchronous** — trigger: a bounded drain loop woken by the room broadcast, one in-process lease per room. Reads
unconsumed `message.created`, `agent.output.created` and `branch.synthesis.completed` events and their bodies, capped per
pass (default 200); writes `AI_DERIVED` assertions at `confidence < 1.0` and `review_status = 'UNCONFIRMED'` in its own
transaction. Inference is slow and fallible, so it never sits in a write path.

**Scheduled** — trigger: a periodic consolidation pass plus an admin-triggered run. Reads existing assertions only, never
raw evidence — deduplication, alias resolution, contradiction detection (writing `CONTRADICTS`), staleness marking; writes
new relationships and `stale_at_sequence` on superseded rows, emitting `ontology.assertion_superseded`. It never deletes,
because a removed assertion cannot be audited.

**Query-time — deliberately deferred.** PRD §10 lists a fourth timing; this build does not ship it, on a measurement:
`db/connection.py` holds one `aiosqlite` connection behind one `asyncio.Lock` that `transaction()` keeps for the
transaction's lifetime, and with one extraction transaction open an unrelated `SELECT 1` blocked for **1016 ms** against
AGENTS.md's 250 ms p95 budget — a bounded pre-step is not bounded in the dimension that matters. Two consequences confirm
it: a read that writes stamps a room current when a pass returns nothing, and the cursor trigger forbids the rewind that
would undo that. A reader ahead of the drain still is not guessing — each claim is labelled as-of, `drain_lag_events` gives
the unread range, and no work is triggered. It returns on one condition: the write path stops serializing every request
behind one connection and one lock, and the blocked-read figure is re-measured under 250 ms.

**Test** (same file): a Meta answer over a room with a backlog leaves every cursor value, every assertion row count and
`MAX(room_events.sequence)` identical and emits no event, while still reporting the right `drain_lag_events` and per-claim
`current` flags. No parameter makes a read write.

## 3. Meta question coverage

**Decision.** Three passes, in this order, and the order is the design:

```python
class MetaQuestionKind(StrEnum): ...  # the seven kinds named in the mapping table below


def classify_meta_question(question: str) -> MetaQuestionKind: ...  # refuses; never returns None
```

1. **Refusal first.** Surveillance and productivity markers refuse immediately, before any kind is considered, so a question
   tripping this pass can never reach a kind.
2. **Then exact match** against a curated corpus of accepted forms, normalized exactly as `_meta_question_kind` normalizes
   today (`" ".join(strip().lower().split()).rstrip("?!. ")`), extended from two kinds to seven.
3. **Anything else refuses.** There is no nearest-kind fallback, ever — that fallback is the defect.

The alternative, an ordered table of compiled patterns, was rejected on evidence: against the eleven strings in
`test_meta_rejects_productivity_and_ambiguous_adjacent_queries` it made 8 of 11 **answer with the wrong kind** rather than
refuse (`"Why did Alice make fewer commits?"` → `WHY_DECISION`, `"Show source code productivity rankings"` →
`DECISION_EVIDENCE`), and answering confidently about the wrong thing is worse than refusing — those two are the
productivity scores AGENTS.md forbids outright.

| Marker family | Refuses on |
|---|---|
| person-scoped comparative | a person reference (`who`, `whom`, `employees`, `individual`, a member handle) inside a comparative or superlative (`most`, `least`, `more`, `fewer`, `hardest`, `best`, `worst`, `top`, `compared to`) |
| ranking | `rank`, `ranking(s)`, `leaderboard`, `scoreboard`, `standings`, `top N` |
| output volume | `commit(s)`, `lines of code`, `how many messages/outputs/runs`, `volume`, `throughput`, `velocity` |
| productivity framing | `productivity`, `productive`, `performance`, `activity`, `engagement`, `contribution`, `effort`, `worked hardest` |

Markers match whole words, and both refusing passes raise `DomainError` under the `"unsupported Meta question"` prefix the
existing test matches, pass 1 appending its own clause. AGENTS.md states authorization is never enforced by prompt text; the
same discipline applies to answering, so classifier, claim set and refusal are all code. A model may render prose *over* an
already-authorized claim set, each sentence carrying its assertion IDs; if the provider is unavailable the answer degrades
to the claim set, never to a guess. Each kind is one named, room-scoped query — forms illustrative, corpus in code:

| Kind | Accepted forms (examples) | Query over durable assertions |
|---|---|---|
| `STATUS` | "what is the status", "where do things stand" | `Task`/`Decision` entities grouped by `properties->>'$.status'`, with their `OWNS` edges |
| `BLOCKERS` | "what is blocking", "what are the blockers" | `BLOCKS` relationships whose `to_entity` is in scope, with both endpoints |
| `CHANGES` | "what changed", "what changed this week" | assertions with `asserted_at_sequence > :since_sequence`, joined to their `room_events` rows |
| `DECISIONS` | "what decisions require attention" | `Decision` entities with `SUPPORTS` edges and `review_status` |
| `DISAGREEMENT` | "where is the disagreement", "what is contested" | `CONTRADICTS` relationships, both endpoint `Claim`/`AgentOutput` entities, distinct source agents |
| `WHY_DECISION`, `DECISION_EVIDENCE` | the eight strings accepted today | the existing frozen-provenance chain, unchanged |

**Refuse, never guess.** The envelope carries `status ∈ {ANSWERED, ANSWERED_UNCONFIRMED_ONLY, REFUSED}` and, when refused,
`refusal_reason ∈ {NO_ASSERTIONS_IN_SCOPE, NO_AUTHORIZED_EVIDENCE}`. An empty authorized result set is a `REFUSED` answer at
HTTP 200 — "we do not know" is a real answer and must be inspectable — whereas an unrecognized question stays a
`DomainError` at 400. `STALE_BEYOND_BOUND` goes with query-time extraction: staleness is labelled per claim, not refused.

**Test** (`tests/e2e/test_meta_decision_intelligence.py`): the eleven strings parametrized in
`test_meta_rejects_productivity_and_ambiguous_adjacent_queries` are a floor — each still raises `DomainError` matching
`"unsupported Meta question"`, and the list may only grow; the eight in
`test_meta_accepts_only_explicit_decision_query_grammar` still resolve unchanged. Added: for each of the five new kinds,
every corpus form resolves to that kind and no other kind's form resolves to it (a cross-product, so a new form cannot
silently widen a neighbour); every corpus entry survives pass 1, proving the layers agree and that markers match whole
words; a question both marker-bearing and an exact corpus form refuses, proving pass 1 is not shadowed by pass 2; one
answered case per kind; a seeded-empty room returns `REFUSED` with a reason; nonsense returns 400; and normalization is
stable across whitespace, case and punctuation.

## 4. Permission filtering

**Decision.** Filtering happens inside the SQL, never after it. Every Meta query takes `(:room_id, :user_id)` and carries
the authorization in its `FROM`:

```sql
JOIN room_members m ON m.room_id = e.room_id AND m.user_id = :user_id
                   AND m.role IN (:reading_roles)
WHERE e.room_id = :room_id
```

**The join carries the role predicate.** Existence-only membership is not authorization: `room_members.role` has no CHECK
(migration 001), so a row bearing any role string — including one `capabilities_for_role` grants nothing for — satisfies
`m.user_id = :user_id` and reads the room. `:reading_roles` expands `roles_with_capability(RoomCapability.READ)`, derived
from the same `_ROLE_CAPABILITIES` table `capabilities_for_role` reads, exactly as `SearchRepo._READING_ROLES` does in
`db/repositories.py` (~line 2114), because a role list copied beside the policy silently outlives a change to it. An
unrecognized role now yields zero rows, so deny-by-default needs no CHECK that SQLite could not add without a table rebuild
(012's precedent), and a missing membership row yields zero rows rather than a forgettable Python branch. The route also
requires `RoomCapability.READ`; the join is the backstop, not the only check.

**Every aggregate is computed inside the authorized scope**, the freshness block included, because a volume count of content
the asker may not read leaks that content's existence and rate. Today `answer_decision_meta` fills
`freshness.room_event_cursor` from `EventRepo.get_latest_sequence`, an unjoined `MAX(sequence)` over `room_events`, and
attaches it to a `NO_AUTHORIZED_EVIDENCE` refusal; inside the authorized scope that aggregate is simply empty, so the
refusal carries no head and no counts as a consequence of the query, not a special case someone must remember.

The two capability vocabularies stay separate: `RoomCapability` in `security/authorization.py` gates humans, while the
`retrieval` string in `security/capabilities.py` belongs to the agent/skill/channel/workspace intersection whose docstring
calls `user_capabilities` "capabilities a human may lend to an agent". A human asking a Meta question is gated by membership
and `RoomCapability.READ` only; `retrieval` is checked when an **agent** retrieves on that human's behalf.
Permission-filtered emptiness reports `NO_AUTHORIZED_EVIDENCE`, distinct from `NO_ASSERTIONS_IN_SCOPE`, so a user learns
whether the room has nothing or they may not see it, neither reason disclosing content.

**Test** (`tests/security/test_meta_authorization.py`): on a room whose Meta answer is non-empty, a non-member, a member of
a sibling room, and a member holding a role outside `roles_with_capability(READ)` each receive zero claims; an agent whose
effective intersection omits `retrieval` is refused when it retrieves on a member's behalf; a non-member's refusal body
carries no room head, unread or drain-lag count or other aggregate; and by query-string inspection **every** Meta query —
freshness and aggregate queries included, with no exemption list — carries the membership join and the role predicate.

## 5. Drill-down

**Decision.** Every claim resolves through one chain, and no answer may contain a claim whose chain terminates early:

```
answer.claims[i].assertion_id            -- entity_id | relationship_id
  -> entity:       ontology_entities.source_object_id, typed by its kind
     relationship: ontology_relationships.source_object_id, typed by source_object_kind
  -> typed source row, by kind:
       Claim       -> artifact_claims.claim_id -> artifact_claim_sources(claim_id, output_id)
       AgentOutput -> agent_outputs.output_id -> execution_id, source_prompt, provider_input, provider_model,
                      provider_response_id
       Artifact, Decision -> artifact_versions.version_id -> content_hash, provenance_hash
       Task -> tasks.task_id     Person -> users.user_id     message-derived -> messages.message_id
  -> room_events.sequence                -- from evidence_event_sequences
```

**Relationships carry their own evidence.** `ontology_relationships` has no `source_object_id` (migration 006), so every
relationship-centric answer — `BLOCKERS`, `DISAGREEMENT`, `DECISIONS` — lacks the hop from edge to durable message, output
or artifact version, and the rule above would forbid returning any of them; migration 015 adds `source_object_kind` and
`source_object_id` there so the chain terminates properly. The writer records the durable row whose content *states* the
relation, not automatically an endpoint: a `BLOCKS` edge is evidenced by the message or output reporting the blockage. Its
non-emptiness is enforced in the write path and the test below, since SQLite cannot add a CHECK to a backfilled column
without a table rebuild. `agent_outputs`, `artifact_versions`, `artifact_claims` and `artifact_claim_sources` are immutable
by trigger (migration 005), so every chain ends in a row that cannot be rewritten under the answer — which is what makes it
evidence rather than a pointer.

**Test** (`tests/regression/test_meta_drilldown.py`): for every claim in every answer of the seeded run, walk the chain and
assert each hop returns exactly one row and the final ID equals the cited one; assert 100% coverage and that an empty chain
cannot be constructed through the service API. A `BLOCKERS` and a `DISAGREEMENT` answer each walk from `relationship_id`
through `source_object_kind`/`source_object_id` to an immutable row and a sequence; an empty `source_object_id` is rejected.

## 6. AI-derived versus confirmed

**Decision.** The presentation contract is structural, not stylistic. Each claim carries `derivation_kind`, `confidence`,
`review_status`, `evidence_ids` and a derived `assurance ∈ {CONFIRMED, SYSTEM_MATERIALIZED, UNCONFIRMED_AI}`; `CONFIRMED`
also carries `review_id` and `reviewed_by` from `ontology_reviews`. `UNCONFIRMED_AI` claims are returned in a separate
`unconfirmed[]` array, never merged into `claims[]`, as two result sets — merging them would require code that does not
exist, a stronger guarantee than a naming convention. They are excluded from every count the summary presents as fact, and
their template is fixed to hedged form ("an unreviewed extraction suggests …"). An answer supported only by them is
`ANSWERED_UNCONFIRMED_ONLY` with `claims[]` empty. Only human review promotes `UNCONFIRMED` to `CONFIRMED`; no confidence
threshold auto-confirms, because that is how PRD §22's ontology noise institutionalizes itself.

**Inherit the weakest.** Any assertion derived from other assertions takes the weakest `derivation_kind` and `review_status`
of its inputs plus `confidence = min(inputs)`, over the orders `AI_DERIVED < SYSTEM_MATERIALIZED` and `UNCONFIRMED <
CORRECTED < CONFIRMED`. Today `derivation_kind` is chosen per writer — IMMEDIATE writes `SYSTEM_MATERIALIZED`, ASYNC writes
`AI_DERIVED` — and SCHEDULED has no rule at all, so a consolidation edge over two `UNCONFIRMED_AI` entities can be written
`SYSTEM_MATERIALIZED` and reach a reader inside `claims[]` as confirmed truth; the rule closes that laundering path because
a derived claim is only as good as its weakest input, and it lives in the one repository method all three timings share.

**Test** (`tests/regression/test_ontology_derivation_inheritance.py`): construct exactly the laundering path — two
`AI_DERIVED`/`UNCONFIRMED` entities, then the SCHEDULED pass that relates them — and assert the edge is `AI_DERIVED` and
`UNCONFIRMED` with `confidence` no greater than either input, appears only in `unconfirmed[]`, and yields
`ANSWERED_UNCONFIRMED_ONLY`; then confirm one input by review and assert the edge does not promote.

**Test** (`tests/e2e/test_minimal_ontology.py`): no element of `claims[]` has `derivation_kind = AI_DERIVED and
review_status = UNCONFIRMED`; an answer whose sole support is unconfirmed returns `ANSWERED_UNCONFIRMED_ONLY` with empty
`claims[]`; a summary never names an `unconfirmed[]` claim outside the hedged template.

## Migrations

`017_ontology_freshness_and_meta.sql`. 013 is the conversation layer, 014 the execution
authorization fix, 015 the mention-handle work and 016 agent identity, so this piece is 017:

1. `CREATE TABLE ontology_extraction_cursors` as above — three extractors, no `QUERY_TIME`.
2. The four `ALTER TABLE ... ADD COLUMN` on `ontology_entities` and on `ontology_relationships`.
3. On `ontology_relationships` only, `source_object_kind` and `source_object_id` (`TEXT NOT NULL DEFAULT ''`), backfilled
   from each edge's `from_entity_id`, because leaving `''` would assert a chain that does not exist.
4. `(room_id, asserted_at_sequence)` indexes on both assertion tables, plus `room_events(room_id, event_type, sequence)` —
   without the last, section 1's currency query scans the log once per answer.
5. A `BEFORE UPDATE ... WHEN NEW.last_sequence < OLD.last_sequence` trigger on the cursors — SQLite cannot add a `CHECK` by
   `ALTER`, and 012 shows the alternative is a table rebuild.
6. Backfill per 013's precedent for `messages.event_sequence`: assertions take `extractor = 'IMMEDIATE'` and
   `asserted_at_sequence` from the `ontology.materialized` event naming them, else `0`; seed the IMMEDIATE cursor per room
   from `MAX(asserted_at_sequence)` as a resume hint — no backfilled row is claimed current, currency being derived.
7. New `EventType` members `ONTOLOGY_EXTRACTION_ADVANCED` and `ONTOLOGY_ASSERTION_SUPERSEDED` (code, not SQL).

Applied in order on top of 001–014 in a scratch database, every statement succeeds, `PRAGMA integrity_check` returns `ok`,
the rewind trigger aborts a decrease and permits an increase, and `extractor = 'QUERY_TIME'` is rejected by the CHECK.

## Not building

- **No graph database or graph engine.** AGENTS.md forbids it before demonstrated need; traversal stays bounded SQL joins at
  depth 2, and a question needing depth 3 argues for a new named query.
- **No employee activity or productivity score, in any disguise.** No per-person counters of messages, outputs, commits or
  runs; no leaderboard, engagement metric, response-time metric or "contribution" figure; no `person_activity` table.
  `CHANGES` is scoped to work objects — Task, Decision, Artifact, Claim — never actors, so the query shape cannot become a
  monitoring feed (PRD §17, §22 surveillance drift).
- **No query-time extraction.** Deferred on the 1016 ms measurement in section 2, not on taste; it returns only if the
  single-connection write path is replaced and the figure is re-measured.
- **No audit event for a pure Meta read.** One per question would build the per-person question trail the previous point
  rules out — and with reads no longer writing, a Meta read has nothing to emit at all.
- **No open-ended extraction.** The async extractor reads a fixed allowlist of event types under a hard per-pass bound;
  there is no "index the workspace" path (PRD §10, §15).
- **No natural-language-to-SQL, no model-authored queries.** `MetaQuestionKind` is a closed enum; an unrecognized question
  is refused, not improvised.
- **No auto-confirmation, auto-deletion or auto-merge of assertions.** Consolidation may mark stale and record
  `CONTRADICTS`; only human review changes `review_status` (PRD §22).
- **No cross-room or workspace-wide Meta here.** The queries take a room-id parameter a membership subquery could widen
  later; widening now multiplies the isolation surface before one room's answers are proven.
- **No external-system extraction.** PRD §16 integrations would change the evidence chain's terminus, which section 5
  depends on being immutable.
