"""Round 2: a redacted message's text must not survive in any derived copy.

Round 1 redacted ``room_events`` and ``messages`` but never looked at the
tables that keep their own copy of a message's text made at a different
time: the full-text search index (``search_documents``, made at send time)
and a branch's ``context_snapshot`` (made at branch-start time, in the
``branches`` table). Either one still handing back the original text after
``erase_user`` is exactly the leak this file proves is closed.

A first-class "thread title" does not exist in this codebase (checked
``domain/models.py`` and ``services/conversation.py``): the closest things
that carry a person-typed title are task and decision titles, and round 1
never swept those at all (their event payload is marked, but the
``tasks``/``decisions`` row's own title column is untouched). That is a
real, separate gap, out of reach of this track's owned files without
touching ``services/records.py``; it is called out under "Needs lead
wiring" in the round 2 report rather than silently left unfixed here.

Similarly, ``services/branches.py``'s synthesis claims quote *agent output*
text, never the raw human message, so seeding the token through a claim
would not exercise a real leak; the actual literal copy of a human message
is the branch's own ``context_snapshot``, which this test seeds and checks
directly. Meta answers are computed live from ``room_events``/``messages``
on every call (``MetaRepo`` has no cache table), so nothing there can hold
a stale copy once the source rows are redacted; the search-hit check that
follows this same reasoning is enough to cover that class of read.
"""

from __future__ import annotations

import json
from typing import Any

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.agent_tasks import Part, PartKind
from multiplayer.domain.models import (
    ArtifactType,
    BranchMode,
    ExecutionIntervention,
    MessageRole,
    OutputDisposition,
    RunSettlement,
    User,
    new_id,
)
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService

TOKEN = "ZEBRA-9911"


async def _seeded() -> tuple[Database, MultiplayerService, str, str]:
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset())
    await svc.initialize()
    await svc.repos.users.create(User(user_id="alice", display_name="Alice", email="a@x.com"))
    await svc.repos.users.create(User(user_id="bob", display_name="Bob", email="b@x.com"))
    org = await svc.create_organization("Org", "org", "alice")
    workspace = await svc.create_workspace(org.org_id, "Ws", "ws", "alice")
    room = await svc.create_room(workspace.workspace_id, "Room", "alice")
    await svc.invite_room_member(room.room_id, "bob", "editor", "alice")
    return db, svc, room.room_id, workspace.workspace_id


async def _all_table_dump(db: Database) -> dict[str, list[dict]]:
    tables = await db.fetch_all("SELECT name FROM sqlite_master WHERE type = 'table'")
    dump: dict[str, list[dict]] = {}
    for row in tables:
        name = row["name"]
        if name.startswith("sqlite_") or name.endswith("_fts") or "_fts_" in name:
            continue
        dump[name] = [dict(r) for r in await db.fetch_all(f"SELECT * FROM {name}")]
    return dump


