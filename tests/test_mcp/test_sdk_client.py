from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest
from structlog.testing import capture_logs

from opensquilla.mcp.sdk_client import SDKMCPClient
from opensquilla.mcp.types import MCPServerConfig


class Adapter(SDKMCPClient):
    def __init__(self, factory: Any) -> None:
        super().__init__(MCPServerConfig(name="synthetic", transport="stdio"))
        self.factory = factory

    def _make_sdk_client(self) -> Any:
        return self.factory()


def sdk_result(
    text: str | None = "done", *, structured: Any = None, is_error: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=[] if text is None else [SimpleNamespace(type="text", text=text)],
        structured_content=structured,
        is_error=is_error,
    )


def sdk_client(result: Any = None) -> SimpleNamespace:
    return SimpleNamespace(
        session=SimpleNamespace(call_tool=AsyncMock(return_value=result or sdk_result())),
        list_tools=AsyncMock(return_value=SimpleNamespace(tools=[], next_cursor=None)),
    )


@asynccontextmanager
async def connected(client: Any):
    async with anyio.create_task_group():
        yield client


async def test_connect_call_and_close_from_different_tasks_keep_one_context_owner() -> None:
    sdk = sdk_client()
    owners: list[asyncio.Task[Any] | None] = []

    @asynccontextmanager
    async def lifetime():
        async with anyio.create_task_group():
            owners.append(asyncio.current_task())
            try:
                yield sdk
            finally:
                owners.append(asyncio.current_task())

    client = Adapter(lifetime)
    await asyncio.wait_for(client.connect(), timeout=2)
    result = await asyncio.create_task(client.call_tool("echo", {"text": "hello"}))
    await asyncio.create_task(client.close())
    await client.close()

    assert result.content == "done"
    assert len(owners) == 2 and owners[0] is owners[1]
    assert owners[0] is not asyncio.current_task()
    sdk.session.call_tool.assert_awaited_once_with(
        "echo", {"text": "hello"}, read_timeout_seconds=30.0,
        allow_input_required=False, allow_claimed=False,
    )
    with pytest.raises(ConnectionError, match="not connected"):
        await client.list_tools()


async def test_cancelled_connect_unwinds_entered_context_without_leaking() -> None:
    started = asyncio.Event()
    closed = asyncio.Event()

    @asynccontextmanager
    async def lifetime():
        try:
            async with anyio.create_task_group():
                started.set()
                await asyncio.Event().wait()
                yield sdk_client()
        finally:
            closed.set()

    client = Adapter(lifetime)
    connecting = asyncio.create_task(client.connect())
    await asyncio.wait_for(started.wait(), timeout=2)
    connecting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(connecting, timeout=2)
    assert closed.is_set()
    await client.close()


async def test_discovery_timeout_closes_unfinished_connection() -> None:
    closed = asyncio.Event()

    @asynccontextmanager
    async def lifetime():
        try:
            async with anyio.create_task_group():
                await asyncio.Event().wait()
                yield sdk_client()
        finally:
            closed.set()

    client = Adapter(lifetime)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(client.connect(), timeout=0.05)
    assert closed.is_set()


async def test_close_during_handshake_settles_connect_waiter() -> None:
    started = asyncio.Event()

    @asynccontextmanager
    async def lifetime():
        async with anyio.create_task_group():
            started.set()
            await asyncio.Event().wait()
            yield sdk_client()

    client = Adapter(lifetime)
    connecting = asyncio.create_task(client.connect())
    await asyncio.wait_for(started.wait(), timeout=2)
    await asyncio.wait_for(client.close(), timeout=2)
    with pytest.raises(ConnectionError, match="closed before connecting"):
        await connecting


async def test_failed_connect_propagates_cause_and_can_be_closed() -> None:
    def fail():
        raise ValueError("synthetic connection failure")

    client = Adapter(fail)
    with pytest.raises(ValueError, match="synthetic connection failure"):
        await asyncio.wait_for(client.connect(), timeout=2)
    await client.close()


@pytest.mark.parametrize("cancelled", [False, True])
async def test_teardown_failure_logs_only_error_type_and_preserves_idempotent_close(
    cancelled: bool,
) -> None:
    @asynccontextmanager
    async def lifetime():
        yield sdk_client()
        if cancelled:
            raise asyncio.CancelledError
        raise RuntimeError("synthetic private server detail")

    client = Adapter(lifetime)
    with capture_logs() as logs:
        await client.connect()
        await client.close()
        await client.close()
    assert client._sdk_client is None
    assert logs == ([] if cancelled else [{
        "event": "mcp.client.lifecycle_failed",
        "server": "synthetic",
        "error_type": "RuntimeError",
        "log_level": "warning",
    }])


async def test_reconnect_creates_a_fresh_sdk_context() -> None:
    sdk = sdk_client()
    client = Adapter(lambda: connected(sdk))
    for _ in range(2):
        await asyncio.wait_for(client.connect(), timeout=2)
        assert (await client.call_tool("echo", {})).content == "done"
        await asyncio.gather(client.close(), client.close())


