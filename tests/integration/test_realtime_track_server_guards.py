"""Findings 35, 37, 64, and 45: an unhandled route exception used to unwind
past `record_request`, so exception-driven 500s were never counted in
`/metrics`; no response carried any defense-in-depth security header; and a
red test skipped the lifespan's teardown, leaking the service globals and an
unclosed database connection into the rest of the suite.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

import multiplayer.api.routes as routes_mod
from multiplayer.server import create_app

TOKENS = {"owner-token": "user_1"}
HEADERS = {"Authorization": "Bearer owner-token"}


@pytest.mark.asyncio
async def test_an_unhandled_route_exception_is_still_counted_as_a_500(monkeypatch):
    app = create_app(":memory:", auth_tokens=TOKENS)
    transport = ASGITransport(app=app, raise_app_exceptions=False)

    async with app.router.lifespan_context(app):

        def _boom():
            raise RuntimeError("simulated route failure")

        monkeypatch.setattr(routes_mod, "_svc_or_404", _boom)

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # /me/context authenticates first, then calls _svc_or_404(): a
            # plain RuntimeError there is not caught by any exception_handler,
            # so it reaches the guard middleware unhandled.
            response = await client.get("/api/v1/me/context", headers=HEADERS)
            assert response.status_code == 500

            metrics_text = (await client.get("/metrics")).text
            assert 'xyzzy_http_requests_total{method="GET",status="5xx"}' in metrics_text


@pytest.mark.asyncio
async def test_every_response_carries_the_security_headers():
    app = create_app(":memory:", auth_tokens=TOKENS)
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for path, headers in (
                ("/api/v1/me/context", HEADERS),
                ("/metrics", {}),
                ("/share/does-not-exist", {}),
            ):
                response = await client.get(path, headers=headers)
                assert response.headers["x-frame-options"] == "DENY"
                assert response.headers["x-content-type-options"] == "nosniff"
                assert response.headers["referrer-policy"] == "no-referrer"
                csp = response.headers["content-security-policy"]
                assert "frame-ancestors 'none'" in csp
                assert "object-src 'none'" in csp
                assert "default-src 'self'" in csp


@pytest.mark.asyncio
async def test_share_page_still_renders_with_the_new_headers():
    app = create_app(":memory:", auth_tokens=TOKENS)
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/share/nonexistent-token")
            assert response.status_code == 404
            assert "text/html" in response.headers["content-type"]
            assert response.headers["content-security-policy"]


@pytest.mark.asyncio
async def test_lifespan_teardown_runs_even_when_startup_fails(monkeypatch):
    """A failure inside the lifespan's try block (standing in for any red
    test or genuine startup defect) must still close the database and clear
    the service globals, or a single failure poisons every later test that
    depends on those globals being None.
    """
    app = create_app(":memory:", auth_tokens=TOKENS)

    import multiplayer.services.service as service_module

    async def _boom_initialize(self):
        raise RuntimeError("simulated startup failure")

    monkeypatch.setattr(service_module.MultiplayerService, "initialize", _boom_initialize)

    with pytest.raises(RuntimeError):
        async with app.router.lifespan_context(app):
            pass  # never reached; startup raises before yield

    assert routes_mod._svc is None
    assert routes_mod._sessions is None
