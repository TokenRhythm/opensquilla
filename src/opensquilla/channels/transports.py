"""Shared channel transport protocols.

Transport objects own ingress mechanics. Channel adapters own semantic parsing,
queueing, and outbound delivery.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from starlette.routing import Route

from opensquilla.channels.types import ChannelHealth, IncomingMessage, OutgoingMessage


@dataclass(frozen=True)
class InboundEventEnvelope:
    """Raw inbound event passed from a transport to its channel adapter."""

    source: str
    event_id: str | None
    event_type: str
    raw: dict[str, Any]
    received_at: datetime


InboundEventHandler = Callable[[InboundEventEnvelope], Awaitable[None]]


@runtime_checkable
class InboundTransport(Protocol):
    """Lifecycle contract for channel ingress transports."""

    async def start(self, handler: InboundEventHandler) -> None:
        """Start receiving events and deliver them to ``handler``."""
        ...

    async def stop(self) -> None:
        """Stop receiving events and release transport resources."""
        ...

    async def health_check(self) -> ChannelHealth:
        """Return transport health."""
        ...


@runtime_checkable
class WebhookInboundTransport(InboundTransport, Protocol):
    """Inbound transport that exposes a Starlette route."""

    def create_route(self, path: str | None = None) -> Route:
        """Create a webhook route."""
        ...


@runtime_checkable
class ReplyRoutingChannel(Protocol):
    """Optional adapter hook for replying to the triggering inbound message."""

    def build_reply_message(
        self,
        content: str,
        inbound: IncomingMessage,
    ) -> OutgoingMessage:
        """Build a batch reply for ``inbound``."""
        ...

    def streaming_reply_kwargs(self, inbound: IncomingMessage) -> Mapping[str, Any]:
        """Return keyword args for ``send_streaming`` for ``inbound``."""
        ...
