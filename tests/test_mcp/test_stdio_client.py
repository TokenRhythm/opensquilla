from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from opensquilla.mcp.stdio import MCPStdioClient
from opensquilla.mcp.types import MCPServerConfig

_LEGACY_SERVER_SCRIPT = str(Path(__file__).parent / "fixtures" / "legacy_server.py")

_SDK_SERVER_SCRIPT = str(Path(__file__).parent / "fixtures" / "fastmcp_server.py")


class _FakeProcess:
    def __init__(self, *, exits_on_terminate: bool = True) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self.wait_calls = 0
        self.exits_on_terminate = exits_on_terminate
        self.stdout = None

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


@pytest.mark.asyncio
async def test_close_waits_for_terminated_stdio_process() -> None:
    process = _FakeProcess(exits_on_terminate=True)

    await _client_with_process(process)._terminate_process()

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

    await client._terminate_process()

    assert process.terminated is True
    assert process.killed is True
    assert process.wait_calls == 2


@pytest.mark.asyncio
async def test_connect_and_list_tools_against_sdk_stdio_server() -> None:
    from mcp.types import LATEST_PROTOCOL_VERSION

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
        assert client._sdk_client.protocol_version == LATEST_PROTOCOL_VERSION
        tools = await asyncio.wait_for(client.list_tools(), timeout=30.0)
        result = await client.call_tool("ping", {"text": "modern round trip"})
        assert result.content == "modern round trip" and not result.is_error
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
            args=[_LEGACY_SERVER_SCRIPT, "error"],
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

    process = _FakeProcess()
    process.stdout = _ChunkedStdout(line)  # type: ignore[attr-defined]
    client = _client_with_process(process)

    result = await client._readline_safe()
    assert result == line


@pytest.mark.asyncio
async def test_readline_safe_reassembles_lines_across_chunks() -> None:
    """_readline_safe must reassemble a line split across multiple read calls."""
    # Pad the line with a long key so it spans multiple 8KB reads.
    long_key = "x" * 20_000
    long_payload = {"jsonrpc": "2.0", "id": 1, "result": {"data": long_key}}
    long_line = (json.dumps(long_payload) + "\n").encode()
    assert len(long_line) > 16384, "line must span at least two 8192-byte reads"

    process = _FakeProcess()
    process.stdout = _ChunkedStdout(long_line)  # type: ignore[attr-defined]
    client = _client_with_process(process)

    result = await client._readline_safe()
    assert result == long_line


@pytest.mark.asyncio
async def test_readline_budget_counts_each_frame_without_its_newline() -> None:
    process = _FakeProcess()
    # Both lines arrive in one read. The next frame must not consume this
    # frame's budget, and multibyte text must be counted as bytes.
    line = "🦑".encode() * 16
    process.stdout = _ChunkedStdout(line + b"\n" + line + b"\n")  # type: ignore[attr-defined]
    client = _client_with_process(process)
    client._MAX_MESSAGE_BYTES = len(line)

    assert await client._readline_safe() == line + b"\n"
    assert await client._readline_safe() == line + b"\n"
    assert await client._readline_safe() == b""


@pytest.mark.asyncio
@pytest.mark.parametrize("delimiter", [b"", b"\n"])
async def test_readline_allows_budget_boundary_across_read_chunks(delimiter: bytes) -> None:
    process = _FakeProcess()
    line = b"x" * 8192
    process.stdout = _ChunkedStdout(line + delimiter)  # type: ignore[attr-defined]
    client = _client_with_process(process)
    client._MAX_MESSAGE_BYTES = len(line)

    assert await client._readline_safe() == line + delimiter


@pytest.mark.asyncio
@pytest.mark.parametrize("delimiter", [b"", b"\n"])
@pytest.mark.parametrize("buffered", [False, True])
async def test_readline_rejects_over_budget_and_disconnects(
    delimiter: bytes, buffered: bool
) -> None:
    process = _FakeProcess()
    prefix = b"{}\n" if buffered else b""
    process.stdout = _ChunkedStdout(prefix + b"x" * 65 + delimiter)  # type: ignore[attr-defined]
    client = _client_with_process(process)
    client._MAX_MESSAGE_BYTES = 64
    if buffered:
        assert await client._readline_safe() == b"{}\n"

    with pytest.raises(ValueError, match="MCP stdio message exceeds 64 byte limit"):
        await client._readline_safe()

    assert process.terminated
    assert client._process is None
    assert client._readahead == b""
    with pytest.raises(ConnectionError, match="not connected"):
        await client.list_tools()


@pytest.mark.asyncio
async def test_close_discards_old_process_readahead() -> None:
    client = _client_with_process(_FakeProcess())
    client._readahead = b'{"jsonrpc":"2.0",'

    await client._terminate_process()

    assert client._readahead == b""