async def test_erasure_leaves_no_trace_in_any_table(tmp_path):
    db, svc, room_id, _ws = await _seeded()
    try:
        await svc.send_message(
            room_id, MessageRole.HUMAN, "alice", f"the plan is {TOKEN} launch tuesday"
        )
        attachment = await svc.upload_attachment(
            room_id, "alice", f"{TOKEN}-notes.txt", "text/plain", b"contents", max_bytes=1_000_000
        )

        templates = await svc.list_agent_templates()
        agent = await svc.spawn_agent(room_id, templates[0].template_id)
        branch, _runs = await svc.start_branch(
            room_id,
            BranchMode.TURN_LOCKED_SINGLE,
            f"kick off the {TOKEN} review",
            "alice",
            [agent.agent_id],
        )
        # The branch's own context_snapshot copied alice's message verbatim at
        # branch-start time; confirm the copy actually landed before erasing.
        before = await svc.repos.branches.get(branch.branch_id)
        assert before is not None
        assert TOKEN in json.dumps(before.context_snapshot)
        # The branch's initiating_prompt (alice's own typed text) and every
        # execution it launched keep their own separate copy of it too.
        assert TOKEN in before.initiating_prompt
        executions_before = await svc.repos.executions.list_by_branch(branch.branch_id)
        assert executions_before
        assert all(TOKEN in json.dumps(e.input_data) for e in executions_before)

        # Round 5: an agent task alice opened by hand carries her own typed
        # words in agent_task_messages.parts, never in the task.delegated
        # event's own payload (that carries only ids and state).
        task = await svc.open_agent_task(
            room_id,
            agent.agent_id,
            (Part(kind=PartKind.TEXT, content=f"please handle the {TOKEN} request"),),
            requested_by="alice",
        )
        task_messages_before = await svc.repos.agent_tasks.list_messages(task.task_id)
        assert any(TOKEN in part.content for m in task_messages_before for part in m.parts)

        # Round 5: a turn parked at a reviewer holds alice's own steer text in
        # suspended_turns.prompt, keyed by acting_as. Seeded directly against
        # one of the branch's own real, non-terminal executions, the same
        # shape services/steps.py writes when a tool call needs approval.
        parked_execution_id = executions_before[0].execution_id
        await svc.repos.suspended_turns.save(
            parked_execution_id, f"keep going: {TOKEN}", "alice", ["a tool ran"]
        )
        run_before = await svc.repos.agent_runs.get_by_execution(parked_execution_id)
        assert run_before is not None
        assert run_before.settlement is None

        # Round 6: execution_interventions.instruction holds alice's own steer
        # text, both a consumed one (seeded directly, the same way the
        # suspended turn above is) and a pending one, seeded through the real
        # intervene_execution call so its human_redirected_agent event is also
        # exercised. The pending one is put on a second, independent
        # execution (not the one already parked above) so its settlement can
        # be checked without entangling it with the suspended-turn sweep.
        consumed_intervention = ExecutionIntervention(
            intervention_id=new_id("interv"),
            execution_id=parked_execution_id,
            intervened_by="alice",
            instruction=f"already applied: {TOKEN}",
        )
        await svc.repos.interventions.create(consumed_intervention)
        await svc.repos.interventions.mark_consumed([consumed_intervention.intervention_id])

        # A second room, since the first one's branch already holds the room's
        # turn lock (TURN_LOCKED_SINGLE) and was never run to completion.
        room2 = await svc.create_room(_ws, "Room Two", "alice")
        agent2 = await svc.spawn_agent(room2.room_id, templates[0].template_id)
        branch2, runs2 = await svc.start_branch(
            room2.room_id,
            BranchMode.TURN_LOCKED_SINGLE,
            "kick off a second review",
            "alice",
            [agent2.agent_id],
        )
        pending_execution_id = runs2[0].execution_id
        await svc.intervene_execution(pending_execution_id, "alice", f"steer it: {TOKEN}")
        pending_run_before = await svc.repos.agent_runs.get_by_execution(pending_execution_id)
        assert pending_run_before is not None
        assert pending_run_before.settlement is None

        result = await svc.erase_user("alice")
        # Alice's message, the room name she typed when she created "Room",
        # the task.delegated event her own open_agent_task call appended, the
        # room name she typed when she created "Room Two", and the
        # human_redirected_agent event her own intervene_execution call
        # appended.
        assert result["redactions"] == 5

        # 1. No table anywhere in the database still holds the literal token,
        #    except inside the redaction bookkeeping's own metadata (which never
        #    stores the token itself, only ids and reasons -- this assertion
        #    would fail immediately if it ever did).
        dump = await _all_table_dump(db)
        offenders = []
        for table, rows in dump.items():
            for r in rows:
                blob = json.dumps(r, default=str)
                if TOKEN in blob:
                    offenders.append((table, r))
        assert offenders == [], f"token leaked into: {offenders}"

        # 2. The branch's context_snapshot copy is gone too, not just the
        #    message and event rows.
        after = await svc.repos.branches.get(branch.branch_id)
        assert after is not None
        assert TOKEN not in json.dumps(after.context_snapshot)
        # 2b. So is the branch's own initiating_prompt column, and every
        #     execution's independent copy of it in input_data.
        assert TOKEN not in after.initiating_prompt
        executions_after = await svc.repos.executions.list_by_branch(branch.branch_id)
        assert executions_after
        assert all(TOKEN not in json.dumps(e.input_data) for e in executions_after)

        # 3. The search index no longer resolves a hit for the erased content.
        # alice's own membership row survives erasure (history needs a slot to
        # say who was there), so she is still a valid reader for this check.
        hits = await svc.search("alice", TOKEN, room_id)
        assert hits == []
        # bob is a different, un-erased room member who would have found it
        # before erasure.
        hits_bob = await svc.search("bob", TOKEN, room_id)
        assert hits_bob == []

        # The attachment itself is untouched by this test's own assertions
        # beyond the full dump above; round 1 already clears its filename and
        # bytes via AttachmentRepo.erase_in_transaction.
        attachment_after = await svc.repos.attachments.get(attachment.attachment_id)
        assert attachment_after is not None
        assert TOKEN not in (attachment_after.filename or "")

        # 4. The agent task's own asker-authored parts are gone, not just the
        #    task.delegated event's payload.
        task_messages_after = await svc.repos.agent_tasks.list_messages(task.task_id)
        assert task_messages_after
        assert all(TOKEN not in part.content for m in task_messages_after for part in m.parts)

        # 5. The suspended turn is gone (discarded, not just blanked), and the
        #    run it belonged to was failed rather than left waiting forever.
        suspended_after = await db.fetch_all(
            "SELECT * FROM suspended_turns WHERE execution_id = ?", (parked_execution_id,)
        )
        assert suspended_after == []
        run_after = await svc.repos.agent_runs.get_by_execution(parked_execution_id)
        assert run_after is not None
        assert run_after.settlement is RunSettlement.AUTHORITY_REVOKED
        execution_after = await svc.repos.executions.get(parked_execution_id)
        assert execution_after is not None
        assert execution_after.status.value in {"FAILED", "CANCELLED"}

        # 6. Round 6: both intervention rows kept (not discarded, unlike the
        #    suspended turn), with their instruction column scrubbed rather
        #    than the row dropped -- the full-dump sweep above already proved
        #    the token is gone from execution_interventions, this confirms
        #    the rows themselves still exist as the audit record 018/020
        #    intend them to.
        consumed_after = await db.fetch_one(
            "SELECT instruction FROM execution_interventions WHERE intervention_id = ?",
            (consumed_intervention.intervention_id,),
        )
        assert consumed_after is not None
        assert TOKEN not in consumed_after["instruction"]
        # The execution whose only pending intervention was alice's own is no
        # longer waiting on anyone: it is settled the same way the suspended
        # turn above is.
        pending_run_after = await svc.repos.agent_runs.get_by_execution(pending_execution_id)
        assert pending_run_after is not None
        assert pending_run_after.settlement is RunSettlement.AUTHORITY_REVOKED
        pending_execution_after = await svc.repos.executions.get(pending_execution_id)
        assert pending_execution_after is not None
        assert pending_execution_after.status.value in {"FAILED", "CANCELLED"}
    finally:
        await db.close()


