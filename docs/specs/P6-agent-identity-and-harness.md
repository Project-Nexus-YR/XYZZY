# P6 — Agent identity and harness

Design for the five P6 conditions in `docs/benchmarks/buzz-agent-workspace-baseline.md`, all of it **on top
of** the five-way intersection in `security/capabilities.py` and none of it widening that. block/buzz has
identity, delegation and a harness contract but no tool scoping, no agent-removal and no crash semantics.

## 1. Agent identity

**Decision.** Each agent instance gets one immutable `agent_identities` row; in `SIGNED_CHALLENGE` mode it
carries an Ed25519 public key whose private half lives in the harness process, never in the workspace, so
identity survives a harness restart on another host and is revoked once rather than per run. Every run proves
membership through a per-run bearer credential stored only as a SHA-256 hash and compared with
`hmac.compare_digest` — the opaque-token discipline already in `security/auth.py`; a signed-challenge harness
proves it a second time, answering a random launch challenge at `initialize` against the stored key.

**Fail-closed.** No run may be created unless a matching identity row exists, `revoked_at IS NULL`, and — in
`SIGNED_CHALLENGE` mode — the challenge was answered: a `BEFORE INSERT` trigger on `agent_runs`, not only a
service check, so a future code path cannot launch an anonymous agent by forgetting the checker. Buzz's
invariant is that a keyless agent refuses to start; we make that refusal a database constraint.

**Two proof modes, and no server keyring.** A signature proves authorship across a transport nobody controls.
Buzz needs one because its agents reach a public relay from anywhere; the NEXUS bridge and the OpenAI provider
run inside this process, where the service constructs the run in the same transaction that writes it — a
keypair for those would invent a secret to guard a boundary that does not exist. So
`agent_identities.proof_mode` is `IN_PROCESS` or `SIGNED_CHALLENGE`, with a CHECK requiring `public_key IS NOT
NULL` exactly when the mode is `SIGNED_CHALLENGE`, and the launch trigger demands a challenge answer only in
that mode. Everything else is identical across both, so the first out-of-process harness adds a row mode and a
verifier, not a redesign. Public keys only: AGENTS.md's rule against building before demonstrated need.
**Events.** `agent.identity.registered`, `agent.identity.revoked`, `agent.launch.refused` (`agent_id`,
`reason` ∈ `no_identity` / `revoked` / `challenge_failed` / `unknown_harness`).
**Test** — `tests/security/test_agent_identity.py`: delete the identity row, then launch through the service
and again through a direct repository insert; both raise, no `agent_runs` row exists, one refusal event is
appended. The wrong-key case runs against a `SIGNED_CHALLENGE` fixture; no production path builds one yet.

## 2. Delegated authority

**Decision.** `agent_runs.authorized_by` is a NOT NULL user id naming the human under whose authority the run
acts, from the authenticated principal, never a request body. It propagates to `tool_requests.authorized_by`
and `approvals.authorized_by`: `requested_by` holds the agent id — the actor, not the authority.

**Two humans, and the initiator is a ceiling.** `POST /executions/{id}/step` authorizes its caller at room
`MUTATE`, then `execute_agent_step` derives `CapabilityTerms` from `branch.initiated_by` alone — so a member
an admin narrowed to `["research"]` may step a run another member initiated and be offered, and executed,
`artifact.write` under that member's terms. `redirect_agent`, which injects prompt text, has the same shape.
**This is a live privilege escalation in the committed code, not one this design merely declines to
introduce.** When the caller is not the initiator, the `user` term becomes the intersection of the initiator's
capabilities and the caller's: the initiator's grant bounds the run from above and never substitutes for it,
so nobody obtains through a run more than they hold. It narrows one term rather than adding a sixth.
`agent_runs.acting_user_id` records who last moved the run, so audit sees both; every verb that advances or
steers a run passes its caller — `step`, `redirect`, `intervene`, `pause`, `resume`, `cancel`, and approval.

