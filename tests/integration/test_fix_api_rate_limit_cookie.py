"""Finding 9: a cookie-authenticated (SSO) request used to be keyed by peer
address, so every SSO user behind one reverse proxy shared a single 429
budget. It must be keyed by the session cookie instead, the same way a
bearer token is keyed by itself, falling back to the address only when
neither is present.

The cookie need not name a real, live session for this: the rate limiter
keys purely on the credential's identity, before anything checks whether it
authenticates. A raw cookie value plays that role here, the same way the
existing operational-guards test uses an unrecognised bearer token to prove
per-token keying without needing that token to be valid.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from multiplayer.server import create_app

TOKENS = {"owner-token": "user_1"}


@pytest.fixture(autouse=True)
def _sso_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    # http (not https) redirect_uri, so the session cookie this deployment
    # sets is the plain "xyzzy_session" name, not "__Host-xyzzy_session".
    monkeypatch.setenv("XYZZY_OIDC_ISSUER", "https://idp.example")
    monkeypatch.setenv("XYZZY_OIDC_CLIENT_ID", "client-1")
    monkeypatch.setenv("XYZZY_OIDC_CLIENT_SECRET", "shh")
    monkeypatch.setenv("XYZZY_OIDC_REDIRECT_URI", "http://x/callback")


async def test_cookie_authenticated_requests_are_keyed_by_session_not_address(monkeypatch):
    """Fails before the fix: two different session cookies, no Authorization
    header, from the same client (hence the same peer address) shared one
    429 budget; after the fix each cookie has its own.
    """
    monkeypatch.setenv("XYZZY_RATE_LIMIT_PER_MINUTE", "3")
    app = create_app(":memory:", auth_tokens=TOKENS)
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={"xyzzy_session": "session-cookie-a"},
        ) as client_a:
            statuses_a = [(await client_a.get("/api/v1/me/context")).status_code for _ in range(4)]
        assert statuses_a[:3] != [429, 429, 429]
        assert statuses_a[3] == 429

        # A different session cookie is a different principal, sharing
        # neither the first cookie's budget nor the bare address's.
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies={"xyzzy_session": "session-cookie-b"},
        ) as client_b:
            other = await client_b.get("/api/v1/me/context")
        assert other.status_code != 429


async def test_an_unauthenticated_request_still_falls_back_to_the_address(monkeypatch):
    """No Authorization header and no session cookie: nothing to key by but
    the address, same as before this fix for a deployment with no SSO
    configured at all.
    """
    monkeypatch.setenv("XYZZY_RATE_LIMIT_PER_MINUTE", "3")
    app = create_app(":memory:", auth_tokens=TOKENS)
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            statuses = [(await client.get("/api/v1/me/context")).status_code for _ in range(4)]
            assert statuses[3] == 429
