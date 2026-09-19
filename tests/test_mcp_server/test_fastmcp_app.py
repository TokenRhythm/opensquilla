from __future__ import annotations

import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement

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

    async def messages_read(self, key: str, limit: int = 1000) -> dict[str, Any]:
        return {"messages": [{"role": "user", "text": key}], "limit": limit}

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

    assert app.kwargs == {}
    assert set(app.tools) == {
        "conversations_list",
        "session_resolve",
        "messages_read",
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


def test_optional_mcp_dependency_excludes_vulnerable_fastmcp_releases() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["optional-dependencies"]["mcp"] == []
    mcp_specs = pyproject["project"]["dependencies"]

    requirement = next(Requirement(spec) for spec in mcp_specs if spec.startswith("mcp"))
    assert "2.2.0" in requirement.specifier
    assert "3.0.0" not in requirement.specifier
    assert "1.28.1" not in requirement.specifier
    assert "1.28.0" not in requirement.specifier
    assert "2.0.0" not in requirement.specifier
