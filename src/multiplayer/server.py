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
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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
    set_shutting_down,
)
from .api.share_page import render_share_not_found_page, render_share_page
from .db.connection import Database
from .metrics import Metrics
from .realtime.hub import RealtimeHub
from .realtime.websocket import websocket_endpoint
from .security import (
    AuthorizationError,
    TokenAuthenticator,
    hash_token,
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
# names no outside host. connect-src is 'self' alone: since CSP level 3 that
# keyword also matches a websocket to the page's own host, so ws: and wss:
# would only have widened the policy to every other host.
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "font-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
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


async def _refuse_demo_against_real_tokens(db: Database) -> None:
    """``--demo`` refuses a database already holding a real credential (finding 12).

    The comment on the identity-provider/``XYZZY_AUTH_TOKENS`` check above
    promises this is "refused before the process ever binds a port"; that
    promise held for the environment but not for the database file itself —
    running ``--demo`` against a database a real user already bootstrapped
    ingested the public demo token into it directly. Any live token that is
    not the demo token's own means this file already holds a real identity,
    and demo mode is not this database's to enter. The reverse direction (a
    non-demo start retiring a leftover demo token) needs no code of its own
    here: ``ingest_bootstrap_tokens`` already retires every bootstrap-labelled
    token absent from the configured map (finding 11), and the demo token is
    ingested as one.
    """
    row = await db.fetch_one(
        "SELECT 1 FROM user_tokens WHERE revoked_at IS NULL AND token_hash != ? LIMIT 1",
        (hash_token(DEMO_BEARER_TOKEN),),
    )
    if row is not None:
        raise RuntimeError(
            "XYZZY_DEMO cannot start against a database that already holds a "
            "real bootstrap or minted token"
        )


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


def _cookie_session_key(request: Request) -> str | None:
    """The session cookie's digest, when this deployment has SSO and the
    request carries one. None whenever there is nothing to key by: no SSO
    configured (no cookie flow exists at all), or the cookie is absent.

    Reads ``current_sessions()`` live rather than a value captured at
    app-creation time, the same discipline ``_ws_session_cookie`` already
    uses, so this stays in step with whatever the deployment's SSO
    configuration actually is on this request.
    """
    live = current_sessions()
    if live is None or not live.provider.settings.configured:
        return None
    cookie_name = session_cookie_name(live.provider.settings.redirect_uri.startswith("https://"))
    cookie_value = request.cookies.get(cookie_name)
    if not cookie_value:
        return None
    return "s:" + hashlib.sha256(cookie_value.encode("utf-8")).hexdigest()[:32]


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

    def _bucket_for(self, candidate: str, request: Request) -> str:
        """Reuse ``candidate``'s live bucket, or mint one while its address is
        under the per-address cap; past that cap, the address bucket is what
        it shares instead. The one path both a bearer token and a session
        cookie go through, so rotating either buys nothing once its address
        has minted enough buckets already.
        """
        if candidate in self.windows:
            return candidate
        address = _peer_address_key(request)
        if self._address_token_counts.get(address, 0) >= self._token_cap_per_address:
            return address
        self._token_bucket_address[candidate] = address
        self._address_token_counts[address] = self._address_token_counts.get(address, 0) + 1
        return candidate

    def key_for(self, request: Request) -> str:
        """Who this request's budget counts against.

        A bearer token is the principal when one is present. Absent that, a
        cookie-authenticated (SSO) request is keyed by its session cookie
        (finding 9) rather than the peer address: an address behind a shared
        reverse proxy would otherwise bucket every SSO user in the deployment
        together, so one user's polling could 429 everybody else sharing that
        proxy. Only a request with neither — no credential at all — falls back
        to the address, because there is nothing else to key it by.
        """
        authorization = request.headers.get("authorization", "")
        if authorization:
            token_key = "t:" + hashlib.sha256(authorization.encode("utf-8")).hexdigest()[:32]
            return self._bucket_for(token_key, request)
        cookie_key = _cookie_session_key(request)
        if cookie_key is not None:
            return self._bucket_for(cookie_key, request)
        return _peer_address_key(request)

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


# Set on the ASGI scope (shared, by reference, with every layer below this
# one) the moment a body crosses its cap. `guard`, below, reads it back after
# `call_next` returns — however that call actually ended — and answers 413
# regardless. The indirection exists because a plain exception does not
# survive the trip: raising out of `receive()` unwinds through
# `BaseHTTPMiddleware`'s own internal task group (the one
# `@app.middleware("http")` is built on), which wraps it into an
# `ExceptionGroup` before FastAPI's body-reading code ever gets to recognise
# it, so even an `HTTPException(413, ...)` raised there arrives looking like
# neither an `HTTPException` nor anything else worth keeping (confirmed
# against this exact FastAPI/Starlette pair). A boolean set on a dict two
# layers can both see sidesteps the whole question of what survives that
# unwind.
_BODY_CAP_EXCEEDED_KEY = "xyzzy.body_cap_exceeded"


class _BodyCapMiddleware:
    """Enforce a body-size cap on the bytes an HTTP request actually delivers
    (finding 3), added as a pure ASGI middleware rather than through
    ``@app.middleware("http")``: the latter is Starlette's ``BaseHTTPMiddleware``,
    which still hands the route a body FastAPI has already read in full before
    this ever gets a say. Wrapping ``receive`` here instead means nothing
    downstream — not pydantic, not a multipart parser, not ``request.json()`` —
    ever sees a byte past the cap, whether the request declared its size
    honestly, lied about it, or (being chunked) never declared one at all.

    Added last among this app's ``add_middleware`` calls, which Starlette
    makes the outermost layer: this is the first thing every request meets.
    """

    def __init__(self, app: ASGIApp, limit_for: Callable[[Scope], int]) -> None:
        self._app = app
        self._limit_for = limit_for

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        limit = self._limit_for(scope)
        seen = 0
        exceeded = False

        async def capped_receive() -> Message:
            nonlocal seen, exceeded
            if exceeded:
                # Already over: every further read answers the same way,
                # rather than resuming a body nothing downstream should
                # finish parsing.
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    exceeded = True
                    scope[_BODY_CAP_EXCEEDED_KEY] = True
                    # Not this chunk: whatever is reading the body (Starlette's
                    # Request.stream(), a multipart parser) sees a disconnect
                    # instead, and stops before appending it to anything it is
                    # accumulating in memory. `guard` answers 413 once this
                    # call unwinds, whatever it unwound as.
                    return {"type": "http.disconnect"}
            return message

        await self._app(scope, capped_receive, send)


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
            if demo_requested:
                await _refuse_demo_against_real_tokens(db)
            # A non-demo start retires a leftover demo token as one case of
            # the general rule below: it is bootstrap-labelled and, on this
            # start, absent from the configured map.
            await ingest_bootstrap_tokens(db, auth_tokens)
            if demo_requested:
                await svc.seed_demo_workspace()
            set_service(svc)
            set_authenticator(authenticator)
            set_sessions(sessions)
            set_demo_enabled(demo_requested)
            # Cleared on every startup, not only set on shutdown: this process's
            # module state outlives one create_app() (every test in this
            # process shares it), so a stream opened against a fresh app must
            # not inherit the previous app's shutdown signal.
            set_shutting_down(False)
            sweeper = asyncio.create_task(sweep_run_leases())
            if fanout is not None:
                fanout.start()
            yield
        finally:
            # First, so a long-lived SSE stream (finding 8) notices this app is
            # stopping and ends itself well within uvicorn's grace period
            # instead of only at its next reauth beat, or not at all if
            # nothing ever forces one.
            set_shutting_down(True)
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
        try:
            # The readiness probe and the metrics scrape are exempt: a monitor
            # polling either must not be able to spend the budget of whoever
            # else shares its address. The body-size cap itself is enforced
            # below this middleware, at the ASGI layer (finding 3): the route
            # never sees a byte past it, declared size or not.
            response: Response
            if request.url.path in RATE_LIMIT_EXEMPT_PATHS:
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
            if request.scope.get(_BODY_CAP_EXCEEDED_KEY):
                # `_BodyCapMiddleware` cut the body off with a disconnect
                # rather than a value this route could read further; whatever
                # that unwound as inside FastAPI's own body-reading (a 400, an
                # unrelated parse error) is not the answer for it, so it never
                # reaches the operator-alert path below either.
                response = JSONResponse(
                    status_code=413, content={"detail": "request body too large"}
                )
            else:
                # An unhandled route exception unwinds past here. Count it as
                # the 500 it will become, or the one signal an operator
                # alerts on never fires and the latency histogram silently
                # omits exactly the requests that matter.
                metrics.record_request(request.method, 500, time.monotonic() - request_started)
                raise
        else:
            if request.scope.get(_BODY_CAP_EXCEEDED_KEY):
                # The route ran to a response anyway (a small enough handler
                # can finish before ever noticing the disconnect) — still not
                # its call to make once the cap already tripped.
                response = JSONResponse(
                    status_code=413, content={"detail": "request body too large"}
                )

        metrics.record_request(
            request.method, response.status_code, time.monotonic() - request_started
        )
        response.headers["Content-Security-Policy"] = _CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def _body_cap_for_scope(scope: Scope) -> int:
        is_attachment_upload = scope.get("method") == "POST" and bool(
            _ATTACHMENT_UPLOAD_PATH.match(scope.get("path", ""))
        )
        return max_attachment_bytes() if is_attachment_upload else max_body_bytes

    # Added after CORS and the rate-limit guard above, which Starlette makes
    # this the outermost of the three: it sees a request, and caps its body,
    # before either of them do.
    app.add_middleware(_BodyCapMiddleware, limit_for=_body_cap_for_scope)

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
        # Unset, uvicorn waits forever for every connection to close on its own
        # (finding 8): one open SSE stream on a task that never terminates then
        # holds a graceful stop until the orchestrator's SIGKILL. This is the
        # ceiling on that wait, not the ceiling on a normal request.
        timeout_graceful_shutdown=_positive_int("XYZZY_SHUTDOWN_GRACE_SECONDS", 10),
    )


if __name__ == "__main__":
    main()
