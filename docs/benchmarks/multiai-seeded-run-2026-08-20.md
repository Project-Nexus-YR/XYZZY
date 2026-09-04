# XYZZY seeded workflow run — 2026-08-20

**Superseded record.** The suite size and file name below predate the
services split and the erasure track: "Automated suite: 143 tests" is a
seventh of the current count, and the file's own name still carries the
product's pre-rename working title, `multiai`, even though the body already
says XYZZY throughout. Re-run the p95 figure with
`pytest -s tests/performance/test_ack_latency.py` (it prints the number,
uncaptured, on a passing test) rather than trusting the value below. No page
in the repository links to this record; it is kept for the seeded-workflow
narrative in the sections that follow, not for its numbers.

## Status

This is the recorded local XYZZY run against the active
`chatgpt-shared-projects-baseline.md` quality bar. It is not a winning comparison yet: the
connected browser had no signed-in three-person ChatGPT shared Project, and the local server had
no `OPENAI_API_KEY`. The reference artifact and live model-backed XYZZY artifact therefore
remain pending. The gauntlet rule counts both unverified conditions as a XYZZY loss.

## Seeded decision

> Should a 20-person AI-native SaaS startup migrate its authentication service from a
> self-managed PostgreSQL session store to a managed identity provider this quarter?

## Browser workflow exercised

The served UI at `http://127.0.0.1:8000` was exercised against file-backed SQLite with an opaque
Bearer token owned by the local test principal.

1. Entered one authenticated decision room.
2. Launched Architect, Researcher, and Security Reviewer concurrently.
3. Observed three completed, persisted, role-attributed AgentOutputs.
4. Included Architect and Researcher; excluded Security Reviewer.
5. Published Decision Brief v1.
6. Opened the artifact and drilled from both claims into their exact source AgentOutputs.
7. Reloaded and re-entered the workspace twice.

Because no model credential was configured, every output was visibly labelled
`SIMULATED WORKFLOW OUTPUT` and explicitly stated that it was not decision analysis. No simulated
text is treated as a real outcome.

## Recorded local evidence

- Parallel run result: 3 completed, 0 failed.
- Review state: 2 included, 1 excluded, 0 unreviewed.
- Artifact: Decision Brief v1 with two selected claims and no excluded claim.
- Provenance: each claim drilled down to its selected AgentOutput and exact evidence.
- Ordered room history: 30 canonical events through `artifact.decision_brief_synthesized`.
- Browser reconnect: two consecutive reload/re-entry cycles restored the same 3 outputs, 2/1
  review state, artifact, and exactly 30 visible events each time.
- Automated suite: 143 tests passed at the time this record was finalized.
- Static gates: Ruff format/check and strict `mypy src` passed.
- Local acknowledgement benchmark: 1.412 ms p95 across 100 atomic file-backed selection writes;
  reopen retained 100 unique, contiguous events.

## Manual-transfer comparison

The XYZZY browser workflow required no manual copying between specialist branches and the
decision artifact. The corresponding hands-on ChatGPT shared Project transfer count remains
unrecorded, so this is evidence about XYZZY only and not a comparative win.

## Remaining external acceptance work

1. Run the seeded workflow in a signed-in shared Project with three real participants and retain
   the authored branches and anonymized final artifact.
2. Configure a server-side OpenAI API credential, rerun the same three specialists, and retain the
   live model-backed outputs and artifact.
3. Blindly compare the anonymized artifacts and record the critic's binary choice and single gap.

## Ontology and Meta follow-up

The same persisted room was reopened after the minimal ontology and Meta phases landed. Publishing
Decision Brief v2 rendered a selected-only Decision → Claim → AgentOutput evidence tree with
AI-derived labels, confidence, provider/source drill-down, and governed review controls. The
browser then asked “Why was this decision made?” and received a room-scoped answer at freshness
cursor 32 using exactly 2 of 2 selected claims, with both exact provider/source evidence records
available for drill-down. The excluded Security Reviewer output was absent. This follow-up also
used simulated, explicitly non-decision content because no live model credential was available.
