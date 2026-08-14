from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import AsyncIterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
import structlog.testing

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.runtime import (
    TurnRunner,
    _SelectorFallbackProvider,
    _SelectorPreTextBuffer,
)
from opensquilla.engine.types import DoneEvent as EngineDoneEvent
from opensquilla.engine.types import ErrorEvent as EngineErrorEvent
from opensquilla.engine.types import ThinkingEvent as EngineThinkingEvent
from opensquilla.persistence.migrator import apply_pending
from opensquilla.persistence.turn_error_writer import open_turn_error_writer
from opensquilla.provider import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    ProviderActivityEvent,
    ReasoningDeltaEvent,
    TextDeltaEvent,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
)
from opensquilla.provider.selector import ProviderConfig
from opensquilla.session.manager import SessionManager
from opensquilla.session.storage import SessionStorage
from opensquilla.tools.types import CallerKind, ToolContext


class _SequenceProvider:
    provider_name = "openai"

    def __init__(self, events: list[Any], *, raised: BaseException | None = None) -> None:
        self.events = events
        self.raised = raised
        self.calls = 0

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages, tools, config
        self.calls += 1
        return self._stream()

    async def _stream(self) -> AsyncIterator[Any]:
        for event in self.events:
            yield event
        if self.raised is not None:
            raise self.raised


class _Selector:
    def __init__(
        self,
        primary: ProviderConfig,
        fallback_config: ProviderConfig,
        fallback: _SequenceProvider,
    ) -> None:
        self.current_config = primary
        self._fallback_config = fallback_config
        self._fallback = fallback
        self.failures: list[str] = []

    def next_fallback_after_failure(self, exc: Exception) -> _SequenceProvider:
        self.failures.append(str(exc))
        self.current_config = self._fallback_config
        return self._fallback


class _MultiSelector:
    def __init__(
        self,
        configs: list[ProviderConfig],
        fallbacks: list[_SequenceProvider],
    ) -> None:
        assert len(configs) == len(fallbacks) + 1
        self._configs = configs
        self._fallbacks = fallbacks
        self._index = 0
        self.current_config = configs[0]

    def next_fallback_after_failure(self, _exc: Exception) -> _SequenceProvider:
        if self._index >= len(self._fallbacks):
            raise IndexError("no fallback")
        fallback = self._fallbacks[self._index]
        self._index += 1
        self.current_config = self._configs[self._index]
        return fallback


class _TurnRunnerSelector:
    is_configured = True

    def __init__(self, provider: _SequenceProvider) -> None:
        self.provider = provider
        self.current_config = ProviderConfig(
            "tokenrhythm",
            "synthetic-model",
            api_key="synthetic-test-key",
        )

    @property
    def active_provider_id(self) -> str:
        return self.current_config.provider

    def clone(self) -> _TurnRunnerSelector:
        return _TurnRunnerSelector(self.provider)

    def resolve(self) -> _SequenceProvider:
        return self.provider

    def remaining_chain(self) -> list[ProviderConfig]:
        return [self.current_config]

    def next_fallback_after_failure(self, _exc: Exception) -> _SequenceProvider:
        raise IndexError("no fallback")


