# Security Policy

## Supported Versions

XYZZY is pre-1.0. The `0.4.x` line is the only one that receives fixes.

| Version | Supported |
| --- | --- |
| 0.4.x | yes |
| < 0.4 | no |

## Reporting a Vulnerability

Use GitHub's private vulnerability reporting on this repository (Security tab
→ "Report a vulnerability"). Do not open a public issue for a security
finding, and do not email a maintainer — private reporting is the only
channel this project monitors for security reports. Include the endpoint or
code path, a reproduction, and the impact you believe it has. Expect an
acknowledgement within a few days; there is no fixed SLA yet at this stage of
the project.

## Security Model

This section is a factual summary with pointers into the code, not a claim
that the system is unbreakable. Read the linked files before relying on any
of it.

**Capability intersection on every tool call.** What an agent may do for a
given tool call is the intersection of five grants — user, agent, skill,
channel, workspace — recomputed from durable rows at the moment of spending,
never cached from an earlier check. `CapabilityTerms.effective` and
`spend_under()` in `src/multiplayer/security/capabilities.py` compute it;
`decide()` in the same file is the gateway that every tool call passes
through. A delegated run is bound the same way: what a delegate may spend is
its asker's authority intersected with its own, re-read live, so narrowing
the asker mid-task narrows the delegate with it.

**Structurally-excluded governance surface.** Certain actions — the ones that
change who can do what, rather than do work inside those bounds — are not
reachable from an agent turn at all, independent of any permission a policy
config might grant. `agent_turn()` and `require_human_boundary()` in
`src/multiplayer/security/boundary.py` enforce this on ambient execution
context: a governance method call made while inside an agent turn raises,
full stop.

**Hash-chained room event log with a verify CLI.** Every room event is
written with a hash over the previous event's hash plus its own fields
(`event_chain_hash()` in `src/multiplayer/security/audit.py`), so the log is
tamper-evident: altering or deleting a row breaks every hash after it.
`verify_event_chain()` in the same file recomputes a room's chain and reports
sequence gaps or hash mismatches. Run it with
`python -m multiplayer.manage <db-path> audit verify`.

**Hashed credential rows with revocation.** Bearer tokens minted by the
operator CLI are stored as a hash, never plaintext; the token itself is
printed once at mint time and is not recoverable from the database.
`python -m multiplayer.manage <db-path> token revoke <token-or-hash>` marks a
row revoked immediately, without a restart. See `src/multiplayer/manage.py`
and the `user add` / `token mint` / `token revoke` / `token list` commands in
the README's Running section.

**OIDC sessions with rotation and back-channel logout.** A refresh token is
spendable once; presenting a spent one revokes the whole session rather than
just that token, since a replay means a copy exists somewhere it should not.
Every access-token refresh also spends the identity provider's own refresh
token (`refresh_at_provider()` in `src/multiplayer/security/oidc.py`), so a
person disabled or password-reset upstream loses the session at the next
rotation instead of only at the absolute session clock.
`POST /api/v1/auth/backchannel-logout` accepts the provider's own logout
token and ends the session from the provider's side
(`src/multiplayer/api/routes.py`).

**HttpOnly-cookie browser sessions with header and Origin CSRF gates.** The
browser's session cookie carries the access token only, HttpOnly, and is
never readable from page script. A cookie authenticates an HTTP request only
when the request also carries the `X-XYZZY-Client: web` header
(`WEB_CLIENT_HEADER` and `_current_user()` in `src/multiplayer/api/routes.py`)
— a cross-origin request cannot attach a custom header without a CORS
preflight that `XYZZY_CORS_ORIGINS` refuses, and a plain top-level navigation
cannot attach one at all. A cookie-authed WebSocket cannot carry that header
either, so its handshake is gated on the `Origin` header matching the
configured allowlist exactly instead
(`src/multiplayer/realtime/websocket.py`), since a script cannot forge that
header on a WebSocket handshake.

**Pre-model screening of untrusted input with per-call provenance fences.**
Text that did not originate from the authenticated caller — another agent's
output, a fetched document, anything from outside this request — is run
through `screen()` before it reaches a model call: control and formatting
characters are stripped and length is bounded
(`MAX_UNTRUSTED_CHARS` in `src/multiplayer/security/screening.py`). `fenced()`
then wraps it in a per-call delimiter that names its source, so the model
sees it labeled as data from a specific origin rather than as an instruction.
This is a deterministic string transform, not a model call, so it cannot fail
open the way a model-based classifier can.

## Data Lifecycle

There is no erasure command today: no CLI verb and no API route deletes a
person's data. This is a known gap, not a policy choice, and it is the
right question for a buyer to ask before this becomes a compliance
dependency.

The honest resolution, when built, is not a hard delete. The hash-chained
event log (`event_chain_hash()` in `src/multiplayer/security/audit.py`)
makes every row load-bearing for every row after it, so removing one breaks
the chain for the whole room. The two positions in tension: a tamper-evident
log wants nothing ever removed, and an erasure request wants a specific
person's data gone. The resolution that keeps both is to pseudonymize the
principal (replace the user id with an opaque token everywhere it appears)
and tombstone the content they authored (replace message/artifact text with
a marker while preserving the stored hash, since the hash covers the
original bytes, not their current display) rather than deleting rows. No
part of this is implemented: there is no `manage.py` command, no API route,
and no tombstone column in any migration today.

## Known Gaps

- **Single-process rate limiting.** `XYZZY_RATE_LIMIT_PER_MINUTE` is enforced
  in process memory; it bounds one server's exposure, not a fleet's.
- **Single-node storage.** SQLite is the only supported backend today, so
  there is no independent second copy of the event log to cross-check
  against.
- **Live model calls are not exercised in CI.** Provider behavior is verified
  with a fake HTTP transport; a real run against the OpenAI Responses API
  needs a server-side credential and is not part of the automated gate.
