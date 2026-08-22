"""Server entry point: wires up FastAPI, WebSocket, and the multiplayer service."""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router, set_authenticator, set_service
from .db.connection import Database
from .realtime.hub import RealtimeHub
from .realtime.websocket import websocket_endpoint
from .security import AuthorizationError, TokenAuthenticator
from .services.service import MultiplayerService

log = logging.getLogger(__name__)


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
    authenticator = TokenAuthenticator(auth_tokens)
    svc = MultiplayerService(db, hub, known_users=frozenset(auth_tokens.values()))

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await db.connect()
        await svc.initialize()
        set_service(svc)
        set_authenticator(authenticator)
        yield
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
