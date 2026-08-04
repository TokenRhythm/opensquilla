from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from opensquilla.gateway_client import GatewayRPCError
from opensquilla.mcp_server.bridge import OpenSquillaMCPBridge


class FakeGatewayClient:
    def __init__(self) -> None:
        self.connected_url: str | None = None
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def connect(self, url: str) -> None:
        self.connected_url = url

    async def close(self) -> None:
        self.closed = True

    async def list_sessions(self, limit: int = 50) -> dict[str, Any]:
        self.calls.append(("sessions.list", {"limit": limit}))
        return {"sessions": [{"key": "agent:main:main", "entry_count": 2}], "count": 1}

    async def resolve_session(self, key: str) -> dict[str, Any]:
        self.calls.append(("sessions.resolve", {"key": key}))
        return {"key": key, "session_id": "sid-1", "agent_id": "main"}

    async def session_history(
        self,
        session_key: str,
        limit: int = 1000,
        *,
        before: str | None = None,
        after: str | None = None,
        include_canonical: bool | None = None,
        include_summaries: bool | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"sessionKey": session_key, "limit": limit}
        if before is not None:
            params["before"] = before
        if after is not None:
            params["after"] = after
        if include_canonical is not None:
            params["includeCanonical"] = include_canonical
        if include_summaries is not None:
            params["includeSummaries"] = include_summaries
        self.calls.append(("chat.history", params))
        return {
            "messages": [
                {
                    "id": "m1",
                    "role": "user",
                    "text": "hello",
                    "timestamp": 1,
                },
                {
                    "id": "m2",
                    "role": "assistant",
                    "text": "looked it up",
                    "timestamp": 2,
                    "tool_calls": [
                        {
                            "type": "tool_use",
                            "tool_use_id": "tool-1",
                            "name": "lookup",
                            "input": {"q": "hello"},
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "name": "lookup",
                            "result": "world",
                            "is_error": False,
                            "execution_status": {
                                "version": 1,
                                "status": "success",
                                "exit_code": 0,
                                "timed_out": False,
                                "truncated": False,
                                "reason": None,
                                "source": "adapter",
                                "preservation_class": "normal",
                            },
                        },
                    ],
                },
            ]
        }

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, params))
        if method == "sessions.messages.subscribe":
            return {
                "subscribed": True,
                "key": params["key"],
                "current_stream_seq": 7,
                "replay_complete": True,
            }
        if method == "sessions.send":
            return {"status": "accepted", "key": params["key"], "task_id": "task-1"}
        raise AssertionError(f"unexpected method: {method}")

    async def recv_event(self, timeout: float | None = None) -> dict[str, Any]:
        if timeout is None:
            return await self.events.get()
        return await asyncio.wait_for(self.events.get(), timeout=timeout)


class PagedHistoryGatewayClient(FakeGatewayClient):
    def __init__(
        self,
        pages: dict[str | None, dict[str, Any]],
        *,
        detail_message: bytes | None = None,
    ) -> None:
        super().__init__()
        self.pages = pages
        self.detail_message = detail_message

    async def session_history(
        self,
        session_key: str,
        limit: int = 1000,
        *,
        before: str | None = None,
        after: str | None = None,
        include_canonical: bool | None = None,
        include_summaries: bool | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"sessionKey": session_key, "limit": limit}
        if before is not None:
            params["before"] = before
        if after is not None:
            params["after"] = after
        if include_canonical is not None:
            params["includeCanonical"] = include_canonical
        if include_summaries is not None:
            params["includeSummaries"] = include_summaries
        self.calls.append(("chat.history", params))
        return dict(self.pages[before])

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if method != "chat.history.entry.v1":
            return await super().call(method, params)
        assert params is not None
        assert self.detail_message is not None
        self.calls.append((method, dict(params)))
        offset = int(params["offset"])
        end = min(len(self.detail_message), offset + int(params["chunkBytes"]))
        return {
            "session_key": params["sessionKey"],
            "cursor": params["cursor"],
            "encoding": "base64",
            "field": params["field"],
            "content_type": "application/json",
            "chunk_base64": base64.b64encode(self.detail_message[offset:end]).decode("ascii"),
            "offset": offset,
            "next": end if end < len(self.detail_message) else None,
            "total": len(self.detail_message),
            "sha256": hashlib.sha256(self.detail_message).hexdigest(),
        }


