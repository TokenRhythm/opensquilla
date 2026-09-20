from __future__ import annotations

import asyncio
import http.server
import json
import queue
import threading
import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from opensquilla.mcp.sse import MCPSSEClient
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
        self.endpoint_template = "/messages?session_id={sid}"
        self.tools = TOOLS
        self.tool_result: dict[str, Any] = {"content": [{"type": "text", "text": "echoed"}]}
        self.modern = False
        self.hang_calls = False
        self.call_started = threading.Event()
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
                endpoint = outer.endpoint_template.format(sid=sid, port=outer.port)
                self._write(f"event: endpoint\ndata: {endpoint}\n\n")
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
                    response = outer.respond(msg)
                    if response is not None:
                        outer.sessions[sid].put(response)
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

    def respond(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        method = msg["method"]
        if method == "server/discover" and self.modern:
            result: dict[str, Any] = {
                "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}},
            }
        elif method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "sse-test", "version": "0.0.1"},
            }
        elif method == "tools/list":
            result = {"tools": self.tools}
        elif method == "tools/call":
            self.call_started.set()
            if self.hang_calls:
                return None
            result = self.tool_result
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
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy",
                "all_proxy", "OPENSQUILLA_TRUST_ENV"):
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
        result = await asyncio.wait_for(client.call_tool("echo", {"text": "hello"}), timeout=10)
    finally:
        await client.close()

    assert [t.name for t in tools] == ["echo"]
    assert result.content == "echoed"
    assert not result.is_error
    assert sse_server.session_posts == [
        "server/discover",
        "initialize",
        "notifications/initialized",
        "tools/list",
        "tools/call",
    ]
    assert sse_server.wrong_posts == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:{port}/messages?session_id={sid}",
        "https://127.0.0.1:{port}/messages?session_id={sid}",
        "http://127.0.0.1:1/messages?session_id={sid}",
    ],
)
async def test_endpoint_event_rejects_cross_origin_url(
    sse_server: EndpointHandshakeSSEServer, endpoint: str, caplog: pytest.LogCaptureFixture,
) -> None:
    sse_server.endpoint_template = endpoint
    client = MCPSSEClient(
        MCPServerConfig(
            name="demo",
            transport="sse",
            url=f"http://127.0.0.1:{sse_server.port}/sse",
        )
    )
    with caplog.at_level("ERROR", logger="mcp.client.sse"):
        connecting = asyncio.create_task(client.connect())
        try:
            async with asyncio.timeout(5):
                while not any(
                    record.name == "mcp.client.sse"
                    and "Endpoint origin does not match connection origin" in record.getMessage()
                    for record in caplog.records
                ):
                    await asyncio.sleep(0.01)
        finally:
            connecting.cancel()
            await asyncio.gather(connecting, return_exceptions=True)
            await client.close()
    assert len(sse_server.sessions) == 1
    assert sse_server.session_posts == []
    assert sse_server.wrong_posts == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "/messages?session_id={sid}",
        "http://127.0.0.1:{port}/messages?session_id={sid}",
    ],
)
async def test_endpoint_event_accepts_same_origin_url(
    sse_server: EndpointHandshakeSSEServer, endpoint: str,
) -> None:
    sse_server.endpoint_template = endpoint
    client = MCPSSEClient(
        MCPServerConfig(
            name="demo",
            transport="sse",
            url=f"http://127.0.0.1:{sse_server.port}/sse",
        )
    )

    try:
        await asyncio.wait_for(client.connect(), timeout=10)
        assert [tool.name for tool in await client.list_tools()] == ["echo"]
    finally:
        await client.close()


async def test_sdk_output_schema_violation_is_a_tool_error(
    sse_server: EndpointHandshakeSSEServer,
) -> None:
    sse_server.tools = [{
        **TOOLS[0],
        "outputSchema": {
            "type": "object", "properties": {"value": {"type": "integer"}},
            "required": ["value"],
        },
    }]
    sse_server.tool_result = {
        "content": [{"type": "text", "text": "looks successful"}],
        "structuredContent": {"value": "wrong type"},
    }
    client = MCPSSEClient(MCPServerConfig(
        name="schema", transport="sse", url=f"http://127.0.0.1:{sse_server.port}/sse",
    ))
    try:
        await asyncio.wait_for(client.connect(), timeout=10)
        await client.list_tools()
        result = await client.call_tool("echo", {"text": "hello"})
    finally:
        await client.close()
    assert result.is_error
    assert "Invalid structured content" in result.content


async def test_explicit_message_endpoint_is_rejected() -> None:
    client = MCPSSEClient(MCPServerConfig(
        name="legacy", transport="sse", url="http://127.0.0.1/sse",
        message_endpoint="/legacy",
    ))
    with pytest.raises(ValueError, match="endpoint event"):
        await client.connect()


async def test_input_required_is_rejected_without_replaying_the_tool(
    sse_server: EndpointHandshakeSSEServer,
) -> None:
    sse_server.modern = True
    sse_server.tool_result = {"resultType": "input_required", "requestState": "synthetic"}
    client = MCPSSEClient(MCPServerConfig(
        name="interactive", transport="sse", url=f"http://127.0.0.1:{sse_server.port}/sse",
    ))
    try:
        await asyncio.wait_for(client.connect(), timeout=10)
        result = await client.call_tool("echo", {})
    finally:
        await client.close()
    assert result.is_error
    assert "InputRequiredResult" in result.content
    assert sse_server.session_posts == ["server/discover", "tools/call"]


async def test_closing_connection_settles_an_outstanding_tool_call(
    sse_server: EndpointHandshakeSSEServer,
) -> None:
    sse_server.hang_calls = True
    client = MCPSSEClient(MCPServerConfig(
        name="pending", transport="sse", url=f"http://127.0.0.1:{sse_server.port}/sse",
    ))
    await asyncio.wait_for(client.connect(), timeout=10)
    calling = asyncio.create_task(client.call_tool("echo", {}))
    try:
        assert await asyncio.to_thread(sse_server.call_started.wait, 2)
        await asyncio.wait_for(client.close(), timeout=2)
        result = await asyncio.wait_for(calling, timeout=2)
    finally:
        await client.close()
        calling.cancel()
        await asyncio.gather(calling, return_exceptions=True)
    assert result.is_error


@pytest.mark.parametrize("trust_env", [False, True])
async def test_sdk_http_factory_preserves_proxy_policy_and_unbounded_idle(
    monkeypatch: pytest.MonkeyPatch, trust_env: bool,
) -> None:
    import httpx2
    import mcp.client.sse

    captured: dict[str, Any] = {}

    def capture_transport(url: str, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(mcp.client.sse, "sse_client", capture_transport)
    monkeypatch.setattr("opensquilla.mcp.sse._trust_env", lambda: trust_env)
    client = MCPSSEClient(MCPServerConfig(
        name="network", transport="sse", url="https://mcp.example.test/sse",
        tool_timeout_seconds=17,
    ))
    sdk = client._make_sdk_client()
    assert sdk.mode == "auto" and sdk.cache is None
    assert sdk.read_timeout_seconds == 17
    async with captured["httpx_client_factory"](
        timeout=httpx2.Timeout(17, read=300), headers={"X-Synthetic": "test"},
    ) as http_client:
        assert http_client.timeout.read is None
        assert http_client.timeout.connect == 17
        assert http_client.timeout.write == 17
        assert http_client.timeout.pool == 17
        assert http_client.trust_env is trust_env
        assert http_client.headers["X-Synthetic"] == "test"
