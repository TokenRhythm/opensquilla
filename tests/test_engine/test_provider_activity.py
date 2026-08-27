from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.agent import _provider_retry_delay_seconds
from opensquilla.engine.routing.health import ProviderHealthLedger
from opensquilla.engine.runtime import (
    _SELECTOR_REASONING_TRUNCATED_NOTICE,
    _report_credential_pool_failure,
    _SelectorFallbackProvider,
    _SelectorPreTextBuffer,
)
from opensquilla.engine.types import ErrorEvent as EngineErrorEvent
from opensquilla.engine.types import ProviderActivityEvent, ThinkingEvent
from opensquilla.provider import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    ProviderFailureKind,
    ReasoningDeltaEvent,
    TextDeltaEvent,
    ToolUseDeltaEvent,
    ToolUseEndEvent,
    ToolUseStartEvent,
    classify_provider_error,
)
from opensquilla.provider import (
    ProviderActivityEvent as ProviderDomainActivityEvent,
)


class _SequenceProvider:
    provider_name = "openrouter"

    def __init__(self, streams: list[list[Any]]) -> None:
        self._streams = streams
        self.calls = 0

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages, tools, config
        index = self.calls
        self.calls += 1
        return self._stream(self._streams[min(index, len(self._streams) - 1)])

    async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
        for event in events:
            yield event


class _CapturingTurnLog:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, kind: str, payload: dict[str, Any]) -> None:
        self.records.append({"kind": kind, "payload": payload})


def test_provider_retry_delay_uses_larger_provider_hint() -> None:
    assert _provider_retry_delay_seconds(
        local_delay_s=2.0,
        provider_retry_after_s=8.0,
    ) == 8.0
    assert _provider_retry_delay_seconds(
        local_delay_s=12.0,
        provider_retry_after_s=8.0,
    ) == 12.0


def test_provider_retry_delay_does_not_clamp_an_excessive_hint_and_retry_early() -> None:
    assert _provider_retry_delay_seconds(
        local_delay_s=1.0,
        provider_retry_after_s=901.0,
    ) is None


def test_pretext_buffer_exhaustion_is_a_recoverable_provider_failure() -> None:
    assert classify_provider_error(
        provider_name="openrouter",
        status_code=None,
        raw_code="provider_pretext_buffer_exhausted",
        message="safe synthetic error",
    ) is ProviderFailureKind.TRANSPORT_TRANSIENT


def test_retry_after_reaches_profile_credential_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []

    class _PoolManager:
        def report_failure(self, *args: Any, **kwargs: Any) -> None:
            calls.append((*args, kwargs))

    monkeypatch.setattr(
        "opensquilla.gateway.llm_runtime.profile_credential_pools",
        lambda: _PoolManager(),
    )
    _report_credential_pool_failure(
        "openai",
        {
            "credential_pool": {
                "provider": "openrouter",
                "session_key": "agent:main:synthetic",
            },
            "routed_provider_applied": "openrouter",
        },
        ErrorEvent(message="synthetic", code="429", retry_after_s=8.0),
    )

    assert len(calls) == 1
    assert calls[0][0:2] == ("openrouter", "agent:main:synthetic")
    assert calls[0][-1] == {"retry_after_seconds": 8.0}


def test_selector_reasoning_buffer_is_byte_bounded_and_notices_only_success() -> None:
    successful = _SelectorPreTextBuffer(reasoning_limit_bytes=4)
    successful.append(ReasoningDeltaEvent(text="ab"))
    successful.append(ReasoningDeltaEvent(text="cdef"))

    drained = successful.drain(successful_leg=True)

    assert [event.text for event in drained if isinstance(event, ReasoningDeltaEvent)] == [
        _SELECTOR_REASONING_TRUNCATED_NOTICE,
        "cdef",
    ]

    failed = _SelectorPreTextBuffer(reasoning_limit_bytes=4)
    failed.append(ReasoningDeltaEvent(text="secret reasoning"))
    failed.append(ToolUseEndEvent(tool_use_id="tool", tool_name="echo", arguments={}))

    failed_events = failed.drain(successful_leg=False)

    assert failed_events == []


