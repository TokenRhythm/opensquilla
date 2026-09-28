from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from opensquilla.mcp.stdio import MCPStdioClient
from opensquilla.mcp.types import MCPServerConfig

_SDK_SERVER_SCRIPT = str(Path(__file__).parent / "fixtures" / "fastmcp_server.py")

# A stdio MCP server (newline-delimited JSON-RPC) exposing one "search" tool
# whose call response carries the MCP result-level ``isError`` flag.
_ERROR_SERVER_SCRIPT = r"""
import json, sys

def send(payload):
    sys.stdout.buffer.write((json.dumps(payload) + "\n").encode())
    sys.stdout.buffer.flush()

while True:
    line = sys.stdin.buffer.readline()
    if not line:
        break
    msg = json.loads(line.decode())
    msg_id = msg.get("id")
    if msg_id is None:
        continue  # notification
    method = msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": msg_id,
              "result": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "serverInfo": {"name": "fake", "version": "0.0.1"}}})
    elif method == "tools/call":
        send({"jsonrpc": "2.0", "id": msg_id,
              "result": {"isError": True,
                         "content": [{"type": "text",
                                      "text": "Error: upstream API rejected the query"}]}})
    else:
        send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
"""


class _FakeProcess:
    def __init__(self, *, exits_on_terminate: bool = True) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.wait_calls = 0
        self.exits_on_terminate = exits_on_terminate

    def terminate(self) -> None:
        self.terminated = True
        if self.exits_on_terminate:
            self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.wait_calls += 1
        if self.returncode is None:
            await asyncio.sleep(3600)
        assert self.returncode is not None
        return self.returncode


def _client_with_process(process: _FakeProcess) -> MCPStdioClient:
    client = MCPStdioClient(MCPServerConfig(name="demo", transport="stdio", command="demo"))
    client._process = process  # type: ignore[assignment]
    return client


class _RecordingStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.changed = asyncio.Event()

    def write(self, data: bytes) -> None:
        self.writes.append(data)
        self.changed.set()

    async def drain(self) -> None:
        return None

    async def wait_for_count(self, count: int) -> None:
        while len(self.writes) < count:
            self.changed.clear()
            await self.changed.wait()


class _QueuedStdout:
    def __init__(self) -> None:
        self.lines: asyncio.Queue[bytes] = asyncio.Queue()

    async def readline(self) -> bytes:
        return await self.lines.get()

    def respond(self, request_id: int) -> None:
        self.lines.put_nowait(
            (json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {}}) + "\n").encode()
        )


class _ConcurrentProcess:
    def __init__(self) -> None:
        self.stdin = _RecordingStdin()
        self.stdout = _QueuedStdout()


@pytest.mark.asyncio
async def test_send_request_before_connect_reports_connection_error() -> None:
    client = MCPStdioClient(MCPServerConfig(name="demo", transport="stdio", command="demo"))

    with pytest.raises(ConnectionError, match="not connected"):
        await client._send_request("tools/list")


@pytest.mark.asyncio
async def test_read_response_skips_invalid_utf8_lines() -> None:
    process = _ConcurrentProcess()
    process.stdout.lines.put_nowait(b"\xff\n")
    process.stdout.respond(1)
    client = _client_with_process(process)  # type: ignore[arg-type]

    assert await client._read_response(1) == {"jsonrpc": "2.0", "id": 1, "result": {}}


@pytest.mark.asyncio
async def test_concurrent_requests_are_serialized_to_preserve_responses() -> None:
    process = _ConcurrentProcess()
    client = _client_with_process(process)  # type: ignore[arg-type]

    first = asyncio.create_task(client._send_request("first"))
    second: asyncio.Task[dict] | None = None
    try:
        await process.stdin.wait_for_count(1)
        second = asyncio.create_task(client._send_request("second"))
        await asyncio.sleep(0)

        assert len(process.stdin.writes) == 1

        process.stdout.respond(1)
        await first
        await process.stdin.wait_for_count(2)
        process.stdout.respond(2)
        await second
    finally:
        first.cancel()
        if second is not None:
            second.cancel()
        pending = [first] if second is None else [first, second]
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_close_waits_for_terminated_stdio_process() -> None:
    process = _FakeProcess(exits_on_terminate=True)

    await _client_with_process(process).close()

    assert process.terminated is True
    assert process.killed is False
    assert process.wait_calls == 1


@pytest.mark.asyncio
async def test_close_kills_stdio_process_when_terminate_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(exits_on_terminate=False)
    client = _client_with_process(process)
    monkeypatch.setattr(client, "_CLOSE_TIMEOUT_SECONDS", 0.001)

    await client.close()

    assert process.terminated is True
    assert process.killed is True
    assert process.wait_calls == 2


def test_encode_request_is_newline_delimited_json() -> None:
    encoded = MCPStdioClient._encode_request({"jsonrpc": "2.0", "id": 1, "method": "x"})

    assert not encoded.startswith(b"Content-Length: ")
    assert encoded.endswith(b"\n")
    assert b"\n" not in encoded[:-1]


@pytest.mark.asyncio
async def test_connect_and_list_tools_against_sdk_stdio_server() -> None:
    pytest.importorskip("mcp")
    client = MCPStdioClient(
        MCPServerConfig(
            name="demo",
            transport="stdio",
            command=sys.executable,
            args=[_SDK_SERVER_SCRIPT],
        )
    )
    try:
        await asyncio.wait_for(client.connect(), timeout=30.0)
        tools = await asyncio.wait_for(client.list_tools(), timeout=30.0)
    finally:
        await client.close()

    assert [t.name for t in tools] == ["ping"]


@pytest.mark.asyncio
async def test_call_tool_honors_result_level_is_error_flag() -> None:
    client = MCPStdioClient(
        MCPServerConfig(
            name="demo",
            transport="stdio",
            command=sys.executable,
            args=["-c", _ERROR_SERVER_SCRIPT],
        )
    )
    try:
        await client.connect()
        result = await client.call_tool("search", {"q": "x"})
    finally:
        await client.close()

    assert "upstream API rejected" in result.content
    assert result.is_error is True


async def test_public_call_receives_media_in_frame_larger_than_64k() -> None:
    client = _content_client()
    try:
        await client.connect()
        result = await client.call_tool("read", {"size": 100_000})
    finally:
        await client.close()

    assert not result.is_error
    assert result.content == "x" * 100_000
    assert [block["type"] for block in result.content_blocks] == ["text", "image", "resource"]
    assert result.content_blocks[1]["data"].startswith("iVBOR")
    assert result.structured_content == {"size": 100_000}


@pytest.mark.parametrize("unterminated", [False, True])
async def test_oversized_stdio_response_fails_and_closes_connection(
    monkeypatch: pytest.MonkeyPatch, unterminated: bool
) -> None:
    monkeypatch.setattr("opensquilla.mcp.stdio.MAX_MCP_MESSAGE_BYTES", 1024)
    client = _content_client()
    try:
        await client.connect()
        with pytest.raises(ValueError, match="MCP stdio response exceeds 1024 bytes"):
            await asyncio.wait_for(
                client.call_tool("read", {"size": 2048, "unterminated": unterminated}),
                timeout=10,
            )
        assert client._process is None
        with pytest.raises(ConnectionError, match="not connected"):
            await client.call_tool("read", {})
    finally:
        await client.close()


def _content_client() -> MCPStdioClient:
    return MCPStdioClient(
        MCPServerConfig(
            name="content",
            transport="stdio",
            command=sys.executable,
            args=["-I", "-B", str(Path(__file__).parent / "fixtures" / "content_server.py")],
        )
    )
