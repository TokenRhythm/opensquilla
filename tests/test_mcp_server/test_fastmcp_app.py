from __future__ import annotations

import asyncio
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from opensquilla.mcp_server.server import create_mcp_server


class FakeFastMCP:
    def __init__(self, name: str, **kwargs: Any) -> None:
        self.name = name
        self.kwargs = kwargs
        self.tools: dict[str, Callable[..., Any]] = {}
        self.resources: dict[str, Callable[..., Any]] = {}
        self.run_calls: list[dict[str, Any]] = []

    def tool(self, name: str | None = None):
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name or func.__name__] = func
            return func

        return decorator

    def resource(self, uri: str):
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.resources[uri] = func
            return func

        return decorator

    def run(self, **kwargs: Any) -> None:
        self.run_calls.append(kwargs)


class FakeBridge:
    async def conversations_list(self, limit: int = 50) -> dict[str, Any]:
        return {"sessions": [{"key": "agent:main:main"}], "limit": limit}

    async def session_resolve(self, key: str) -> dict[str, Any]:
        return {"key": key, "session_id": "sid-1"}

    async def messages_read(
        self,
        key: str,
        limit: int = 1000,
        *,
        before: str | None = None,
        after: str | None = None,
    ) -> dict[str, Any]:
        return {
            "messages": [{"role": "user", "text": key}],
            "limit": limit,
            "before": before,
            "after": after,
        }

    async def message_detail_read(
        self,
        key: str,
        cursor: str,
        *,
        offset: int = 0,
        chunk_bytes: int = 128 * 1024,
        field: str = "message",
    ) -> dict[str, Any]:
        return {
            "key": key,
            "cursor": cursor,
            "offset": offset,
            "chunk_bytes": chunk_bytes,
            "field": field,
        }

    async def messages_send(
        self, key: str, message: str, intent: str = "continue"
    ) -> dict[str, Any]:
        return {"status": "accepted", "key": key, "message": message, "intent": intent}

    async def events_wait(
        self,
        key: str,
        since_stream_seq: int | None = None,
        timeout_ms: int = 30000,
        max_events: int = 100,
        terminal_only: bool = False,
    ) -> dict[str, Any]:
        return {
            "key": key,
            "since_stream_seq": since_stream_seq,
            "timeout_ms": timeout_ms,
            "max_events": max_events,
            "terminal_only": terminal_only,
            "events": [],
        }

    async def transcript_jsonl(self, key: str, limit: int = 1000) -> str:
        return '{"type":"message","message":{"role":"user","content":[]}}'


def test_create_mcp_server_registers_product_tools_and_resources() -> None:
    app = create_mcp_server(FakeBridge(), fastmcp_cls=FakeFastMCP)

    assert app.kwargs == {"json_response": True}
    assert set(app.tools) == {
        "conversations_list",
        "session_resolve",
        "messages_read",
        "message_detail_read",
        "messages_send",
        "events_wait",
        "transcript_export",
    }
    assert set(app.resources) == {
        "opensquilla://sessions",
        "opensquilla://sessions/{key}",
        "opensquilla://sessions/{key}/messages",
        "opensquilla://sessions/{key}/transcript.jsonl",
    }


def test_create_mcp_server_has_no_benchmark_or_mock_public_tools() -> None:
    app = create_mcp_server(FakeBridge(), fastmcp_cls=FakeFastMCP)

    names = " ".join([*app.tools, *app.resources])
    assert "benchmark" not in names
    assert "mock" not in names


def test_messages_read_tool_exposes_bounded_page_cursors() -> None:
    app = create_mcp_server(FakeBridge(), fastmcp_cls=FakeFastMCP)

    payload = asyncio.run(
        app.tools["messages_read"](
            "agent:main:main",
            limit=25,
            before="12|4",
        )
    )

    assert payload["limit"] == 25
    assert payload["before"] == "12|4"
    assert payload["after"] is None


def test_message_detail_read_tool_exposes_bounded_chunk_parameters() -> None:
    app = create_mcp_server(FakeBridge(), fastmcp_cls=FakeFastMCP)

    payload = asyncio.run(
        app.tools["message_detail_read"](
            "agent:main:main",
            "9|9",
            offset=128,
            chunk_bytes=256,
            field="text",
        )
    )

    assert payload == {
        "key": "agent:main:main",
        "cursor": "9|9",
        "offset": 128,
        "chunk_bytes": 256,
        "field": "text",
    }


def test_optional_mcp_dependency_minimum_supports_fastmcp() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    mcp_specs = pyproject["project"]["optional-dependencies"]["mcp"]

    assert "mcp>=1.2.0" in mcp_specs