**Re-checked at execution time.** This repository's recurring defect class is check-then-use: a decision taken
when the run was requested and never revisited at the write. The rule is that **no capability set is ever an
input to a later decision**. `CapabilityTerms` is re-derived from durable records, inside the writing
transaction, at every point: **run creation** — identity, addressing, authority, terms · **before each
`session/prompt`** — authority, terms, which shape the offered tool list · **tool gateway decision** —
authority, terms · **inside each tool writer's own transaction** — authority, terms, the missing leg today ·
**interrupt, cancel, resume** — authority, addressing · **before an `AgentOutput`** — authority.

**Pushed down, not wrapped around.** The obvious home for the last leg is `_run_tool`, which dispatches the
approved tool — but `_run_tool` calls `create_task` and `create_artifact`, each of which opens `async with
self.db.transaction()`, and `Database.transaction()` raises `RuntimeError: nested database transactions are
not supported` on re-entry, so a re-check there would sit *outside* the write and relocate check-then-use an
eighth time rather than end it. The check goes down into the writers instead: each takes an authorization
context and re-derives the effective terms inside the transaction it already opens, making check and write one
transaction by construction, with `_run_tool` unrestructured. `authorization=None` is the human-caller path,
unchanged and already guarded by `_require_mutate_in_transaction`; a value adds the re-derivation beside that
guard. `approve_action` re-derives today through `_current_tool_decision` *after* its own transaction has
closed, and moves inside for the same reason; its re-stamped `effective_json` stays an audit record, never an
input. A re-check finding the authorizing human gone, the caller narrowed, or either holding a role that no
longer yields the capability, settles the run `AUTHORITY_REVOKED` and writes nothing.

```python
async def _capability_terms(self, agent: AgentInstance, room_id: str,   # §2 caller intersection
                            initiated_by: str, acting_user_id: str) -> CapabilityTerms: ...
class RunAuthorization:   # frozen, slots, as in §4
    run_id: str; agent_id: str; room_id: str
    authorized_by: str; acting_user_id: str; required_capability: str
async def create_task(self, ..., *, authorization: RunAuthorization | None = None) -> Task: ...
async def create_artifact(self, ..., *, authorization: RunAuthorization | None = None) -> Artifact: ...
```

**Events.** `agent.run.authority_revoked` (`run_id`, `authorized_by`, `acting_user_id`, `stage`,
`missing_capability`); plus the existing `tool.call_rejected`.
**Test** — `tests/security/test_delegated_authority.py`: approve `artifact.write`, remove the authorizing
member, release the approval — no artifact, request `REJECTED`, event names the stage; a second case demotes
admin to viewer between prompt and tool execution; a third demotes after `_run_tool` is entered, where only
the writer's own transaction can still catch it. `tests/security/test_acting_user_intersection.py` runs a case
per verb — `step`, `redirect`, `intervene`, `pause`, `resume`, `cancel`, approval — with a caller narrowed to
`["research"]` acting on an unrestricted member's run, asserting the effective set never exceeds the caller's.

## 3. Addressing modes

**Decision.** Addressing is a durable per-agent record, not harness configuration:
`agent_addressing(agent_id, room_id, mode, owner_user_id)` with `mode` ∈ `OWNER_ONLY`, `ALLOWLIST`, `ANYONE`,
`NOBODY`, plus `agent_address_allowlist`. Buzz configures addressing in the harness, so its relay trusts each
harness to police its own audience; storing it here means a compromised harness cannot widen its own.
`may_address(mode, owner_user_id, allowlist, user_id)` is a pure function in `security/capabilities.py`,
evaluated before a run is created, before a mention invokes an agent, and on interrupt, cancel and resume.
`NOBODY` parks the agent: history readable, no run starts. Addressing gates who may point it, not what it does.
**Events.** `agent.addressing.updated` (requires room `ADMINISTER`), `agent.addressing.refused`.
**Test** — `tests/security/test_addressing_modes.py`: for each mode, an owner, an allowlisted member, a plain
member and a non-member each attempt a direct run and a mention; the matrix is asserted, and a harness
advertising a wider audience changes nothing.