def test_selector_buffer_coalesces_tool_deltas_and_rejects_oversized_content() -> None:
    successful = _SelectorPreTextBuffer(reasoning_limit_bytes=1_024)
    successful.append(ToolUseStartEvent(tool_use_id="tool", tool_name="echo"))
    successful.append(ToolUseDeltaEvent(tool_use_id="tool", json_fragment='{"value":'))
    successful.append(ToolUseDeltaEvent(tool_use_id="tool", json_fragment='"ok"}'))
    successful.append(
        ToolUseEndEvent(tool_use_id="tool", tool_name="echo", arguments={"value": "ok"})
    )

    drained = successful.drain(successful_leg=True)

    assert [type(event) for event in drained] == [
        ToolUseStartEvent,
        ToolUseDeltaEvent,
        ToolUseEndEvent,
    ]
    assert drained[1].json_fragment == '{"value":"ok"}'

    oversized = _SelectorPreTextBuffer(reasoning_limit_bytes=32)
    oversized.append(ReasoningDeltaEvent(text="discardable reasoning"))
    oversized.append(ToolUseStartEvent(tool_use_id="tool", tool_name="echo"))
    oversized.append(ToolUseDeltaEvent(tool_use_id="tool", json_fragment="x" * 64))

    assert oversized.overflowed is True
    assert oversized.buffered_bytes == 0
    assert oversized.drain(successful_leg=True) == []


@pytest.mark.asyncio
async def test_agent_emits_structured_activity_and_honors_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _SequenceProvider(
        [
            [ErrorEvent(message="synthetic rate limit", code="429", retry_after_s=8.0)],
            [TextDeltaEvent(text="ok"), DoneEvent(stop_reason="stop")],
        ]
    )
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("opensquilla.engine.agent.asyncio.sleep", fake_sleep)
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_provider_retries=1,
            retry_base_backoff_ms=1_000,
            retry_max_backoff_ms=1_000,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]
    activity = [event for event in events if isinstance(event, ProviderActivityEvent)]

    assert provider.calls == 2
    assert sleeps == [8.0]
    assert [event.phase for event in activity] == [
        "requesting",
        "retry_wait",
        "retrying",
        "requesting",
    ]
    assert activity[1].reason == "rate_limited"
    assert activity[1].retry_after_ms == 8_000
    assert not any("synthetic rate limit" in repr(event) for event in activity)


@pytest.mark.asyncio
async def test_agent_normalizes_untrusted_provider_activity_fields() -> None:
    raw_id_marker = "RAW_PROVIDER_ACTIVITY_ID_MUST_NOT_ESCAPE"
    raw_phase_marker = "RAW_PROVIDER_ACTIVITY_PHASE_MUST_NOT_ESCAPE"
    raw_reason_marker = "RAW_PROVIDER_ACTIVITY_REASON_MUST_NOT_ESCAPE"
    upstream_activity = ProviderDomainActivityEvent(heartbeat=True)
    # A provider plugin can bypass static Literal annotations at runtime.
    upstream_activity.__dict__.update(
        {
            "schema_version": 99,
            "activity_id": raw_id_marker,
            "phase": raw_phase_marker,
            "reason": raw_reason_marker,
        }
    )
    valid_activity = ProviderDomainActivityEvent(
        phase="retry_wait",
        reason="rate_limited",
        retry_attempt=1,
        retry_limit=2,
        retry_after_ms=8_000,
    )
    provider = _SequenceProvider(
        [[valid_activity, upstream_activity, TextDeltaEvent(text="ok"), DoneEvent()]]
    )
    agent = Agent(provider=provider, config=AgentConfig())

    events = [event async for event in agent.run_turn("hello")]

    activities = [event for event in events if isinstance(event, ProviderActivityEvent)]
    projected = next(event for event in activities if event.heartbeat)
    valid_projected = next(event for event in activities if event.phase == "retry_wait")
    assert projected.schema_version == 1
    assert projected.activity_id == activities[0].activity_id
    assert projected.phase == "requesting"
    assert projected.reason == "unknown"
    assert valid_projected.reason == "rate_limited"
    assert valid_projected.retry_attempt == 1
    assert valid_projected.retry_limit == 2
    assert valid_projected.retry_after_ms == 8_000
    for marker in (raw_id_marker, raw_phase_marker, raw_reason_marker):
        assert marker not in repr(events)


@pytest.mark.asyncio
async def test_retry_after_that_exceeds_turn_deadline_does_not_retry_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _SequenceProvider(
        [[ErrorEvent(message="synthetic rate limit", code="429", retry_after_s=8.0)]]
    )
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("opensquilla.engine.agent.asyncio.sleep", fake_sleep)
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            timeout=1,
            max_provider_retries=1,
            retry_base_backoff_ms=1_000,
            retry_max_backoff_ms=1_000,
        ),
    )

    events = [event async for event in agent.run_turn("hello")]

    assert provider.calls == 1
    assert sleeps == []
    assert not any(
        isinstance(event, ProviderActivityEvent)
        and event.phase in {"retry_wait", "retrying"}
        for event in events
    )
    assert any(isinstance(event, EngineErrorEvent) and event.code == "429" for event in events)


