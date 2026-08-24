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
from .api.routes import router, set_authenticator, set_service
from .db.connection import Database
from .realtime.hub import RealtimeHub
from .realtime.websocket import websocket_endpoint
from .security import AuthorizationError, TokenAuthenticator, ingest_bootstrap_tokens
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
    if auth_tokens is None:
        raw_tokens = os.environ.get("XYZZY_AUTH_TOKENS", "{}")
        try:
            configured_tokens = json.loads(raw_tokens)
        except json.JSONDecodeError as exc:
            raise RuntimeError("XYZZY_AUTH_TOKENS must be a JSON object") from exc
        if not isinstance(configured_tokens, dict):
            raise RuntimeError("XYZZY_AUTH_TOKENS must be a JSON object")
        auth_tokens = {str(token): str(user_id) for token, user_id in configured_tokens.items()}
    authenticator = TokenAuthenticator(db)
    svc = MultiplayerService(db, hub, known_users=frozenset(auth_tokens.values()))

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
        sweeper = asyncio.create_task(sweep_run_leases())
        yield
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper
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
        declared = request.headers.get("content-length", "")
        # A request that declares its size is refused before the body is read. A
        # chunked request declares nothing, so this caps the honest case only.
        if declared.isdigit() and int(declared) > max_body_bytes:
            return JSONResponse(status_code=413, content={"detail": "request body too large"})
        # The readiness probe is exempt: a monitor polling it must not be able to
        # spend the budget of whoever else shares its address.
        if request.url.path == "/api/v1/health":
            return await call_next(request)
        key = _client_key(request)
        now = time.monotonic()
        started, count = windows.get(key, (now, 0))
        if now - started >= RATE_LIMIT_WINDOW_SECONDS:
            started, count = now, 0
        if count >= rate_limit:
            return JSONResponse(
                status_code=429,
                content={"detail": "rate limit exceeded"},
                headers={"Retry-After": str(int(RATE_LIMIT_WINDOW_SECONDS - (now - started)) + 1)},
            )
        windows[key] = (started, count + 1)
        if len(windows) > MAX_TRACKED_CLIENTS:
            # A key is dead the moment its window rolls. Dropping the rolled ones
            # is what keeps an unbounded client population from being a leak.
            for stale in [
                k for k, (at, _) in windows.items() if now - at >= RATE_LIMIT_WINDOW_SECONDS
            ]:
                del windows[stale]

        return await call_next(request)

    app.include_router(router)

    @app.exception_handler(AuthorizationError)
    async def forbidden(_request: Request, exc: AuthorizationError) -> JSONResponse:
        # Raised inside a write transaction when membership changed under the request.
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.websocket("/ws")
    async def ws_route(websocket: WebSocket) -> None:
        await websocket_endpoint(websocket, hub, authenticator, svc.authorization)

    # Serve the web UI
    static_dir = Path(__file__).parent.parent.parent / "web"
    if static_dir.exists():

        @app.get("/")
        async def serve_ui() -> FileResponse:
            return FileResponse(str(static_dir / "index.html"))

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
