# P8: A channel posture, and a reviewer's bound

Two things, on top of the five-way intersection in `security/capabilities.py` and neither of them
widening it.

The first is the posture gap in `docs/benchmarks/buzz-agent-workspace-baseline.md`. XYZZY has
per-tool `requires_approval` booleans decided at write time by whoever registered the tool and
nothing above them: no way to say "in this room, everything pauses", and no way afterwards to show
which rule was in force. `yc-software/qm` declares three postures: Strict pauses every tool call,
Auto screens external content, Dangerous does neither. We take one of them.

The second is an over-reach found in shipped code while building the first, and fixed here because
it is the same machinery: a reviewer who releases one parked tool call was bound into the whole
remaining run.

An earlier draft of this document specified three postures, two scopes, a rank table, a
tighten-only trigger, and a set of governing source rooms. An adversarial review reproduced six
defects in it, five of which lived in machinery that deleting removes entirely. What is built is
its own recommendation: two postures, on the room, admin-gated.

## 1. Two postures, on the channel

**Decision.** `Posture` is `GUARDED` and `STRICT`, declared on a room and nowhere else.

- `GUARDED` is today's behaviour, byte for byte: the five-way intersection decides what an agent
  may do, and the per-tool floor decides what pauses.
- `STRICT` adds one rule (every call pauses for a human) and takes nothing away.

There is no scope but the room, no rank table, no middle tier, and no third posture. A workspace
tier would be a second scope to compose, and composition is where four of the reviewed defects
lived; a `SUPERVISED` tier over `MUTATING_CAPABILITIES` is definable but nothing asked for it; a
`Dangerous` tier is refused outright, because a floor a setting can remove is not a floor and
postures are per room, so the weakest room would define the product. qm's `Auto` screens external
content and XYZZY has no external ingress for it to screen.

Two named values need no ordering to compare, so there is no rank table and nothing that reads one.

```python
class Posture(StrEnum):
    GUARDED = "GUARDED"
    STRICT = "STRICT"
```

## 2. What a posture may do, and what it structurally cannot

**Decision.** A posture is one function on one field, applied *after* the capability check:

```python
def under_posture(decision: GatewayDecision, posture: Posture) -> GatewayDecision:
    if posture is not Posture.STRICT or not decision.allowed or decision.requires_approval:
        return decision
    return replace(decision, requires_approval=True, reason=STRICT_PAUSE_REASON)
```

The only value it returns other than the decision it was handed is a copy naming
`requires_approval` and `reason`. So "may a posture change what a channel permits" is not a
question about anybody's discipline: `allowed` is not an expression this function contains. A
refused decision is returned untouched rather than offered to a reviewer: "ask a human" applied to
a denial would permit the call by whoever answered instead of by the records.

**Raised, never set.** `STRICT` cannot lower a floor tool to unpaused, because the branch returns
early when `requires_approval` is already true. That is the difference between a posture and a
policy, and it is one word wide.

**Derived, never stamped.** The declaration rows are read inside `_handle_tool_request`, beside the
terms, at the moment a call becomes a pause or an execution, the one moment that decision is made.
Nothing resolved is stored: no column on `rooms`, no field on `tool_requests`. `tool_requests.reason`
names the strict posture when the posture is why a call paused, which is a record of a cause, not an
input to a later decision. This repository has lost fourteen rounds to a decision captured at one
moment and spent at another, and a stamped posture would be that defect wearing a new hat.

**Consequence, stated rather than discovered.** A posture governs calls decided after it is
declared. A call already parked at a reviewer is released by that reviewer or by nobody; declaring
`GUARDED` underneath it does not release it, and declaring `STRICT` does not re-park a call that
already ran.

## 3. The floor

Unchanged. `ToolSpec.requires_approval` still marks the tools that pause under every posture,
`GUARDED` included: `task.create` creates work a human must dispose of and `artifact.write`
publishes a version with provenance attached, while `message.react` is durable and retractable and
`channel.read_context` writes nothing. There is no tier that guarantees nothing, which is the whole
of why qm's third posture is not copied.

## 4. Changing it

**Decision.** `declare_room_posture(room_id, posture, declared_by)` requires `RoomCapability.ADMINISTER`,
checked inside the transaction that writes, and emits `room.posture_declared` carrying the
declaration id, the posture and the declarer. There is no other door to a posture: it is not a field
on `set_room_policy`, and there is no unguarded insert path.

**Loosening is permitted.** In one sentence: a posture that could only rise would make one mistaken
`STRICT` permanent and the channel disposable, and the harm that would buy is not available to be
bought, because §2 reads the posture once, at the moment a call is decided, so loosening cannot
reach a decision already made.