@pytest.mark.asyncio
async def test_agent_terminal_and_turn_log_do_not_expose_provider_error_prose() -> None:
    raw_detail = "RAW_PROVIDER_BODY_DO_NOT_PERSIST"
    provider = _SequenceProvider(
        [[ErrorEvent(message=f"bad request: {raw_detail}", code="400")]]
    )
    turn_log = _CapturingTurnLog()
    agent = Agent(
        provider=provider,
        config=AgentConfig(max_provider_retries=0),
        turn_call_logger=turn_log,  # type: ignore[arg-type]
    )

    events = [event async for event in agent.run_turn("hello")]

    terminal = next(event for event in events if isinstance(event, EngineErrorEvent))
    assert terminal.code == "400"
    assert terminal.failure_kind == ProviderFailureKind.BAD_REQUEST.value
    assert terminal.message == "The model provider rejected the request."
    assert raw_detail not in repr(turn_log.records)
    llm_error = next(row for row in turn_log.records if row["kind"] == "llm_error")
    assert llm_error["payload"]["error"] == {
        "code": "400",
        "code_chars": 3,
        "message_chars": len(f"bad request: {raw_detail}"),
    }


@pytest.mark.asyncio
async def test_agent_terminal_normalizes_provider_controlled_error_code() -> None:
    provider = _SequenceProvider(
        [[ErrorEvent(message="bad request", code="PRIVATE_PROVIDER_CODE_BODY")]]
    )
    agent = Agent(provider=provider, config=AgentConfig(max_provider_retries=0))

    events = [event async for event in agent.run_turn("hello")]

    terminal = next(event for event in events if isinstance(event, EngineErrorEvent))
    assert terminal.code == "provider_error"
    assert "PRIVATE_PROVIDER_CODE_BODY" not in repr(terminal)


@pytest.mark.asyncio
async def test_first_reasoning_delta_emits_activity_before_thinking() -> None:
    provider = _SequenceProvider(
        [[ReasoningDeltaEvent(text="think"), TextDeltaEvent(text="ok"), DoneEvent()]]
    )
    agent = Agent(provider=provider, config=AgentConfig())

    events = [event async for event in agent.run_turn("hello")]

    reasoning_index = next(
        index
        for index, event in enumerate(events)
        if isinstance(event, ProviderActivityEvent) and event.phase == "reasoning"
    )
    thinking_index = next(
        index for index, event in enumerate(events) if isinstance(event, ThinkingEvent)
    )
    assert reasoning_index < thinking_index


@pytest.mark.asyncio
async def test_selector_streams_each_primary_reasoning_delta_before_text() -> None:
    provider = _SequenceProvider(
        [[
            ReasoningDeltaEvent(text="first "),
            ReasoningDeltaEvent(text="second"),
            TextDeltaEvent(text="answer"),
            DoneEvent(stop_reason="stop"),
        ]]
    )

    class _Selector:
        current_config = SimpleNamespace(provider="openrouter", model="primary/model")

    events = [
        event
        async for event in _SelectorFallbackProvider(provider, _Selector()).chat(
            [Message(role="user", content="hi")]
        )
    ]

    visible = [
        event
        for event in events
        if isinstance(event, (ReasoningDeltaEvent, TextDeltaEvent))
    ]
    assert [type(event) for event in visible] == [
        ReasoningDeltaEvent,
        ReasoningDeltaEvent,
        TextDeltaEvent,
    ]
    assert [event.text for event in visible] == ["first ", "second", "answer"]


@pytest.mark.asyncio
async def test_selector_streams_each_fallback_reasoning_delta_before_text() -> None:
    primary = _SequenceProvider([[ErrorEvent(message="busy", code="503")]])
    fallback = _SequenceProvider(
        [[
            ReasoningDeltaEvent(text="fallback first "),
            ReasoningDeltaEvent(text="fallback second"),
            TextDeltaEvent(text="answer"),
            DoneEvent(stop_reason="stop"),
        ]]
    )

    class _Selector:
        current_config = SimpleNamespace(provider="openrouter", model="primary/model")

        def next_fallback_after_failure(self, exc: Exception) -> Any:
            del exc
            self.current_config = SimpleNamespace(
                provider="openrouter",
                model="fallback/model",
            )
            return fallback

    events = [
        event
        async for event in _SelectorFallbackProvider(primary, _Selector()).chat(
            [Message(role="user", content="hi")]
        )
    ]

    visible = [
        event
        for event in events
        if isinstance(event, (ReasoningDeltaEvent, TextDeltaEvent))
    ]
    assert [event.text for event in visible] == [
        "fallback first ",
        "fallback second",
        "answer",
    ]
    assert primary.calls == fallback.calls == 1


