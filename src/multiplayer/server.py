"""Server entry point: wires up FastAPI, WebSocket, and the multiplayer service."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.resources
import json
import logging
import os
import re
import sys
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api.a2a import router as a2a_router
from .api.routes import (
    current_sessions,
    max_attachment_bytes,
    router,
    set_authenticator,
    set_demo_enabled,
    set_service,
    set_sessions,
)
from .api.share_page import render_share_not_found_page, render_share_page
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
from .services.service import DEMO_USER_ID, MultiplayerService

log = logging.getLogger(__name__)

# How often the run-lease sweep runs while the process is up. Long enough that it is
# not a poll loop, short enough that a dead harness is described the same shift.
RUN_LEASE_SWEEP_SECONDS = 60.0

RATE_LIMIT_WINDOW_SECONDS = 60.0
DEFAULT_RATE_LIMIT = 120
DEFAULT_MAX_BODY_BYTES = 1_048_576
MAX_TRACKED_CLIENTS = 10_000
# A bearer header buckets by its own hash pre-auth, deliberately: the limiter
# runs in middleware, ahead of any code that could tell a real token from
# junk. Without a ceiling that becomes a mint — a script rotating garbage
# Authorization values buys itself one fresh 120-request budget per value,
# and the per-IP path never engages because an address with a header never
# takes it. Past this many distinct token buckets, further unknown tokens
# from the same address share its address bucket instead: rotation stops
# buying anything once the address's own budget is what is left to spend.
TOKEN_BUCKETS_PER_ADDRESS_CAP = 20
DEFAULT_ORIGINS = ("http://localhost:8000", "http://127.0.0.1:8000")
# Probes exempt from the rate limiter: a monitor polling either must not be
# able to spend the budget of whoever else shares its address.
RATE_LIMIT_EXEMPT_PATHS = frozenset({"/api/v1/health", "/metrics"})
# The one route allowed past the general body cap, and only up to its own —
# larger — cap: an attachment upload is legitimately bigger than any other
# request body this API accepts, so it needs its own ceiling, not none at all.
_ATTACHMENT_UPLOAD_PATH = re.compile(r"^/api/v1/rooms/[^/]+/attachments$")

# The one bearer token a demo deployment ever issues, mapped through the same
# XYZZY_AUTH_TOKENS machinery every other deployment uses — demo mode adds no
# new authentication path, only a fixed value on the existing one.
DEMO_BEARER_TOKEN = "demo"

# Defense in depth behind the escaping the app shell and the share page both
# rely on for member-authored content. The client carries no inline script,
# no inline style and no on* attribute (round 2 moved every one of them to
# app.js and app.css), so script-src and style-src need only 'self': a loaded
# remote script or an injected style attribute is refused, not merely logged.
# Fonts are vendored under web/fonts/ and served same origin, so font-src
# names no outside host. connect-src carries ws: and wss: for the realtime
# socket, which is not same-scheme as the https/http page that opens it.
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "font-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self' ws: wss:; "
    "frame-ancestors 'none'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


def resolve_static_dir() -> Path:
    """Where the bundled web client lives, editable install or not.

    The client is package data under ``multiplayer/web`` (see pyproject's
    ``[tool.setuptools.package-data]``), so ``importlib.resources`` finds it
    the same way whether this package is an editable checkout (the files sit
    on disk under ``src/multiplayer/web``) or a normal, non-editable install
    (the files were copied into site-packages alongside the code). Neither
    case is ever a zipped wheel here, so the returned traversable is always a
    real path on disk, safe to hand to ``StaticFiles`` and ``FileResponse``.
    """
    return Path(str(importlib.resources.files("multiplayer") / "web"))


def _demo_mode_requested() -> bool:
    """XYZZY_DEMO=1 (or any value but empty/"0"/"false") turns on the solo on-ramp."""
    raw = os.environ.get("XYZZY_DEMO", "").strip().lower()
    return raw not in ("", "0", "false")


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


def _peer_address_key(request: Request) -> str:
    return "a:" + (request.client.host if request.client else "unknown")


class _RateLimitBuckets:
    """The rate limiter's whole mutable state: bounded always, minted for junk never.

    ``windows`` is an insertion/access-ordered map — every touch moves its key
    to the end, so the front is always the true least-recently-used entry —
    capped at MAX_TRACKED_CLIENTS regardless of whether any window has rolled.
    Left alone, a rolled-window-only sweep never fires against a live attack:
    50 rotated Authorization values inside one minute are all still "live" by
    that test, so the map would grow by one entry per garbage value forever.

    ``_token_bucket_address`` and ``_address_token_counts`` are the other
    half: which peer address minted each live token bucket, and how many it
    has minted, so a cap can be enforced per address before a bucket is even
    created rather than after the map has already grown.
    """

    def __init__(self, max_tracked: int, token_cap_per_address: int) -> None:
        self._max_tracked = max_tracked
        self._token_cap_per_address = token_cap_per_address
        self.windows: OrderedDict[str, tuple[float, int]] = OrderedDict()
        self._token_bucket_address: dict[str, str] = {}
        self._address_token_counts: dict[str, int] = {}

    def _evict(self, key: str) -> None:
        self.windows.pop(key, None)
        address = self._token_bucket_address.pop(key, None)
        if address is not None:
            remaining = self._address_token_counts.get(address, 1) - 1
            if remaining <= 0:
                self._address_token_counts.pop(address, None)
            else:
                self._address_token_counts[address] = remaining

    def key_for(self, request: Request) -> str:
        """Who this request's budget counts against.

        The bearer token is the principal, so an already-tracked token keeps
        counting against its own bucket. A token seen for the first time only
        mints a fresh bucket while its address is under the cap; past it, the
        address bucket is what further unknown tokens from that address
        share, which is what stops rotation from buying fresh budget. The
        address is the proxy's when a proxy is in front, which is why the
        token is preferred at all rather than the other way round.
        """
        authorization = request.headers.get("authorization", "")
        if not authorization:
            return _peer_address_key(request)
        token_key = "t:" + hashlib.sha256(authorization.encode("utf-8")).hexdigest()[:32]
        if token_key in self.windows:
            return token_key
        address = _peer_address_key(request)
        if self._address_token_counts.get(address, 0) >= self._token_cap_per_address:
            return address
        self._token_bucket_address[token_key] = address
        self._address_token_counts[address] = self._address_token_counts.get(address, 0) + 1
        return token_key

    def touch(self, key: str, now: float, window_seconds: float) -> tuple[float, int]:
        """This key's (window_start, count) as of now, freshly moved to the LRU end."""
        started, count = self.windows.get(key, (now, 0))
        if now - started >= window_seconds:
            started, count = now, 0
        self.windows[key] = (started, count)
        self.windows.move_to_end(key)
        return started, count

    def record(self, key: str, started: float, count: int) -> None:
        self.windows[key] = (started, count)
        self.windows.move_to_end(key)
        self._enforce_cap()

    def _enforce_cap(self) -> None:
        if len(self.windows) <= self._max_tracked:
            return
        now = time.monotonic()
        # A key is dead the moment its window rolls; those go first.
        for stale in [
            k for k, (at, _) in self.windows.items() if now - at >= RATE_LIMIT_WINDOW_SECONDS
        ]:
            self._evict(stale)
        # Still over the cap after every rolled window is gone means the map
        # is bounded by rotation alone — every entry still live. The map is
        # hard-capped regardless: the least-recently-touched entry goes,
        # window age or not, because bounded-always is the property that
        # matters, not which entries happened to earn their spot honestly.
        while len(self.windows) > self._max_tracked:
            oldest_key = next(iter(self.windows))
            self._evict(oldest_key)


