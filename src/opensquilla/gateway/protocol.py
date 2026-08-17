"""WebSocket protocol frame types and constants."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import asdict
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from opensquilla.contracts.gateway_transport import ANSWER_GENERATION_RESET_CAPABILITY
from opensquilla.contracts.turn_execution import StickyExecutionRole
from opensquilla.engine.types import (
    AnswerGenerationResetEvent,
    ControlTerminalEvent,
    ControlTerminalReason,
)
from opensquilla.provider.types import ProviderAttemptFailure
from opensquilla.redaction import redact_error_text

# Protocol version negotiated during handshake
PROTOCOL_VERSION = 4

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
# Typed session-event payloads
# ---------------------------------------------------------------------------


class ProviderAttemptFailureWire(BaseModel):
    """Trusted/internal serialization shape for one provider attempt failure.

    This model is intentionally not included in ``PublicEventPayload``.  It
    exists so trusted coordinators can persist or inspect a redacted failure
    without making provider diagnostics a public session event by default.
    Unknown fields are ignored so a newer internal producer cannot accidentally
    widen the wire shape with a raw provider payload.
    """

    model_config = ConfigDict(extra="ignore")

    kind: Literal["provider_attempt_failure"] = "provider_attempt_failure"
    turn_id: str = ""
    assistant_message_id: str = ""
    role: StickyExecutionRole = StickyExecutionRole.PRIMARY_AGGREGATOR
    logical_call_index: int = Field(default=0, ge=0)
    attempt_index: int = Field(default=0, ge=0)
    lease_id: str = ""
    provider: str = ""
    model: str = ""
    failure_kind: str = "unknown"
    retryable: bool = False
    request_started: bool = False
    safe_message: str = ""
    generation_epoch: int = Field(default=0, ge=0)
    sequence: int = Field(default=0, ge=0)

    @field_validator("safe_message")
    @classmethod
    def _keep_message_safe(cls, value: str) -> str:
        return redact_error_text(value)


class AnswerGenerationResetWire(BaseModel):
    """Public same-message generation replacement payload."""

    model_config = ConfigDict(extra="ignore")

    kind: Literal["answer_generation_reset"] = "answer_generation_reset"
    turn_id: str = ""
    assistant_message_id: str = ""
    old_generation_epoch: int = Field(default=0, ge=0)
    new_generation_epoch: int = Field(default=0, ge=0)
    from_role: StickyExecutionRole = StickyExecutionRole.PRIMARY_AGGREGATOR
    to_role: StickyExecutionRole = StickyExecutionRole.PRIMARY_AGGREGATOR
    safe_reason: str = ""
    preserve_completed_tools: bool = True
    authoritative_text_snapshot: str = ""
    authoritative_reasoning_snapshot: str = ""
    sequence: int = Field(default=0, ge=0)
    terminal: bool = False
    terminal_text_snapshot: str | None = None


class ControlTerminalWire(BaseModel):
    """Public control-owned terminal payload."""

    model_config = ConfigDict(extra="ignore")

    kind: Literal["control_terminal"] = "control_terminal"
    turn_id: str = ""
    assistant_message_id: str = ""
    sequence: int = Field(default=0, ge=0)
    reason: ControlTerminalReason = ControlTerminalReason.CANCEL
    preserve_completed_tools: Literal[True] = True
    terminal: Literal[True] = True


# Naming aliases keep the event-payload terminology available to callers while
# retaining the explicit ``Wire`` names used by this module's frame types.
ProviderAttemptFailurePayload = ProviderAttemptFailureWire
AnswerGenerationResetPayload = AnswerGenerationResetWire
ControlTerminalPayload = ControlTerminalWire

type PublicEventPayload = Annotated[
    AnswerGenerationResetWire | ControlTerminalWire,
    Field(discriminator="kind"),
]
type PublicEvent = AnswerGenerationResetEvent | ControlTerminalEvent

_PUBLIC_EVENT_ADAPTER: TypeAdapter[PublicEventPayload] = TypeAdapter(PublicEventPayload)

_ANSWER_GENERATION_RESET_EVENT = "session.event.answer_generation_reset"
_LEGACY_ERROR_EVENT = "session.event.error"
_INTERNAL_TERMINAL_RESET_FIELDS = frozenset(
    {
        "terminal_error_message",
        "terminal_error_code",
        "terminal_failure_kind",
    }
)


def is_public_event(event: object) -> bool:
    """Return whether an event is allowed on the public session-event plane."""

    return isinstance(event, (AnswerGenerationResetEvent, ControlTerminalEvent))


def serialize_public_event(event: PublicEvent) -> dict[str, Any]:
    """Serialize a public typed event without exposing provider diagnostics."""

    if isinstance(event, AnswerGenerationResetEvent):
        return AnswerGenerationResetWire.model_validate(asdict(event)).model_dump(mode="json")
    if isinstance(event, ControlTerminalEvent):
        return ControlTerminalWire.model_validate(asdict(event)).model_dump(mode="json")
    raise TypeError(f"event is not public: {type(event).__name__}")


def serialize_provider_attempt_failure(event: ProviderAttemptFailure) -> dict[str, Any]:
    """Serialize an internal failure for trusted diagnostics only.

    This is deliberately separate from :func:`serialize_public_event`; a
    caller must opt into the internal shape explicitly.
    """

    if not isinstance(event, ProviderAttemptFailure):
        raise TypeError(f"event is not a ProviderAttemptFailure: {type(event).__name__}")
    return ProviderAttemptFailureWire.model_validate(asdict(event)).model_dump(mode="json")


def deserialize_public_event(payload: Mapping[str, Any]) -> PublicEventPayload:
    """Validate a public typed event payload from a decoded wire frame."""

    return cast(PublicEventPayload, _PUBLIC_EVENT_ADAPTER.validate_python(dict(payload)))


def project_session_event_for_client(
    event_name: str,
    payload: Any,
    *,
    client_caps: Collection[str] | None = None,
) -> tuple[str, Any]:
    """Project one public session event to a client's negotiated capabilities.

    A terminal answer-generation reset is the runtime's sole visible failed
    outcome.  Clients that explicitly understand replacement semantics receive
    that event unchanged (apart from defense-in-depth metadata scrubbing).
    Capability-less clients receive the equivalent legacy ``session.event.error``
    frame instead, never both frames.
    """

    if event_name != _ANSWER_GENERATION_RESET_EVENT or not isinstance(payload, Mapping):
        return event_name, payload

    public_payload = dict(payload)
    for field_name in _INTERNAL_TERMINAL_RESET_FIELDS:
        public_payload.pop(field_name, None)

    if (
        public_payload.get("terminal") is not True
        or ANSWER_GENERATION_RESET_CAPABILITY in (client_caps or ())
    ):
        return event_name, public_payload

    terminal_snapshot = public_payload.get("terminal_text_snapshot")
    authoritative_snapshot = public_payload.get("authoritative_text_snapshot")
    if isinstance(terminal_snapshot, str) and terminal_snapshot.strip():
        message = terminal_snapshot
    elif isinstance(authoritative_snapshot, str) and authoritative_snapshot.strip():
        message = authoritative_snapshot
    else:
        message = "The model could not complete this answer."

    public_payload.pop("kind", None)
    public_payload.update(
        {
            "message": message,
            "terminal_message": message,
            "terminal_reason": "error",
            "error_message": message,
            "code": "ensemble_fixed_error",
        }
    )
    return _LEGACY_ERROR_EVENT, public_payload


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
    concurrent_history_reads: bool = False
    concurrent_optional_read_methods: list[str] = []
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