## 4. The harness contract

**Decision.** One `Protocol` modelled on ACP over stdio, transport-agnostic so an in-process and a subprocess
harness satisfy the same type. Streaming is a callback, not an async generator, because a generator's return
value is untyped under mypy and the terminal `StopReason` must be one checked value. Every dataclass below is
`@dataclass(frozen=True, slots=True)`.

```python
class StopReason(StrEnum):  END_TURN = "end_turn"; CANCELLED = "cancelled"; MAX_TOKENS = "max_tokens"
class UpdateKind(StrEnum):  MESSAGE_DELTA = "message_delta"; THOUGHT = "thought"; TOOL_CALL = "tool_call"
class HarnessInfo:     # advertised_capabilities is display only; never a capability term
    harness_id: str; protocol_version: int; advertised_capabilities: frozenset[str]
class RunContext:      # what the server hands a harness; authority travels with the run
    run_id: str; agent_id: str; identity_id: str; room_id: str; run_credential: str
    authorized_by: str; acting_user_id: str       # initiator, then whoever moved it last
class SessionHandle:   run_id: str; harness_session_id: str
class PromptRequest:   handle: SessionHandle; prompt: str
                       response_schema: dict[str, Any]; offered_tools: tuple[str, ...]
class SessionUpdate:   run_id: str; kind: UpdateKind; payload: dict[str, Any]
class TurnResult:      stop_reason: StopReason; output: dict[str, Any]; provenance: dict[str, Any]

class AgentHarness(Protocol):
    async def initialize(self, challenge: bytes | None) -> tuple[HarnessInfo, bytes | None]: ...
    async def session_new(self, run: RunContext) -> SessionHandle: ...
    async def session_prompt(self, request: PromptRequest,
                             on_update: Callable[[SessionUpdate], Awaitable[None]]) -> TurnResult: ...
    async def session_cancel(self, handle: SessionHandle, reason: str) -> None: ...
```

**The challenge is optional because the boundary is.** `initialize` takes and returns `bytes | None`, present
exactly in `SIGNED_CHALLENGE` mode. An in-process harness is handed `None` and returns `None` rather than
signing against a key the server holds — ceremony dressing a boundary §1 argues does not exist.

**Two implementations, no behaviour change.** `NexusHarness` wraps `NexusAgentBridge`: `session_new` calls
`create_execution`; `session_prompt` calls `execute_step`, emits one `SessionUpdate` per step, maps `action ==
"finish"` to `END_TURN` and a set cancel flag to `CANCELLED`; `session_cancel` calls `request_cancellation`.
Prompts, step schema and provenance are untouched. `ModelProviderHarness` wraps anything with
`acomplete(prompt, schema)`, so `OpenAIResponsesProvider` and `WorkflowOnlyModelProvider` satisfy it: one
prompt is one `acomplete`, one `MESSAGE_DELTA`, then `END_TURN`. `agent_instances.harness_id` selects from a
registry; an unknown id refuses to launch.

**`MAX_TOKENS` waits for a provider that reports truncation.** `OpenAIResponsesProvider._decode_response`
hardcodes `"action": "finish"` and reads no truncation field, so nothing here can reach that state today. The
value stays — a truncated turn is a real terminal outcome, and adding it later reopens a closed state machine
— but the conformance suite cannot exercise it, and this spec says so rather than specify an unrunnable test.
**Test** — `tests/unit/test_harness_contract.py` runs one conformance suite against both (initialize answers a
challenge when given one and returns `None` when not, every prompt terminates in exactly one `StopReason`,
cancel is idempotent), plus a golden test: the seeded branch run keeps identical `AgentOutput` provenance.

## 5. Lifecycle