class _TurnRunnerFallbackSelector:
    is_configured = True

    def __init__(
        self,
        primary: _SequenceProvider,
        fallback: _SequenceProvider,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._primary_config = ProviderConfig(
            "tokenrhythm",
            "synthetic-primary",
            api_key="synthetic-primary-key",
        )
        self._fallback_config = ProviderConfig(
            "deepseek",
            "synthetic-fallback",
            api_key="synthetic-fallback-key",
        )
        self.current_config = self._primary_config

    @property
    def active_provider_id(self) -> str:
        return self.current_config.provider

    def clone(self) -> _TurnRunnerFallbackSelector:
        return _TurnRunnerFallbackSelector(self._primary, self._fallback)

    def resolve(self) -> _SequenceProvider:
        return self._primary

    def remaining_chain(self) -> list[ProviderConfig]:
        return [self._primary_config, self._fallback_config]

    def next_fallback_after_failure(self, _exc: Exception) -> _SequenceProvider:
        self.current_config = self._fallback_config
        return self._fallback


def test_pretext_buffer_tracks_interleaved_tool_ids_as_an_open_set() -> None:
    buffer = _SelectorPreTextBuffer()
    frames = [
        ToolUseStartEvent(tool_use_id="a", tool_name="first"),
        ToolUseStartEvent(tool_use_id="b", tool_name="second"),
        ToolUseDeltaEvent(tool_use_id="b", json_fragment='{"b":'),
        ToolUseDeltaEvent(tool_use_id="a", json_fragment='{"a":1}'),
        ToolUseEndEvent(tool_use_id="a", tool_name="first", arguments={"a": 1}),
        ToolUseDeltaEvent(tool_use_id="b", json_fragment="2}"),
        ToolUseEndEvent(tool_use_id="b", tool_name="second", arguments={"b": 2}),
    ]

    for frame in frames:
        buffer.append(frame)

    assert buffer.protocol_error is False
    assert buffer.has_incomplete_tool_call is False
    assert buffer.has_completed_tool_call is True
    drained = buffer.drain(successful_leg=True)
    assert [event.kind for event in drained] == [
        "tool_use_start",
        "tool_use_start",
        "tool_use_delta",
        "tool_use_delta",
        "tool_use_end",
        "tool_use_delta",
        "tool_use_end",
    ]


@pytest.mark.parametrize(
    "frames",
    [
        [ToolUseDeltaEvent(tool_use_id="unknown", json_fragment="{}")],
        [ToolUseEndEvent(tool_use_id="unknown", tool_name="echo", arguments={})],
        [
            ToolUseStartEvent(tool_use_id="same", tool_name="echo"),
            ToolUseStartEvent(tool_use_id="same", tool_name="echo"),
        ],
        [
            ToolUseStartEvent(tool_use_id="same", tool_name="echo"),
            ToolUseEndEvent(tool_use_id="same", tool_name="echo", arguments={}),
            ToolUseEndEvent(tool_use_id="same", tool_name="echo", arguments={}),
        ],
        [
            ToolUseStartEvent(tool_use_id="same", tool_name="echo"),
            ToolUseEndEvent(tool_use_id="same", tool_name="echo", arguments={}),
            ToolUseDeltaEvent(tool_use_id="same", json_fragment="late"),
        ],
        [
            ToolUseStartEvent(tool_use_id="same", tool_name="echo"),
            ToolUseEndEvent(tool_use_id="same", tool_name="different", arguments={}),
        ],
    ],
)
def test_pretext_buffer_rejects_unknown_duplicate_and_late_tool_frames(
    frames: list[Any],
) -> None:
    buffer = _SelectorPreTextBuffer()
    buffer.append(ReasoningDeltaEvent(text="failed-leg-secret"))

    for frame in frames:
        buffer.append(frame)

    assert buffer.protocol_error is True
    assert buffer.buffered_bytes == 0
    assert buffer.drain(successful_leg=True) == []


@pytest.mark.asyncio
async def test_invalid_multi_tool_primary_falls_back_without_leaking_failed_leg() -> None:
    raw_marker = "FAILED_LEG_PRIVATE_ARGUMENT"
    primary = _SequenceProvider(
        [
            ToolUseStartEvent(tool_use_id="a", tool_name="echo"),
            ToolUseStartEvent(tool_use_id="b", tool_name="echo"),
            ToolUseDeltaEvent(tool_use_id="a", json_fragment=raw_marker),
            ToolUseEndEvent(tool_use_id="a", tool_name="echo", arguments={}),
            ToolUseDeltaEvent(tool_use_id="a", json_fragment="late"),
            ReasoningDeltaEvent(text="failed private reasoning"),
            DoneEvent(stop_reason="tool_use"),
        ]
    )
    fallback = _SequenceProvider(
        [TextDeltaEvent(text="safe fallback"), DoneEvent(stop_reason="stop")]
    )
    selector = _Selector(
        ProviderConfig("openrouter", "primary", api_key="primary-key"),
        ProviderConfig("deepseek", "fallback", api_key="fallback-key"),
        fallback,
    )

    events = [
        event
        async for event in _SelectorFallbackProvider(primary, selector).chat(
            [Message(role="user", content="hello")]
        )
    ]

    assert fallback.calls == 1
    assert any(
        isinstance(event, TextDeltaEvent) and event.text == "safe fallback"
        for event in events
    )
    assert not any(
        isinstance(
            event,
            (
                ReasoningDeltaEvent,
                ToolUseStartEvent,
                ToolUseDeltaEvent,
                ToolUseEndEvent,
            ),
        )
        for event in events
    )
    assert raw_marker not in repr(events)
    assert "failed private reasoning" not in repr(events)


@pytest.mark.asyncio
async def test_selector_failover_hook_never_receives_raw_provider_prose() -> None:
    raw_marker = "RAW_PROVIDER_BODY_FOR_PLUGIN_HOOK"
    primary = _SequenceProvider(
        [ErrorEvent(message=raw_marker, code="503")]
    )
    fallback = _SequenceProvider(
        [TextDeltaEvent(text="safe fallback"), DoneEvent(stop_reason="stop")]
    )
    selector = _Selector(
        ProviderConfig("openrouter", "primary", api_key="primary-key"),
        ProviderConfig("deepseek", "fallback", api_key="fallback-key"),
        fallback,
    )

    events = [
        event
        async for event in _SelectorFallbackProvider(primary, selector).chat(
            [Message(role="user", content="hello")]
        )
    ]

    assert fallback.calls == 1
    assert selector.failures == [
        "The model provider is temporarily overloaded. Try again later."
    ]
    assert raw_marker not in repr(selector.failures)
    assert raw_marker not in repr(events)


@pytest.mark.asyncio
async def test_first_text_cannot_commit_while_any_tool_id_is_open() -> None:
    primary = _SequenceProvider(
        [
            ToolUseStartEvent(tool_use_id="a", tool_name="echo"),
            TextDeltaEvent(text="must not escape"),
        ]
    )
    fallback = _SequenceProvider(
        [TextDeltaEvent(text="safe fallback"), DoneEvent(stop_reason="stop")]
    )
    selector = _Selector(
        ProviderConfig("openrouter", "primary", api_key="primary-key"),
        ProviderConfig("deepseek", "fallback", api_key="fallback-key"),
        fallback,
    )

    events = [
        event
        async for event in _SelectorFallbackProvider(primary, selector).chat(
            [Message(role="user", content="hello")]
        )
    ]

    assert "must not escape" not in repr(events)
    assert any(
        isinstance(event, TextDeltaEvent) and event.text == "safe fallback"
        for event in events
    )


@pytest.mark.asyncio
async def test_same_authority_fallback_waits_for_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("opensquilla.engine.runtime.asyncio.sleep", fake_sleep)
    primary = _SequenceProvider(
        [ErrorEvent(message="raw rate limit body", code="429", retry_after_s=8.0)]
    )
    fallback = _SequenceProvider(
        [TextDeltaEvent(text="safe fallback"), DoneEvent(stop_reason="stop")]
    )
    primary_config = ProviderConfig(
        "openrouter",
        "primary",
        api_key="same-account",
        base_url="https://example.invalid/v1",
    )
    fallback_config = ProviderConfig(
        "openrouter",
        "fallback",
        api_key="same-account",
        base_url="https://example.invalid/v1",
    )
    wrapper = _SelectorFallbackProvider(
        primary,
        _Selector(primary_config, fallback_config, fallback),
    )

    events = [
        event
        async for event in wrapper.chat(
            [Message(role="user", content="hello")],
            config=ChatConfig(turn_deadline_at_monotonic=time.monotonic() + 30),
        )
    ]

    assert sleeps == [8.0]
    assert fallback.calls == 1
    phases = [event.phase for event in events if isinstance(event, ProviderActivityEvent)]
    assert phases.index("retry_wait") < phases.index("fallback")


@pytest.mark.asyncio
async def test_independent_authority_fallback_does_not_wait_for_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("opensquilla.engine.runtime.asyncio.sleep", fake_sleep)
    primary = _SequenceProvider(
        [ErrorEvent(message="raw rate limit body", code="429", retry_after_s=8.0)]
    )
    fallback = _SequenceProvider(
        [TextDeltaEvent(text="safe fallback"), DoneEvent(stop_reason="stop")]
    )
    wrapper = _SelectorFallbackProvider(
        primary,
        _Selector(
            ProviderConfig("tokenrhythm", "primary", api_key="account-a"),
            ProviderConfig("deepseek", "fallback", api_key="account-b"),
            fallback,
        ),
    )

    events = [
        event
        async for event in wrapper.chat([Message(role="user", content="hello")])
    ]

    assert sleeps == []
    assert fallback.calls == 1
    assert any(isinstance(event, TextDeltaEvent) for event in events)


@pytest.mark.asyncio
async def test_same_authority_retry_after_past_deadline_is_typed_terminal() -> None:
    raw_marker = "RAW_RETRY_AFTER_BODY"
    primary = _SequenceProvider(
        [ErrorEvent(message=raw_marker, code="429", retry_after_s=8.0)]
    )
    fallback = _SequenceProvider(
        [TextDeltaEvent(text="must not run"), DoneEvent(stop_reason="stop")]
    )
    first = ProviderConfig("openrouter", "primary", api_key="same-account")
    second = ProviderConfig("openrouter", "fallback", api_key="same-account")
    wrapper = _SelectorFallbackProvider(primary, _Selector(first, second, fallback))

    events = [
        event
        async for event in wrapper.chat(
            [Message(role="user", content="hello")],
            config=ChatConfig(turn_deadline_at_monotonic=time.monotonic() + 1),
        )
    ]

    terminal = next(
        event
        for event in events
        if isinstance(event, ErrorEvent)
        and event.code == "provider_retry_after_deadline"
    )
    assert fallback.calls == 0
    assert terminal.retry_after_s == 8.0
    assert raw_marker not in repr(events)


@pytest.mark.asyncio
async def test_same_authority_retry_after_over_wait_ceiling_is_typed_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("opensquilla.engine.runtime.asyncio.sleep", fake_sleep)
    primary = _SequenceProvider(
        [ErrorEvent(message="raw rate limit body", code="429", retry_after_s=901.0)]
    )
    fallback = _SequenceProvider(
        [TextDeltaEvent(text="must not run"), DoneEvent(stop_reason="stop")]
    )
    first = ProviderConfig("openrouter", "primary", api_key="same-account")
    second = ProviderConfig("openrouter", "fallback", api_key="same-account")
    wrapper = _SelectorFallbackProvider(primary, _Selector(first, second, fallback))

    events = [
        event
        async for event in wrapper.chat(
            [Message(role="user", content="hello")],
            config=ChatConfig(turn_deadline_at_monotonic=time.monotonic() + 2_000),
        )
    ]

    assert sleeps == []
    assert fallback.calls == 0
    terminal = next(event for event in events if isinstance(event, ErrorEvent))
    assert terminal.code == "provider_retry_after_deadline"
    assert terminal.retry_after_s == 901.0


@pytest.mark.asyncio
async def test_agent_does_not_advance_same_authority_after_retry_deadline_terminal() -> None:
    primary = _SequenceProvider(
        [ErrorEvent(message="raw rate limit body", code="429", retry_after_s=901.0)]
    )
    first_fallback = _SequenceProvider(
        [TextDeltaEvent(text="must not run first"), DoneEvent(stop_reason="stop")]
    )
    second_fallback = _SequenceProvider(
        [TextDeltaEvent(text="must not run second"), DoneEvent(stop_reason="stop")]
    )
    configs = [
        ProviderConfig("openrouter", "primary", api_key="same-account"),
        ProviderConfig("openrouter", "fallback-one", api_key="same-account"),
        ProviderConfig("openrouter", "fallback-two", api_key="same-account"),
    ]
    wrapper = _SelectorFallbackProvider(
        primary,
        _MultiSelector(configs, [first_fallback, second_fallback]),
    )
    agent = Agent(
        provider=wrapper,
        config=AgentConfig(max_provider_retries=3, timeout=2_000),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert primary.calls == 1
    assert first_fallback.calls == 0
    assert second_fallback.calls == 0
    terminal = next(event for event in events if isinstance(event, EngineErrorEvent))
    assert terminal.code == "provider_retry_after_deadline"
    assert terminal.failure_kind == "rate_limited"


@pytest.mark.asyncio
async def test_selector_projects_stream_timeout_and_falls_back_without_raw_prose() -> None:
    raw_marker = "provider sdk timeout"
    primary = _SequenceProvider([], raised=TimeoutError(raw_marker))
    fallback = _SequenceProvider(
        [TextDeltaEvent(text="safe fallback"), DoneEvent(stop_reason="stop")]
    )
    wrapper = _SelectorFallbackProvider(
        primary,
        _Selector(
            ProviderConfig("tokenrhythm", "primary", api_key="account-a"),
            ProviderConfig("deepseek", "fallback", api_key="account-b"),
            fallback,
        ),
    )

    events = [
        event
        async for event in wrapper.chat([Message(role="user", content="hello")])
    ]

    assert fallback.calls == 1
    assert raw_marker not in repr(events)
    assert any(
        isinstance(event, TextDeltaEvent) and event.text == "safe fallback"
        for event in events
    )


@pytest.mark.asyncio
async def test_selector_preserves_stream_cancellation_without_fallback() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class _BlockingProvider:
        provider_name = "openai"

        def __init__(self) -> None:
            self.calls = 0

        def chat(
            self,
            messages: list[Message],
            tools: list[Any] | None = None,
            config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            del messages, tools, config
            self.calls += 1
            return self._stream()

        async def _stream(self) -> AsyncIterator[Any]:
            entered.set()
            await release.wait()
            yield DoneEvent(stop_reason="stop")

    primary = _BlockingProvider()
    fallback = _SequenceProvider(
        [TextDeltaEvent(text="must not run"), DoneEvent(stop_reason="stop")]
    )
    wrapper = _SelectorFallbackProvider(
        primary,
        _Selector(
            ProviderConfig("tokenrhythm", "primary", api_key="account-a"),
            ProviderConfig("deepseek", "fallback", api_key="account-b"),
            fallback,
        ),
    )

    async def consume() -> None:
        async for _event in wrapper.chat([Message(role="user", content="hello")]):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert primary.calls == 1
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_exception_raised_before_content_uses_selector_fallback_without_raw_prose() -> None:
    raw_marker = "RAW_STREAM_EXCEPTION_MARKER"
    primary = _SequenceProvider([], raised=RuntimeError(raw_marker))
    fallback = _SequenceProvider(
        [TextDeltaEvent(text="safe fallback"), DoneEvent(stop_reason="stop")]
    )
    wrapper = _SelectorFallbackProvider(
        primary,
        _Selector(
            ProviderConfig("tokenrhythm", "primary", api_key="account-a"),
            ProviderConfig("deepseek", "fallback", api_key="account-b"),
            fallback,
        ),
    )

    events = [
        event
        async for event in wrapper.chat([Message(role="user", content="hello")])
    ]

    assert fallback.calls == 1
    assert raw_marker not in repr(events)
    assert any(
        isinstance(event, TextDeltaEvent) and event.text == "safe fallback"
        for event in events
    )


@pytest.mark.asyncio
async def test_agent_retries_raised_exception_before_user_visible_content() -> None:
    raw_marker = "RAW_PRECONTENT_EXCEPTION_MARKER"

    class _RetryThenSuccessProvider:
        provider_name = "openai"

        def __init__(self) -> None:
            self.calls = 0

        def chat(
            self,
            messages: list[Message],
            tools: list[Any] | None = None,
            config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            del messages, tools, config
            self.calls += 1
            return self._stream(self.calls)

        async def _stream(self, attempt: int) -> AsyncIterator[Any]:
            if attempt == 1:
                raise RuntimeError(raw_marker)
            yield TextDeltaEvent(text="PREFIX-FINAL")
            yield DoneEvent(stop_reason="stop")

    provider = _RetryThenSuccessProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert provider.calls == 2
    assert [
        event.text for event in events if getattr(event, "kind", "") == "text_delta"
    ] == ["PREFIX-FINAL"]
    assert any(isinstance(event, EngineDoneEvent) for event in events)
    assert not any(isinstance(event, EngineErrorEvent) for event in events)
    assert raw_marker not in repr(events)


@pytest.mark.asyncio
async def test_agent_does_not_replay_after_user_visible_text_then_raised_exception() -> None:
    raw_marker = "RAW_POSTCONTENT_EXCEPTION_MARKER"

    class _PartialThenSuccessProvider:
        provider_name = "openai"

        def __init__(self) -> None:
            self.calls = 0

        def chat(
            self,
            messages: list[Message],
            tools: list[Any] | None = None,
            config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            del messages, tools, config
            self.calls += 1
            return self._stream(self.calls)

        async def _stream(self, attempt: int) -> AsyncIterator[Any]:
            if attempt == 1:
                yield TextDeltaEvent(text="PREFIX-")
                raise RuntimeError(raw_marker)
            yield TextDeltaEvent(text="PREFIX-FINAL")
            yield DoneEvent(stop_reason="stop")

    provider = _PartialThenSuccessProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    visible_text = [
        event.text for event in events if getattr(event, "kind", "") == "text_delta"
    ]
    terminal = next(event for event in events if isinstance(event, EngineErrorEvent))
    assert provider.calls == 1
    assert visible_text == ["PREFIX-"]
    assert terminal.code == "response_incomplete"
    assert terminal.failure_kind == "transport_transient"
    assert not any(isinstance(event, EngineDoneEvent) for event in events)
    assert not any(
        getattr(event, "phase", "") in {"retry_wait", "retrying"}
        for event in events
    )
    assert raw_marker not in repr(events)


@pytest.mark.asyncio
async def test_agent_retries_after_uncommitted_tool_lifecycle_fails() -> None:
    class _PartialToolThenSuccessProvider:
        provider_name = "openai"

        def __init__(self) -> None:
            self.calls = 0

        def chat(
            self,
            messages: list[Message],
            tools: list[Any] | None = None,
            config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            del messages, tools, config
            self.calls += 1
            return self._stream(self.calls)

        async def _stream(self, attempt: int) -> AsyncIterator[Any]:
            if attempt == 1:
                yield ToolUseStartEvent(tool_use_id="partial", tool_name="echo")
                raise RuntimeError("partial tool stream")
            yield TextDeltaEvent(text="retry response")
            yield DoneEvent(stop_reason="stop")

    provider = _PartialToolThenSuccessProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert provider.calls == 2
    assert not any(
        getattr(event, "kind", "").startswith("tool_use_") for event in events
    )
    assert any(getattr(event, "text", "") == "retry response" for event in events)
    assert any(isinstance(event, EngineDoneEvent) for event in events)
    assert not any(isinstance(event, EngineErrorEvent) for event in events)


@pytest.mark.asyncio
async def test_agent_does_not_replay_after_visible_reasoning_then_raised_exception() -> None:
    raw_marker = "VISIBLE_REASONING_FROM_FIRST_ATTEMPT"

    class _ReasoningThenSuccessProvider:
        provider_name = "openai"

        def __init__(self) -> None:
            self.calls = 0

        def chat(
            self,
            messages: list[Message],
            tools: list[Any] | None = None,
            config: ChatConfig | None = None,
        ) -> AsyncIterator[Any]:
            del messages, tools, config
            self.calls += 1
            return self._stream(self.calls)

        async def _stream(self, attempt: int) -> AsyncIterator[Any]:
            if attempt == 1:
                yield ReasoningDeltaEvent(text=raw_marker)
                raise RuntimeError("stream reset after published reasoning")
            yield TextDeltaEvent(text="must not replay")
            yield DoneEvent(stop_reason="stop")

    provider = _ReasoningThenSuccessProvider()
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=1,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    thinking = [event for event in events if isinstance(event, EngineThinkingEvent)]
    terminal = next(event for event in events if isinstance(event, EngineErrorEvent))
    assert provider.calls == 1
    assert [event.text for event in thinking] == [raw_marker]
    assert terminal.code == "response_incomplete"
    assert terminal.failure_kind == "transport_transient"
    assert not any(isinstance(event, EngineDoneEvent) for event in events)
    assert not any(
        getattr(event, "phase", "") in {"retry_wait", "retrying"}
        for event in events
    )
    assert not any("must not replay" in repr(event) for event in events)


@pytest.mark.asyncio
async def test_selector_does_not_fallback_after_visible_reasoning_then_exception() -> None:
    raw_marker = "VISIBLE_REASONING_COMMITS_LEG"
    primary = _SequenceProvider(
        [ReasoningDeltaEvent(text=raw_marker)],
        raised=RuntimeError("stream reset after reasoning"),
    )
    fallback = _SequenceProvider(
        [TextDeltaEvent(text="safe fallback"), DoneEvent(stop_reason="stop")]
    )
    wrapper = _SelectorFallbackProvider(
        primary,
        _Selector(
            ProviderConfig("tokenrhythm", "primary", api_key="account-a"),
            ProviderConfig("deepseek", "fallback", api_key="account-b"),
            fallback,
        ),
    )

    events = [
        event
        async for event in wrapper.chat([Message(role="user", content="hello")])
    ]

    assert primary.calls == 1
    assert fallback.calls == 0
    assert any(
        isinstance(event, ReasoningDeltaEvent) and event.text == raw_marker
        for event in events
    )
    terminal = next(event for event in events if isinstance(event, ErrorEvent))
    assert terminal.code == "response_incomplete"


@pytest.mark.asyncio
async def test_agent_converts_raised_stream_exception_to_safe_typed_error() -> None:
    raw_marker = "RAW_STREAM_EXCEPTION_MARKER"
    provider = _SequenceProvider([], raised=RuntimeError(raw_marker))
    turn_log: list[tuple[str, dict[str, Any]]] = []

    class _TurnLog:
        def write(self, kind: str, payload: dict[str, Any]) -> None:
            turn_log.append((kind, payload))

    agent = Agent(
        provider=provider,
        config=AgentConfig(max_provider_retries=0),
        turn_call_logger=_TurnLog(),  # type: ignore[arg-type]
    )

    events = [event async for event in agent.run_turn("hello")]

    terminal = next(event for event in events if isinstance(event, EngineErrorEvent))
    assert terminal.code == "request_error"
    assert terminal.failure_kind == "transport_transient"
    assert raw_marker not in repr(events)
    assert raw_marker not in repr(turn_log)


@pytest.mark.asyncio
async def test_raised_provider_marker_stays_out_of_turn_record_transcript_and_logs(
    tmp_path: Path,
) -> None:
    raw_marker = "RAW_PROVIDER_EXCEPTION_MUST_NOT_PERSIST"
    db_path = tmp_path / "sessions.sqlite"
    apply_pending(str(db_path), Path(__file__).resolve().parents[2] / "migrations")
    storage = SessionStorage(str(db_path))
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:webchat:provider-exception"
    await manager.create(session_key)
    writer = open_turn_error_writer(str(db_path))
    runner = TurnRunner(
        provider_selector=_TurnRunnerSelector(
            _SequenceProvider([], raised=RuntimeError(raw_marker))
        ),
        session_manager=manager,
        turn_error_writer=writer,
    )

    try:
        with structlog.testing.capture_logs() as logs:
            events = [
                event
                async for event in runner.run(
                    "synthetic prompt",
                    session_key,
                    ToolContext(is_owner=True, caller_kind=CallerKind.CLI),
                    history_has_persisted_user=False,
                    no_memory_capture=True,
                    max_provider_retries=0,
                )
            ]
        transcript = await manager.get_transcript(session_key)
        with sqlite3.connect(db_path) as conn:
            turn_errors = conn.execute(
                "SELECT error_class, message, traceback FROM turn_errors"
            ).fetchall()
    finally:
        writer.close()
        await storage.close()

    assert raw_marker not in repr(events)
    assert raw_marker not in repr(transcript)
    assert raw_marker not in repr(turn_errors)
    assert raw_marker not in repr(logs)
    terminal = next(event for event in events if isinstance(event, EngineErrorEvent))
    assert terminal.failure_kind == "transport_transient"
    assert turn_errors
    assert turn_errors[-1][0] == "request_error"
    assert turn_errors[-1][2] is None


@pytest.mark.asyncio
async def test_successful_fallback_redacts_provider_markers_from_all_outputs(
    tmp_path: Path,
) -> None:
    raw_body_marker = "PRIVATE_PROVIDER_BODY_MUST_NOT_ESCAPE"
    raw_code_marker = "PRIVATE_PROVIDER_CODE_MUST_NOT_ESCAPE"
    primary = _SequenceProvider(
        [
            ErrorEvent(
                message=f"HTTP 503 upstream overloaded: {raw_body_marker}",
                code=raw_code_marker,
            )
        ]
    )
    fallback = _SequenceProvider(
        [TextDeltaEvent(text="safe fallback"), DoneEvent(stop_reason="stop")]
    )
    db_path = tmp_path / "sessions.sqlite"
    apply_pending(str(db_path), Path(__file__).resolve().parents[2] / "migrations")
    storage = SessionStorage(str(db_path))
    await storage.connect()
    manager = SessionManager(storage)
    session_key = "agent:main:webchat:provider-fallback"
    await manager.create(session_key)
    runner = TurnRunner(
        provider_selector=_TurnRunnerFallbackSelector(primary, fallback),
        session_manager=manager,
    )

    try:
        with structlog.testing.capture_logs() as logs:
            events = [
                event
                async for event in runner.run(
                    "synthetic prompt",
                    session_key,
                    ToolContext(is_owner=True, caller_kind=CallerKind.CLI),
                    history_has_persisted_user=False,
                    no_memory_capture=True,
                    max_provider_retries=0,
                )
            ]
        transcript = await manager.get_transcript(session_key)
    finally:
        await storage.close()

    terminal = next(event for event in events if isinstance(event, EngineDoneEvent))
    wire_payload = asdict(terminal)
    fallback_leg = terminal.execution_legs[-1]

    assert fallback.calls == 1
    assert fallback_leg["kind"] == "provider_fallback"
    assert fallback_leg["reason"] == "provider_provider_overloaded"
    assert wire_payload["execution_legs"][-1]["reason"] == "provider_provider_overloaded"
    assert "provider_provider_overloaded" in repr(transcript)
    for output in (events, wire_payload, transcript, logs):
        assert raw_body_marker not in repr(output)
        assert raw_code_marker not in repr(output)