async def test_search_documents_row_is_gone_after_erasure():
    """Narrower, direct check on the table the critic quoted: a raw SELECT
    against search_documents must not turn up the token either."""
    db, svc, room_id, _ws = await _seeded()
    try:
        await svc.send_message(room_id, MessageRole.HUMAN, "alice", f"secret {TOKEN} content")
        await svc.erase_user("alice")
        rows = await db.fetch_all(
            "SELECT * FROM search_documents WHERE content LIKE ?", (f"%{TOKEN}%",)
        )
        assert rows == []
    finally:
        await db.close()


async def test_task_and_decision_titles_leave_no_trace(tmp_path):
    """Round 3: the gap round 2 flagged under "Needs lead wiring".

    Round 2 redacted a ``task.created``/``decision.created`` event's own
    payload (title lives in ``_PERSONAL_PAYLOAD_KEYS``) but never touched the
    ``tasks``/``decisions`` row's own title/description/content columns or
    their ``search_documents`` rows, so a reader of current state (the task
    list, the decision list, a search hit) still saw the original text. This
    seeds the same token into a task title, a task description, and a
    decision title, then reuses the same full-database-dump sweep as the
    message leak test above.
    """
    db, svc, room_id, _ws = await _seeded()
    try:
        task = await svc.create_task(
            room_id,
            f"plan the {TOKEN} launch",
            description=f"notes about {TOKEN} that only alice wrote",
            created_by="alice",
        )
        decision = await svc.create_decision(
            room_id,
            f"ship {TOKEN} on friday",
            "content without the token",
            created_by="alice",
        )

        result = await svc.erase_user("alice")
        # The room name ("Room"), the task title, and the decision title each
        # carry personal payload and each get their own redaction id.
        assert result["redactions"] == 3

        dump = await _all_table_dump(db)
        offenders = [
            (table, r)
            for table, rows in dump.items()
            for r in rows
            if TOKEN in json.dumps(r, default=str)
        ]
        assert offenders == [], f"token leaked into: {offenders}"

        task_after = await svc.repos.tasks.get(task.task_id)
        assert task_after is not None
        assert TOKEN not in task_after.title
        assert TOKEN not in task_after.description

        decision_after = await svc.repos.decisions.get(decision.decision_id)
        assert decision_after is not None
        assert TOKEN not in decision_after.title

        search_rows = await db.fetch_all(
            "SELECT * FROM search_documents WHERE object_kind IN ('TASK', 'DECISION')"
        )
        assert all(TOKEN not in (r["content"] or "") for r in search_rows)
    finally:
        await db.close()


