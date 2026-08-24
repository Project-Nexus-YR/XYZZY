"""The sign-in endpoints, driven over HTTP.

The lifecycle itself is covered in tests/security/test_sso_session_lifecycle.py.
What is asserted here is the part only the route layer can get wrong: that a
deployment with no identity provider says so instead of failing, that the
redirect actually carries the PKCE parameters, and that the back-channel
endpoint reads a form body the way the specification sends one.
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


def _configured(idp: FakeProvider) -> SessionService:
    """Point a configured session service at the running app's own database.

    A second :memory: database would be a different, empty one — the migrations
    that create these tables ran on the app's.
    """
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
            )
        ),
    )
    transport = httpx.MockTransport(idp.handler)
    sessions._client = lambda: httpx.AsyncClient(transport=transport)  # type: ignore[method-assign]
    return sessions


@pytest.mark.asyncio
async def test_a_deployment_with_no_provider_says_so_rather_than_failing():
    app = create_app(":memory:", auth_tokens=TOKENS)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            answer = await client.get("/api/v1/auth/login")
            assert answer.status_code == 501
            assert "identity provider" in answer.json()["detail"]


@pytest.mark.asyncio
async def test_the_login_redirect_carries_the_pkce_parameters():
    app = create_app(":memory:", auth_tokens=TOKENS)
    idp = FakeProvider()
    async with app.router.lifespan_context(app):
        routes.set_sessions(_configured(idp))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            answer = await client.get("/api/v1/auth/login")

    assert answer.status_code == 307
    params = httpx.URL(answer.headers["location"]).params
    assert params["code_challenge_method"] == "S256"
    assert params["client_id"] == CLIENT_ID
    assert params["nonce"] and params["state"]

    # The browser is given something only it holds, so that the callback can
    # tell the browser that started this login from one that merely knows state.
    cookie = answer.headers["set-cookie"]
    assert "xyzzy_login=" in cookie
    assert "httponly" in cookie.lower()
    # Lax, not Strict: the browser returns by a cross-site redirect from the
    # provider, and Strict withholds the cookie at exactly that moment.
    assert "samesite=lax" in cookie.lower()


@pytest.mark.asyncio
async def test_the_back_channel_endpoint_reads_a_form_body_and_refuses_an_empty_one():
    app = create_app(":memory:", auth_tokens=TOKENS)
    idp = FakeProvider()
    async with app.router.lifespan_context(app):
        routes.set_sessions(_configured(idp))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            empty = await client.post("/api/v1/auth/backchannel-logout", content=b"")
            # A well-formed token naming no session of ours revokes nothing and
            # still answers 204: the provider is not owed our session inventory.
            accepted = await client.post(
                "/api/v1/auth/backchannel-logout",
                content=f"logout_token={idp.logout_token()}".encode(),
                headers={"content-type": "application/x-www-form-urlencoded"},
            )

    assert empty.status_code == 400
    assert accepted.status_code == 204


@pytest.mark.asyncio
async def test_signing_out_a_credential_that_is_not_a_session_is_refused_plainly():
    app = create_app(":memory:", auth_tokens=TOKENS)
    idp = FakeProvider()
    async with app.router.lifespan_context(app):
        routes.set_sessions(_configured(idp))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            answer = await client.post(
                "/api/v1/auth/logout", headers={"Authorization": "Bearer owner-token"}
            )

    assert answer.status_code == 400
    assert "not a sign-in session" in answer.json()["detail"]
