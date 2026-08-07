"""Opt-in levers: placeholder-offense escalation + pre-deadline wrap-up.

Covers OPENSQUILLA_PLACEHOLDER_ESCALATION_THRESHOLD and
OPENSQUILLA_DEADLINE_WRAPUP_MARGIN_SECONDS (both off by default). Motivation:
in long unattended runs, models can keep re-issuing tool calls that reference
compacted placeholders despite delivered per-call feedback, and deadline-capped
runs can be cut off mid-exploration with no wrap-up attempt.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ThinkingLevel, ToolResult
from opensquilla.engine.agent import (
    _DEADLINE_WRAPUP_DIRECTIVE_TEMPLATE,
    _INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY,
    _PLACEHOLDER_ESCALATION_DIRECTIVE,
)
from opensquilla.provider import (
    ChatConfig,
    Message,
    ModelCapabilities,
    ToolDefinition,
    ToolInputSchema,
)
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import ReasoningDeltaEvent as ProviderReasoning
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart

_WRAPUP_PREFIX = _DEADLINE_WRAPUP_DIRECTIVE_TEMPLATE.split("{minutes}", maxsplit=1)[0]


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
        self.calls.append({"messages": messages, "tools": tools})
        events = self.streams[index] if index < len(self.streams) else self.streams[-1]
        return self._stream(events)

    async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
        for event in events:
            if isinstance(event, float):
                # Wall-clock gap between stream events, for tests that need a
                # stream to span a deadline margin mid-flight.
                await asyncio.sleep(event)
                continue
            yield event

    async def list_models(self) -> list[Any]:
        return []


class _NonRetrySafeSequenceProvider(_SequenceProvider):
    retry_failed_call_safe = False


def _placeholder_tool_call(tool_use_id: str) -> list[Any]:
    return [
        ProviderToolUseStart(tool_use_id=tool_use_id, tool_name="echo"),
        ProviderToolUseEnd(
            tool_use_id=tool_use_id,
            tool_name="echo",
            arguments={_INVALID_PROVIDER_CONTEXT_ARGUMENTS_KEY: True},
        ),
        ProviderDone(stop_reason="tool_use", input_tokens=3, output_tokens=1),
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


def _empty_response() -> list[Any]:
    return [ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=0)]


def _final_text() -> list[Any]:
    return [
        ProviderText(text="done"),
        ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=1),
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


def _user_texts(messages: list[Message]) -> list[str]:
    return [
        message.content
        for message in messages
        if message.role == "user" and isinstance(message.content, str)
    ]


@pytest.mark.asyncio
async def test_placeholder_escalation_fires_at_threshold() -> None:
    provider = _SequenceProvider(
        [
            _placeholder_tool_call("blocked-1"),
            _placeholder_tool_call("blocked-2"),
            _final_text(),
        ]
    )
    agent = _echo_agent(
        provider,
        AgentConfig(
            max_iterations=5,
            placeholder_escalation_threshold=2,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 3
    # Below threshold after offense 1: no escalation in call 2.
    assert _PLACEHOLDER_ESCALATION_DIRECTIVE not in _user_texts(provider.calls[1]["messages"])
    # At threshold after offense 2: escalation delivered in call 3.
    assert _PLACEHOLDER_ESCALATION_DIRECTIVE in _user_texts(provider.calls[2]["messages"])


@pytest.mark.asyncio
async def test_placeholder_escalation_default_off() -> None:
    provider = _SequenceProvider(
        [
            _placeholder_tool_call("blocked-1"),
            _placeholder_tool_call("blocked-2"),
            _final_text(),
        ]
    )
    agent = _echo_agent(
        provider,
        AgentConfig(
            max_iterations=5,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    for call in provider.calls:
        assert _PLACEHOLDER_ESCALATION_DIRECTIVE not in _user_texts(call["messages"])


@pytest.mark.asyncio
async def test_deadline_wrapup_splices_directive_when_margin_reached() -> None:
    provider = _SequenceProvider([_final_text()])
    # margin > timeout: the wrap-up arms at the first loop-top check.
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            timeout=30.0,
            deadline_wrapup_margin_seconds=60,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    wrapup_texts = [
        text
        for text in _user_texts(provider.calls[0]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]
    assert len(wrapup_texts) == 1


@pytest.mark.asyncio
async def test_deadline_wrapup_default_off() -> None:
    provider = _SequenceProvider([_final_text()])
    agent = Agent(
        provider=provider,
        config=AgentConfig(timeout=30.0),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert not [
        text
        for text in _user_texts(provider.calls[0]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]


@pytest.mark.asyncio
async def test_deadline_wrapup_not_armed_when_margin_not_reached() -> None:
    provider = _SequenceProvider([_final_text()])
    # Large timeout, small margin: the trigger stays far in the future.
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            timeout=3600.0,
            deadline_wrapup_margin_seconds=60,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert not [
        text
        for text in _user_texts(provider.calls[0]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]


@pytest.mark.asyncio
async def test_deadline_wrapup_persists_across_calls_without_history_growth() -> None:
    provider = _SequenceProvider([_echo_tool_call("use-1"), _final_text()])
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=30.0,
            deadline_wrapup_margin_seconds=60,
            max_iterations=5,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 2
    for call in provider.calls:
        wrapup_texts = [
            text for text in _user_texts(call["messages"]) if text.startswith(_WRAPUP_PREFIX)
        ]
        # Spliced into every request exactly once, never accumulated.
        assert len(wrapup_texts) == 1
    assert not [
        message
        for message in agent._history
        if message.role == "user"
        and isinstance(message.content, str)
        and message.content.startswith(_WRAPUP_PREFIX)
    ]


@pytest.mark.asyncio
async def test_deadline_wrapup_defers_to_max_iterations_finalization() -> None:
    provider = _SequenceProvider([_echo_tool_call("use-1"), _final_text()])
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=30.0,
            deadline_wrapup_margin_seconds=60,
            max_iterations=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 2
    assert [
        text
        for text in _user_texts(provider.calls[0]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]
    finalization_texts = [
        text
        for text in _user_texts(provider.calls[1]["messages"])
        if "iteration limit has been reached" in text
    ]
    assert finalization_texts
    assert not [
        text
        for text in _user_texts(provider.calls[1]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]


@pytest.mark.asyncio
async def test_deadline_wrapup_preserves_post_tool_empty_response_recovery() -> None:
    # post_tool_empty_decision only fires on post_tool_turn=True; the spliced
    # wrap-up directive (a plain user message, always last in the request)
    # must not mask the post-tool shape of the underlying turn.
    provider = _SequenceProvider(
        [
            _echo_tool_call("use-1"),
            _empty_response(),
            _final_text(),
        ]
    )
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=30.0,
            deadline_wrapup_margin_seconds=60,
            max_iterations=5,
            post_tool_empty_recovery_mode="warn_model",
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert any(
        event.kind == "warning" and event.code == "post_tool_empty_recovery" for event in events
    )
    assert len(provider.calls) == 3
    assert any(
        text.startswith("[Runtime recovery]") for text in _user_texts(provider.calls[2]["messages"])
    )


@pytest.mark.asyncio
async def test_deadline_wrapup_skips_splice_on_reasoning_prefill_tail() -> None:
    provider = _SequenceProvider(
        [
            [
                ProviderDone(
                    stop_reason="stop",
                    input_tokens=10,
                    output_tokens=5,
                    reasoning_tokens=5,
                    reasoning_content="internal reasoning",
                )
            ],
            _final_text(),
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            timeout=30.0,
            deadline_wrapup_margin_seconds=60,
            thinking=ThinkingLevel.MEDIUM,
            model_capabilities=ModelCapabilities(
                supports_reasoning=True,
                supports_tools=True,
                reasoning_format="openrouter",
            ),
            reasoning_prefill_recovery_mode="recover",
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 2
    assert [
        text
        for text in _user_texts(provider.calls[0]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]
    # The prefill continuation keeps the assistant tail last; the wrap-up is
    # withheld for that request rather than displacing the prefill.
    prefill_tail = provider.calls[1]["messages"][-1]
    assert prefill_tail.role == "assistant"
    assert prefill_tail.reasoning_content == "internal reasoning"
    assert not [
        text
        for text in _user_texts(provider.calls[1]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]


@pytest.mark.asyncio
async def test_deadline_wrapup_preempts_reasoning_only_stream() -> None:
    # A reasoning-only stream that spans the margin boundary is preempted while
    # margin remains; the retried call carries the directive. Without the
    # preempt, arming waits for the next iteration boundary, which for this
    # stream would land past the hard deadline with no directive delivered.
    provider = _SequenceProvider(
        [
            [
                ProviderReasoning(text="deep thought"),
                2.5,
                ProviderReasoning(text="more thought"),
                ProviderText(text="never reached in the preempted attempt"),
                ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=2),
            ],
            _final_text(),
        ]
    )
    # timeout 8 / margin 6: the preempt threshold sits 2s in; the first
    # reasoning delta arrives before it, the second lands past it.
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            timeout=8.0,
            deadline_wrapup_margin_seconds=6,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 2
    assert not [
        text
        for text in _user_texts(provider.calls[0]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]
    wrapup_texts = [
        text
        for text in _user_texts(provider.calls[1]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]
    assert len(wrapup_texts) == 1


@pytest.mark.asyncio
async def test_deadline_wrapup_does_not_replay_non_retry_safe_provider() -> None:
    provider = _NonRetrySafeSequenceProvider(
        [
            [
                ProviderReasoning(text="deep thought"),
                2.5,
                ProviderReasoning(text="more thought"),
                ProviderText(text="composite completed"),
                ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=2),
            ],
            _final_text(),
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            timeout=8.0,
            deadline_wrapup_margin_seconds=6,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 1
    assert not [
        text
        for text in _user_texts(provider.calls[0]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]


@pytest.mark.asyncio
async def test_deadline_wrapup_preempt_skips_streams_past_reasoning_phase() -> None:
    # Once the attempt has emitted user-visible output, the stream may be
    # writing the final answer; it must run to completion even when a late
    # reasoning delta arrives inside the margin.
    provider = _SequenceProvider(
        [
            [
                ProviderText(text="working on it"),
                2.5,
                ProviderReasoning(text="late reasoning"),
                ProviderText(text=" done"),
                ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=2),
            ],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            timeout=8.0,
            deadline_wrapup_margin_seconds=6,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 1
    assert not [
        text
        for text in _user_texts(provider.calls[0]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]


@pytest.mark.asyncio
async def test_deadline_wrapup_preempt_default_off() -> None:
    # Margin unset: the same margin-spanning reasoning stream runs to
    # completion in a single call with no directive anywhere.
    provider = _SequenceProvider(
        [
            [
                ProviderReasoning(text="deep thought"),
                2.5,
                ProviderReasoning(text="more thought"),
                ProviderText(text="answer"),
                ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=2),
            ],
        ]
    )
    agent = Agent(provider=provider, config=AgentConfig(timeout=8.0))

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 1
    for call in provider.calls:
        assert not [
            text
            for text in _user_texts(call["messages"])
            if text.startswith(_WRAPUP_PREFIX)
        ]


@pytest.mark.asyncio
async def test_deadline_wrapup_preempt_one_shot_once_armed() -> None:
    # Already-armed turns keep the upstream behavior: the directive is in the
    # request, and the reasoning stream runs to completion without a second
    # splice or a preempt retry.
    provider = _SequenceProvider(
        [
            [
                ProviderReasoning(text="thinking"),
                0.2,
                ProviderReasoning(text="still thinking"),
                ProviderText(text="answer"),
                ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=2),
            ],
        ]
    )
    # margin > timeout: armed at the first loop-top check, before streaming.
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            timeout=30.0,
            deadline_wrapup_margin_seconds=60,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    assert len(provider.calls) == 1
    wrapup_texts = [
        text
        for text in _user_texts(provider.calls[0]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]
    assert len(wrapup_texts) == 1


@pytest.mark.asyncio
async def test_deadline_wrapup_preempt_skipped_during_finalization() -> None:
    # The preempt exists to get the directive spliced into a retry; during the
    # max-iterations finalization the finalization message takes precedence
    # and the directive would never be spliced, so preempting only discards
    # the finalization stream for a byte-identical retry.
    provider = _SequenceProvider(
        [
            _echo_tool_call("use-1"),
            [
                ProviderReasoning(text="summarizing"),
                2.5,
                ProviderReasoning(text="still summarizing"),
                ProviderText(text="final summary"),
                ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=2),
            ],
        ]
    )
    # timeout 8 / margin 6: the preempt threshold sits 2s in; the tool
    # iteration finishes before it, the finalization stream crosses it.
    agent = _echo_agent(
        provider,
        AgentConfig(
            timeout=8.0,
            deadline_wrapup_margin_seconds=6,
            max_iterations=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    # No preempt retry of the finalization call.
    assert len(provider.calls) == 2
    assert [
        text
        for text in _user_texts(provider.calls[1]["messages"])
        if "iteration limit has been reached" in text
    ]
    for call in provider.calls:
        assert not [
            text
            for text in _user_texts(call["messages"])
            if text.startswith(_WRAPUP_PREFIX)
        ]


@pytest.mark.asyncio
async def test_deadline_wrapup_preempt_skipped_on_reasoning_prefill_tail() -> None:
    # The splice is withheld while the turn ends on the prefill assistant
    # tail; a preempt there would discard the continuation stream for a
    # directive-free, byte-identical retry.
    provider = _SequenceProvider(
        [
            [
                ProviderDone(
                    stop_reason="stop",
                    input_tokens=10,
                    output_tokens=5,
                    reasoning_tokens=5,
                    reasoning_content="internal reasoning",
                )
            ],
            [
                ProviderReasoning(text="continuing"),
                2.5,
                ProviderReasoning(text="still continuing"),
                ProviderText(text="answer"),
                ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=2),
            ],
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            timeout=8.0,
            deadline_wrapup_margin_seconds=6,
            thinking=ThinkingLevel.MEDIUM,
            model_capabilities=ModelCapabilities(
                supports_reasoning=True,
                supports_tools=True,
                reasoning_format="openrouter",
            ),
            reasoning_prefill_recovery_mode="recover",
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    # The continuation call crosses the preempt threshold mid-stream but runs
    # to completion: no preempt retry.
    assert len(provider.calls) == 2
    prefill_tail = provider.calls[1]["messages"][-1]
    assert prefill_tail.role == "assistant"
    for call in provider.calls:
        assert not [
            text
            for text in _user_texts(call["messages"])
            if text.startswith(_WRAPUP_PREFIX)
        ]


@pytest.mark.asyncio
async def test_deadline_wrapup_preempt_records_no_tool_loop_event(tmp_path) -> None:
    # The preempted attempt is incomplete by engine choice; it must not be
    # reported to the tool-loop observer as a provider stream failure.
    runtime_events_path = tmp_path / "runtime_events.jsonl"
    provider = _SequenceProvider(
        [
            [
                ProviderReasoning(text="deep thought"),
                2.5,
                ProviderReasoning(text="more thought"),
                ProviderDone(stop_reason="stop", input_tokens=5, output_tokens=2),
            ],
            _final_text(),
        ]
    )
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            timeout=8.0,
            deadline_wrapup_margin_seconds=6,
            tool_loop_observer_mode="log",
            runtime_events_path=str(runtime_events_path),
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("fix the bug")]

    assert any(event.kind == "done" for event in events)
    # The preempt itself still happened: the retry carries the directive.
    assert len(provider.calls) == 2
    assert [
        text
        for text in _user_texts(provider.calls[1]["messages"])
        if text.startswith(_WRAPUP_PREFIX)
    ]
    observer_events = []
    if runtime_events_path.exists():
        observer_events = [
            json.loads(line)
            for line in runtime_events_path.read_text().splitlines()
            if line.strip()
            and json.loads(line).get("mechanism") == "tool_loop_observer"
        ]
    assert observer_events == []


def test_env_plumbing_for_both_levers(monkeypatch: pytest.MonkeyPatch) -> None:
    # Helper-level check only; the full env -> bootstrap-stage -> AgentConfig
    # threading is covered in turn_runner/test_agent_bootstrap_stage_unit.py.
    from opensquilla.engine.turn_runner.agent_bootstrap_stage import (
        _nonnegative_int_from_env,
    )

    monkeypatch.delenv("OPENSQUILLA_PLACEHOLDER_ESCALATION_THRESHOLD", raising=False)
    monkeypatch.delenv("OPENSQUILLA_DEADLINE_WRAPUP_MARGIN_SECONDS", raising=False)
    assert _nonnegative_int_from_env("OPENSQUILLA_PLACEHOLDER_ESCALATION_THRESHOLD", 0) == 0
    assert _nonnegative_int_from_env("OPENSQUILLA_DEADLINE_WRAPUP_MARGIN_SECONDS", 0) == 0
    monkeypatch.setenv("OPENSQUILLA_PLACEHOLDER_ESCALATION_THRESHOLD", "3")
    monkeypatch.setenv("OPENSQUILLA_DEADLINE_WRAPUP_MARGIN_SECONDS", "360")
    assert _nonnegative_int_from_env("OPENSQUILLA_PLACEHOLDER_ESCALATION_THRESHOLD", 0) == 3
    assert _nonnegative_int_from_env("OPENSQUILLA_DEADLINE_WRAPUP_MARGIN_SECONDS", 0) == 360


def test_agent_config_defaults_keep_both_levers_off() -> None:
    config = AgentConfig()

    assert config.placeholder_escalation_threshold == 0
    assert config.deadline_wrapup_margin_seconds == 0