def create_app(
    db_path: str = ":memory:",
    *,
    auth_tokens: dict[str, str] | None = None,
    demo: bool | None = None,
) -> FastAPI:
    db = Database(db_path)
    metrics = Metrics(version=__version__)

    # Absent (the default): one process, exactly today's behavior, and no
    # redis import ever happens. Present: the hub gains a cross-process
    # fan-out layer and presence moves from process memory to Redis TTL keys.
    # See src/multiplayer/realtime/fanout.py for the guarantee this upholds.
    redis_url = os.environ.get("XYZZY_REDIS_URL", "").strip()
    hub = RealtimeHub(metrics=metrics)
    fanout = None
    presence_redis = None
    if redis_url:
        import redis.asyncio as redis_asyncio

        from .realtime.fanout import RedisFanout

        presence_redis = redis_asyncio.from_url(redis_url)
        fanout = RedisFanout(presence_redis, hub, metrics=metrics)
        hub.attach_fanout(fanout)
    demo_requested = _demo_mode_requested() if demo is None else demo
    if auth_tokens is None:
        raw_tokens = os.environ.get("XYZZY_AUTH_TOKENS", "{}")
        try:
            configured_tokens = json.loads(raw_tokens)
        except json.JSONDecodeError as exc:
            raise RuntimeError("XYZZY_AUTH_TOKENS must be a JSON object") from exc
        if not isinstance(configured_tokens, dict):
            raise RuntimeError("XYZZY_AUTH_TOKENS must be a JSON object")
        auth_tokens = {str(token): str(user_id) for token, user_id in configured_tokens.items()}
    if demo_requested:
        # Demo entry is a one-click credential into a workspace nobody else set
        # up. Bolting it onto a deployment that already trusts a real identity
        # provider or a real token list would hand that one-click entry the same
        # standing those grant — this is refused before the process ever binds
        # a port, rather than left to be discovered in a security review.
        if settings_from_environment().configured or "XYZZY_AUTH_TOKENS" in os.environ:
            raise RuntimeError(
                "XYZZY_DEMO cannot be combined with a configured identity provider "
                "or XYZZY_AUTH_TOKENS"
            )
        auth_tokens = {DEMO_BEARER_TOKEN: DEMO_USER_ID}
    svc = MultiplayerService(
        db,
        hub,
        known_users=frozenset(auth_tokens.values()),
        presence_redis=presence_redis,
        metrics=metrics,
    )
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
            try:
                # Presence entries nobody heartbeats any more would otherwise
                # sit in the in-memory map for the life of the process.
                await svc.presence.cleanup_stale()
            except Exception:
                log.exception("Presence sweep failed")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await db.connect()
        sweeper: asyncio.Task[None] | None = None
        try:
            await svc.initialize()
            await ingest_bootstrap_tokens(db, auth_tokens)
            if demo_requested:
                await svc.seed_demo_workspace()
            set_service(svc)
            set_authenticator(authenticator)
            set_sessions(sessions)
            set_demo_enabled(demo_requested)
            sweeper = asyncio.create_task(sweep_run_leases())
            if fanout is not None:
                fanout.start()
            yield
        finally:
            # A red test (or a real startup failure) must not skip this: the
            # service globals and the sweeper otherwise outlive the database
            # they point at, and every later test sharing this process sees
            # a service whose in-memory database was never closed.
            if fanout is not None:
                await fanout.stop()
            if sweeper is not None:
                sweeper.cancel()
                with suppress(asyncio.CancelledError):
                    await sweeper
            # Every A2A dispatch svc scheduled fire-and-forget (see
            # dispatch_agent_task_in_background) still holds the database this
            # shutdown is about to close; cancelling here is what keeps one from
            # racing db.close() below and dying mid-write instead of cleanly.
            for running in list(svc._background_tasks):
                running.cancel()
            for running in list(svc._background_tasks):
                with suppress(asyncio.CancelledError):
                    await running
            set_demo_enabled(False)
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
    buckets = _RateLimitBuckets(MAX_TRACKED_CLIENTS, TOKEN_BUCKETS_PER_ADDRESS_CAP)

    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_started = time.monotonic()
        declared = request.headers.get("content-length", "")
        is_attachment_upload = request.method == "POST" and bool(
            _ATTACHMENT_UPLOAD_PATH.match(request.url.path)
        )
        body_limit = max_attachment_bytes() if is_attachment_upload else max_body_bytes
        try:
            # A request that declares its size is refused before the body is
            # read. A chunked request declares nothing, so this caps the
            # honest case only.
            if declared.isdigit() and int(declared) > body_limit:
                response: Response = JSONResponse(
                    status_code=413, content={"detail": "request body too large"}
                )
            # The readiness probe and the metrics scrape are exempt: a monitor
            # polling either must not be able to spend the budget of whoever
            # else shares its address.
            elif request.url.path in RATE_LIMIT_EXEMPT_PATHS:
                response = await call_next(request)
            else:
                key = buckets.key_for(request)
                now = time.monotonic()
                started, count = buckets.touch(key, now, RATE_LIMIT_WINDOW_SECONDS)
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
                    buckets.record(key, started, count + 1)
                    response = await call_next(request)
        except Exception:
            # An unhandled route exception unwinds past here. Count it as the
            # 500 it will become, or the one signal an operator alerts on
            # never fires and the latency histogram silently omits exactly
            # the requests that matter.
            metrics.record_request(request.method, 500, time.monotonic() - request_started)
            raise

        metrics.record_request(
            request.method, response.status_code, time.monotonic() - request_started
        )
        response.headers["Content-Security-Policy"] = _CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
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
            svc,
            configured_origins(),
            _ws_session_cookie(),
            presence=svc.presence,
        )

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        # Single-process gauge: the count of subscriptions live in this
        # server's own hub, not a fleet-wide total.
        metrics.set_websocket_connections(await hub.subscriber_count())
        return Response(content=metrics.render(), media_type="text/plain; version=0.0.4")

    @app.get("/share/{token}")
    async def share_page(token: str) -> Response:
        """The growth object: unauthenticated, rate-limited like any other route
        (it is not in RATE_LIMIT_EXEMPT_PATHS), and answers the same 404 page for
        an unknown token, a revoked one, and a malformed one — nothing about the
        room this artifact lives in is visible from the difference.
        """
        resolved = await svc.resolve_public_share(token)
        if resolved is None:
            return HTMLResponse(render_share_not_found_page(), status_code=404)
        artifact, version = resolved
        return HTMLResponse(
            render_share_page(
                title=artifact.name,
                content=version.content,
                published_at=version.created_at.date().isoformat(),
            )
        )

    # Serve the web UI
    static_dir = resolve_static_dir()
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
    argv = [arg for arg in sys.argv[1:] if arg != "--demo"]
    # A bare flag, not an env var, so `xyzzy --demo` needs no shell-specific
    # export syntax to try in under two minutes. None (not False) when the flag
    # is absent, so XYZZY_DEMO in the environment still decides on its own.
    demo = True if "--demo" in sys.argv[1:] else None
    db_path = argv[0] if argv else "multiplayer.db"
    app = create_app(db_path, demo=demo)
    # Loopback stays the default: a process that binds every interface because
    # nobody configured it is a deployment decision made by omission.
    uvicorn.run(
        app,
        host=os.environ.get("XYZZY_HOST", "127.0.0.1"),
        port=_positive_int("XYZZY_PORT", 8000),
    )


if __name__ == "__main__":
    main()