**Decision.** `agent_runs` is the identity-and-authority envelope around the existing `executions` row, not a
second state machine over the same fact: `executions.status` stays the domain state and
`agent_runs.harness_state` the transport, each mapping to one domain status, so a fact keeps one owner.

| `harness_state` | Meaning | Recovery |
|---|---|---|
| `STARTING` | row written, `session/new` unacknowledged | lease sweep settles `ORPHANED` |
| `STREAMING` | prompt in flight | lease sweep settles `ORPHANED` |
| `AWAITING_APPROVAL` | tool gated, no harness work in flight | long lease; sweep settles `ORPHANED` |
| `CANCEL_REQUESTED` | settlement decided, harness not yet told | sweep settles `CANCELLED` regardless of the harness |
| `SETTLED` | terminal, `settlement` set | none |

`settlement` ∈ `END_TURN`, `CANCELLED`, `MAX_TOKENS`, `FAILED`, `ORPHANED`, `AUTHORITY_REVOKED`,
`AGENT_REMOVED`, `APPROVAL_REFUSED`. Every non-settled run holds a heartbeat lease — a long one while
`AWAITING_APPROVAL`, since a reviewer may take hours — and a sweep at startup and on an interval settles each
expired lease `ORPHANED`. **No state is exempt**: an exemption is not a longer deadline but no deadline, and
manufactures the fourth case the guarantee denies — a run is settled, holds a live lease, or is swept.

**A refused approval settles the run.** `reject_action` today resolves the tool request `REJECTED` and stops,
leaving the run `AWAITING_APPROVAL`: non-settled, unleased, and under the old exemption unsweepable forever.
Rejection now ends in one of two named places, inside the transaction that writes it — settling the run
`APPROVAL_REFUSED`, or returning it to `STREAMING` on a fresh lease when the reviewer refuses the tool but
wants the turn continued. No third path leaves the run where it found it.

**Interrupt, cancel, resume.** Interrupt injects an intervention into the next step and the run continues;
cancel is terminal. Both re-run the addressing and authority checks, so a second admin may stop a run the
owner started — buzz validates interrupt verbs against the owner's signature alone, which cannot express that.
An `ORPHANED` run is never resumed in place, since that re-adopts a state nobody observed; resume opens a new
run with `resumed_from_run_id`, same identity, fresh authority. Each verb passes its caller, per §2.

**Removal settles deterministically — and this piece builds it.** `remove_agent_from_room` does not exist,
`AGENT_LEFT_ROOM` is declared in `domain/events.py` and never emitted, and `_running_executions` is declared
on the service and never read; none of that is a hook to extend. The new verb requires room `ADMINISTER` and,
in one transaction, sets `agent_room_memberships.removed_at`, moves every non-settled run for that agent in
that room through `CANCEL_REQUESTED` to `SETTLED`/`AGENT_REMOVED`, and appends `agent.left_room` plus one
`agent.run.settled` per run. Settlement is decided by the database and telling the harness is best-effort, so
an in-flight turn can still land — and the credential does not stop it, because the in-flight write path is
`complete_execution`, which consults neither `agent_runs` nor any credential. So `complete_execution` re-reads
its run inside the transaction it already opens and refuses when `harness_state = 'SETTLED'`: no output, no
terminal status, settlement intact. **That refusal, not the credential, stops a settled run writing.**
**Events.** `agent.run.settled` (`run_id`, `settlement`, `decided_by`), `agent.run.orphaned`, plus the
existing `agent.left_room`, `execution.cancelled`, `execution.failed`.
**Test** — `tests/failure/test_harness_crash.py` kills a harness mid-stream, runs the sweep, and asserts every
run is `SETTLED` with a named settlement and a matching event, and that a run parked `AWAITING_APPROVAL` past
its long lease is swept, not exempted; `tests/e2e/test_agent_removal.py` removes an agent with two runs in
flight and one awaiting approval: all three settle `AGENT_REMOVED`, a late `session/update` is rejected, a
late `complete_execution` raises and writes no `agent_outputs` row. A rejected approval settles or resumes.

