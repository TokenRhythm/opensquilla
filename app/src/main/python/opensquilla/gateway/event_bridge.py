"""EventBridge — emit session events to WebSocket subscribers without RpcContext.

Decouples channel dispatch event broadcasting from the RPC handler layer.
The gateway boot code creates an EventBridge and threads it through
ChannelManager → run_channel_dispatch.
"""

from __future__ import annotations

from typing import Any

import structlog

from opensquilla.engine.types import AgentEvent
from opensquilla.gateway.scopes import operator_scope_satisfies
from opensquilla.gateway.session_streams import get_session_streams

log = structlog.get_logger(__name__)


def bridge_event_name(event: AgentEvent) -> str:
    """Return the canonical ``session.event.*`` name for an engine event.

    Derives the name from the event's ``kind`` discriminator (e.g. a
    :class:`~opensquilla.engine.types.MetaStepStateEvent` whose ``kind`` is
    ``"meta_step_state"`` maps to ``"session.event.meta_step_state"``).

    This single derivation point lets new event types (engine dataclasses
    in :mod:`opensquilla.engine.types`) flow through to WebSocket
    subscribers without per-event hardcoded mappings. Existing call
    sites in ``channel_dispatch`` still use hardcoded strings; future
    refactors can adopt this helper to centralise the mapping.
    """
    return f"session.event.{event.kind}"


class EventBridge:
    """Emit session events to WebSocket subscribers.

    Uses the same ``SubscriptionManager`` and ``ConnectionRegistry`` as
    the RPC path, but without requiring an ``RpcContext``.
    """

    def __init__(self, subscription_manager: Any, connection_registry: Any) -> None:
        self._subs = subscription_manager
        self._registry = connection_registry

    async def emit(
        self,
        session_key: str,
        event_name: str,
        payload: dict[str, Any] | None = None,
        *,
        replay_recorded: bool = False,
    ) -> None:
        """Broadcast an event to all WS connections subscribed to ``session_key``.

        Args:
            session_key: The session key to scope the broadcast.
            event_name: Event type (e.g. ``session.event.text_delta``,
                ``sessions.changed``).
            payload: Event payload dict.
        """
        if self._subs is None:
            return

        try:
            send_payload = payload or {}
            if event_name.startswith("session.event.") and not replay_recorded:
                send_payload = get_session_streams().record(session_key, event_name, send_payload)

            subscriber_ids = self._subs.get_message_subscribers(session_key)
            if event_name.startswith("sessions."):
                subscriber_ids = subscriber_ids | self._subs.get_session_subscribers()
            if not subscriber_ids:
                return

            for conn_id in subscriber_ids:
                conn = self._registry.get(conn_id)
                if conn is not None:
                    try:
                        await conn.send_event(event_name, send_payload)
                    except Exception:
                        log.debug(
                            "event_bridge.send_failed",
                            conn_id=conn_id,
                            event_name=event_name,
                        )
        except Exception as exc:
            log.debug(
                "event_bridge.emit_failed",
                event_name=event_name,
                error_type=type(exc).__name__,
                error=str(exc),
            )

    async def broadcast_scoped(
        self,
        event_name: str,
        payload: dict[str, Any] | None = None,
        *,
        required_scope: str,
    ) -> None:
        """Broadcast an event to every connection whose scopes satisfy ``required_scope``.

        Gateway-wide events (e.g. approval lifecycle pushes) are not tied to
        one session's message subscribers; instead they reach every connected
        operator that could call the matching RPC surface, using the same
        scope-implication rules as request dispatch.
        """
        if self._registry is None:
            return
        try:
            send_payload = payload or {}
            for conn in self._registry.all():
                principal = getattr(conn, "principal", None)
                scopes = getattr(principal, "scopes", None)
                if not scopes or not operator_scope_satisfies(required_scope, scopes):
                    continue
                try:
                    await conn.send_event(event_name, send_payload)
                except Exception:
                    log.debug(
                        "event_bridge.send_failed",
                        conn_id=getattr(conn, "conn_id", None),
                        event_name=event_name,
                    )
        except Exception as exc:
            log.debug(
                "event_bridge.broadcast_failed",
                event_name=event_name,
                error_type=type(exc).__name__,
                error=str(exc),
            )
