"""MCP SSE transport client."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import structlog

from opensquilla import __version__
from opensquilla.env import trust_env as _trust_env
from opensquilla.mcp.client import MCPClient
from opensquilla.mcp.types import MCPServerConfig, MCPToolDef, MCPToolResult

log = structlog.get_logger(__name__)


def _http_origin(url: str) -> tuple[str, str, int]:
    """Return a normalized HTTP origin for endpoint-event validation."""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"} or not hostname:
        raise ValueError("MCP SSE endpoint must use a valid HTTP(S) URL")
    default_port = 443 if scheme == "https" else 80
    return scheme, hostname, parsed.port or default_port


class MCPSSEClient(MCPClient):
    """MCP client for the 2024-11-05 HTTP+SSE transport.

    The transport is: open a long-lived ``GET`` SSE stream, receive an
    ``endpoint`` event carrying the session-scoped message URL, POST JSON-RPC
    requests to that URL, and read the responses back off the already-open
    stream. (The earlier implementation POSTed to a guessed sessionless path
    before subscribing and opened a fresh stream per request, so a conformant
    server never answered and ``connect()`` hung.)
    """

    _ENDPOINT_TIMEOUT_SECONDS = 30.0

    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None
        self._request_id = 0
        # Legacy explicit endpoint, used only if no ``endpoint`` event arrives.
        self._legacy_message_endpoint = config.message_endpoint or "/message"
        self._message_url: str | None = None
        self._endpoint_ready = asyncio.Event()
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._stream_ctx: Any = None
        self._closed = False

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    @property
    def _base_url(self) -> str:
        assert self.config.url is not None
        return self.config.url.rstrip("/")

    async def connect(self) -> None:
        """Open the SSE stream, complete the endpoint handshake, initialize."""
        timeout = getattr(self.config, "tool_timeout_seconds", None) or 30.0
        self._client = httpx.AsyncClient(
            trust_env=_trust_env(),
            timeout=httpx.Timeout(timeout, read=None),
        )
        self._closed = False
        self._reader_task = asyncio.create_task(self._read_stream())

        try:
            await asyncio.wait_for(
                self._endpoint_ready.wait(), timeout=self._ENDPOINT_TIMEOUT_SECONDS
            )
        except TimeoutError:
            # No endpoint event: fall back to the legacy explicit path so a
            # server pinned to the old behavior via config still works.
            self._message_url = f"{self._base_url}{self._legacy_message_endpoint}"
            log.debug("mcp.sse.endpoint_fallback", url=self._message_url)

        await self._send_and_receive(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "opensquilla", "version": __version__},
            },
        )
        await self._send_notification("notifications/initialized")

    async def _read_stream(self) -> None:
        """Background task: read the SSE stream, resolve pending responses."""
        assert self._client is not None
        try:
            async with self._client.stream("GET", self._base_url) as response:
                response.raise_for_status()
                event_name = "message"
                data_lines: list[str] = []
                async for line in response.aiter_lines():
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip(" "))
                    elif line == "":
                        if data_lines:
                            self._handle_event(event_name, "".join(data_lines))
                        event_name = "message"
                        data_lines = []
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface to any waiter
            self._fail_pending(exc)
            if not self._endpoint_ready.is_set():
                # Unblock connect()'s wait so it can fall back / error out.
                self._endpoint_ready.set()

    def _handle_event(self, event_name: str, data: str) -> None:
        if event_name == "endpoint":
            # data is a plain URI string (relative or absolute), NOT JSON.
            message_url = urljoin(self._base_url + "/", data.strip())
            if _http_origin(message_url) != _http_origin(self._base_url):
                raise ValueError("MCP SSE endpoint must use the same origin as the SSE URL")
            self._message_url = message_url
            self._endpoint_ready.set()
            return
        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            log.debug("mcp.sse.non_json_event", event=event_name, data=data[:200])
            return
        if not isinstance(message, dict):
            return
        msg_id = message.get("id")
        if isinstance(msg_id, int) and msg_id in self._pending:
            future = self._pending.pop(msg_id)
            if not future.done():
                future.set_result(message)

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    async def close(self) -> None:
        """Cancel the reader and close the HTTP client session."""
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._reader_task = None
        self._fail_pending(ConnectionError("MCP SSE client closed"))
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _send_and_receive(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """POST a JSON-RPC request and await the response from the SSE stream."""
        assert self._client is not None
        if self._message_url is None:
            raise ConnectionError("MCP SSE endpoint handshake did not complete")

        req_id = self._next_id()
        request: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            request["params"] = params

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[req_id] = future

        try:
            resp = await self._client.post(self._message_url, json=request)
            resp.raise_for_status()
            timeout = getattr(self.config, "tool_timeout_seconds", None) or 30.0
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(req_id, None)

    async def _send_notification(self, method: str) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        assert self._client is not None
        if self._message_url is None:
            raise ConnectionError("MCP SSE endpoint handshake did not complete")
        notification: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        resp = await self._client.post(self._message_url, json=notification)
        resp.raise_for_status()

    async def list_tools(self) -> list[MCPToolDef]:
        """List tools from the MCP server."""
        response = await self._send_and_receive("tools/list")
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
        response = await self._send_and_receive(
            "tools/call", {"name": name, "arguments": arguments}
        )

        if "error" in response:
            return MCPToolResult(
                content=response["error"].get("message", "Unknown error"),
                is_error=True,
            )

        result = response.get("result", {})
        content_list = result.get("content", [])
        text = "\n".join(c.get("text", "") for c in content_list if c.get("type") == "text")
        # Honor the MCP result-level ``isError`` flag so a tool-execution
        # failure reaches the agent as an error, not a plain result.
        return MCPToolResult(content=text, is_error=bool(result.get("isError", False)))