def _legacy_client(mode: str, *, timeout: float = 5.0) -> MCPStdioClient:
    return MCPStdioClient(
        MCPServerConfig(
            name="synthetic",
            transport="stdio",
            command=sys.executable,
            args=["-u", _LEGACY_SERVER_SCRIPT, mode],
            tool_timeout_seconds=timeout,
        )
    )


@pytest.mark.asyncio
async def test_legacy_negotiation_large_messages_and_multibyte_results() -> None:
    client = _legacy_client("large")
    try:
        await client.connect()
        tools = await client.list_tools()
        assert [tool.name for tool in tools] == ["echo"]
        assert len(tools[0].description) == 129 * 1024
        result = await client.call_tool("echo", {})
        assert not result.is_error
        assert result.content == "🦑" * (256 * 1024)
        assert len(await client.list_tools()) == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_concurrent_requests_match_out_of_order_responses_and_notifications() -> None:
    client = _legacy_client("reorder")
    try:
        await client.connect()
        await client.list_tools()
        async with asyncio.timeout(10):
            first, second = await asyncio.gather(
                client.call_tool("echo", {"text": "first"}),
                client.call_tool("echo", {"text": "second"}),
            )
        assert (first.content, second.content) == ("first", "second")
        assert not first.is_error and not second.is_error
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_sdk_parser_tolerates_blank_and_invalid_lines() -> None:
    client = _legacy_client("noisy")
    try:
        await client.connect()
        assert [tool.name for tool in await client.list_tools()] == ["echo"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stdio_environment_inheritance_and_explicit_overrides(monkeypatch) -> None:
    monkeypatch.setenv("OPENSQUILLA_MCP_TEST_INHERITED", "inherited")
    monkeypatch.setenv("OPENSQUILLA_MCP_TEST_OVERRIDE", "original")
    client = _legacy_client("environment")
    client.config.env = {"OPENSQUILLA_MCP_TEST_OVERRIDE": "override"}
    try:
        await client.connect()
        result = await client.call_tool("echo", {})
        assert json.loads(result.content) == {
            "OPENSQUILLA_MCP_TEST_INHERITED": "inherited",
            "OPENSQUILLA_MCP_TEST_OVERRIDE": "override",
        }
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_real_subprocess_cross_task_connect_call_close_and_reconnect() -> None:
    client = _legacy_client("normal")
    baseline = asyncio.all_tasks()
    await asyncio.create_task(client.connect())
    process = client._process
    result = await asyncio.create_task(client.call_tool("echo", {"text": "hello"}))
    assert not result.is_error and result.content == "hello"
    await asyncio.create_task(client.close())
    await client.close()
    assert process is not None and process.returncode is not None
    assert client._process is None
    client._readahead = b"old incomplete message"
    await client.connect()
    assert [tool.name for tool in await client.list_tools()] == ["echo"]
    await client.close()
    await asyncio.sleep(0)
    assert not (asyncio.all_tasks() - baseline)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "timeout", "close"])
async def test_initialization_interruption_reaps_real_subprocess(action: str) -> None:
    client = _legacy_client("hang", timeout=0.2 if action == "timeout" else 5)
    task = asyncio.create_task(client.connect())
    try:
        async with asyncio.timeout(10):
            while client._process is None:
                await asyncio.sleep(0.005)
            process = client._process
            if action == "cancel":
                task.cancel()
            elif action == "close":
                await client.close()
            with pytest.raises((asyncio.CancelledError, Exception)):
                await task
            await client.close()
            assert process.returncode is not None
            assert client._process is None
    finally:
        await client.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["exit", "oversize"])
async def test_disconnect_fails_pending_call_and_reaps_child(mode: str) -> None:
    client = _legacy_client(mode)
    try:
        await client.connect()
        process = client._process
        async with asyncio.timeout(10):
            result = await client.call_tool("echo", {})
            assert result.is_error
            await client.close()
        assert process is not None and process.returncode is not None
        assert client._process is None
        assert process.stdout is not None and process.stdout.at_eof()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_close_during_call_wakes_request_without_leaving_tasks() -> None:
    client = _legacy_client("hang_call")
    await client.connect()
    pending = asyncio.create_task(client.call_tool("echo", {}))
    await asyncio.sleep(0.05)
    await client.close()
    result = await asyncio.wait_for(pending, 2)
    assert result.is_error


@pytest.mark.asyncio
@pytest.mark.parametrize("excess", [0, 1])
async def test_actual_16_mib_preparse_boundary(excess: int) -> None:
    process = _FakeProcess()
    budget = 16 * 1024 * 1024
    process.stdout = _ChunkedStdout(b"x" * (budget + excess) + b"\n")
    client = _client_with_process(process)
    assert client._MAX_MESSAGE_BYTES == budget
    if excess:
        with pytest.raises(ValueError, match="byte limit"):
            await client._readline_safe()
        assert process.terminated
    else:
        assert len(await client._readline_safe()) == budget + 1
    await client._terminate_process()
