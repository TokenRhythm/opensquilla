from __future__ import annotations

import asyncio
import http.server
import json
import queue
import threading
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from opensquilla.mcp.sse import MCPSSEClient, _bounded_lines
from opensquilla.mcp.types import MCPServerConfig

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the given text back.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }
]


class EndpointHandshakeSSEServer:
    """Minimal 2024-11-05 HTTP+SSE MCP server.

    * ``GET /sse`` opens a ``text/event-stream`` whose first event is
      ``event: endpoint`` carrying the session message URL; responses to
      POSTed requests arrive on this same stream.
    * ``POST /messages?session_id=<id>`` returns 202 and queues the response
      onto that session's stream.
    * Anything else returns 404 and is recorded in ``wrong_posts``.
    """

    def __init__(self) -> None:
        self.sessions: dict[str, queue.Queue[dict[str, Any]]] = {}
        self.wrong_posts: list[str] = []
        self.session_posts: list[str] = []
        self._shutdown = threading.Event()

        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                if urlparse(self.path).path != "/sse":
                    self._plain(404, b"Not Found")
                    return
                sid = uuid.uuid4().hex
                q: queue.Queue[dict[str, Any]] = queue.Queue()
                outer.sessions[sid] = q
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self._write(f"event: endpoint\ndata: /messages?session_id={sid}\n\n")
                while not outer._shutdown.is_set():
                    try:
                        payload = q.get(timeout=0.1)
                    except queue.Empty:
                        if not self._write(": keepalive\n\n"):
                            return
                        continue
                    if not self._write(f"event: message\ndata: {json.dumps(payload)}\n\n"):
                        return

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                parsed = urlparse(self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                sid = (parse_qs(parsed.query).get("session_id") or [""])[0]
                if parsed.path != "/messages" or sid not in outer.sessions:
                    outer.wrong_posts.append(parsed.path)
                    self._plain(404, b"Not Found")
                    return
                msg = json.loads(body)
                outer.session_posts.append(msg.get("method", "?"))
                if "id" in msg:
                    outer.sessions[sid].put(outer.respond(msg))
                self._plain(202, b"Accepted")

            def _write(self, chunk: str) -> bool:
                try:
                    self.wfile.write(chunk.encode())
                    self.wfile.flush()
                    return True
                except (BrokenPipeError, ConnectionError, OSError):
                    return False

            def _plain(self, status: int, payload: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @staticmethod
    def respond(msg: dict[str, Any]) -> dict[str, Any]:
        method = msg["method"]
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sse-test", "version": "0.0.1"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        else:
            return {
                "jsonrpc": "2.0",
                "id": msg["id"],
                "error": {"code": -32601, "message": f"unknown method {method}"},
            }
        return {"jsonrpc": "2.0", "id": msg["id"], "result": result}

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._shutdown.set()
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def sse_server(monkeypatch: pytest.MonkeyPatch) -> Any:
    for var in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "OPENSQUILLA_TRUST_ENV",
    ):
        monkeypatch.delenv(var, raising=False)
    server = EndpointHandshakeSSEServer()
    server.start()
    yield server
    server.stop()


async def test_connect_completes_endpoint_handshake_and_lists_tools(
    sse_server: EndpointHandshakeSSEServer,
) -> None:
    config = MCPServerConfig(
        name="demo",
        transport="sse",
        url=f"http://127.0.0.1:{sse_server.port}/sse",
    )
    client = MCPSSEClient(config)
    try:
        await asyncio.wait_for(client.connect(), timeout=10)
        tools = await asyncio.wait_for(client.list_tools(), timeout=10)
    finally:
        await client.close()

    assert [t.name for t in tools] == ["echo"]
    assert sse_server.session_posts == [
        "initialize",
        "notifications/initialized",
        "tools/list",
    ]
    assert sse_server.wrong_posts == []


def test_endpoint_event_rejects_cross_origin_url() -> None:
    client = MCPSSEClient(
        MCPServerConfig(
            name="demo",
            transport="sse",
            url="https://mcp.example.test/sse",
        )
    )

    with pytest.raises(ValueError, match="same origin"):
        client._handle_event("endpoint", "http://127.0.0.1:8080/private")

    assert client._message_url is None
    assert not client._endpoint_ready.is_set()


@pytest.mark.parametrize(
    "endpoint",
    [
        "/messages?session_id=1",
        "https://mcp.example.test/messages?session_id=1",
        "https://mcp.example.test:443/messages?session_id=1",
    ],
)
def test_endpoint_event_accepts_same_origin_url(endpoint: str) -> None:
    client = MCPSSEClient(
        MCPServerConfig(
            name="demo",
            transport="sse",
            url="https://mcp.example.test/sse",
        )
    )

    client._handle_event("endpoint", endpoint)

    assert client._message_url is not None
    assert client._endpoint_ready.is_set()


async def test_public_sse_call_preserves_media(
    sse_server: EndpointHandshakeSSEServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = sse_server.respond
    blocks = [
        {"type": "text", "text": "x" * 100_000},
        {"type": "image", "data": "AA==", "mimeType": "image/png"},
        {"type": "resource", "resource": {"uri": "sample:///text", "text": "body"}},
    ]

    def respond(message: dict[str, Any]) -> dict[str, Any]:
        if message["method"] == "tools/call":
            return {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {"content": blocks, "structuredContent": {"count": 3}},
            }
        return original(message)

    monkeypatch.setattr(sse_server, "respond", respond)
    client = MCPSSEClient(
        MCPServerConfig(
            name="media", transport="sse", url=f"http://127.0.0.1:{sse_server.port}/sse"
        )
    )
    try:
        await client.connect()
        result = await client.call_tool("read", {})
    finally:
        await client.close()
    assert not result.is_error
    assert result.content == "x" * 100_000
    assert result.content_blocks == blocks
    assert result.structured_content == {"count": 3}


async def test_oversized_sse_response_fails_pending_and_future_calls(
    sse_server: EndpointHandshakeSSEServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("opensquilla.mcp.sse.MAX_MCP_MESSAGE_BYTES", 1024)
    original = sse_server.respond

    def respond(message: dict[str, Any]) -> dict[str, Any]:
        if message["method"] == "tools/call":
            return {
                "id": message["id"],
                "result": {"content": [{"type": "text", "text": "x" * 2048}]},
            }
        return original(message)

    monkeypatch.setattr(sse_server, "respond", respond)
    client = MCPSSEClient(
        MCPServerConfig(
            name="large", transport="sse", url=f"http://127.0.0.1:{sse_server.port}/sse"
        )
    )
    try:
        await client.connect()
        for _ in range(2):
            with pytest.raises(ValueError, match="MCP SSE line exceeds 1024 bytes"):
                await asyncio.wait_for(client.call_tool("read", {}), timeout=10)
    finally:
        await client.close()


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


async def test_bounded_sse_lines_preserve_split_utf8_and_line_endings() -> None:
    response = httpx.Response(
        200,
        stream=_Chunks([b"\xef\xbb\xbfdata: \xe4", b"\xb8\xad\r", b"\nnext\r\rlast\n"]),
    )
    assert [line async for line in _bounded_lines(response)] == ["data: \u4e2d", "next", "", "last"]


async def test_unterminated_sse_line_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("opensquilla.mcp.sse.MAX_MCP_MESSAGE_BYTES", 128)
    response = httpx.Response(200, stream=_Chunks([b"a" * 64, b"b" * 65]))
    with pytest.raises(ValueError, match="line exceeds 128 bytes"):
        _ = [line async for line in _bounded_lines(response)]


async def test_sse_event_is_bounded_across_multiple_short_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("opensquilla.mcp.sse.MAX_MCP_MESSAGE_BYTES", 128)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_Chunks([b"data: " + b"x" * 40 + b"\n"] * 4))

    client = MCPSSEClient(MCPServerConfig(name="event", transport="sse", url="http://test/sse"))
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    client._pending[1] = future
    try:
        await client._read_stream()
        with pytest.raises(ValueError, match="event exceeds 128 bytes"):
            await future
    finally:
        await client.close()
