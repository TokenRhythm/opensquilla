"""Publish session events through the shared replay and subscription runtime.

RPC handlers and background session services both need the same epoch fencing,
replay buffering, and subscriber fan-out.  Keeping that orchestration here
prevents non-RPC services from importing a handler module while preserving the
existing v4 event payloads and ordering.
"""

from __future__ import annotations

from typing import Any, Protocol, cast

import structlog

from opensquilla.contracts.adapters.sessions_changed_contract import (
    SESSIONS_CHANGED_EVENT,
    observe_sessions_changed_payload,
)
from opensquilla.gateway.session_services import (
    get_session_epoch,
    get_session_storage,
    set_session_epoch,
)
from opensquilla.gateway.session_streams import get_session_streams

log = structlog.get_logger(__name__)


class SessionEventBuffer(Protocol):
    """Minimal replay-buffer capability needed by the publisher."""

    def record(
        self,
        session_key: str,
        event_name: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any]: ...


def buffer_session_event(
    session_key: str,
    event_name: str,
    payload: dict[str, Any] | None,
    *,
    streams: object | None = None,
) -> dict[str, Any]:
    """Record replayable message events and pass session projections through."""

    if event_name.startswith("session.event."):
        registry = cast(
            SessionEventBuffer,
            streams if streams is not None else get_session_streams(),
        )
        return registry.record(session_key, event_name, payload)
    return dict(payload or {})


async def prepare_session_event_payload(
    ctx: object,
    session_key: str,
    event_name: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Resolve epoch metadata before an event enters the replay buffer."""

    prepared = dict(payload)
    if event_name.startswith("session.event.") or event_name == "sessions.changed":
        session_manager = getattr(ctx, "session_manager", None)
        cached_epoch = get_session_epoch(session_manager, session_key)
        if cached_epoch is not None:
            prepared["epoch"] = cached_epoch
        else:
            storage = get_session_storage(session_manager)
            if storage is not None and hasattr(storage, "get_epoch"):
                try:
                    epoch = await storage.get_epoch(session_key)
                    set_session_epoch(session_manager, session_key, epoch)
                    prepared["epoch"] = epoch
                except Exception:
                    pass  # best-effort; never block event delivery
    if event_name == SESSIONS_CHANGED_EVENT:
        prepared = observe_sessions_changed_payload(
            prepared,
            source="gateway.rpc_sessions",
            allow_legacy=False,
        )
    return prepared


async def send_prepared_to_subscribers(
    ctx: object,
    session_key: str,
    event_name: str,
    send_payload: dict[str, Any],
) -> None:
    """Broadcast an already-buffered event without mutating replay state."""

    from opensquilla.gateway.websocket import get_registry

    sub_mgr = getattr(ctx, "subscription_manager", None)
    if sub_mgr is None:
        return

    registry = get_registry()
    conn_ids = sub_mgr.get_message_subscribers(session_key)
    if event_name.startswith("sessions."):
        conn_ids = conn_ids | sub_mgr.get_session_subscribers()

    for conn_id in conn_ids:
        conn = registry.get(conn_id)
        if conn is not None:
            try:
                await conn.send_event(event_name, send_payload)
            except Exception:
                log.warning("emit.send_failed", conn_id=conn_id, ws_event=event_name)


async def emit_session_event(
    ctx: object,
    session_key: str,
    event_name: str,
    payload: dict[str, Any],
) -> None:
    """Prepare, replay-buffer, then broadcast one session event."""

    prepared = await prepare_session_event_payload(ctx, session_key, event_name, payload)
    send_payload = buffer_session_event(session_key, event_name, prepared)
    await send_prepared_to_subscribers(ctx, session_key, event_name, send_payload)


__all__ = [
    "buffer_session_event",
    "emit_session_event",
    "prepare_session_event_payload",
    "send_prepared_to_subscribers",
]
