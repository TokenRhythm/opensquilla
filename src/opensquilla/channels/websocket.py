"""WebSocketChannel: channel adapter wrapping a Gateway WsConnection."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import structlog

from opensquilla.channels.types import IncomingMessage, OutgoingMessage
from opensquilla.gateway.websocket import WsConnection

log = structlog.get_logger(__name__)


@dataclass
class WebSocketChannel:
    """Channel adapter that wraps a Gateway WsConnection.

    Inbound messages arrive via an asyncio.Queue populated by the caller
    (e.g. the gateway message loop) using ``enqueue``.
    """

    conn: WsConnection
    channel_id: str = "websocket"
    sender_id: str = "ws-client"
    _queue: asyncio.Queue[IncomingMessage] = field(
        default_factory=asyncio.Queue, init=False, repr=False
    )

    def enqueue(self, message: IncomingMessage) -> None:
        """Push an inbound message into the receive queue."""
        self._queue.put_nowait(message)

    async def receive(self) -> IncomingMessage:
        """Block until an inbound message is available."""
        msg = await self._queue.get()
        log.debug("ws_channel.receive", conn_id=self.conn.conn_id, content=msg.content[:80])
        return msg

    async def send(self, message: OutgoingMessage) -> None:
        """Send an outbound message over the WebSocket connection."""
        payload: dict[str, Any] = {"content": message.content}
        if message.reply_to:
            payload["reply_to"] = message.reply_to
        if message.attachments:
            payload["attachments"] = [a.model_dump() for a in message.attachments]
        if message.metadata:
            payload["metadata"] = message.metadata

        await self.conn.send_event("channel.message", payload)
        log.debug("ws_channel.send", conn_id=self.conn.conn_id, content=message.content[:80])

    async def edit(self, message_id: str, content: str) -> None:
        """Send an edit event over the WebSocket connection."""
        await self.conn.send_event("channel.edit", {"message_id": message_id, "content": content})
        log.debug("ws_channel.edit", conn_id=self.conn.conn_id, message_id=message_id)

    async def delete(self, message_id: str) -> None:
        """Send a delete event over the WebSocket connection."""
        await self.conn.send_event("channel.delete", {"message_id": message_id})
        log.debug("ws_channel.delete", conn_id=self.conn.conn_id, message_id=message_id)

    # ------------------------------------------------------------------
    # Convenience: build IncomingMessage from raw WS frame payload
    # ------------------------------------------------------------------

    def parse_incoming(self, payload: dict[str, Any]) -> IncomingMessage:
        """Parse a raw WS payload dict into IncomingMessage."""
        return IncomingMessage(
            sender_id=payload.get("sender_id", self.sender_id),
            channel_id=payload.get("channel_id", self.channel_id),
            content=payload.get("content", ""),
            attachments=payload.get("attachments", []),
            metadata=payload.get("metadata", {}),
        )
