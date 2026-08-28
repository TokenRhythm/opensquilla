"""MCP stdio transport client."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, cast

import structlog

from opensquilla import __version__
from opensquilla.mcp.client import MCPClient
from opensquilla.mcp.types import MCPServerConfig, MCPToolDef, MCPToolResult

log = structlog.get_logger(__name__)


class MCPStdioClient(MCPClient):
    """MCP client using the stdio transport.

    The MCP stdio transport (protocolVersion 2024-11-05) frames each JSON-RPC
    message as one line of UTF-8, LF-terminated, with no embedded newlines and
    no headers. (The earlier LSP-style ``Content-Length`` framing this client
    used is a different protocol and no conformant MCP server answers it.)
    """

    _CLOSE_TIMEOUT_SECONDS = 2.0

    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(config)
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._request_lock = asyncio.Lock()

    @staticmethod
    def _encode_request(request: dict[str, Any]) -> bytes:
        """Encode a JSON-RPC message as one LF-terminated line.

        ``json.dumps`` with default separators never emits a literal newline, so
        the message is guaranteed to occupy exactly one line as the transport
        requires.
        """
        return (json.dumps(request) + "\n").encode()

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def connect(self) -> None:
        """Spawn the subprocess and perform MCP initialization handshake."""
        assert self.config.command is not None, "stdio transport requires command"

        env: dict[str, str] | None = None
        if self.config.env:
            env = {**os.environ, **self.config.env}

        self._process = await asyncio.create_subprocess_exec(
            self.config.command,
            *self.config.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            env=env,
        )

        # MCP initialize handshake
        await self._send_request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "opensquilla", "version": __version__},
            },
        )
        # Send initialized notification
        await self._send_notification("notifications/initialized")

    async def close(self) -> None:
        """Terminate the subprocess."""
        process = self._process
        self._process = None
        if process is None:
            return
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
            await process.wait()

    async def _send_request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and read the matching response."""
        if self._process is None or self._process.stdin is None or self._process.stdout is None:
            raise ConnectionError("MCP stdio client is not connected")

        # One coroutine must own both the write and its matching read. Without
        # this lock, concurrent callers can each consume and discard the other
        # request's response while waiting for their own id.
        async with self._request_lock:
            req_id = self._next_id()
            request: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
            if params is not None:
                request["params"] = params

            encoded = self._encode_request(request)
            self._process.stdin.write(encoded)
            await self._process.stdin.drain()

            return await self._read_response(req_id)

    async def _send_notification(self, method: str) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        assert self._process is not None
        assert self._process.stdin is not None

        notification = {"jsonrpc": "2.0", "method": method}
        encoded = self._encode_request(notification)
        self._process.stdin.write(encoded)
        await self._process.stdin.drain()

    async def _read_response(self, expected_id: int) -> dict[str, Any]:
        """Read newline-delimited JSON-RPC messages until the response arrives.

        Server-initiated notifications and requests (messages with no ``id``, or
        with a ``method`` key) are skipped so they cannot be mistaken for the
        response to ``expected_id``.
        """
        assert self._process is not None
        assert self._process.stdout is not None

        while True:
            line = await self._process.stdout.readline()
            if not line:
                raise ConnectionError("MCP stdio server closed the connection")
            try:
                text = line.decode("utf-8").strip()
            except UnicodeDecodeError:
                log.debug("mcp.stdio.invalid_utf8_line")
                continue
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                log.debug("mcp.stdio.non_json_line", line=text[:200])
                continue
            if not isinstance(message, dict):
                continue
            # Skip server-initiated notifications/requests (no id, or a method).
            if "method" in message and "id" not in message:
                log.debug("mcp.stdio.notification", method=message.get("method"))
                continue
            if message.get("id") != expected_id:
                # A response to a different id (or a server request). Ignore it
                # rather than return it for the wrong call.
                continue
            return cast(dict[str, Any], message)

    async def list_tools(self) -> list[MCPToolDef]:
        """List tools from the MCP server."""
        response = await self._send_request("tools/list")
        tools_data = response.get("result", {}).get("tools", [])
        return [
            MCPToolDef(
                name=t["name"],
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
            )
            for t in tools_data
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult:
        """Call a tool on the MCP server."""
        response = await self._send_request("tools/call", {"name": name, "arguments": arguments})

        if "error" in response:
            return MCPToolResult(
                content=response["error"].get("message", "Unknown error"),
                is_error=True,
            )

        result = response.get("result", {})
        content_list = result.get("content", [])
        text = "\n".join(c.get("text", "") for c in content_list if c.get("type") == "text")
        # The MCP result-level ``isError`` flag signals a tool-execution failure;
        # propagate it so the agent sees the error instead of a plain result.
        return MCPToolResult(content=text, is_error=bool(result.get("isError", False)))