@pytest.mark.asyncio
async def test_bridge_reuses_gateway_read_rpcs() -> None:
    client = FakeGatewayClient()
    bridge = OpenSquillaMCPBridge(
        gateway_url="ws://127.0.0.1:18791/ws",
        gateway_client_factory=lambda: client,
    )

    sessions = await bridge.conversations_list(limit=10)
    resolved = await bridge.session_resolve("agent:main:main")
    messages = await bridge.messages_read("agent:main:main", limit=5)

    assert client.connected_url == "ws://127.0.0.1:18791/ws"
    assert sessions["sessions"][0]["key"] == "agent:main:main"
    assert resolved["session_id"] == "sid-1"
    assert messages["messages"][0]["text"] == "hello"
    assert client.calls[:3] == [
        ("sessions.list", {"limit": 10}),
        ("sessions.resolve", {"key": "agent:main:main"}),
        ("chat.history", {"sessionKey": "agent:main:main", "limit": 5}),
    ]


@pytest.mark.asyncio
async def test_bridge_messages_read_forwards_bounded_page_cursors() -> None:
    client = FakeGatewayClient()
    bridge = OpenSquillaMCPBridge(gateway_client_factory=lambda: client)

    await bridge.messages_read("agent:main:main", limit=25, before="12|4")
    await bridge.messages_read("agent:main:main", limit=10, after="18|9")

    assert client.calls == [
        (
            "chat.history",
            {"sessionKey": "agent:main:main", "limit": 25, "before": "12|4"},
        ),
        (
            "chat.history",
            {"sessionKey": "agent:main:main", "limit": 10, "after": "18|9"},
        ),
    ]


@pytest.mark.asyncio
async def test_transcript_jsonl_pages_up_to_total_limit_in_chronological_order() -> None:
    pages = {
        None: {
            "messages": [
                {"message_id": "m3", "role": "user", "text": "three", "timestamp": 3},
                {
                    "message_id": "m4",
                    "role": "assistant",
                    "text": "four",
                    "timestamp": 4,
                },
            ],
            "has_more": True,
            "oldest_cursor": "3|3",
            "newest_cursor": "4|4",
            "canonical_available": True,
            "canonical_complete": True,
        },
        "3|3": {
            "messages": [
                {"message_id": "m1", "role": "user", "text": "one", "timestamp": 1},
                {
                    "message_id": "m2",
                    "role": "assistant",
                    "text": "two",
                    "timestamp": 2,
                },
            ],
            "has_more": False,
            "oldest_cursor": "1|1",
            "newest_cursor": "2|2",
            "canonical_available": True,
            "canonical_complete": True,
        },
    }
    client = PagedHistoryGatewayClient(pages)
    bridge = OpenSquillaMCPBridge(gateway_client_factory=lambda: client)

    transcript = await bridge.transcript_jsonl("agent:main:main", limit=4)

    rows = [json.loads(line) for line in transcript.splitlines()]
    assert [row["message"]["content"][0]["text"] for row in rows] == [
        "one",
        "two",
        "three",
        "four",
    ]
    history_calls = [params for method, params in client.calls if method == "chat.history"]
    assert [params.get("before") for params in history_calls] == [None, "3|3"]
    assert [params["limit"] for params in history_calls] == [4, 2]
    assert all(params["includeCanonical"] is True for params in history_calls)
    assert all(params["includeSummaries"] is False for params in history_calls)


@pytest.mark.asyncio
async def test_transcript_jsonl_limit_is_total_not_page_size() -> None:
    pages = {
        None: {
            "messages": [
                {"message_id": "m3", "role": "user", "text": "three", "timestamp": 3},
                {"message_id": "m4", "role": "assistant", "text": "four", "timestamp": 4},
            ],
            "has_more": True,
            "oldest_cursor": "3|3",
            "newest_cursor": "4|4",
            "canonical_available": True,
            "canonical_complete": True,
        },
        "3|3": {
            "messages": [
                {"message_id": "m1", "role": "user", "text": "one", "timestamp": 1},
                {"message_id": "m2", "role": "assistant", "text": "two", "timestamp": 2},
            ],
            "has_more": False,
            "oldest_cursor": "1|1",
            "newest_cursor": "2|2",
            "canonical_available": True,
            "canonical_complete": True,
        },
    }
    client = PagedHistoryGatewayClient(pages)
    bridge = OpenSquillaMCPBridge(gateway_client_factory=lambda: client)

    transcript = await bridge.transcript_jsonl("agent:main:main", limit=2)

    rows = [json.loads(line) for line in transcript.splitlines()]
    assert [row["message"]["content"][0]["text"] for row in rows] == ["three", "four"]
    history_calls = [params for method, params in client.calls if method == "chat.history"]
    assert len(history_calls) == 1
    assert history_calls[0]["limit"] == 2