async def test_failed_connect_does_not_close_a_new_connection_generation() -> None:
    started = asyncio.Event()
    fail = asyncio.Event()
    attempts = 0
    sdk = sdk_client()

    @asynccontextmanager
    async def lifetime():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            started.set()
            await fail.wait()
            raise ValueError("synthetic first handshake failure")
        yield sdk

    client = Adapter(lifetime)
    first = asyncio.create_task(client.connect())
    await asyncio.wait_for(started.wait(), timeout=2)
    # Schedule a replacement immediately after the old owner fails, before its
    # connect waiter resumes and performs failure cleanup.
    fail.set()
    second = asyncio.create_task(client.connect())
    try:
        results = await asyncio.wait_for(
            asyncio.gather(first, second, return_exceptions=True), timeout=2,
        )
        assert isinstance(results[0], ValueError)
        assert results[1] is None
        assert attempts == 2
        assert await client.list_tools() == []
    finally:
        await client.close()


async def test_cancelled_close_waiter_does_not_cancel_owned_cleanup() -> None:
    cleaning = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()

    @asynccontextmanager
    async def lifetime():
        try:
            yield sdk_client()
        finally:
            cleaning.set()
            await release.wait()
            closed.set()

    client = Adapter(lifetime)
    await client.connect()
    closing = asyncio.create_task(client.close())
    try:
        await asyncio.wait_for(cleaning.wait(), timeout=2)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert not closed.is_set()
    finally:
        release.set()
        await asyncio.wait_for(client.close(), timeout=2)
    assert closed.is_set()


async def test_lists_every_page_without_losing_schema() -> None:
    schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    sdk = sdk_client()
    sdk.list_tools.side_effect = [
        SimpleNamespace(
            tools=[SimpleNamespace(name="first", description=None, input_schema=schema)],
            next_cursor="second-page",
        ),
        SimpleNamespace(
            tools=[SimpleNamespace(name="second", description="Second", input_schema={})],
            next_cursor=None,
        ),
    ]
    client = Adapter(lambda: connected(sdk))
    await client.connect()
    try:
        tools = await client.list_tools()
    finally:
        await client.close()
    assert [tool.name for tool in tools] == ["first", "second"]
    assert tools[0].input_schema == schema
    assert tools[0].description == ""
    assert sdk.list_tools.await_args_list[1].kwargs == {"cursor": "second-page"}


async def test_repeated_pagination_cursor_fails_instead_of_looping() -> None:
    sdk = sdk_client()
    sdk.list_tools.return_value = SimpleNamespace(tools=[], next_cursor="same")
    client = Adapter(lambda: connected(sdk))
    await client.connect()
    try:
        with pytest.raises(RuntimeError, match="repeated pagination cursor"):
            await asyncio.wait_for(client.list_tools(), timeout=2)
    finally:
        await client.close()
    assert sdk.list_tools.await_count == 2


@pytest.mark.parametrize(
    ("result", "expected", "error"),
    [
        (sdk_result("text", structured={"value": 2}), "text", False),
        (sdk_result("", structured={"value": 2}), "", False),
        (sdk_result("  ", structured={"value": 2}), "  ", False),
        (sdk_result(None, structured={"value": "中文"}), '{"value": "中文"}', False),
        (sdk_result(None, structured={}), "{}", False),
        (sdk_result("rejected", is_error=True), "rejected", True),
        (sdk_result(None), "", False),
    ],
)
async def test_tool_result_projection(result: Any, expected: str, error: bool) -> None:
    client = Adapter(lambda: connected(sdk_client(result)))
    await client.connect()
    try:
        actual = await client.call_tool("echo", {})
    finally:
        await client.close()
    assert actual.content == expected
    assert actual.is_error is error
    if result.structured_content is not None and result.content == []:
        assert json.loads(actual.content) == result.structured_content


async def test_nontext_only_result_is_not_an_empty_success() -> None:
    result = sdk_result(None)
    result.content = [SimpleNamespace(type="image")]
    client = Adapter(lambda: connected(sdk_client(result)))
    await client.connect()
    try:
        actual = await client.call_tool("screenshot", {})
    finally:
        await client.close()
    assert actual.is_error
    assert "unsupported content types: image" in actual.content


async def test_multiple_text_blocks_preserve_order_and_empty_blocks() -> None:
    result = sdk_result(None, structured={"ignored": True})
    result.content = [
        SimpleNamespace(type="text", text="first"),
        SimpleNamespace(type="text", text=""),
        SimpleNamespace(type="text", text="last"),
    ]
    client = Adapter(lambda: connected(sdk_client(result)))
    await client.connect()
    try:
        actual = await client.call_tool("echo", {})
    finally:
        await client.close()
    assert actual.content == "first\n\nlast"


@pytest.mark.parametrize("kind", ["protocol", "output_schema", "input_required"])
async def test_sdk_tool_failures_preserve_error_semantics(kind: str) -> None:
    from mcp import MCPError

    sdk = sdk_client()
    error = (
        MCPError(-32602, "invalid parameters")
        if kind == "protocol"
        else RuntimeError(f"unsupported {kind}")
    )
    sdk.session.call_tool.side_effect = error
    client = Adapter(lambda: connected(sdk))
    await client.connect()
    try:
        result = await client.call_tool("echo", {})
    finally:
        await client.close()
    assert result.is_error
    assert str(error) in result.content
