"""Isolated real WebSocket auth boundary for browser credential recovery tests."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute

from opensquilla.gateway.config import AuthConfig, GatewayConfig
from opensquilla.gateway.rpc import get_dispatcher
from opensquilla.gateway.websocket import (
    SubscriptionManager,
    get_registry,
    handle_ws_connection,
)
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage

TOKEN = "synthetic-browser-gateway-token"


async def main() -> None:
    origin, state_path, mode = sys.argv[1:]
    state = Path(state_path)
    state.mkdir(parents=True, exist_ok=True)
    config = GatewayConfig(
        host="127.0.0.1",
        state_dir=str(state),
        auth=AuthConfig(mode=mode, token=TOKEN),
    )
    config.cors.allowed_origins = [origin]
    storage = await SessionStorage.open(str(state / "sessions.db"))
    manager = SessionManager(storage)
    subscriptions = SubscriptionManager()
    release_handshake = asyncio.Event()

    async def websocket(ws) -> None:
        if ws.query_params.get("holdHandshake") == "1":
            await release_handshake.wait()
        await handle_ws_connection(
            ws,
            config,
            dispatcher=get_dispatcher(),
            session_manager=manager,
            subscription_manager=subscriptions,
        )

    async def enable_token(_request):
        config.auth = AuthConfig(mode="token", token=TOKEN)
        # A configuration restart applies the new auth policy on fresh handshakes.
        for connection in get_registry().all():
            await connection.close(code=1012, reason="synthetic_auth_configuration_restart")
        return JSONResponse({"mode": "token"})

    async def release_connection(_request):
        release_handshake.set()
        return JSONResponse({"released": True})

    async def reconnect(_request):
        for connection in get_registry().all():
            await connection.close(code=1012, reason="synthetic_connection_restart")
        return JSONResponse({"restarted": True})

    app = Starlette(routes=[
        WebSocketRoute("/ws", websocket),
        Route("/enable-token", enable_token, methods=["POST"]),
        Route("/release-handshake", release_connection, methods=["POST"]),
        Route("/reconnect", reconnect, methods=["POST"]),
    ])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if task.done():
                await task
                raise RuntimeError("Auth fixture stopped before becoming ready")
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        print(json.dumps({"authFixturePort": port}), flush=True)
        await task
    finally:
        server.should_exit = True
        await storage.close()


if __name__ == "__main__":
    asyncio.run(main())