class _OneShotSynthesisProvider:
    """Answers a branch turn, then a synthesis, with fixed, parseable output.

    The synthesis response's claim names a source output id the caller
    substitutes in after the turn runs (the id is only known once the turn's
    own output exists), the same way test_split_track_token_usage.py does it.
    """

    def __init__(self) -> None:
        self.output_id: str | None = None

    async def acomplete(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        del response_schema
        if "You are the synthesis stage" in prompt:
            content = (
                '{"summary": "ok", "recommendation": "ship it", '
                f'"claims": [{{"text": "a", "source_output_ids": ["{self.output_id}"], '
                '"confidence": 0.7}], "risks": [], "uncertainties": [], '
                '"next_action": "none"}'
            )
            return {
                "action": "finish",
                "output": {"content": content, "provider": "test-model", "model": "synth-test"},
                "token_usage": 1,
                "provider_name": "test-model",
                "provider_model": "synth-test",
                "provider_response_id": "resp_synth",
                "provider_evidence": content,
            }
        return {
            "action": "finish",
            "output": {"content": "the specialist's answer", "provider": "test-model"},
            "token_usage": 1,
            "provider_name": "test-model",
            "provider_model": "turn-test",
            "provider_response_id": "resp_turn",
            "provider_evidence": "the specialist's answer",
        }


async def test_branch_synthesis_title_and_artifact_name_leave_no_trace(monkeypatch):
    """Round 3: two more user-typed titles round 2 did not reach.

    A branch synthesis's title never rides inside any chained event payload
    (``branch.synthesis.started`` carries only ids), so nothing before this
    round ever redacted the durable ``branch_syntheses.title`` column. A
    hand-created artifact's name does ride inside ``artifact.created``'s
    payload, but round 2's per-room loop only knew about
    content/body/title/filename, not "name", so neither the event payload nor
    the durable ``artifacts.name``/``description`` columns were ever touched.

    This does not reuse the full-table-dump sweep the other tests here run:
    once a branch executes, the room's own event log (including the artifact's
    name and the synthesis title) is folded verbatim into the prompt an agent
    received, and that prompt is logged into ``agent_outputs.provider_input``
    and ``artifact_claim_sources.provider_input``, both append-only evidence
    tables (migration 003) no redaction may rewrite: it is the exact class of
    leak round 2 already ruled out of scope for ``ArtifactClaim.evidence``
    (agent output text, not the raw thing a human typed, frozen at generation
    time). What this test checks instead is that the two durable read paths
    this round actually fixes, ``artifacts.name``/``description`` and
    ``branch_syntheses.title``, no longer show the token.
    """
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db, svc, room_id, _ws = await _seeded()
    try:
        provider = _OneShotSynthesisProvider()
        svc.nexus = NexusAgentBridge(model_provider=provider)

        artifact = await svc.create_artifact(
            room_id,
            f"{TOKEN} plan",
            ArtifactType.DOCUMENT,
            description=f"notes about {TOKEN}",
            created_by="alice",
        )

        templates = await svc.list_agent_templates()
        agent = await svc.spawn_agent(room_id, templates[0].template_id)
        branch, runs = await svc.start_branch(
            room_id, BranchMode.TURN_LOCKED_SINGLE, "kick off the review", "alice", [agent.agent_id]
        )
        result = await svc.execute_branch_run(branch.branch_id, runs[0].execution_id)
        provider.output_id = str(result["output_id"])
        await svc.select_branch_output(
            branch.branch_id, str(result["output_id"]), OutputDisposition.INCLUDED, "alice"
        )
        _synth_artifact, version = await svc.synthesize_branch_decision_brief(
            branch.branch_id, f"decide on {TOKEN}", "alice"
        )
        assert version.branch_synthesis_id is not None

        result = await svc.erase_user("alice")
        assert result["redactions"] >= 1  # the hand-created artifact's name, at least

        artifact_after = await svc.repos.artifacts.get(artifact.artifact_id)
        assert artifact_after is not None
        assert TOKEN not in artifact_after.name
        assert TOKEN not in artifact_after.description

        synthesis_after = await svc.repos.branch_syntheses.get(version.branch_synthesis_id)
        assert synthesis_after is not None
        assert TOKEN not in synthesis_after.title
    finally:
        await db.close()
