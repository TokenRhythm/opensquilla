"""WebSocket protocol frame types and constants."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

# Protocol version negotiated during handshake
PROTOCOL_VERSION = 3

# Payload limits
MAX_PAYLOAD_BYTES = 26_214_400  # 25 MiB
MAX_BUFFERED_BYTES = 52_428_800  # 50 MiB
MAX_PREAUTH_PAYLOAD_BYTES = 65_536  # 64 KiB

# Timing constants
TICK_INTERVAL_MS = 30_000
HEALTH_REFRESH_INTERVAL_MS = 60_000
PREAUTH_TIMEOUT_MS = 10_000
DEDUPE_TTL_MS = 300_000
DEDUPE_MAX_ENTRIES = 1000

# Graceful shutdown WS close code
WS_CLOSE_SERVICE_RESTART = 1012


# ---------------------------------------------------------------------------
# Client → Server frames
# ---------------------------------------------------------------------------


class ReqFrame(BaseModel):
    """RPC request frame sent by client."""

    type: Literal["req"] = "req"
    id: str
    method: str
    params: Any | None = None


# ---------------------------------------------------------------------------
# Server → Client frames
# ---------------------------------------------------------------------------


class ErrorShape(BaseModel):
    code: str
    message: str
    details: Any | None = None
    retryable: bool | None = None
    retry_after_ms: int | None = None
    accepted: bool | None = None


class ResFrame(BaseModel):
    """RPC response frame sent by server."""

    type: Literal["res"] = "res"
    id: str
    ok: bool
    payload: Any | None = None
    error: ErrorShape | None = None


class StateVersion(BaseModel):
    presence: int = 0
    health: int = 0


class EventFrame(BaseModel):
    """Server-pushed event frame."""

    type: Literal["event"] = "event"
    event: str
    payload: Any | None = None
    meta: dict[str, Any] | None = None
    seq: int | None = None
    state_version: StateVersion | None = None


class PingFrame(BaseModel):
    type: Literal["ping"] = "ping"


class PongFrame(BaseModel):
    type: Literal["pong"] = "pong"


# ---------------------------------------------------------------------------
# Handshake frames
# ---------------------------------------------------------------------------


class ClientInfo(BaseModel):
    id: str
    display_name: str | None = None
    version: str
    platform: str
    device_family: str | None = None
    model_identifier: str | None = None
    mode: str
    instance_id: str | None = None


class ConnectParams(BaseModel):
    min_protocol: int
    max_protocol: int
    client: ClientInfo
    caps: list[str] | None = None
    commands: list[str] | None = None
    permissions: dict[str, bool] | None = None
    path_env: str | None = None
    role: str = "operator"
    scopes: list[str] | None = None
    auth: dict[str, Any] | None = None
    locale: str | None = None
    user_agent: str | None = None


class ServerInfo(BaseModel):
    version: str
    conn_id: str


class FeaturesInfo(BaseModel):
    methods: list[str]
    events: list[str]


class SnapshotInfo(BaseModel):
    presence: list[Any] = []
    health: Any = None
    state_version: StateVersion = StateVersion()
    uptime_ms: int = 0
    config_path: str | None = None
    state_dir: str | None = None
    auth_mode: str | None = None


class PolicyInfo(BaseModel):
    max_payload: int = MAX_PAYLOAD_BYTES
    max_buffered_bytes: int = MAX_BUFFERED_BYTES
    tick_interval_ms: int = TICK_INTERVAL_MS
    agent_stream_heartbeat_interval_ms: int = 15_000
    agent_stream_idle_timeout_ms: int = 600_000
    webui_stream_idle_grace_ms: int = 630_000
    client_ws_keepalive_timeout_ms: int = 120_000


class HelloOk(BaseModel):
    type: Literal["hello-ok"] = "hello-ok"
    protocol: int
    server: ServerInfo
    features: FeaturesInfo
    snapshot: SnapshotInfo
    policy: PolicyInfo
    auth: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------

ERROR_NOT_LINKED = "NOT_LINKED"
ERROR_NOT_PAIRED = "NOT_PAIRED"
ERROR_AGENT_TIMEOUT = "AGENT_TIMEOUT"
ERROR_INVALID_REQUEST = "INVALID_REQUEST"
ERROR_APPROVAL_NOT_FOUND = "APPROVAL_NOT_FOUND"
ERROR_UNAVAILABLE = "UNAVAILABLE"
ERROR_UNAUTHORIZED = "UNAUTHORIZED"
ERROR_NOT_FOUND = "NOT_FOUND"
ERROR_METHOD_NOT_FOUND = "METHOD_NOT_FOUND"


def make_error_res(
    req_id: str,
    code: str,
    message: str,
    retryable: bool = False,
    details: Any | None = None,
    retry_after_ms: int | None = None,
    accepted: bool | None = None,
) -> ResFrame:
    return ResFrame(
        id=req_id,
        ok=False,
        error=ErrorShape(
            code=code,
            message=message,
            retryable=retryable,
            retry_after_ms=retry_after_ms,
            accepted=accepted,
            details=details,
        ),
    )


def make_ok_res(req_id: str, payload: Any = None) -> ResFrame:
    return ResFrame(id=req_id, ok=True, payload=payload)


def make_event(
    event: str,
    payload: Any = None,
    seq: int | None = None,
    meta: dict[str, Any] | None = None,
) -> EventFrame:
    return EventFrame(event=event, payload=payload, seq=seq, meta=meta)
