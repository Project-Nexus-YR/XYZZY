"""Server entry point: wires up FastAPI, WebSocket, and the multiplayer service."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api.a2a import router as a2a_router
from .api.routes import current_sessions, router, set_authenticator, set_service, set_sessions
from .db.connection import Database
from .metrics import Metrics
from .realtime.hub import RealtimeHub
from .realtime.websocket import websocket_endpoint
from .security import (
    AuthorizationError,
    TokenAuthenticator,
    ingest_bootstrap_tokens,
    session_cookie_name,
)
from .security.oidc import OidcProvider, settings_from_environment
from .security.sessions import SessionService
from .security.sessions import settings_from_environment as session_settings
from .services.service import MultiplayerService

log = logging.getLogger(__name__)

# How often the run-lease sweep runs while the process is up. Long enough that it is
# not a poll loop, short enough that a dead harness is described the same shift.
RUN_LEASE_SWEEP_SECONDS = 60.0

RATE_LIMIT_WINDOW_SECONDS = 60.0
DEFAULT_RATE_LIMIT = 120
DEFAULT_MAX_BODY_BYTES = 1_048_576
MAX_TRACKED_CLIENTS = 10_000
DEFAULT_ORIGINS = ("http://localhost:8000", "http://127.0.0.1:8000")
# Probes exempt from the rate limiter: a monitor polling either must not be
# able to spend the budget of whoever else shares its address.
RATE_LIMIT_EXEMPT_PATHS = frozenset({"/api/v1/health", "/metrics"})


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a whole number") from exc
    if value < 1:
        raise RuntimeError(f"{name} must be at least 1")
    return value


def configured_origins() -> list[str]:
    """The browser origins allowed to call the API.

    Unset keeps the two loopback origins the bundled client is served from, so a
    local run needs no configuration. A deployment behind a real hostname has to
    say so: a wildcard here would pair with `allow_credentials` to let any site
    spend a signed-in user's session.
    """
    configured = [
        origin.strip()
        for origin in os.environ.get("XYZZY_CORS_ORIGINS", "").split(",")
        if origin.strip()
    ]
    if "*" in configured:
        raise RuntimeError("XYZZY_CORS_ORIGINS must name origins; '*' is refused with credentials")
    return configured or list(DEFAULT_ORIGINS)


def _client_key(request: Request) -> str:
    """Who the rate limit counts against.

    The bearer token is the principal, so count against it where there is one and
    fall back to the peer address. The address is the proxy's when a proxy is in
    front, which is why the token is preferred rather than the other way round.
    """
    authorization = request.headers.get("authorization", "")
    if authorization:
        return "t:" + hashlib.sha256(authorization.encode("utf-8")).hexdigest()[:32]
    return "a:" + (request.client.host if request.client else "unknown")


def create_app(
    db_path: str = ":memory:",
    *,
    auth_tokens: dict[str, str] | None = None,
) -> FastAPI:
    db = Database(db_path)
    hub = RealtimeHub()
    metrics = Metrics(version=__version__)
    if auth_tokens is None:
        raw_tokens = os.environ.get("XYZZY_AUTH_TOKENS", "{}")
        try:
            configured_tokens = json.loads(raw_tokens)
        except json.JSONDecodeError as exc:
            raise RuntimeError("XYZZY_AUTH_TOKENS must be a JSON object") from exc
        if not isinstance(configured_tokens, dict):
            raise RuntimeError("XYZZY_AUTH_TOKENS must be a JSON object")
        auth_tokens = {str(token): str(user_id) for token, user_id in configured_tokens.items()}
    svc = MultiplayerService(db, hub, known_users=frozenset(auth_tokens.values()))
    sessions = SessionService(
        db=db,
        repos=svc.repos,
        provider=OidcProvider(settings=settings_from_environment()),
        settings=session_settings(),
    )
    # The authenticator pushes the idle clock as a side effect of authenticating,
    # which is the only way a clock that measures inactivity can be kept honest.
    authenticator = TokenAuthenticator(db, sessions.note_used)

    async def sweep_run_leases() -> None:
        """A run is settled, holds a live lease, or is swept. Startup is not enough:
        a process that stays up long enough to outlive its own leases has to sweep
        them, or a run whose harness died mid-shift waits for the next restart."""
        while True:
            await asyncio.sleep(RUN_LEASE_SWEEP_SECONDS)
            try:
                await svc.sweep_expired_run_leases()
            except Exception:
                log.exception("Run lease sweep failed")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await db.connect()
        await svc.initialize()
        await ingest_bootstrap_tokens(db, auth_tokens)
        set_service(svc)
        set_authenticator(authenticator)
        set_sessions(sessions)
        sweeper = asyncio.create_task(sweep_run_leases())
        yield
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper
        set_sessions(None)
        set_authenticator(None)
        set_service(None)
        await db.close()

    app = FastAPI(
        title="XYZZY",
        description="Persistent shared workspace for humans and AI agents",
        version=__version__,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=configured_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # One process, one set of counters: this bounds a single server's exposure,
    # not a fleet's. The window resets on the minute rather than sliding, which
    # is enough to stop a script and is not a fair-share scheduler.
    rate_limit = _positive_int("XYZZY_RATE_LIMIT_PER_MINUTE", DEFAULT_RATE_LIMIT)
    max_body_bytes = _positive_int("XYZZY_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES)
    windows: dict[str, tuple[float, int]] = {}

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_started = time.monotonic()
        declared = request.headers.get("content-length", "")
        # A request that declares its size is refused before the body is read. A
        # chunked request declares nothing, so this caps the honest case only.
        if declared.isdigit() and int(declared) > max_body_bytes:
            response: Response = JSONResponse(
                status_code=413, content={"detail": "request body too large"}
            )
        # The readiness probe and the metrics scrape are exempt: a monitor
        # polling either must not be able to spend the budget of whoever else
        # shares its address.
        elif request.url.path in RATE_LIMIT_EXEMPT_PATHS:
            response = await call_next(request)
        else:
            key = _client_key(request)
            now = time.monotonic()
            started, count = windows.get(key, (now, 0))
            if now - started >= RATE_LIMIT_WINDOW_SECONDS:
                started, count = now, 0
            if count >= rate_limit:
                metrics.record_rate_limited()
                response = JSONResponse(
                    status_code=429,
                    content={"detail": "rate limit exceeded"},
                    headers={
                        "Retry-After": str(int(RATE_LIMIT_WINDOW_SECONDS - (now - started)) + 1)
                    },
                )
            else:
                windows[key] = (started, count + 1)
                if len(windows) > MAX_TRACKED_CLIENTS:
                    # A key is dead the moment its window rolls. Dropping the
                    # rolled ones is what keeps an unbounded client population
                    # from being a leak.
                    for stale in [
                        k for k, (at, _) in windows.items() if now - at >= RATE_LIMIT_WINDOW_SECONDS
                    ]:
                        del windows[stale]
                response = await call_next(request)

        metrics.record_request(
            request.method, response.status_code, time.monotonic() - request_started
        )
        return response

    app.include_router(router)
    # The A2A surface is rooted rather than under /api/v1: its endpoint path and
    # its well-known card path are both fixed by a specification this product
    # does not get to renumber.
    app.include_router(a2a_router)

    @app.exception_handler(AuthorizationError)
    async def forbidden(_request: Request, exc: AuthorizationError) -> JSONResponse:
        # Raised inside a write transaction when membership changed under the request.
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    def _ws_session_cookie() -> str | None:
        # Read at connection time, not app-creation time: a deployment's SSO
        # configuration is fixed at startup, but resolving it here rather than
        # once keeps this in step with `_current_user`, which reads the same
        # live sessions service on every request. None when SSO is
        # unconfigured — there is no cookie flow at all in that case, so the
        # WebSocket path skips cookie auth entirely rather than guessing at a
        # scheme nothing configured.
        live = current_sessions()
        if live is None or not live.provider.settings.configured:
            return None
        return session_cookie_name(live.provider.settings.redirect_uri.startswith("https://"))

    @app.websocket("/ws")
    async def ws_route(websocket: WebSocket) -> None:
        await websocket_endpoint(
            websocket,
            hub,
            authenticator,
            svc.authorization,
            configured_origins(),
            _ws_session_cookie(),
        )

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        # Single-process gauge: the count of subscriptions live in this
        # server's own hub, not a fleet-wide total.
        metrics.set_websocket_connections(await hub.subscriber_count())
        return Response(content=metrics.render(), media_type="text/plain; version=0.0.4")

    # Serve the web UI
    static_dir = Path(__file__).parent.parent.parent / "web"
    if static_dir.exists():

        @app.get("/")
        async def serve_ui() -> FileResponse:
            # no-cache means revalidate, not never-store: the browser keeps a copy
            # but asks before using it, so a deploy is visible on the next load
            # instead of whenever the heuristic cache happens to expire.
            return FileResponse(
                str(static_dir / "index.html"),
                headers={"Cache-Control": "no-cache"},
            )

        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=os.environ.get("XYZZY_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db_path = sys.argv[1] if len(sys.argv) > 1 else "multiplayer.db"
    app = create_app(db_path)
    # Loopback stays the default: a process that binds every interface because
    # nobody configured it is a deployment decision made by omission.
    uvicorn.run(
        app,
        host=os.environ.get("XYZZY_HOST", "127.0.0.1"),
        port=_positive_int("XYZZY_PORT", 8000),
    )


if __name__ == "__main__":
    main()
