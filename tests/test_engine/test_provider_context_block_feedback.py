"""Provider projection keeps rejected-call feedback available for self-recovery."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.agent import (
    _INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY,
)
from opensquilla.provider import (
    ChatConfig,
    ContentBlockToolResult,
    ContentBlockToolUse,
    Message,
)


class CapturingProvider:
    provider_name = "fake"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        self.calls.append({"messages": messages, "tools": tools, "config": config})
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        return
        yield

    async def list_models(self) -> list[Any]:
        return []


REJECTION_TEXT = (
    "The apply_patch arguments were compacted for provider context and are not "
    "executable. The tool was not run."
)


def _blocked_history(*, blocked_last: bool) -> list[Message]:
    """History whose blocked pair sits at the tail (loop case) or mid-history."""
    messages = [
        Message(role="user", content="fix the bug"),
        Message(
            role="assistant",
            content=[
                ContentBlockToolUse(
                    id="blocked-1",
                    name="apply_patch",
                    input={_INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY: True},
                ),
            ],
        ),
        Message(
            role="user",
            content=[
                ContentBlockToolResult(
                    tool_use_id="blocked-1",
                    content=REJECTION_TEXT,
                    is_error=True,
                )
            ],
        ),
    ]
    if not blocked_last:
        messages.extend(
            [
                Message(
                    role="assistant",
                    content=[
                        ContentBlockToolUse(
                            id="ok-1", name="read_file", input={"path": "a.py"}
                        ),
                    ],
                ),
                Message(
                    role="user",
                    content=[
                        ContentBlockToolResult(
                            tool_use_id="ok-1",
                            content="file body",
                            is_error=False,
                        )
                    ],
                ),
            ]
        )
    return messages


def _tool_use_ids(messages: list[Message]) -> list[str]:
    ids: list[str] = []
    for message in messages:
        if not isinstance(message.content, list):
            continue
        for block in message.content:
            if isinstance(block, ContentBlockToolUse):
                ids.append(block.id)
    return ids


@pytest.mark.parametrize("legacy_feedback", [False, True])
def test_feedback_keeps_pair_without_an_extra_instruction(legacy_feedback: bool) -> None:
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(provider_context_block_feedback=legacy_feedback),
    )
    projected = agent._strip_provider_context_marker_replay_for_provider(
        _blocked_history(blocked_last=True)
    )
    assert "blocked-1" in _tool_use_ids(projected)
    blocked_use = projected[1].content[0]
    assert isinstance(blocked_use, ContentBlockToolUse)
    assert blocked_use.input[_INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY] is True
    assert blocked_use.input["reason"] == "provider_context_omitted"
    blocked_result = projected[2].content[0]
    assert isinstance(blocked_result, ContentBlockToolResult)
    assert blocked_result.content == REJECTION_TEXT
    assert blocked_result.is_error is True
    assert len(projected) == 3
    assert projected[-1].role == "user"
    assert projected[-1].content == [blocked_result]
    assert (
        agent.config.metadata["tool_argument_projection_replay_feedback"] == 1
    )


def test_feedback_preserves_later_results_when_model_recovered() -> None:
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(provider_context_block_feedback=True),
    )
    history = _blocked_history(blocked_last=False)
    projected = agent._strip_provider_context_marker_replay_for_provider(history)
    assert "blocked-1" in _tool_use_ids(projected)
    # The rejection is stale (model moved on) - no trailing nudge, and the
    # projection keeps the same number of messages as the input history.
    assert len(projected) == len(history)
    assert projected[-1].content[0].tool_use_id == "ok-1"


def test_feedback_does_not_mutate_input_history() -> None:
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(provider_context_block_feedback=True),
    )
    history = _blocked_history(blocked_last=True)
    original_input = dict(history[1].content[0].input)
    agent._strip_provider_context_marker_replay_for_provider(history)
    assert history[1].content[0].input == original_input
    assert len(history) == 3
