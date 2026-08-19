"""Server entry point: wires up FastAPI, WebSocket, and the multiplayer service."""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api.routes import router, set_service
from .db.connection import Database
from .realtime.hub import RealtimeHub
from .realtime.websocket import websocket_endpoint
from .services.service import MultiplayerService

log = logging.getLogger(__name__)


def create_app(db_path: str = ":memory:") -> FastAPI:
    db = Database(db_path)
    hub = RealtimeHub()
    svc = MultiplayerService(db, hub)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.connect()
        await svc.initialize()
        set_service(svc)
        yield
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

    @app.websocket("/ws")
    async def ws_route(websocket: WebSocket):
        await websocket_endpoint(websocket, hub)

    # Serve the web UI
    static_dir = Path(__file__).parent.parent.parent / "web"
    if static_dir.exists():
        @app.get("/")
        async def serve_ui():
            return FileResponse(str(static_dir / "index.html"))

        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    return app


def main():
    import uvicorn
    db_path = sys.argv[1] if len(sys.argv) > 1 else "multiplayer.db"
    app = create_app(db_path)
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