## 6. Tool permissions stay the five-way intersection

**Decision.** Identity is a **gate**, never a term. Order: identity valid → addressing allows → authority
re-derived → `CapabilityTerms.effective` → `decide(tool, effective)` → approval → execute → audit.
`agent_identities` has no capability column by design, and `HarnessInfo.advertised_capabilities` is display
metadata never unioned into the intersection — a harness claiming `coding` for an agent whose template lacks
it changes nothing. A sixth term would break the AGENTS.md invariant that effective capabilities are the
intersection of user, agent, skill, channel and workspace; a gate cannot widen the set, only refuse earlier.
The §2 caller intersection obeys this: it narrows `user` rather than adding a term. Buzz substitutes pod
sandboxing for capability restriction; we keep the intersection and let identity say who acted.
**Test** — `tests/security/test_capability_enforcement.py` gains a case where a valid, signed,
owner-addressed identity requests `artifact.write` while the authorizing human is a viewer: the request is
`REJECTED`, and the effective set is asserted identical with and without identity.

## Migrations

`016_agent_identity_and_harness.sql`. 013 is the conversation layer, 014 the execution
authorization fix, and 015 the mention-handle work, so this piece is 016. New tables `agent_identities`, `agent_addressing`,
`agent_address_allowlist`, `agent_runs`; new columns `agent_instances.harness_id`,
`tool_requests.authorized_by`, `approvals.authorized_by`, `agent_room_memberships.removed_at`. The update guard
copies `executions_reject_branch_update` (007), the delete guard the `reject_delete` shape (005); the `ON
DELETE RESTRICT` parents depart from the prevailing `CASCADE` because a run is evidence, not tidied away with
what it describes.

