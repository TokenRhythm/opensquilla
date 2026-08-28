"""TerminalChannel: interactive stdin/stdout channel adapter."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field

import structlog

from opensquilla.channels.types import IncomingMessage, OutgoingMessage

log = structlog.get_logger(__name__)


@dataclass
class TerminalChannel:
    """Channel adapter for interactive terminal (stdin/stdout)."""

    channel_id: str = "terminal"
    sender_id: str = "user"
    _reader: asyncio.StreamReader | None = field(default=None, init=False, repr=False)
    _reader_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def _get_reader(self) -> asyncio.StreamReader:
        async with self._reader_lock:
            if self._reader is None:
                loop = asyncio.get_event_loop()
                reader = asyncio.StreamReader()
                protocol = asyncio.StreamReaderProtocol(reader)
                await loop.connect_read_pipe(lambda: protocol, sys.stdin)
                self._reader = reader
            return self._reader

    async def receive(self) -> IncomingMessage:
        """Read one line from stdin and return as IncomingMessage."""
        reader = await self._get_reader()
        line_bytes = await reader.readline()
        content = line_bytes.decode(errors="replace").rstrip("\n")
        log.debug("terminal.receive", content=content[:80])
        return IncomingMessage(
            sender_id=self.sender_id,
            channel_id=self.channel_id,
            content=content,
        )

    async def send(self, message: OutgoingMessage) -> None:
        """Write message content to stdout."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._write_stdout, message.content)
        log.debug("terminal.send", content=message.content[:80])

    async def edit(self, message_id: str, content: str) -> None:
        """Edit is not supported on terminal; re-print with prefix."""
        prefix = f"[edit:{message_id}] "
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._write_stdout, prefix + content)
        log.debug("terminal.edit", message_id=message_id)

    async def delete(self, message_id: str) -> None:
        """Delete is not supported on terminal; print a notice."""
        notice = f"[deleted:{message_id}]\n"
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._write_stdout, notice)
        log.debug("terminal.delete", message_id=message_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _write_stdout(text: str) -> None:
        if not text.endswith("\n"):
            text += "\n"
        sys.stdout.write(text)
        sys.stdout.flush()