@pytest.mark.asyncio
async def test_selector_reasoning_does_not_commit_incomplete_primary_tool_frames() -> None:
    secret_fragment = '{"api_key":"must-not-escape"'
    primary = _SequenceProvider(
        [[
            ToolUseStartEvent(tool_use_id="open", tool_name="echo"),
            ToolUseDeltaEvent(tool_use_id="open", json_fragment=secret_fragment),
            ReasoningDeltaEvent(text="reasoning after an incomplete tool"),
        ]]
    )
    fallback = _SequenceProvider(
        [[TextDeltaEvent(text="safe fallback"), DoneEvent(stop_reason="stop")]]
    )

    class _Selector:
        current_config = SimpleNamespace(provider="openrouter", model="primary/model")

        def next_fallback_after_failure(self, exc: Exception) -> Any:
            del exc
            self.current_config = SimpleNamespace(
                provider="openrouter",
                model="fallback/model",
            )
            return fallback

    events = [
        event
        async for event in _SelectorFallbackProvider(primary, _Selector()).chat(
            [Message(role="user", content="hi")]
        )
    ]

    assert primary.calls == fallback.calls == 1
    assert secret_fragment not in repr(events)
    assert not any(
        isinstance(event, (ToolUseStartEvent, ToolUseDeltaEvent, ReasoningDeltaEvent))
        for event in events
    )
    assert any(
        isinstance(event, TextDeltaEvent) and event.text == "safe fallback"
        for event in events
    )


@pytest.mark.asyncio
async def test_selector_reasoning_does_not_commit_incomplete_fallback_tool_frames() -> None:
    secret_fragment = '{"token":"must-not-escape"'
    primary = _SequenceProvider([[ErrorEvent(message="busy", code="503")]])
    fallback = _SequenceProvider(
        [[
            ToolUseStartEvent(tool_use_id="open", tool_name="echo"),
            ToolUseDeltaEvent(tool_use_id="open", json_fragment=secret_fragment),
            ReasoningDeltaEvent(text="reasoning after an incomplete tool"),
        ]]
    )

    class _Selector:
        current_config = SimpleNamespace(provider="openrouter", model="primary/model")

        def next_fallback_after_failure(self, exc: Exception) -> Any:
            del exc
            self.current_config = SimpleNamespace(
                provider="openrouter",
                model="fallback/model",
            )
            return fallback

    events = [
        event
        async for event in _SelectorFallbackProvider(primary, _Selector()).chat(
            [Message(role="user", content="hi")]
        )
    ]

    assert primary.calls == fallback.calls == 1
    assert secret_fragment not in repr(events)
    assert not any(
        isinstance(event, (ToolUseStartEvent, ToolUseDeltaEvent, ReasoningDeltaEvent))
        for event in events
    )
    terminal = next(event for event in events if isinstance(event, ErrorEvent))
    assert terminal.code == "invalid_stream_order"


@pytest.mark.asyncio
async def test_selector_reasoning_commits_primary_and_suppresses_fallback() -> None:
    primary = _SequenceProvider(
        [[
            ReasoningDeltaEvent(text="failed secret"),
            ErrorEvent(code="429", retry_after_s=90.0),
        ]]
    )
    fallback = _SequenceProvider(
        [[TextDeltaEvent(text="safe answer"), DoneEvent(stop_reason="stop")]]
    )

    class _Selector:
        current_config = SimpleNamespace(provider="openrouter", model="primary/model")

        def next_fallback_after_failure(self, exc: Exception) -> Any:
            del exc
            self.current_config = SimpleNamespace(
                provider="openrouter",
                model="fallback/model",
            )
            return fallback

    class _Clock:
        now = 1_000.0

        def __call__(self) -> float:
            return self.now

    clock = _Clock()
    health = ProviderHealthLedger(clock=clock)
    wrapper = _SelectorFallbackProvider(primary, _Selector(), health_ledger=health)
    events = [event async for event in wrapper.chat([Message(role="user", content="hi")])]

    reasoning_index = next(
        index
        for index, event in enumerate(events)
        if isinstance(event, ProviderDomainActivityEvent) and event.phase == "reasoning"
    )
    text_index = next(
        index for index, event in enumerate(events) if isinstance(event, ReasoningDeltaEvent)
    )
    assert reasoning_index < text_index
    assert primary.calls == 1
    assert fallback.calls == 0
    assert any(
        isinstance(event, ReasoningDeltaEvent) and event.text == "failed secret"
        for event in events
    )
    assert not any(
        isinstance(event, ProviderDomainActivityEvent) and event.phase == "fallback"
        for event in events
    )
    assert any(isinstance(event, ErrorEvent) for event in events)
    clock.now += 60.0
    assert not health.is_benched("openrouter", "primary/model")


