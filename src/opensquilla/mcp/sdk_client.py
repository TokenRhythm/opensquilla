"""Product adapter for the official MCP SDK's client and session lifecycle."""

from __future__ import annotations

import asyncio
import json
from abc import abstractmethod
from typing import Any

import anyio
import structlog
from pydantic import ValidationError

from opensquilla.mcp.client import MCPClient
from opensquilla.mcp.types import MCPServerConfig, MCPToolDef, MCPToolResult

log = structlog.get_logger(__name__)


class SDKMCPClient(MCPClient):
    """Keep SDK context managers in one task while callers use ordinary methods.

    Gateway discovery runs inside ``asyncio.wait_for`` and shutdown runs in a
    different task. AnyIO task groups must enter and exit in the same task, so
    neither public method owns the SDK's context managers directly.
    """

    def __init__(self, config: MCPServerConfig) -> None:
        super().__init__(config)
        self._lifetime_task: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._stop: asyncio.Event | None = None
        self._scope: anyio.CancelScope | None = None
        self._sdk_client: Any = None
        self._failure: BaseException | None = None

    @abstractmethod
    def _make_sdk_client(self) -> Any:
        """Lazily construct a fresh SDK Client for the selected transport."""

    async def connect(self) -> None:
        if self._lifetime_task is None or self._lifetime_task.done():
            self._ready = asyncio.get_running_loop().create_future()
            self._stop = asyncio.Event()
            self._failure = None
            self._lifetime_task = asyncio.create_task(
                self._run_client(self._ready, self._stop),
                name=f"mcp-client:{self.config.name}",
            )
        elif self._stop is not None and self._stop.is_set():
            raise ConnectionError("MCP client is closing")
        assert self._ready is not None
        task = self._lifetime_task
        ready = self._ready
        try:
            await asyncio.shield(ready)
        except BaseException:
            # Cancelling discovery must not leave a connecting subprocess or
            # HTTP stream behind, nor cancel the task that owns SDK teardown.
            # An older failed waiter must not close a replacement connection.
            # Enter close directly so it captures this owner before any yield;
            # close itself shields the lifetime task while awaiting teardown.
            if task is self._lifetime_task:
                await self.close()
            raise

    async def _run_client(self, ready: asyncio.Future[None], stop: asyncio.Event) -> None:
        try:
            with anyio.CancelScope() as scope:
                self._scope = scope
                if not stop.is_set():
                    async with self._make_sdk_client() as client:
                        self._sdk_client = client
                        ready.set_result(None)
                        await stop.wait()
        except BaseException as exc:
            self._failure = exc
            if not ready.done():
                ready.set_exception(exc)
            elif not isinstance(exc, asyncio.CancelledError):
                log.warning(
                    "mcp.client.lifecycle_failed",
                    server=self.config.name,
                    error_type=type(exc).__name__,
                )
        finally:
            self._sdk_client = None
            self._scope = None
            if not ready.done():
                ready.set_exception(ConnectionError("MCP client closed before connecting"))

    async def close(self) -> None:
        task = self._lifetime_task
        ready = self._ready
        if task is None:
            return
        assert self._stop is not None
        self._stop.set()
        if self._sdk_client is None and self._scope is not None:
            # Stop an unfinished handshake through its own cancel scope. A raw
            # asyncio Task.cancel() could interrupt the SDK's shielded cleanup.
            self._scope.cancel()
        await asyncio.shield(task)
        if ready is not None and ready.done() and not ready.cancelled():
            # A cancelled connect waiter may no longer be observing this future.
            ready.exception()

    def _connected_client(self) -> Any:
        if self._sdk_client is None or self._stop is None or self._stop.is_set():
            raise ConnectionError("MCP client is not connected") from self._failure
        return self._sdk_client

    async def list_tools(self) -> list[MCPToolDef]:
        client = self._connected_client()
        tools: list[MCPToolDef] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            page = await client.list_tools(cursor=cursor)
            tools.extend(
                MCPToolDef(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=tool.input_schema,
                )
                for tool in page.tools
            )
            cursor = page.next_cursor
            if cursor is None:
                return tools
            if cursor in seen:
                raise RuntimeError("MCP tools/list returned a repeated pagination cursor")
            seen.add(cursor)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> MCPToolResult:
        client = self._connected_client()
        from mcp import MCPError

        try:
            # Use the public session API to keep sampling, elicitation and
            # extension results outside this tools-only adapter's capabilities.
            result = await client.session.call_tool(
                name,
                arguments,
                read_timeout_seconds=self.config.tool_timeout_seconds,
                allow_input_required=False,
                allow_claimed=False,
            )
        except (MCPError, ValidationError, RuntimeError) as exc:
            # This includes SDK output-schema failures. Discovery translates the
            # flag into SafeToolError at the normal tool execution boundary.
            return MCPToolResult(content=str(exc), is_error=True)
        text_blocks = [block.text for block in result.content if block.type == "text"]
        text = "\n".join(text_blocks)
        if not text_blocks and result.structured_content is not None:
            text = json.dumps(result.structured_content, ensure_ascii=False)
        if not text_blocks and result.structured_content is None and result.content:
            kinds = ", ".join(sorted({block.type for block in result.content}))
            return MCPToolResult(
                content=f"MCP tool returned unsupported content types: {kinds}",
                is_error=True,
            )
        return MCPToolResult(content=text, is_error=result.is_error)
