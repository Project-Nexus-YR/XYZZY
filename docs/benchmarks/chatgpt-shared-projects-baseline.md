# ChatGPT shared Projects baseline

## Reference

The active quality bar is the current ChatGPT shared Projects workflow documented by OpenAI at:

<https://help.openai.com/en/articles/10169521-using-projects-in-chatgpt-496>

Reference facts verified on 2026-08-20:

- a shared Project is a live context hub containing chats, files, and project instructions;
- members can see other members' contributions and each chat is associated with its author;
- shared Projects use project-only memory;
- members can create, move, and branch chats without altering the original chat;
- branching is asynchronous rather than synchronous same-chat collaboration;
- owners control membership, while chat and edit access have different privileges.

The public logged-out ChatGPT UI was inspected, but a signed-in shared Project was not available in
the connected browser. A hands-on reference run remains pending; critics must not invent its
results. Until then, official current product documentation is the recorded source of truth.

## Seeded decision

> Should a 20-person AI-native SaaS startup migrate its authentication service from a
> self-managed PostgreSQL session store to a managed identity provider this quarter?

## Reference workflow

1. Create a shared Project with project-only memory and the seeded decision as project context.
2. Add engineering, security, and product participants with appropriate access.
3. Create an initial decision chat, then branch it into independent engineering, security, and
   product analyses.
4. Inspect the three authored branches and create a final decision brief in the Project.
5. Record branch discoverability, context reuse, attribution, permissions, manual transfers,
   retained disagreements, and traceability from final claims to branch evidence.

## Binary comparison rule

Remove product labels from the resulting decision artifacts before criticism. MultiAI wins only
if it matches the reference on shared-context continuity, authored branch discoverability, and
access boundaries, while clearly outperforming it on all of the following:

1. launching independent specialist analyses in parallel;
2. comparing individual outputs side by side without hiding them after synthesis;
3. explicitly including or excluding each output before synthesis;
4. tracing every selected output and final claim to exact source evidence;
5. enforcing user, agent, skill, channel, and workspace capabilities deterministically;
6. surviving reconnect with no lost or duplicated ordered events.

The full test suite must pass and local p95 acknowledgement latency must remain below 250 ms. Ties
and unverified claims count as a MultiAI loss. The critic returns a binary winner and the single
largest remaining gap.

