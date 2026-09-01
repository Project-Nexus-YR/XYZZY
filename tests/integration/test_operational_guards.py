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


@pytest.mark.asyncio
async def test_metrics_exposes_expected_series_and_advances_after_a_request():
    app = create_app(":memory:", auth_tokens=TOKENS)
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            before = await client.get("/metrics")
            assert before.status_code == 200
            assert before.headers["content-type"].startswith("text/plain")
            before_text = before.text
            for series in (
                "# TYPE xyzzy_http_requests_total counter",
                "# TYPE xyzzy_http_request_seconds histogram",
                "# TYPE xyzzy_rate_limited_total counter",
                "# TYPE xyzzy_websocket_connections gauge",
                "# TYPE xyzzy_build_info gauge",
                'xyzzy_build_info{version="',
            ):
                assert series in before_text

            await client.get("/api/v1/me/context", headers=HEADERS)

            after_text = (await client.get("/metrics")).text
            assert 'xyzzy_http_requests_total{method="GET",status="2xx"}' in after_text


@pytest.mark.asyncio
async def test_metrics_counts_the_429_branch_and_is_itself_rate_limit_exempt(monkeypatch):
    monkeypatch.setenv("XYZZY_RATE_LIMIT_PER_MINUTE", "1")
    app = create_app(":memory:", auth_tokens=TOKENS)
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            await client.get("/api/v1/me/context", headers=HEADERS)
            limited = await client.get("/api/v1/me/context", headers=HEADERS)
            assert limited.status_code == 429

            # /metrics itself must not be spendable, and must not count itself
            # against the same budget the assertion above just exhausted.
            for _ in range(3):
                assert (await client.get("/metrics")).status_code == 200

            text = (await client.get("/metrics")).text
            assert "xyzzy_rate_limited_total 1" in text


@pytest.mark.asyncio
async def test_rotating_bearer_tokens_from_one_address_cannot_buy_infinite_budget(monkeypatch):
    """`_client_key` used to bucket ANY Authorization header by its own hash,
    pre-auth: a script rotating junk tokens bought one fresh 120-request
    budget per value, and the per-IP path never engaged once a header was
    present. Past the per-address cap, unknown tokens from the same address
    now share its bucket instead, so rotation stops buying anything once
    that shared budget is spent.
    """
    import multiplayer.server as server_module

    monkeypatch.setattr(server_module, "TOKEN_BUCKETS_PER_ADDRESS_CAP", 3)
    monkeypatch.setenv("XYZZY_RATE_LIMIT_PER_MINUTE", "2")
    app = create_app(":memory:", auth_tokens=TOKENS)
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            statuses = [
                (
                    await client.get(
                        "/api/v1/me/context", headers={"Authorization": f"Bearer junk-{i}"}
                    )
                ).status_code
                for i in range(20)
            ]
            # Fifty (here twenty, to keep the test fast) distinct tokens from one
            # address must not each buy a fresh 200 the way they used to.
            assert statuses[-1] == 429


def test_the_rate_limit_store_never_exceeds_its_cap(monkeypatch):
    """Eviction used to run only over windows that had already rolled, so a
    live attack — every entry still inside its minute — grew the map by one
    entry per garbage value forever. The store is a hard-capped LRU now: at
    the cap, the least-recently-touched entry goes regardless of window age.
    """
    from multiplayer.server import _RateLimitBuckets

    class _FakeClient:
        def __init__(self, host: str) -> None:
            self.host = host

    class _FakeRequest:
        def __init__(self, host: str) -> None:
            self.client = _FakeClient(host)
            self.headers: dict[str, str] = {}

    max_tracked = 5
    buckets = _RateLimitBuckets(max_tracked=max_tracked, token_cap_per_address=10_000)
    now = 1_000.0
    for i in range(50):
        # A fresh address every time, all still well inside their one-minute
        # window when the next one arrives — nothing here would ever be
        # evicted by a rolled-window-only sweep.
        key = buckets.key_for(_FakeRequest(f"10.0.0.{i}"))
        started, count = buckets.touch(key, now, 60.0)
        buckets.record(key, started, count + 1)
        now += 0.001
        assert len(buckets.windows) <= max_tracked


def test_cors_origins_default_to_loopback_and_refuse_a_wildcard(monkeypatch):
    monkeypatch.delenv("XYZZY_CORS_ORIGINS", raising=False)
    assert configured_origins() == list(DEFAULT_ORIGINS)

    monkeypatch.setenv("XYZZY_CORS_ORIGINS", "https://xyzzy.example, https://admin.example")
    assert configured_origins() == ["https://xyzzy.example", "https://admin.example"]

    # A wildcard with allow_credentials would let any site spend a signed-in session.
    monkeypatch.setenv("XYZZY_CORS_ORIGINS", "*")
    with pytest.raises(RuntimeError):
        configured_origins()
