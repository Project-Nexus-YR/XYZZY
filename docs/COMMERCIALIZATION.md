# Commercialization Plan

Decided 2026-08-30, from primary-source research on comparable open-source
companies (Supabase, Cal.com, n8n, HashiCorp, Sentry) and a 30-day sweep of the
agent-tooling landscape. Sources for every claim: `docs/research/` fetch notes
or the URLs inline below.

## Where XYZZY sits in the market

The crowded lane is single-user coding orchestrators (claude-orgtree, Runner,
DeerFlow) and visual builders (Langflow 146k stars, Dify 136k, Flowise 51k:
approachable UI, not raw capability, is what accumulates stars). The open lane
is XYZZY's: a persistent room where several HUMANS and several agents work
together, with governance and provenance built in. Two 2026 signals say the
lane is warming: agent-call security is becoming its own product category, and
"who does the agent answer to" drew the month's biggest Hacker News thread
(1,022 points). Both are questions XYZZY answers structurally. A2A support is
becoming table stakes: XYZZY already speaks it.

## Model: open-core + hosted cloud

The Supabase/Cal.com pattern, not a BSL/license-change play: there is no
installed base to protect yet, and HashiCorp's move shows the backlash cost of
changing terms later on a large community.

- **The wedge (free, forever):** the whole single-tenant product. Rooms,
  branches, synthesis, provenance, the credential CLI, every model-provider
  integration (n8n keeps integrations free for the same reason: they are the
  adoption surface, not the product).
- **First gates (team-buyer features):** SSO/OIDC beyond the operator CLI, and
  audit/compliance exports of the hash-chained event log. These are what a team
  lead points at to justify a purchase, per Cal.com's and Supabase's tiers.
- **Second gates:** multi-node/HA, then managed hosting, the long-term revenue
  engine once teams exist who do not want to run FastAPI + SQLite themselves.

## License: decide before the repo goes public

Two defensible paths, and the deadline is external contributions (relicensing
after they arrive needs every contributor's consent):

1. **Stay MIT**, the Supabase bet: adoption outweighs the risk that a cloud
   vendor hosts a competing XYZZY. Simplest, most credible "truly open".
2. **Switch to an n8n-style Sustainable Use License**: free for any internal
   use including commercial, forbids reselling XYZZY itself as a hosted
   service. Protects the future cloud without AGPL's viral complexity (which
   Cal.com's own history shows still needs a commercial license on top).

**Decided 2026-08-30: stay MIT.** The adoption bet wins; the LICENSE file already
says MIT and stays as it is. Revisit only if a competing hosted fork actually
materializes, knowing a change can then only apply to future versions.

**Revised 2026-08-31: Apache 2.0.** Same adoption bet, same permissiveness for
users, plus the explicit patent grant, the Supabase main-repo choice. Done
before the repo goes public, while no external contributions exist and the
switch needs nobody's consent but the author's.

## Adoption prerequisites (before any of the above matters)

1. **One-command self-host**: `docker compose up` from a fresh clone to a
   working workspace. The Dockerfile exists; compose + a data volume + token
   bootstrap docs finish it.
2. **Docs a team can self-evaluate from**: quickstart, the security model
   (capability intersection, provenance, screening) as a selling page, API
   reference, A2A interop notes.
3. **A live public demo room**: XYZZY is multiplayer by nature; letting a
   stranger join a room with agents and another human is a stronger first-touch
   than any screenshot. None of the comparables can offer this; it is the
   distinctive lever. (Needs the rate limiting and posture controls already
   shipped, plus a reset-on-interval demo workspace.)

## Sequencing

Ship 1-3 with the current UI polish → public repo + Show HN/r/AI_Agents launch
(the communities where this month's comparable launches ran) → watch what
teams ask to pay for → gate SSO + audit export first. Managed cloud only after
self-host adoption proves the shape.
