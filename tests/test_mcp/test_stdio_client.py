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
        self._readahead = b""

    async def readline(self) -> bytes:
        return await self.lines.get()

    async def read(self, n: int) -> bytes:
        """Read up to n bytes, compatible with _readline_safe chunked reads."""
        if self._readahead:
            chunk = self._readahead[:n]
            self._readahead = self._readahead[n:]
            return chunk
        try:
            data = await asyncio.wait_for(self.lines.get(), timeout=10.0)
        except TimeoutError:
            return b""
        if len(data) > n:
            self._readahead = data[n:]
            return data[:n]
        return data

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


class _ChunkedStdout:
    """Mock stdout that delivers data via ``read(chunk_size)`` instead of ``readline()``.

    Used to test ``_readline_safe()`` which avoids the 64 KB StreamReader limit
    by reading in chunks and splitting on newlines itself.
    """

    def __init__(self, data: bytes) -> None:
        self._buffer = data
        self._pos = 0

    async def read(self, n: int) -> bytes:
        chunk = self._buffer[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk


@pytest.mark.asyncio
async def test_readline_safe_handles_line_larger_than_64kb() -> None:
    """_readline_safe must read a single line > 64 KB without error."""
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"data": "x" * 70_000}}
    line = (json.dumps(payload) + "\n").encode()
    assert len(line) > 65536, "test line must exceed 64 KB"

    # Simulate the first chunk containing the full line.
    process = _FakeProcess()
    process.stdout = _ChunkedStdout(line)  # type: ignore[attr-defined]
    client = _client_with_process(process)

    result = await client._readline_safe()
    assert result == line.rstrip(b"\n")
    assert len(result) == len(line) - 1


@pytest.mark.asyncio
async def test_readline_safe_reassembles_lines_across_chunks() -> None:
    """_readline_safe must reassemble a line split across multiple read calls."""
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"data": "hello"}}
    line = (json.dumps(payload) + "\n").encode()
    # Pad the line with a long key so it spans multiple 8KB reads.
    long_key = "x" * 20_000
    long_payload = {"jsonrpc": "2.0", "id": 1, "result": {"data": long_key}}
    long_line = (json.dumps(long_payload) + "\n").encode()
    assert len(long_line) > 16384, "line must span at least two 8192-byte reads"

    process = _FakeProcess()
    process.stdout = _ChunkedStdout(long_line)  # type: ignore[attr-defined]
    client = _client_with_process(process)

    result = await client._readline_safe()
    assert result == long_line.rstrip(b"\n")
    assert len(result) == len(long_line) - 1