```sql
CREATE TABLE IF NOT EXISTS agent_identities (
    identity_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, revoked_at TEXT,
    proof_mode TEXT NOT NULL CHECK(proof_mode IN ('IN_PROCESS','SIGNED_CHALLENGE')),
    public_key TEXT, key_fingerprint TEXT UNIQUE,
    agent_id TEXT NOT NULL UNIQUE REFERENCES agent_instances(agent_id) ON DELETE CASCADE,
    -- A key exists exactly when there is an untrusted transport to prove authorship across.
    CHECK((proof_mode = 'SIGNED_CHALLENGE') = (public_key IS NOT NULL)));
CREATE TABLE IF NOT EXISTS agent_addressing (
    agent_id TEXT PRIMARY KEY REFERENCES agent_instances(agent_id) ON DELETE CASCADE,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE CASCADE,
    mode TEXT NOT NULL CHECK(mode IN ('OWNER_ONLY','ALLOWLIST','ANYONE','NOBODY')),
    owner_user_id TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);
-- agent_address_allowlist: (agent_id -> agent_addressing ON DELETE CASCADE, user_id, added_by,
-- created_at), PRIMARY KEY (agent_id, user_id).
-- Every parent below is RESTRICT: a run must outlive what it names. Under CASCADE, deleting an
-- instance wiped its runs, so identity_id's RESTRICT never fired and the trail went in one delete.
CREATE TABLE IF NOT EXISTS agent_runs (run_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL UNIQUE REFERENCES executions(execution_id) ON DELETE RESTRICT,
    agent_id TEXT NOT NULL REFERENCES agent_instances(agent_id) ON DELETE RESTRICT,
    identity_id TEXT NOT NULL REFERENCES agent_identities(identity_id) ON DELETE RESTRICT,
    room_id TEXT NOT NULL REFERENCES rooms(room_id) ON DELETE RESTRICT,
    authorized_by TEXT NOT NULL, acting_user_id TEXT NOT NULL,   -- initiator, then last caller
    harness_id TEXT NOT NULL, credential_hash TEXT NOT NULL,
    harness_state TEXT NOT NULL CHECK(harness_state IN
        ('STARTING','STREAMING','AWAITING_APPROVAL','CANCEL_REQUESTED','SETTLED')),
    settlement TEXT CHECK(settlement IN ('END_TURN','CANCELLED','MAX_TOKENS','FAILED',
        'ORPHANED','AUTHORITY_REVOKED','AGENT_REMOVED','APPROVAL_REFUSED')),
    resumed_from_run_id TEXT REFERENCES agent_runs(run_id) ON DELETE RESTRICT,
    lease_expires_at TEXT NOT NULL, created_at TEXT NOT NULL, settled_at TEXT,
    -- Settled with no settlement is terminal to the machine and invisible to the sweep: stuck.
    CHECK(harness_state <> 'SETTLED' OR settlement IS NOT NULL));
CREATE INDEX IF NOT EXISTS idx_runs_open ON agent_runs(lease_expires_at) WHERE harness_state <> 'SETTLED';
CREATE INDEX IF NOT EXISTS idx_runs_agent_room ON agent_runs(agent_id, room_id);
-- Fail-closed launch, below the service so a future code path cannot launch anonymously.
CREATE TRIGGER IF NOT EXISTS agent_runs_require_live_identity BEFORE INSERT ON agent_runs
WHEN NOT EXISTS (SELECT 1 FROM agent_identities i WHERE i.identity_id = NEW.identity_id
    AND i.agent_id = NEW.agent_id AND i.revoked_at IS NULL)
BEGIN SELECT RAISE(ABORT, 'an agent without a live identity may not launch'); END;
-- The INSERT trigger guards only the first write. These three close the ways a run was otherwise
-- rewritten: settled twice, re-pointed at another agent, or deleted and reinserted to launder a
-- settlement the UPDATE guard refused.
CREATE TRIGGER IF NOT EXISTS agent_runs_settlement_is_final BEFORE UPDATE ON agent_runs
WHEN OLD.harness_state = 'SETTLED'
BEGIN SELECT RAISE(ABORT, 'a settled run is terminal'); END;
CREATE TRIGGER IF NOT EXISTS agent_runs_reject_actor_update
BEFORE UPDATE OF agent_id, identity_id ON agent_runs
BEGIN SELECT RAISE(ABORT, 'a run may not be re-pointed at another agent or identity'); END;
CREATE TRIGGER IF NOT EXISTS agent_runs_reject_delete BEFORE DELETE ON agent_runs
BEGIN SELECT RAISE(ABORT, 'a run is an audit record and is never deleted'); END;
ALTER TABLE agent_instances ADD COLUMN harness_id TEXT NOT NULL DEFAULT 'nexus';
ALTER TABLE tool_requests ADD COLUMN authorized_by TEXT NOT NULL DEFAULT '';
ALTER TABLE approvals ADD COLUMN authorized_by TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_room_memberships ADD COLUMN removed_at TEXT;
```

Backfill: existing instances get an `IN_PROCESS` identity row with no key — every harness today runs in this
process — and `agent_addressing` at `OWNER_ONLY` owned by their room's creator, the narrowest mode that keeps
existing rooms working, where `ANYONE` would silently widen them. Historical `tool_requests` and `approvals`
keep `authorized_by = ''`, read as "authority not recorded" rather than having one invented; `acting_user_id`
backfills to `authorized_by`, the only caller those runs are known to have had.

## Not building

From the scope guardrails in `AGENTS.md` — what a buzz reader might expect here and must not get:

- **No agent-to-agent society.** Delegation stays a parent run's tool call under the same human's authority.
- **No unrestricted swarm.** No self-launching agents, schedules or fan-out — a run exists because a human asked.
- **No custom model-serving stack.** A harness is a client of an existing runtime: no weights, no server.
- **No skill marketplace, no per-harness sandbox pods.** Buzz substitutes pod isolation for capability scoping.
- **No private-key storage.** Public keys only, so no agent secret can leak through an audit view.
- **No sixth capability term.** The caller intersection of §2 narrows `user`; it does not add a term.
