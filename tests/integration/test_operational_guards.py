"""Coverage for the guards that stand between the API and the open internet.

Each one is configuration a deployment gets wrong by default, so each is asserted
at the edge the deployment actually meets: the readiness probe has to read the
database rather than answer from a constant, the body cap has to refuse before
the body is read, and the rate limiter has to count the principal rather than the
route.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from multiplayer.server import DEFAULT_ORIGINS, configured_origins, create_app

TOKENS = {"owner-token": "user_1"}
HEADERS = {"Authorization": "Bearer owner-token"}


@pytest.mark.asyncio
async def test_health_reports_ready_only_while_the_database_answers():
    app = create_app(":memory:", auth_tokens=TOKENS)
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            ready = await client.get("/api/v1/health")
            assert ready.status_code == 200
            assert ready.json() == {"status": "ok"}

    # Outside the lifespan the service reference is cleared, which is the same
    # shape as a process that is listening before its database is open.
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/v1/health")).status_code == 503


@pytest.mark.asyncio
async def test_a_body_larger_than_the_cap_is_refused_before_the_route_runs():
    app = create_app(":memory:", auth_tokens=TOKENS)
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            oversized = await client.post(
                "/api/v1/organizations",
                headers=HEADERS,
                content=b"x" * 1_048_577,
            )
            assert oversized.status_code == 413


@pytest.mark.asyncio
async def test_the_rate_limit_counts_the_principal_and_exempts_the_probe(monkeypatch):
    monkeypatch.setenv("XYZZY_RATE_LIMIT_PER_MINUTE", "3")
    app = create_app(":memory:", auth_tokens=TOKENS)
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            statuses = [
                (await client.get("/api/v1/me/context", headers=HEADERS)).status_code
                for _ in range(4)
            ]
            assert statuses[:3] == [200, 200, 200]
            assert statuses[3] == 429

            # A different bearer token is a different principal with its own budget.
            other = await client.get(
                "/api/v1/me/context", headers={"Authorization": "Bearer unknown-token"}
            )
            assert other.status_code != 429

            # The probe a monitor polls must not be spendable.
            assert (await client.get("/api/v1/health")).status_code == 200


def test_cors_origins_default_to_loopback_and_refuse_a_wildcard(monkeypatch):
    monkeypatch.delenv("XYZZY_CORS_ORIGINS", raising=False)
    assert configured_origins() == list(DEFAULT_ORIGINS)

    monkeypatch.setenv("XYZZY_CORS_ORIGINS", "https://xyzzy.example, https://admin.example")
    assert configured_origins() == ["https://xyzzy.example", "https://admin.example"]

    # A wildcard with allow_credentials would let any site spend a signed-in session.
    monkeypatch.setenv("XYZZY_CORS_ORIGINS", "*")
    with pytest.raises(RuntimeError):
        configured_origins()