@pytest.mark.asyncio
async def test_transcript_jsonl_resolves_giant_detail_without_dropping_preview() -> None:
    content = "详情开头🦐" + ("x" * 1_100_000) + "详情结尾"
    full_message = {
        "message_id": "giant-message",
        "transcript_id": 9,
        "role": "assistant",
        "text": content,
        "timestamp": 9,
    }
    detail_message = json.dumps(
        full_message,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    pages = {
        None: {
            "messages": [
                {
                    "message_id": "giant-message",
                    "transcript_id": 9,
                    "role": "assistant",
                    "preview": "详情开头🦐",
                    "timestamp": 9,
                    "original_bytes": len(detail_message),
                    "detail_ref": {
                        "method": "chat.history.entry.v1",
                        "sessionKey": "agent:main:main",
                        "cursor": "9|9",
                    },
                    "truncated_by_bytes": True,
                }
            ],
            "has_more": False,
            "oldest_cursor": "9|9",
            "newest_cursor": "9|9",
            "canonical_available": True,
            "canonical_complete": True,
        }
    }
    client = PagedHistoryGatewayClient(pages, detail_message=detail_message)
    bridge = OpenSquillaMCPBridge(gateway_client_factory=lambda: client)

    transcript = await bridge.transcript_jsonl("agent:main:main")

    row = json.loads(transcript)
    assert row["message"]["role"] == "assistant"
    assert row["message"]["content"][0]["text"] == content
    detail_calls = [
        params for method, params in client.calls if method == "chat.history.entry.v1"
    ]
    assert len(detail_calls) > 2
    assert [params["offset"] for params in detail_calls] == [
        index * 128 * 1024 for index in range(len(detail_calls))
    ]
    assert all(params["field"] == "message" for params in detail_calls)


@pytest.mark.parametrize("preview_field", ["preview", "text"])
@pytest.mark.asyncio
async def test_transcript_jsonl_rejects_any_truncated_message_without_detail_ref(
    preview_field: str,
) -> None:
    pages = {
        None: {
            "messages": [
                {
                    "message_id": "truncated-message",
                    "transcript_id": 9,
                    "role": "assistant",
                    preview_field: "incomplete preview",
                    "timestamp": 9,
                    "truncated_by_bytes": True,
                }
            ],
            "has_more": False,
            "oldest_cursor": "9|9",
            "newest_cursor": "9|9",
            "canonical_available": True,
            "canonical_complete": True,
        }
    }
    client = PagedHistoryGatewayClient(pages)
    bridge = OpenSquillaMCPBridge(gateway_client_factory=lambda: client)

    with pytest.raises(GatewayRPCError) as exc_info:
        await bridge.transcript_jsonl("agent:main:main")

    assert exc_info.value.code == "HISTORY_DETAIL_REFERENCE_MISSING"


@pytest.mark.asyncio
async def test_bridge_message_detail_read_exposes_one_bounded_chunk() -> None:
    detail_message = b'{"message_id":"m1","text":"hello"}'
    client = PagedHistoryGatewayClient({}, detail_message=detail_message)
    bridge = OpenSquillaMCPBridge(gateway_client_factory=lambda: client)

    chunk = await bridge.message_detail_read(
        "agent:main:main",
        "1|1",
        offset=5,
        chunk_bytes=7,
    )

    assert base64.b64decode(chunk["chunk_base64"]) == detail_message[5:12]
    assert client.calls[-1] == (
        "chat.history.entry.v1",
        {
            "sessionKey": "agent:main:main",
            "cursor": "1|1",
            "offset": 5,
            "chunkBytes": 7,
            "field": "message",
        },
    )


@pytest.mark.asyncio
async def test_transcript_jsonl_preserves_tool_result_execution_status() -> None:
    client = FakeGatewayClient()
    bridge = OpenSquillaMCPBridge(gateway_client_factory=lambda: client)

    transcript = await bridge.transcript_jsonl("agent:main:main", limit=5)

    rows = [json.loads(line) for line in transcript.splitlines()]
    tool_result_message = rows[2]["message"]
    assert tool_result_message["isError"] is False
    assert tool_result_message["executionStatus"]["status"] == "success"


@pytest.mark.asyncio
async def test_messages_send_subscribes_before_accepting_turn() -> None:
    client = FakeGatewayClient()
    bridge = OpenSquillaMCPBridge(gateway_client_factory=lambda: client)

    result = await bridge.messages_send("agent:main:main", "continue", intent="continue")

    assert result == {
        "status": "accepted",
        "key": "agent:main:main",
        "task_id": "task-1",
        "current_stream_seq": 7,
        "replay_complete": True,
        "replay_gap_reason": None,
    }
    assert client.closed is True
    assert client.calls == [
        (
            "sessions.messages.subscribe",
            {
                "key": "agent:main:main",
                "since_stream_seq": None,
                "fast_ack": True,
            },
        ),
        (
            "sessions.send",
            {
                "key": "agent:main:main",
                "message": "continue",
                "attachments": [],
                "intent": "continue",
                "_source": {
                    "caller_kind": "cli",
                    "channel_kind": "cli",
                    "channel_id": "mcp:bridge",
                    "source_kind": "mcp",
                    "source_name": "mcp_server",
                },
            },
        ),
    ]


@pytest.mark.asyncio
async def test_events_wait_returns_session_events_until_terminal() -> None:
    client = FakeGatewayClient()
    await client.events.put(
        {
            "event": "session.event.text_delta",
            "payload": {"session_key": "agent:main:main", "stream_seq": 8, "text": "hi"},
        }
    )
    await client.events.put(
        {
            "event": "session.event.done",
            "payload": {"session_key": "agent:main:main", "stream_seq": 9, "reason": "stop"},
        }
    )
    bridge = OpenSquillaMCPBridge(gateway_client_factory=lambda: client)

    result = await bridge.events_wait("agent:main:main", since_stream_seq=7, timeout_ms=1000)

    assert result["current_stream_seq"] == 9
    assert [event["event"] for event in result["events"]] == [
        "session.event.text_delta",
        "session.event.done",
    ]


@pytest.mark.asyncio
async def test_transcript_jsonl_exports_standard_tool_evidence() -> None:
    bridge = OpenSquillaMCPBridge(gateway_client_factory=FakeGatewayClient)

    text = await bridge.transcript_jsonl("agent:main:main")
    rows = [json.loads(line) for line in text.splitlines()]

    assert rows[0]["message"]["role"] == "user"
    assert rows[1]["message"]["role"] == "assistant"
    assert rows[1]["message"]["content"][0]["type"] == "toolCall"
    assert rows[1]["message"]["content"][0]["name"] == "lookup"
    assert rows[2]["message"]["role"] == "toolResult"
    assert rows[2]["message"]["toolCallId"] == "tool-1"


def test_mcp_server_package_does_not_import_cli_layer() -> None:
    package_root = Path("src/opensquilla/mcp_server")
    imported_modules: set[str] = set()
    for path in package_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)

    assert not any(module.startswith("opensquilla.cli") for module in imported_modules)
    assert not any(
        module == "opensquilla.gateway" or module.startswith("opensquilla.gateway.")
        for module in imported_modules
    )


@pytest.mark.asyncio
async def test_events_wait_uses_dedicated_connection_and_closes_it() -> None:
    clients: list[FakeGatewayClient] = []
    event_client = FakeGatewayClient()

    def factory() -> FakeGatewayClient:
        if len(clients) == 1:
            clients.append(event_client)
            return event_client
        client = FakeGatewayClient()
        clients.append(client)
        return client

    bridge = OpenSquillaMCPBridge(gateway_client_factory=factory)
    await bridge.conversations_list()
    await clients[0].events.put(
        {
            "event": "session.event.done",
            "payload": {"session_key": "agent:main:main", "stream_seq": 3},
        }
    )
    await event_client.events.put(
        {
            "event": "session.event.done",
            "payload": {"session_key": "agent:main:main", "stream_seq": 8},
        }
    )

    result = await bridge.events_wait("agent:main:main", timeout_ms=1000)

    assert len(clients) == 2
    assert result["current_stream_seq"] == 8
    assert clients[0].closed is False
    assert event_client.closed is True
