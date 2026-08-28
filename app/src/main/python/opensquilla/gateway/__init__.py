"""opensquilla.gateway — ASGI Gateway with WebSocket support, middleware, and RPC."""

from opensquilla.gateway.app import create_gateway_app
from opensquilla.gateway.boot import (
    GatewayServer,
    ServiceContainer,
    build_services,
    build_turn_runner_from_services,
    start_gateway_server,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.control_ui import create_control_ui_routes
from opensquilla.gateway.protocol import (
    PROTOCOL_VERSION,
    ConnectParams,
    ErrorShape,
    EventFrame,
    HelloOk,
    PingFrame,
    PongFrame,
    ReqFrame,
    ResFrame,
    make_error_res,
    make_event,
    make_ok_res,
)
from opensquilla.gateway.rpc import RpcContext, RpcDispatcher, get_dispatcher
from opensquilla.gateway.websocket import ConnectionRegistry, WsConnection, get_registry

__all__ = [
    # App factory
    "create_gateway_app",
    # Boot
    "GatewayServer",
    "ServiceContainer",
    "build_services",
    "build_turn_runner_from_services",
    "start_gateway_server",
    # Config
    "GatewayConfig",
    # Control UI
    "create_control_ui_routes",
    # Protocol frames
    "PROTOCOL_VERSION",
    "ReqFrame",
    "ResFrame",
    "EventFrame",
    "PingFrame",
    "PongFrame",
    "ConnectParams",
    "HelloOk",
    "ErrorShape",
    "make_ok_res",
    "make_error_res",
    "make_event",
    # RPC
    "RpcContext",
    "RpcDispatcher",
    "get_dispatcher",
    # WebSocket
    "WsConnection",
    "ConnectionRegistry",
    "get_registry",
]
