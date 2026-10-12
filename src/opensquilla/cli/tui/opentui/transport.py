"""Authenticated single-client loopback JSONL transport for the TUI host."""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Mapping
from contextlib import suppress

from opensquilla.cli.tui.opentui.host_runtime import (
    HOST_PROTOCOL_VERSION,
    HostFailureReason,
    HostRuntimeError,
)

_AUTH_MAX_BYTES = 16 * 1024


class HostConnection:
    """One authenticated host connection accepted on loopback only."""

    def __init__(self, *, auth_timeout: float = 5.0) -> None:
        self.auth_timeout = auth_timeout
        self._token = secrets.token_urlsafe(32)
        self._server: asyncio.AbstractServer | None = None
        self._accepted: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] | None = (
            None
        )
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._host = "127.0.0.1"
        self._port = 0

    async def listen(self) -> None:
        if self._server is not None:
            return
        loop = asyncio.get_running_loop()
        self._accepted = loop.create_future()
        try:
            self._server = await asyncio.start_server(self._authenticate, self._host, 0)
        except OSError as exc:
            raise HostRuntimeError(
                f"OpenTUI IPC listener could not start: {exc}",
                reason=HostFailureReason.TRANSPORT,
            ) from exc
        sockets = self._server.sockets or ()
        if not sockets:
            await self.close()
            raise HostRuntimeError(
                "OpenTUI IPC listener did not expose a socket",
                reason=HostFailureReason.TRANSPORT,
            )
        self._port = int(sockets[0].getsockname()[1])

    @property
    def environment(self) -> Mapping[str, str]:
        if self._server is None or self._port <= 0:
            raise HostRuntimeError(
                "OpenTUI IPC listener is not started",
                reason=HostFailureReason.TRANSPORT,
            )
        return {
            "OPENSQUILLA_OPENTUI_IPC_HOST": self._host,
            "OPENSQUILLA_OPENTUI_IPC_PORT": str(self._port),
            "OPENSQUILLA_OPENTUI_IPC_TOKEN": self._token,
            "OPENSQUILLA_OPENTUI_PROTOCOL_VERSION": str(HOST_PROTOCOL_VERSION),
        }

    async def wait_for_client(self, *, timeout: float) -> None:
        accepted = self._accepted
        if accepted is None:
            raise HostRuntimeError(
                "OpenTUI IPC listener is not started",
                reason=HostFailureReason.TRANSPORT,
            )
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.shield(accepted), timeout=timeout
            )
        except TimeoutError as exc:
            raise HostRuntimeError(
                "OpenTUI host did not become ready "
                f"(no authenticated connection within {timeout:.1f}s)",
                reason=HostFailureReason.READY_TIMEOUT,
            ) from exc
        server = self._server
        if server is not None:
            # Python 3.12's Server.wait_closed() also waits for accepted client
            # transports. That would deadlock here because this accepted socket
            # is the live IPC channel we are about to return.
            server.close()
            self._server = None

    async def send_frame(self, frame: str) -> None:
        writer = self._writer
        if writer is None:
            raise HostRuntimeError(
                "OpenTUI IPC transport is not connected",
                reason=HostFailureReason.TRANSPORT,
            )
        try:
            writer.write(frame.encode("utf-8", errors="backslashreplace"))
            await writer.drain()
        except (ConnectionError, OSError, RuntimeError) as exc:
            raise HostRuntimeError(
                "OpenTUI host IPC write failed",
                reason=HostFailureReason.TRANSPORT,
            ) from exc

    async def readline(self) -> str:
        reader = self._reader
        if reader is None:
            raise HostRuntimeError(
                "OpenTUI IPC transport is not connected",
                reason=HostFailureReason.TRANSPORT,
            )
        try:
            raw = await reader.readline()
        except (ConnectionError, OSError, RuntimeError) as exc:
            raise HostRuntimeError(
                "OpenTUI host IPC read failed",
                reason=HostFailureReason.TRANSPORT,
            ) from exc
        return raw.decode("utf-8", errors="replace")

    async def close(self) -> None:
        server = self._server
        if server is not None:
            server.close()
            await server.wait_closed()
            self._server = None
        writer = self._writer
        if writer is not None:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
        self._reader = None
        self._writer = None
        accepted = self._accepted
        if accepted is not None and not accepted.done():
            accepted.cancel()
        self._accepted = None

    async def _authenticate(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        accepted = self._accepted
        if accepted is None or accepted.done():
            await _reject(writer, "connection already claimed")
            return
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=self.auth_timeout)
            if not raw or len(raw) > _AUTH_MAX_BYTES:
                raise ValueError("invalid auth frame")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("invalid auth frame")
            token = payload.get("token")
            protocol = payload.get("protocol")
            if payload.get("type") != "auth" or not isinstance(token, str):
                raise ValueError("invalid auth frame")
            if not secrets.compare_digest(token, self._token):
                raise ValueError("authentication failed")
            if protocol != HOST_PROTOCOL_VERSION:
                raise ValueError("protocol mismatch")
            writer.write(
                (
                    json.dumps(
                        {"type": "auth.ok", "protocol": HOST_PROTOCOL_VERSION},
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            accepted.set_result((reader, writer))
        except (TimeoutError, ValueError, json.JSONDecodeError, ConnectionError, OSError):
            await _reject(writer, "authentication failed")


async def _reject(writer: asyncio.StreamWriter, message: str) -> None:
    with suppress(Exception):
        writer.write((json.dumps({"type": "auth.error", "message": message}) + "\n").encode())
        await writer.drain()
    writer.close()
    with suppress(Exception):
        await writer.wait_closed()
