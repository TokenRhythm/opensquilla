"""Provider projection keeps rejected-call feedback available for self-recovery."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.agent import (
    _IDENTICAL_REQUEST_LOOP_NUDGE,
    _INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY,
    _PROVIDER_CONTEXT_REPAIR_PROMPT,
)
from opensquilla.provider import (
    ChatConfig,
    ContentBlockText,
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
def test_feedback_keeps_pair_and_appends_repair_prompt(legacy_feedback: bool) -> None:
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
    assert projected[-1].role == "user"
    assert projected[-1].content == _PROVIDER_CONTEXT_REPAIR_PROMPT
    assert (
        agent.config.metadata["tool_argument_projection_replay_feedback"] == 1
    )


def test_feedback_skips_repair_prompt_when_model_recovered() -> None:
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


def _request(text: str) -> list[Message]:
    return [Message(role="user", content=text)]


def test_loop_break_off_by_default() -> None:
    agent = Agent(provider=CapturingProvider(), config=AgentConfig())
    assert AgentConfig().identical_request_loop_break_threshold == 0
    for _ in range(10):
        action = agent._identical_request_loop_break_action(
            _request("same"), first_attempt=True
        )
        assert action is None


def test_loop_break_perturbs_at_threshold_and_aborts_at_double() -> None:
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(identical_request_loop_break_threshold=3),
    )
    actions = [
        agent._identical_request_loop_break_action(_request("same"), first_attempt=True)
        for _ in range(6)
    ]
    assert actions == [None, None, "perturb", "perturb", "perturb", "abort"]


def test_loop_break_resets_on_new_payload() -> None:
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(identical_request_loop_break_threshold=3),
    )
    for _ in range(2):
        agent._identical_request_loop_break_action(_request("same"), first_attempt=True)
    assert (
        agent._identical_request_loop_break_action(
            _request("different"), first_attempt=True
        )
        is None
    )
    assert agent._identical_request_streak == 1


def test_loop_break_retry_attempts_do_not_advance_streak() -> None:
    agent = Agent(
        provider=CapturingProvider(),
        config=AgentConfig(identical_request_loop_break_threshold=3),
    )
    agent._identical_request_loop_break_action(_request("same"), first_attempt=True)
    for _ in range(10):
        action = agent._identical_request_loop_break_action(
            _request("same"), first_attempt=False
        )
        assert action is None
    assert agent._identical_request_streak == 1


def test_loop_break_nudge_names_recovery_actions() -> None:
    assert "Do not repeat the previous tool call" in _IDENTICAL_REQUEST_LOOP_NUDGE


def test_append_nudge_merges_into_trailing_string_user_message() -> None:
    agent = Agent(provider=CapturingProvider(), config=AgentConfig())
    request_messages = [
        Message(role="assistant", content="thinking"),
        Message(role="user", content="please retry"),
    ]
    result = agent._append_identical_request_loop_nudge(request_messages)
    assert len(result) == len(request_messages)
    assert result[-1].role == "user"
    assert "please retry" in result[-1].content
    assert _IDENTICAL_REQUEST_LOOP_NUDGE in result[-1].content


def test_append_nudge_merges_into_trailing_list_user_message() -> None:
    agent = Agent(provider=CapturingProvider(), config=AgentConfig())
    tool_result = ContentBlockToolResult(
        tool_use_id="t1", content="result body", is_error=False
    )
    request_messages = [
        Message(role="assistant", content=[ContentBlockToolUse(id="t1", name="grep", input={})]),
        Message(role="user", content=[tool_result]),
    ]
    result = agent._append_identical_request_loop_nudge(request_messages)
    assert len(result) == len(request_messages)
    assert result[-1].role == "user"
    assert isinstance(result[-1].content, list)
    assert result[-1].content[0] is tool_result
    assert len(result[-1].content) == 2
    nudge_block = result[-1].content[1]
    assert isinstance(nudge_block, ContentBlockText)
    assert nudge_block.text == _IDENTICAL_REQUEST_LOOP_NUDGE
    # Original message is untouched (no in-place mutation).
    assert len(request_messages[-1].content) == 1


def test_append_nudge_appends_new_message_when_trailing_is_assistant() -> None:
    agent = Agent(provider=CapturingProvider(), config=AgentConfig())
    request_messages = [
        Message(role="user", content="do it"),
        Message(role="assistant", content="ok, doing it"),
    ]
    result = agent._append_identical_request_loop_nudge(request_messages)
    assert len(result) == len(request_messages) + 1
    assert result[-1].role == "user"
    assert result[-1].content == _IDENTICAL_REQUEST_LOOP_NUDGE


def test_env_plumbing_for_both_levers(monkeypatch) -> None:
    from opensquilla.engine.turn_runner.agent_bootstrap_stage import (
        _bool_from_env,
        _nonnegative_int_from_env,
    )

    assert _bool_from_env("OPENSQUILLA_PROVIDER_CONTEXT_BLOCK_FEEDBACK", False) is False
    assert (
        _nonnegative_int_from_env("OPENSQUILLA_IDENTICAL_REQUEST_LOOP_BREAK", 0) == 0
    )
    monkeypatch.setenv("OPENSQUILLA_PROVIDER_CONTEXT_BLOCK_FEEDBACK", "1")
    monkeypatch.setenv("OPENSQUILLA_IDENTICAL_REQUEST_LOOP_BREAK", "3")
    assert _bool_from_env("OPENSQUILLA_PROVIDER_CONTEXT_BLOCK_FEEDBACK", False) is True
    assert (
        _nonnegative_int_from_env("OPENSQUILLA_IDENTICAL_REQUEST_LOOP_BREAK", 0) == 3
    )