@pytest.mark.asyncio
async def test_selector_fallback_discards_failed_leg_tool_frames() -> None:
    primary = _SequenceProvider(
        [[
            ToolUseStartEvent(tool_use_id="ghost", tool_name="echo"),
            ToolUseDeltaEvent(tool_use_id="ghost", json_fragment='{"secret":true}'),
            ToolUseEndEvent(
                tool_use_id="ghost",
                tool_name="echo",
                arguments={"secret": True},
            ),
            ErrorEvent(code="429", retry_after_s=90.0),
        ]]
    )
    fallback = _SequenceProvider(
        [[TextDeltaEvent(text="safe answer"), DoneEvent(stop_reason="stop")]]
    )

    class _Selector:
        current_config = SimpleNamespace(provider="openrouter", model="primary/model")

        def next_fallback_after_failure(self, exc: Exception) -> Any:
            del exc
            self.current_config = SimpleNamespace(
                provider="openrouter",
                model="fallback/model",
            )
            return fallback

    wrapper = _SelectorFallbackProvider(primary, _Selector())
    events = [event async for event in wrapper.chat([Message(role="user", content="hi")])]

    assert not any(
        isinstance(event, (ToolUseStartEvent, ToolUseDeltaEvent, ToolUseEndEvent))
        for event in events
    )
    assert any(
        isinstance(event, TextDeltaEvent) and event.text == "safe answer"
        for event in events
    )


@pytest.mark.asyncio
async def test_selector_buffer_exhaustion_falls_back_before_agent_surfaces_error() -> None:
    primary = _SequenceProvider(
        [[
            ToolUseStartEvent(tool_use_id="oversized", tool_name="echo"),
            ToolUseDeltaEvent(
                tool_use_id="oversized",
                json_fragment="x" * (2 * 1024 * 1024 + 1),
            ),
            ToolUseEndEvent(
                tool_use_id="oversized",
                tool_name="echo",
                arguments={},
            ),
            DoneEvent(stop_reason="tool_use"),
        ]]
    )
    fallback = _SequenceProvider(
        [[TextDeltaEvent(text="bounded fallback"), DoneEvent(stop_reason="stop")]]
    )

    class _Selector:
        current_config = SimpleNamespace(provider="openrouter", model="primary/model")

        def next_fallback_after_failure(self, exc: Exception) -> Any:
            del exc
            self.current_config = SimpleNamespace(
                provider="openrouter",
                model="fallback/model",
            )
            return fallback

    wrapper = _SelectorFallbackProvider(primary, _Selector())
    agent = Agent(provider=wrapper, config=AgentConfig(max_provider_retries=0))

    events = [event async for event in agent.run_turn("hello")]

    assert primary.calls == 1
    assert fallback.calls == 1
    assert not any(str(getattr(event, "kind", "")).startswith("tool_use") for event in events)
    assert not any(isinstance(event, EngineErrorEvent) for event in events)


@pytest.mark.asyncio
async def test_selector_exposes_reasoning_only_leg_after_visible_commit() -> None:
    provider = _SequenceProvider(
        [[ReasoningDeltaEvent(text="failed secret"), DoneEvent(reasoning_content="failed secret")]]
    )

    class _Selector:
        current_config = SimpleNamespace(provider="openrouter", model="primary/model")

    wrapper = _SelectorFallbackProvider(provider, _Selector())
    events = [event async for event in wrapper.chat([Message(role="user", content="hi")])]

    assert any(
        isinstance(event, ReasoningDeltaEvent) and event.text == "failed secret"
        for event in events
    )
    assert any(isinstance(event, DoneEvent) for event in events)
