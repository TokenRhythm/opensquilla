"""Tests for incremental prefix caches in session_sanitize (P1)."""

from __future__ import annotations

import pytest

from opensquilla.engine.session_sanitize import (
    project_historical_tool_payloads,
    project_historical_tool_payloads_cached,
    sanitize_session_messages,
    sanitize_session_messages_cached,
)
from opensquilla.provider import (
    ContentBlockText,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
)


def _msg(role: str, text: str) -> Message:
    return Message(role=role, content=[ContentBlockText(text=text)])


def _tool_use(block_id: str, name: str, payload: str) -> Message:
    return Message(
        role="assistant",
        content=[ContentBlockToolUse(id=block_id, name=name, input={"data": payload})],
    )


def _tool_result(block_id: str, content: str, is_error: bool = False) -> Message:
    return Message(
        role="user",
        content=[ContentBlockToolResult(tool_use_id=block_id, content=content, is_error=is_error)],
    )


def test_sanitize_cache_matches_uncached_across_appends() -> None:
    messages = [_msg("user", "hello"), _msg("assistant", "hi")]
    cache = None
    for _ in range(3):
        messages.append(_msg("user", "more" if len(messages) % 2 else "again"))
        messages.append(_msg("assistant", "ok"))
        cached, result, cache = sanitize_session_messages_cached(messages, cache)
        plain, plain_result = sanitize_session_messages(messages)
        assert len(cached) == len(plain)
        assert [m.role for m in cached] == [m.role for m in plain]
        assert result.messages_out == plain_result.messages_out


def test_project_cache_matches_uncached_with_recoverable_refs() -> None:
    big = "x" * 200_000
    messages = [
        _tool_use("tu1", "write_file", big),
        _tool_result("tu1", big),
        _msg("user", "continue"),
    ]
    cache = None
    refs = frozenset()
    for _ in range(3):
        messages.append(_msg("user", "next"))
        messages.append(_msg("assistant", "ok"))
        cached, result, cache = project_historical_tool_payloads_cached(
            messages,
            preserve_reasoning_content=False,
            recoverable_references=refs,
            cache=cache,
        )
        plain, plain_result = project_historical_tool_payloads(
            messages,
            preserve_reasoning_content=False,
            recoverable_references=refs,
        )
        assert len(cached) == len(plain)
        assert result.messages_out == plain_result.messages_out
        assert result.tool_uses_projected == plain_result.tool_uses_projected


def test_project_cache_invalidates_on_recoverable_refs_change() -> None:
    big = "x" * 200_000
    messages = [
        _tool_use("tu1", "write_file", big),
        _tool_result("tu1", big),
    ]
    cache = None
    _, _, cache = project_historical_tool_payloads_cached(
        messages,
        preserve_reasoning_content=False,
        recoverable_references=frozenset(),
        cache=cache,
    )
    # Change the external reference set: cache must fall back to full compute.
    other_refs = frozenset({("store:test", "abc123")})
    cached, result, cache = project_historical_tool_payloads_cached(
        messages,
        preserve_reasoning_content=False,
        recoverable_references=other_refs,
        cache=cache,
    )
    plain, plain_result = project_historical_tool_payloads(
        messages,
        preserve_reasoning_content=False,
        recoverable_references=other_refs,
    )
    assert len(cached) == len(plain)
    assert result.messages_out == plain_result.messages_out
