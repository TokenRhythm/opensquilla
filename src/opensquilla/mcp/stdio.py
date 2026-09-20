"""SDK stdio client with a byte limit enforced before JSON parsing."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from opensquilla import __version__
from opensquilla.mcp.sdk_client import SDKMCPClient
from opensquilla.mcp.types import MCPServerConfig


class MCPStdioClient(SDKMCPClient):
    """Let the SDK own the protocol over bounded, LF-delimited subprocess pipes."""

    _CLOSE_TIMEOUT_SECONDS = 2.0
    # Count UTF-8 bytes before parsing, excluding LF (but including any CR).
    _MAX_MESSAGE_BYTES = 16 * 1024 * 1024

    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(config)
        self._process: asyncio.subprocess.Process | None = None
        self._readahead = b""

    def _make_sdk_client(self) -> Any:
        from mcp import Client
        from mcp.types import Implementation

        return Client(
            self._transport(),
            mode="auto",
            cache=None,
            read_timeout_seconds=self.config.tool_timeout_seconds,
            client_info=Implementation(name="opensquilla", version=__version__),
        )

    @asynccontextmanager
    async def _transport(self) -> AsyncIterator[tuple[Any, Any]]:
        import anyio
        from mcp.shared.message import SessionMessage
        from mcp.types import jsonrpc_message_adapter

        if not self.config.command:
            raise ValueError("stdio transport requires command")
        self._readahead = b""
        # Shield spawn until the child reference is retained and cleanup installed.
        try:
            with anyio.CancelScope(shield=True):
                self._process = await asyncio.create_subprocess_exec(
                    self.config.command,
                    *self.config.args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    env={**os.environ, **self.config.env},
                )
            process = self._process
            assert process is not None
            assert process.stdin is not None
            read_send, read_receive = anyio.create_memory_object_stream[SessionMessage | Exception](
                0
            )
            write_send, write_receive = anyio.create_memory_object_stream[SessionMessage](0)

            async def read_stdout() -> None:
                async with read_send:
                    try:
                        while line := await self._readline_safe():
                            if not line.strip():
                                continue
                            try:
                                message = jsonrpc_message_adapter.validate_json(line, by_name=False)
                            except ValueError as exc:
                                await read_send.send(exc)
                                continue
                            await read_send.send(SessionMessage(message))
                    except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                        pass
                    except Exception as exc:
                        await read_send.send(exc)
                    finally:
                        with anyio.CancelScope(shield=True):
                            await self._terminate_process()

            async def write_stdin() -> None:
                assert process.stdin is not None
                try:
                    async with write_receive:
                        async for message in write_receive:
                            payload = message.message.model_dump_json(
                                by_alias=True, exclude_unset=True
                            )
                            process.stdin.write((payload + "\n").encode("utf-8"))
                            await process.stdin.drain()
                except (anyio.ClosedResourceError, anyio.BrokenResourceError, OSError):
                    # Wake pending SDK requests immediately on a broken pipe.
                    await read_send.aclose()

            async with read_receive, write_send, anyio.create_task_group() as tasks:
                tasks.start_soon(read_stdout)
                tasks.start_soon(write_stdin)
                try:
                    yield read_receive, write_send
                finally:
                    tasks.cancel_scope.cancel()
        finally:
            with anyio.CancelScope(shield=True):
                await self._terminate_process()

    async def _terminate_process(self) -> None:
        """Terminate and reap the direct child, including during cancellation."""
        import anyio

        with anyio.CancelScope(shield=True):
            process = self._process
            self._process = None
            self._readahead = b""
            if process is None:
                return

            # A full StreamReader buffer pauses the pipe transport. Waiting for
            # child exit without draining it can leave wait() blocked and the
            # stdout file descriptor open after the child has already died.
            # Discard raw bytes only; an invalid frame is never parsed again.
            async def drain_stdout() -> None:
                if process.stdout is not None:
                    while await process.stdout.read(8192):
                        pass

            drain = asyncio.create_task(drain_stdout())
            try:
                if process.returncode is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=self._CLOSE_TIMEOUT_SECONDS)
                except TimeoutError:
                    if process.returncode is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                    await asyncio.wait_for(process.wait(), timeout=self._CLOSE_TIMEOUT_SECONDS)
            finally:
                # Normally wait() observes EOF after the drain. Bound this join
                # as well: a descendant outside our direct-child ownership may
                # have inherited stdout and kept it open.
                try:
                    await asyncio.wait_for(drain, timeout=self._CLOSE_TIMEOUT_SECONDS)
                except TimeoutError:
                    pass

    async def _readline_safe(self) -> bytes:
        """Read one bounded frame, retaining LF to distinguish it from EOF."""
        if self._process is None or self._process.stdout is None:
            raise ConnectionError("MCP stdio client is not connected")
        stdout = self._process.stdout
        chunks: list[bytes] = []
        size = 0
        data = self._readahead
        self._readahead = b""
        while True:
            if not data:
                data = await stdout.read(8192)
            if not data:
                return b"".join(chunks)
            nl_idx = data.find(b"\n")
            size += nl_idx if nl_idx >= 0 else len(data)
            if size > self._MAX_MESSAGE_BYTES:
                await self._terminate_process()
                raise ValueError(f"MCP stdio message exceeds {self._MAX_MESSAGE_BYTES} byte limit")
            if nl_idx >= 0:
                chunks.append(data[: nl_idx + 1])
                self._readahead = data[nl_idx + 1 :]
                return b"".join(chunks)
            chunks.append(data)
            data = b""
