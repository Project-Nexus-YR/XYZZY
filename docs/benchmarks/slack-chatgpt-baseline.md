# Superseded: Slack + separate ChatGPT baseline

## Status

Superseded by `chatgpt-shared-projects-baseline.md` at the user's direction on 2026-08-20.
The prepared Slack channel remains available as historical benchmark setup, but it is not the
active gauntlet quality bar and was never executed as a three-person run.

- Slack channel: `multiai-gauntlet-benchmark` (private)
- Seed message: <https://newworkspace-nf01857.slack.com/archives/C0BRAKARBFF/p1787187536295479>
- Required participants: engineering lead, security lead, product lead
- Reference tools: one Slack channel/thread plus one separate current ChatGPT chat per participant

## Seeded decision

> Should a 20-person AI-native SaaS startup migrate its authentication service from a
> self-managed PostgreSQL session store to a managed identity provider this quarter?

Each participant analyzes the same question independently from their assigned perspective:

1. Engineering: implementation, migration, reliability, and rollback.
2. Security: threat model, compliance, identity controls, and residual risk.
3. Product: user impact, delivery timing, cost, and decision criteria.

Each person manually transfers their ChatGPT output into Slack. The group discusses differences
in a Slack thread and manually writes one final decision brief.

## Measurements

Record evidence rather than estimates:

- start timestamp, final-artifact timestamp, and elapsed time;
- manual copy/paste transfers between ChatGPT and Slack;
- prompts, source chat identifiers or links, and Slack message identifiers;
- number of original claims used in the final brief;
- number of final claims traceable to an exact participant output;
- disagreements surfaced and resolved or left open;
- assumptions, risks, recommendation, and next actions retained in the final brief;
- corrections, dropped claims, and attribution errors.

## Binary comparison rule

Remove product labels from the resulting artifacts before criticism. MultiAI wins only if its
three-person run:

1. uses fewer manual copy/paste transfers;
2. gives every selected agent output persistent, inspectable identity;
3. gives every final claim a drill-down path to exact selected output and original evidence;
4. preserves individual outputs after synthesis;
5. loses or duplicates no ordered events during reconnect tests;
6. completes the same decision workflow without introducing a larger critical usability or
   correctness failure.

Ties and unverified conditions count as a MultiAI loss. The critic returns only a binary winner
and the single largest remaining gap.
