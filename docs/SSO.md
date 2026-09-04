# Signing in through an identity provider

SSO is additive. With none of these set the server behaves exactly as before:
bootstrap tokens and `manage token mint`, so a deployment without a provider is
untouched.

| Variable | What it decides |
| --- | --- |
| `XYZZY_OIDC_ISSUER` | The provider's issuer URL. Its configuration is discovered from `{issuer}/.well-known/openid-configuration`. |
| `XYZZY_OIDC_CLIENT_ID` | This deployment's client id. |
| `XYZZY_OIDC_CLIENT_SECRET` | Optional; omit for a public client relying on PKCE alone. |
| `XYZZY_OIDC_REDIRECT_URI` | Where the provider sends the browser back. |
| `XYZZY_OIDC_SCOPES` | Space separated; `openid profile email` by default. |
| `XYZZY_OIDC_POST_LOGOUT_REDIRECTS` | Comma-separated allowlist. A redirect target taken from a request would be an open redirect. |
| `XYZZY_SESSION_IDLE_SECONDS` | Idle clock, 1800 by default (Keycloak's). |
| `XYZZY_SESSION_ABSOLUTE_SECONDS` | Absolute ceiling, 36000 by default (Keycloak's). |
| `XYZZY_SESSION_ACCESS_SECONDS` | How long one access credential lives before it must be refreshed, 300 by default (Keycloak's). |
| `XYZZY_OIDC_ALLOW_UNVERIFIABLE_SESSIONS` | Accept a login from a provider that issues no refresh token. Off by default, because such a session can never be re-checked; when on, it is capped at 15 minutes. |
| `XYZZY_OIDC_PROVIDER_LABEL` | The provider name shown on the sign-in button. `single sign-on` by default. |

`GET /api/v1/auth/login` starts the flow, `GET /api/v1/auth/callback` finishes it
and returns an access token and a refresh token, `POST /api/v1/auth/refresh`
rotates them, `POST /api/v1/auth/logout` ends this session,
`POST /api/v1/auth/logout-everywhere` ends all of them, and
`POST /api/v1/auth/backchannel-logout` accepts the provider's logout token.
Every one of them sits under the `/api/v1` prefix, so `XYZZY_OIDC_REDIRECT_URI`
must too.

Three things worth knowing before you deploy it. A refresh token is spendable
once, and presenting a spent one revokes the entire session rather than that
token: a replay means a copy exists somewhere it should not, and revoking only
the copy leaves whoever holds the original inside. And an SSO login is keyed on
the provider's issuer and subject, never on the email address, so it does **not**
attach to an operator-created account that happens to share an email. Linking
those is a deliberate act; inferring it from a string is how accounts get taken
over. And there is no reuse grace window: a refresh
whose answer is lost cannot be retried, and the person signs in again. A window
was tried and removed, because it let a thief presenting the stolen predecessor
take a working session and leave the victim's own next refresh to be judged the
replay. Keycloak's default is no reuse either.

Every refresh also spends the provider's own refresh token, so a person
disabled, locked out, or password-reset upstream loses this session at the next
rotation rather than at the absolute clock.

The browser itself never sees either token. `GET /api/v1/auth/callback` sets a
cookie only when the request prefers `text/html` (a browser arriving by
redirect); that cookie carries the access token alone, HttpOnly, and expires
with the session's idle clock. Whether it is also `Secure` and `__Host-`
prefixed follows the scheme of the configured `XYZZY_OIDC_REDIRECT_URI`, not
the scheme of the request that reached this process: set it to the `https://`
address the browser sees, even when a reverse proxy or load balancer
terminates TLS in front of XYZZY and forwards plain HTTP, or the cookie is
issued without either protection and a startup warning names the mismatch.
Every other caller (curl, an agent, `refresh`/`logout`) still gets the JSON
body with both tokens, unchanged. A cookie authenticates an HTTP request only
when it also carries header `X-XYZZY-Client: web`, on every method including
GET, which is what keeps a mutating GET like `/auth/end-session` out of CSRF
reach: a cross-origin request cannot attach a custom header without a CORS
preflight `XYZZY_CORS_ORIGINS` refuses, and a top-level navigation cannot
attach one at all. A cookie-authed WebSocket cannot carry that header either,
so it is gated on `Origin` matching `configured_origins()` exactly instead.

**Trying it locally:** `scripts/dev_idp.py` is a throwaway identity provider:
stdlib/FastAPI, one hardcoded user, a fresh RS256 key generated on every start.
It refuses to run unless its own issuer is a loopback host, because it trusts
every caller completely.

```bash
python scripts/dev_idp.py --port 9100
# in another shell
export XYZZY_OIDC_ISSUER="http://127.0.0.1:9100"
export XYZZY_OIDC_CLIENT_ID="dev-client"
export XYZZY_OIDC_REDIRECT_URI="http://127.0.0.1:8000/api/v1/auth/callback"
python -m multiplayer.server
```

Open http://localhost:8000 and sign in through the provider; `XYZZY_DEV_IDP_SUB`,
`XYZZY_DEV_IDP_NAME`, and `XYZZY_DEV_IDP_EMAIL` change the one user's claims.
