from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from opensquilla.engine.history import reconstruct_messages_from_entry
from opensquilla.engine.runtime import TurnRunner
from opensquilla.provider import (
    ContentBlockText,
    ContentBlockToolResult,
    ContentBlockToolUse,
)


def test_provider_history_cleans_goal_text_segments_without_mutating_source() -> None:
    raw_segments = [
        {"type": "text", "text": "NO_REPLY\nStill checking."},
        {
            "type": "tool_use",
            "tool_use_id": "call-1",
            "name": "read_status",
            "input": {"id": "job-1"},
        },
        {
            "type": "tool_result",
            "tool_use_id": "call-1",
            "result": "pending",
            "is_error": False,
        },
        {"type": "text", "text": "HEARTBEAT_OK"},
    ]

    messages = reconstruct_messages_from_entry(
        "assistant",
        "NO_REPLY\nStill checking.",
        raw_segments,
        turn_context={"intent": "goal_continuation"},
    )

    assert len(messages) == 2
    assert messages[0].role == "assistant"
    assert isinstance(messages[0].content, list)
    assert isinstance(messages[0].content[0], ContentBlockText)
    assert messages[0].content[0].text == "Still checking."
    assert isinstance(messages[0].content[1], ContentBlockToolUse)
    assert messages[1].role == "user"
    assert isinstance(messages[1].content, list)
    assert isinstance(messages[1].content[0], ContentBlockToolResult)
    assert messages[1].content[0].content == "pending"
    assert raw_segments[0]["text"] == "NO_REPLY\nStill checking."
    assert raw_segments[-1]["text"] == "HEARTBEAT_OK"


def test_provider_history_suppresses_split_goal_marker_around_tool_pair() -> None:
    raw_segments = [
        {"type": "text", "text": "NO_"},
        {
            "type": "tool_use",
            "tool_use_id": "call-split",
            "name": "read_status",
            "input": {"id": "job-split"},
        },
        {
            "type": "tool_result",
            "tool_use_id": "call-split",
            "name": "read_status",
            "result": "pending",
            "is_error": False,
        },
        {"type": "text", "text": "REPLY"},
    ]

    messages = reconstruct_messages_from_entry(
        "assistant",
        "NO_REPLY",
        raw_segments,
        turn_context={"intent": "goal_continuation"},
    )

    assert [message.role for message in messages] == ["assistant", "user"]
    first_assistant = messages[0].content
    tool_results = messages[1].content
    assert isinstance(first_assistant, list)
    assert isinstance(first_assistant[0], ContentBlockToolUse)
    assert first_assistant[0].id == "call-split"
    assert isinstance(tool_results, list)
    assert isinstance(tool_results[0], ContentBlockToolResult)
    assert tool_results[0].tool_use_id == "call-split"
    assert "NO_" not in repr(messages)
    assert "REPLY" not in repr(messages)
    assert raw_segments[0]["text"] == "NO_"
    assert raw_segments[-1]["text"] == "REPLY"


def test_provider_history_keeps_unattributed_mixed_sentinel_text() -> None:
    messages = reconstruct_messages_from_entry(
        "assistant",
        "NO_REPLY\nThis is ordinary historical prose.",
        None,
    )

    assert len(messages) == 1
    assert messages[0].content == "NO_REPLY\nThis is ordinary historical prose."


def test_provider_history_drops_exact_sentinel_without_provenance() -> None:
    assert reconstruct_messages_from_entry("assistant", "HEARTBEAT_OK", None) == []


class _HistoryManager:
    def __init__(self, entries: list[object]) -> None:
        self.entries = entries

    async def get_transcript(self, _session_key: str) -> list[object]:
        return list(self.entries)

    async def get_context_states(self, _session_key: str) -> list[object]:
        return []


@pytest.mark.asyncio
async def test_turn_runner_load_history_passes_goal_provenance_to_sanitizer() -> None:
    raw = "NO_REPLY\nStill checking."
    entry = SimpleNamespace(
        role="assistant",
        content=raw,
        tool_calls=None,
        reasoning_content=None,
        message_id="message-1",
        turn_context={"intent": "goal_continuation"},
    )
    manager = _HistoryManager([entry])
    runner = TurnRunner(provider_selector=MagicMock(), session_manager=manager)
    agent = SimpleNamespace(
        provider=SimpleNamespace(provider_name="test"),
        config=SimpleNamespace(
            materialize_historical_attachments=False,
            preserve_historical_images=False,
        ),
        set_history=MagicMock(),
    )

    await runner._load_history(agent, "agent:main:test", trim_last_user=False)

    history = agent.set_history.call_args.args[0]
    assert [message.content for message in history] == ["Still checking."]
    assert entry.content == raw


def test_emergency_compaction_projection_sanitizes_without_mutating_source() -> None:
    raw_segments = [{"type": "text", "text": "HEARTBEAT_OK\nVisible warning."}]
    entry = SimpleNamespace(
        role="assistant",
        content="HEARTBEAT_OK\nVisible warning.",
        tool_calls=raw_segments,
        message_id="message-2",
        token_count=10,
        tool_call_id=None,
        reasoning_content=None,
        turn_usage=None,
        turn_context={"intent": "goal_continuation"},
    )

    projected = TurnRunner._entry_for_emergency_compaction(entry)

    assert projected["content"] == "Visible warning."
    assert projected["tool_calls"] == [
        {"type": "text", "text": "Visible warning."}
    ]
    assert projected["turn_context"] == {"intent": "goal_continuation"}
    assert entry.content == "HEARTBEAT_OK\nVisible warning."
    assert raw_segments[0]["text"].startswith("HEARTBEAT_OK")