**A declaration is an audit record.** Rows in `room_postures`, append-only, one per declaration with
a surrogate `declaration_id`. `UPDATE` and `DELETE` abort. `INSERT OR REPLACE` against an existing
`declaration_id` aborts too, on a `BEFORE INSERT` trigger rather than the delete one, because SQLite
does not fire delete triggers for `REPLACE` unless `recursive_triggers` happens to be on: that is
the exact defeat the review used against the previous draft. So "which rule governed this action"
is a query over rows that cannot have changed since.

## 5. A reviewer bounds the call they approved, not the run

**The defect.** `approve_action` called `record_caller(pending.execution_id, reviewer_id)`. Callers
are bounding principals of the run, `_lendable_terms` intersects `_user_term` for every principal,
and every later call of that run re-derives from the same set: an administrator scoped to
`["retrieval"]` who approved a single read permanently stripped `writing` from the rest of the run,
turning calls that would have paused into calls that were refused. And `agent_runs.advance` writes
its acting human into the same table by trigger, so releasing the run under `reviewer_id` put it
back through the database even where the service line was removed.

It fails closed, so it is not an escalation. It is over-reach, and the kind that teaches people not
to answer approvals.

**Decision.** The reviewer is recorded against the call she released (`tool_request_reviewers`,
keyed `(request_id, reviewer_id)`, append-only) and the run is advanced under the principal the
turn was parked on, which is the same one `_resume_suspended_turn` already carried the rest of the
turn under.

**The bound is narrowed in scope, never removed.** Both doors that decide a stored call, the
reviewer's own (`_current_tool_decision`) and the writer's, inside the transaction that writes
(`_run_authorization`), reach the reviewers through one helper, so neither can be the one that
forgot them. A reviewer still cannot approve herself past her own grant on the call she is
answering for.

**Why this is not a fourteenth relocation.** `_authorization_for` remains the single filler of a
run's bounding set, takes no principal from its caller, and is still the only function that reads
`bounding_principals`. What the reviewer enters through is `BoundingPrincipals.also_bounded_by`,
whose only operation is a union: there is no expression in it that drops a principal the durable
rows named. Since the terms are an intersection over the set, a wider set is always a narrower
grant, so nothing built from these two can yield more authority than `_authorization_for` alone.
The escalation class is *adding* authority, and this can only subtract.

## Migration

`031_a_posture_and_a_reviewers_bound.sql`.

```sql
CREATE TABLE room_postures (declaration_id TEXT PRIMARY KEY, room_id TEXT NOT NULL
    REFERENCES rooms(room_id) ON DELETE RESTRICT,
    posture TEXT NOT NULL CHECK (posture IN ('GUARDED','STRICT')),
    declared_by TEXT NOT NULL, declared_at TEXT NOT NULL);
-- written once, never deleted, never replaced  (§4)
CREATE TABLE tool_request_reviewers (request_id TEXT NOT NULL
    REFERENCES tool_requests(request_id) ON DELETE RESTRICT,
    reviewer_id TEXT NOT NULL, reviewed_at TEXT NOT NULL,
    PRIMARY KEY (request_id, reviewer_id));
-- written once, never deleted
```

No backfill in either direction. No declaration rows means every existing channel is `GUARDED`,
which is what it already was; a reviewer already written into `execution_callers` by the old path
stays there, because narrowing a historical run's bound after the fact would be rewriting what
governed it.

## Tests

`tests/security/test_postures_and_the_per_call_bound.py`.

- `allowed` is unchanged and `requires_approval` only rises, over every tool × every posture ×
  every subset of the vocabulary; and adding principals to a bound only grows the set.
- A `STRICT` room pauses a `channel.read_context` a `GUARDED` room executes, with the two rows'
  `effective_json` equal byte for byte.
- The floor pauses `task.create` under both postures, and under `GUARDED` the cause is the floor's.
- A posture change is refused without `ADMINISTER`, leaving no row and no event; with it, the event
  carries the declaration id.
- A declaration cannot be updated, deleted or `INSERT OR REPLACE`d.
- Loosening leaves an already-parked call parked; only the reviewer releases it.
- A reviewer holding `["retrieval"]` who approves a `task.create` gets a refusal, not a task.
- A reviewer holding `["writing"]` who approves a `task.create` does not bound the
  `channel.read_context` the same run asks for next.

## Not building

- **No `Dangerous` tier and no `Auto` screening tier.** §1.
- **No workspace or org scope, and no composition.** One scope declares; there is nothing to compose.
- **No rank table and no middle tier.** Two values compare without an ordering.
- **No cross-room source set.** Nothing crosses today; a rule for a column that does not exist is a
  rule nobody can test.
- **No command-string policy.** qm calls its own a speed bump, and we have no free-text tool.
- **No per-agent or per-user posture.** A posture is a property of a place; a principal's reach is
  the intersection.
- **No reviewer override.** Granting an approval decides one action. It never lowers the rule for
  the next one, and after §5 it does not raise it either.
- **No sixth capability term and no posture input to the intersection.** §2: one field, raised only.
