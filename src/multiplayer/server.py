"""Server entry point: wires up FastAPI, WebSocket, and the multiplayer service."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

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


def create_app(
    db_path: str = ":memory:",
    *,
    auth_tokens: dict[str, str] | None = None,
) -> FastAPI:
    db = Database(db_path)
    hub = RealtimeHub()
    if auth_tokens is None:
        raw_tokens = os.environ.get("MULTIAI_AUTH_TOKENS", "{}")
        try:
            configured_tokens = json.loads(raw_tokens)
        except json.JSONDecodeError as exc:
            raise RuntimeError("MULTIAI_AUTH_TOKENS must be a JSON object") from exc
        if not isinstance(configured_tokens, dict):
            raise RuntimeError("MULTIAI_AUTH_TOKENS must be a JSON object")
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
        title="Multiplayer AI Workspace",
        description="Persistent shared workspace for humans and AI agents",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:8000", "http://127.0.0.1:8000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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

    db_path = sys.argv[1] if len(sys.argv) > 1 else "multiplayer.db"
    app = create_app(db_path)
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
