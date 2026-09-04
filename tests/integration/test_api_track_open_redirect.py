"""Finding 46: `permits_redirect` (sessions.py) is the only allowlist standing
between an attacker-supplied `redirect_to` and the identity provider's
post-logout redirect, and nothing called it. Deleting the check, inverting
it, or making it always return True left the rest of the suite green.

These two assertions pin the behaviour directly: an unlisted target is
refused, and the configured allowlist entry is accepted and threaded through
to `end_session_url`.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from multiplayer.api import routes
from multiplayer.security.oidc import OidcProvider, OidcSettings
from multiplayer.security.sessions import SessionService
from multiplayer.server import create_app

from ..security.test_sso_session_lifecycle import CLIENT_ID, ISSUER, FakeProvider

TOKENS = {"owner-token": "user_1"}
ALLOWED_REDIRECT = "https://xyzzy.example/bye"


def _configured(idp: FakeProvider) -> SessionService:
    svc = routes._svc
    assert svc is not None
    sessions = SessionService(
        db=svc.db,
        repos=svc.repos,
        provider=OidcProvider(
            settings=OidcSettings(
                issuer=ISSUER,
                client_id=CLIENT_ID,
                client_secret="shh",
                redirect_uri="https://xyzzy.example/callback",
                post_logout_redirects=frozenset({ALLOWED_REDIRECT}),
            )
        ),
    )
    transport = httpx.MockTransport(idp.handler)
    sessions._client = lambda: httpx.AsyncClient(transport=transport)  # type: ignore[method-assign]
    return sessions


async def _sign_in_by_cookie(client: AsyncClient, idp: FakeProvider) -> str:
    login = await client.get("/api/v1/auth/login")
    params = httpx.URL(login.headers["location"]).params
    idp.expect_nonce = params["nonce"]
    login_binding = login.cookies.get("xyzzy_login") or login.cookies.get("__Host-xyzzy_login")

    callback = await client.get(
        "/api/v1/auth/callback",
        params={"state": params["state"], "code": "auth-code"},
        headers={"accept": "text/html,application/xhtml+xml"},
        cookies={"xyzzy_login": login_binding, "__Host-xyzzy_login": login_binding},
    )
    assert callback.status_code == 200, callback.text
    session_cookie = callback.cookies.get("__Host-xyzzy_session") or callback.cookies.get(
        "xyzzy_session"
    )
    assert session_cookie
    return session_cookie


@pytest.mark.asyncio
async def test_end_session_refuses_a_redirect_target_not_on_the_allowlist():
    app = create_app(":memory:", auth_tokens=TOKENS)
    idp = FakeProvider()
    async with app.router.lifespan_context(app):
        routes.set_sessions(_configured(idp))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            session_cookie = await _sign_in_by_cookie(client, idp)
            response = await client.get(
                "/api/v1/auth/end-session",
                params={"redirect_to": "https://evil.example"},
                cookies={"__Host-xyzzy_session": session_cookie},
                headers={"X-XYZZY-Client": "web"},
            )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_end_session_accepts_the_configured_allowlist_entry():
    app = create_app(":memory:", auth_tokens=TOKENS)
    idp = FakeProvider()
    async with app.router.lifespan_context(app):
        routes.set_sessions(_configured(idp))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            session_cookie = await _sign_in_by_cookie(client, idp)
            response = await client.get(
                "/api/v1/auth/end-session",
                params={"redirect_to": ALLOWED_REDIRECT},
                cookies={"__Host-xyzzy_session": session_cookie},
                headers={"X-XYZZY-Client": "web"},
            )
    assert response.status_code == 200
    end_session_url = response.json()["end_session_url"]
    assert httpx.URL(end_session_url).params["post_logout_redirect_uri"] == ALLOWED_REDIRECT
