"""Retired dedup configuration preserves history and shared projection contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from opensquilla.engine import Agent, AgentConfig
from opensquilla.provider import (
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
)
from opensquilla.provider.types import ChatConfig


class _StubProvider:
    provider_name = "fake"

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        return
        yield

    async def list_models(self) -> list[Any]:
        return []


# Repeated, nontrivial results must remain independent tool transactions.
BIG_RESULT = "line of grep output\n" * 60


def _pair(use_id: str, content: str, *, is_error: bool = False) -> list[Message]:
    return [
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id=use_id, name="grep_search", input={"pattern": "foo"}
                ),
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id=use_id, content=content, is_error=is_error
                )
            ],
        ),
    ]


def _history(*contents: tuple[str, str]) -> list[Message]:
    messages: list[Message] = [Message(role="user", content="find the bug")]
    for use_id, content in contents:
        messages.extend(_pair(use_id, content))
    return messages


def _tool_results(messages: list[Message]) -> list[ContentBlockToolResult]:
    results: list[ContentBlockToolResult] = []
    for message in messages:
        if not isinstance(message.content, list):
            continue
        for block in message.content:
            if isinstance(block, ContentBlockToolResult):
                results.append(block)
    return results


def test_tool_result_artifact_detection_is_cached() -> None:
    from opensquilla.engine.agent import _tool_result_content_has_artifact

    content = '{"status": "published", "marker": "cache-test-unique-9f3"}'
    _tool_result_content_has_artifact.cache_clear()
    before = _tool_result_content_has_artifact.cache_info()
    assert _tool_result_content_has_artifact(content) is True
    assert _tool_result_content_has_artifact(content) is True
    after = _tool_result_content_has_artifact.cache_info()
    assert after.hits == before.hits + 1
    assert after.misses == before.misses + 1


def test_retired_dedup_config_cannot_elide_repeated_results() -> None:
    agent = Agent(
        provider=_StubProvider(),
        config=AgentConfig(
            provider_history_dedup_enabled=True,
            provider_history_dedup_min_repeats=2,
        ),
    )
    history = _history(("g1", BIG_RESULT), ("g2", BIG_RESULT), ("g3", BIG_RESULT))
    projected = agent._provider_request_messages(
        history,
        request_context_message=None,
        request_context_insert_index=0,
        runtime_context_message=Message(role="user", content="[Runtime context for this turn]"),
        runtime_context_insert_index=0,
    )
    assert [r.content for r in _tool_results(projected)] == [BIG_RESULT] * 3
    assert [r.content for r in _tool_results(history)] == [BIG_RESULT] * 3
    assert not any(key.startswith("provider_history_dedup") for key in agent.config.metadata)


def test_historical_dedup_marker_is_not_frozen_as_standalone_evidence() -> None:
    from opensquilla.engine.agent import _tool_result_content_is_provider_projection

    marker = "[duplicate_tool_result_elided]\nidentical_to_tool_use_id: g2\n"
    assert _tool_result_content_is_provider_projection(marker)
    agent = Agent(provider=_StubProvider(), config=AgentConfig())
    history = _history(("g1", marker), ("g2", BIG_RESULT))
    agent._remember_provider_visible_tool_results(history)
    assert "g1" not in agent._provider_tool_result_frozen_full_ids
    assert "g1" not in agent._provider_tool_result_frozen_overrides
    assert "g2" in agent._provider_tool_result_frozen_full_ids
