"""Provider thinking fallback and retired experiment compatibility."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ThinkingLevel, ToolResult
from opensquilla.provider import (
    ChatConfig,
    Message,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import ErrorEvent as ProviderError
from opensquilla.provider import ReasoningDeltaEvent as ProviderReasoning
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart


class _SequenceProvider:
    provider_name = "fake"

    def __init__(self, streams: list[list[Any]]) -> None:
        self.streams = streams
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        index = len(self.calls)
        self.calls.append({"messages": messages, "tools": tools, "config": config})
        events = self.streams[index] if index < len(self.streams) else self.streams[-1]
        return self._stream(events)

    async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
        for event in events:
            yield event

    async def list_models(self) -> list[Any]:
        return []


def _reasoning_only_done() -> list[Any]:
    return [
        ProviderDone(
            stop_reason="stop",
            input_tokens=10,
            output_tokens=5,
            reasoning_tokens=5,
            reasoning_content="internal reasoning",
        )
    ]


def _final_text() -> list[Any]:
    return [
        ProviderText(text="ok"),
        ProviderDone(stop_reason="stop", input_tokens=11, output_tokens=1),
    ]


def _echo_tool_call(tool_use_id: str) -> list[Any]:
    return [
        ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="echo"),
        ProviderToolUseEnd(
            tool_use_id=tool_use_id,
            tool_name="echo",
            arguments={"value": "hi"},
        ),
        ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1),
    ]


def _echo_agent(provider: _SequenceProvider, config: AgentConfig) -> Agent:
    async def tool_handler(call: object) -> ToolResult:
        return ToolResult(
            tool_use_id=getattr(call, "tool_use_id"),
            tool_name=getattr(call, "tool_name"),
            content="tool ok",
        )

    return Agent(
        provider=provider,
        config=config,
        tool_definitions=[
            ToolDefinition(
                name="echo",
                description="Echo.",
                input_schema=ToolInputSchema(
                    properties={"value": {"type": "string"}},
                    required=["value"],
                ),
            )
        ],
        tool_handler=tool_handler,
    )


@pytest.mark.asyncio
async def test_retired_experiment_config_does_not_preempt_a_normal_turn() -> None:
    provider = _SequenceProvider(
        [[ProviderReasoning(text="reasoning " * 100), *_final_text()]]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            timeout=60,
            deadline_thinking_off_margin_seconds=120,
            finalize_evidence_strict=True,
            finalize_variant_challenge=True,
            submit_review_enabled=True,
            tool_loop_observer_mode="log",
            runtime_state_capsule_mode="inject",
            text_only_tool_recovery_mode="warn_model",
            reasoning_stream_char_cap=5,
            placeholder_escalation_threshold=1,
            mid_budget_no_diff_nudge=True,
            endgame_fix_directive_margin_seconds=120,
            post_write_convergence_enabled=True,
            post_write_convergence_warn_threshold=1,
            post_write_convergence_finalize_after_warning=1,
            patch_hygiene_block_mode="protected_paths",
            scratch_verify_mirror=True,
            endgame_git_freeze_margin_seconds=120,
            endgame_git_freeze_instrumentation_exempt=True,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert not any(event.kind == "error" for event in events)
    assert next(event for event in events if event.kind == "done").text == "ok"
    assert len(provider.calls) == 1
    assert provider.calls[0]["config"].thinking is True
    assert any(event.kind == "thinking" for event in events)
    for message in provider.calls[0]["messages"]:
        if isinstance(message.content, str):
            assert not message.content.startswith(
                (
                    "Progress check: about ",
                    "Time check: about ",
                    "STOP: multiple tool calls this turn reused compacted placeholder text",
                )
            )


@pytest.mark.asyncio
async def test_reasoning_only_fallback_disables_thinking_on_retry() -> None:
    provider = _SequenceProvider([_reasoning_only_done(), _final_text()])
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            reasoning_only_thinking_fallback=True,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert [event.kind for event in events if event.kind == "error"] == []
    warning = next(
        event
        for event in events
        if event.kind == "warning" and event.code == "provider_reasoning_only_retry"
    )
    assert "thinking disabled" in warning.message
    done = next(event for event in events if event.kind == "done")
    assert done.text == "ok"
    assert len(provider.calls) == 2
    assert provider.calls[0]["config"].thinking is True
    assert provider.calls[1]["config"].thinking is False


@pytest.mark.asyncio
async def test_reasoning_only_fallback_default_off_keeps_thinking() -> None:
    provider = _SequenceProvider([_reasoning_only_done(), _final_text()])
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    warning = next(
        event
        for event in events
        if event.kind == "warning" and event.code == "provider_reasoning_only_retry"
    )
    assert "thinking disabled" not in warning.message
    assert len(provider.calls) == 2
    assert provider.calls[1]["config"].thinking is True


@pytest.mark.asyncio
async def test_reasoning_only_fallback_restores_thinking_after_retry_call() -> None:
    # Retry (thinking off) returns a tool call; the next iteration's provider
    # call must run with thinking re-enabled — the fallback is one-shot.
    provider = _SequenceProvider(
        [_reasoning_only_done(), _echo_tool_call("use-1"), _final_text()]
    )
    agent = _echo_agent(
        provider,
        AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            reasoning_only_thinking_fallback=True,
            max_iterations=5,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 3
    assert provider.calls[0]["config"].thinking is True
    assert provider.calls[1]["config"].thinking is False
    assert provider.calls[2]["config"].thinking is True


@pytest.mark.asyncio
async def test_provider_error_thinking_fallback_default_on_disables_retry() -> None:
    provider = _SequenceProvider(
        [
            [ProviderError(message="reasoning is unavailable", code="400")],
            _final_text(),
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 2
    assert provider.calls[0]["config"].thinking is True
    assert provider.calls[1]["config"].thinking is False


@pytest.mark.asyncio
async def test_provider_error_thinking_fallback_strict_off_never_disables() -> None:
    provider = _SequenceProvider(
        [
            [ProviderError(message="reasoning is unavailable", code="400")],
            _final_text(),
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            thinking=ThinkingLevel.MEDIUM,
            provider_error_thinking_fallback=False,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert any(event.kind == "error" for event in events)
    assert provider.calls
    assert all(call["config"].thinking is True for call in provider.calls)


def test_reasoning_only_thinking_fallback_env_plumbing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Helper-level check only; the full env -> bootstrap-stage -> AgentConfig
    # threading is covered in turn_runner/test_agent_bootstrap_stage_unit.py.
    from opensquilla.engine.turn_runner.agent_bootstrap_stage import (
        _bool_from_env,
    )

    monkeypatch.delenv("OPENSQUILLA_REASONING_ONLY_THINKING_FALLBACK", raising=False)
    assert _bool_from_env("OPENSQUILLA_REASONING_ONLY_THINKING_FALLBACK", False) is False
    monkeypatch.setenv("OPENSQUILLA_REASONING_ONLY_THINKING_FALLBACK", "1")
    assert _bool_from_env("OPENSQUILLA_REASONING_ONLY_THINKING_FALLBACK", False) is True


def test_agent_config_preserves_thinking_fallback_defaults() -> None:
    config = AgentConfig()

    assert config.reasoning_only_thinking_fallback is False
    assert config.provider_error_thinking_fallback is True


def test_provider_error_thinking_fallback_env_is_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opensquilla.engine.turn_runner.agent_bootstrap_stage import (
        _strict_bool_from_env,
    )

    name = "OPENSQUILLA_PROVIDER_ERROR_THINKING_FALLBACK"
    monkeypatch.delenv(name, raising=False)
    assert _strict_bool_from_env(name, True) is True
    monkeypatch.setenv(name, "off")
    assert _strict_bool_from_env(name, True) is False
    monkeypatch.setenv(name, "of")
    with pytest.raises(ValueError, match=name):
        _strict_bool_from_env(name, True)
