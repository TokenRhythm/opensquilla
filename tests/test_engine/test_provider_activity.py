from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.agent import _provider_retry_delay_seconds, _ToolArgumentProgress
from opensquilla.engine.routing.health import ProviderHealthLedger
from opensquilla.engine.runtime import (
    _SELECTOR_REASONING_TRUNCATED_NOTICE,
    _report_credential_pool_failure,
    _SelectorFallbackProvider,
    _SelectorPreTextBuffer,
)
from opensquilla.engine.types import ErrorEvent as EngineErrorEvent
from opensquilla.engine.types import ProviderActivityEvent, RunHeartbeatEvent, ThinkingEvent
from opensquilla.provider import (
    ChatConfig,
    DoneEvent,
    ErrorEvent,
    Message,
    ProviderFailureKind,
    ProviderHeartbeatEvent,
    ReasoningDeltaEvent,
    TextDeltaEvent,
    ToolDefinition,
    ToolInputSchema,
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


class _ObservedSequenceProvider(_SequenceProvider):
    def __init__(self, streams: list[list[Any]]) -> None:
        super().__init__(streams)
        self.observed: list[Any] = []

    async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
        for event in events:
            self.observed.append(event)
            yield event


def _tool_progress_agent(provider: Any, executions: list[str]) -> Agent:
    async def handler(call: Any) -> ToolResult:
        executions.append(call.tool_use_id)
        return ToolResult(
            tool_use_id=call.tool_use_id,
            tool_name=call.tool_name,
            content="ok",
        )

    return Agent(
        provider=provider,
        config=AgentConfig(max_provider_retries=0),
        tool_definitions=[
            ToolDefinition(
                name="echo",
                description="Echo a value.",
                input_schema=ToolInputSchema(),
            )
        ],
        tool_handler=handler,
    )


@pytest.mark.asyncio
async def test_first_tool_argument_fragment_reports_progress_before_tool_commit() -> None:
    provider = _ObservedSequenceProvider([
        [
            ToolUseStartEvent(tool_use_id="first", tool_name="echo"),
            ToolUseDeltaEvent(tool_use_id="first", json_fragment=""),
            ToolUseDeltaEvent(tool_use_id="first", json_fragment="{"),
            ToolUseStartEvent(tool_use_id="second", tool_name="echo"),
            ToolUseDeltaEvent(tool_use_id="second", json_fragment="{}"),
            ToolUseDeltaEvent(tool_use_id="first", json_fragment="}"),
            ToolUseEndEvent(tool_use_id="first", tool_name="echo", arguments={}),
            ToolUseEndEvent(tool_use_id="second", tool_name="echo", arguments={}),
            DoneEvent(stop_reason="tool_calls"),
        ],
        [TextDeltaEvent(text="done"), DoneEvent(stop_reason="stop")],
    ])
    executions: list[str] = []
    events: list[Any] = []

    async for event in _tool_progress_agent(provider, executions).run_turn("hello"):
        events.append(event)
        if isinstance(event, RunHeartbeatEvent) and event.phase == "llm_tool_arguments":
            assert isinstance(provider.observed[-1], ToolUseDeltaEvent)
            assert provider.observed[-1].json_fragment == "{"
            assert not any(isinstance(raw, DoneEvent) for raw in provider.observed)
            assert executions == []
            assert not any(str(getattr(item, "kind", "")).startswith("tool_use") for item in events)
            assert event.message == "Receiving tool arguments"
            assert "first" not in repr(event) and "second" not in repr(event)

    heartbeats = [event for event in events if isinstance(event, RunHeartbeatEvent)]
    # Empty frames and parallel starts do not repeat the initial signal.
    assert len(heartbeats) == 1
    assert sorted(executions) == ["first", "second"]


@pytest.mark.asyncio
async def test_tool_argument_progress_preserves_periodic_character_boundary() -> None:
    prefix = '{"value":"'
    provider = _ObservedSequenceProvider([
        [
            ToolUseStartEvent(tool_use_id="large", tool_name="echo"),
            ToolUseDeltaEvent(tool_use_id="large", json_fragment=prefix),
            ToolUseDeltaEvent(tool_use_id="large", json_fragment="x" * (4095 - len(prefix))),
            ToolUseDeltaEvent(tool_use_id="large", json_fragment="x"),
            ToolUseDeltaEvent(tool_use_id="large", json_fragment='"}'),
            ToolUseEndEvent(tool_use_id="large", tool_name="echo", arguments={"value": "x"}),
            DoneEvent(stop_reason="tool_calls"),
        ],
        [TextDeltaEvent(text="done"), DoneEvent(stop_reason="stop")],
    ])
    executions: list[str] = []
    heartbeat_chars: list[int] = []

    async for event in _tool_progress_agent(provider, executions).run_turn("hello"):
        if isinstance(event, RunHeartbeatEvent) and event.phase == "llm_tool_arguments":
            heartbeat_chars.append(sum(
                len(raw.json_fragment)
                for raw in provider.observed
                if isinstance(raw, ToolUseDeltaEvent)
            ))

    assert heartbeat_chars == [len(prefix), 4096]
    assert executions == ["large"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_prefix", [True, False])
async def test_tool_argument_progress_never_commits_invalid_or_failed_attempt(
    invalid_prefix: bool,
) -> None:
    provider = _ObservedSequenceProvider([[
        ToolUseStartEvent(tool_use_id="pending", tool_name="echo"),
        ToolUseDeltaEvent(
            tool_use_id="unknown" if invalid_prefix else "pending",
            json_fragment='{"private":"uncommitted"',
        ),
        ErrorEvent(message="request rejected", code="400"),
    ]])
    executions: list[str] = []

    events = [event async for event in _tool_progress_agent(provider, executions).run_turn("hello")]

    assert executions == []
    assert not any(str(getattr(event, "kind", "")).startswith("tool_use") for event in events)
    assert "uncommitted" not in repr(events)
    heartbeats = [event for event in events if isinstance(event, RunHeartbeatEvent)]
    assert len(heartbeats) == (0 if invalid_prefix else 1)
    assert any(isinstance(event, EngineErrorEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_leg", [False, True])
async def test_selector_reports_pending_argument_progress_before_commit(
    fallback_leg: bool,
) -> None:
    streamed = _ObservedSequenceProvider([[
        ToolUseStartEvent(tool_use_id="private-tool-id", tool_name="echo"),
        ToolUseDeltaEvent(tool_use_id="private-tool-id", json_fragment='{"secret":'),
        ToolUseDeltaEvent(tool_use_id="private-tool-id", json_fragment="true}"),
        ToolUseEndEvent(
            tool_use_id="private-tool-id", tool_name="echo", arguments={"secret": True},
        ),
        DoneEvent(stop_reason="tool_calls"),
    ]])
    primary = _SequenceProvider([[ErrorEvent(code="503")]]) if fallback_leg else streamed

    class _Selector:
        current_config = SimpleNamespace(provider="openrouter", model="primary/model")

        def next_fallback_after_failure(self, exc: Exception) -> Any:
            del exc
            self.current_config = SimpleNamespace(provider="openrouter", model="fallback/model")
            return streamed if fallback_leg else None

    visible: list[Any] = []
    progress_count = 0
    async for event in _SelectorFallbackProvider(primary, _Selector()).chat(
        [Message(role="user", content="hello")]
    ):
        visible.append(event)
        if isinstance(event, ProviderHeartbeatEvent) and event.phase == "llm_tool_arguments":
            progress_count += 1
            assert streamed.observed[-1].json_fragment == '{"secret":'
            assert not any(isinstance(raw, DoneEvent) for raw in streamed.observed)
            assert event.upstream_activity is True
            assert "secret" not in repr(event) and "private-tool-id" not in repr(event)
            assert not any(
                isinstance(raw, (ToolUseStartEvent, ToolUseDeltaEvent)) for raw in visible
            )

    assert progress_count == 1
    assert any(isinstance(event, ToolUseEndEvent) for event in visible)


@pytest.mark.asyncio
@pytest.mark.parametrize("selector_wrapped", [False, True])
async def test_slow_small_tool_deltas_refresh_progress_for_more_than_twenty_seconds(
    monkeypatch: pytest.MonkeyPatch,
    selector_wrapped: bool,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(
        "opensquilla.engine.agent._ToolArgumentProgress",
        lambda: _ToolArgumentProgress(clock=lambda: clock[0]),
    )
    monkeypatch.setattr(
        "opensquilla.engine.runtime._ToolArgumentProgress",
        lambda: _ToolArgumentProgress(clock=lambda: clock[0]),
    )

    class _SlowSmallProvider(_ObservedSequenceProvider):
        async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
            for event in events:
                if isinstance(event, ToolUseDeltaEvent):
                    clock[0] += 1.0
                self.observed.append(event)
                yield event

    provider = _SlowSmallProvider([
        [
            ToolUseStartEvent(tool_use_id="slow", tool_name="echo"),
            ToolUseDeltaEvent(tool_use_id="slow", json_fragment='{"value":"'),
            *[ToolUseDeltaEvent(tool_use_id="slow", json_fragment="x") for _ in range(24)],
            ToolUseDeltaEvent(tool_use_id="slow", json_fragment='"}'),
            ToolUseEndEvent(tool_use_id="slow", tool_name="echo", arguments={"value": "x" * 24}),
            DoneEvent(stop_reason="tool_calls"),
        ],
        [TextDeltaEvent(text="done"), DoneEvent(stop_reason="stop")],
    ])

    class _Selector:
        current_config = SimpleNamespace(provider="openrouter", model="primary/model")

    agent_provider = (
        _SelectorFallbackProvider(provider, _Selector()) if selector_wrapped else provider
    )
    executions: list[str] = []
    pulse_times: list[float] = []
    async for event in _tool_progress_agent(agent_provider, executions).run_turn("hello"):
        if (
            isinstance(event, RunHeartbeatEvent)
            and event.phase == "llm_tool_arguments"
            and not any(isinstance(raw, DoneEvent) for raw in provider.observed)
        ):
            pulse_times.append(clock[0])
            assert executions == []

    assert pulse_times == list(range(1, 26, 2))
    assert executions == ["slow"]


def test_tool_argument_progress_ignores_empty_fragments_and_resets_for_retry() -> None:
    clock = [0.0]
    progress = _ToolArgumentProgress(clock=lambda: clock[0])
    assert progress.record("first", "{")
    clock[0] = 25.0
    assert not progress.record("first", "")  # Time alone never manufactures progress.
    assert progress.record("first", "x")
    progress.clear()
    assert progress.record("second-attempt", "{")


def test_selector_progress_does_not_escape_invalid_frames_or_failed_buffer() -> None:
    buffer = _SelectorPreTextBuffer()
    assert buffer.append(ToolUseDeltaEvent(tool_use_id="missing", json_fragment="private")) is None
    assert buffer.protocol_error
    assert buffer.drain(successful_leg=False) == []
    buffer.append(ToolUseStartEvent(tool_use_id="next", tool_name="echo"))
    pulse = buffer.append(ToolUseDeltaEvent(tool_use_id="next", json_fragment="{"))
    assert isinstance(pulse, ProviderHeartbeatEvent)
    assert buffer.drain(successful_leg=False) == []
    buffer.append(ToolUseStartEvent(tool_use_id="retry", tool_name="echo"))
    assert isinstance(
        buffer.append(ToolUseDeltaEvent(tool_use_id="retry", json_fragment="{")),
        ProviderHeartbeatEvent,
    )


@pytest.fixture
async def retry_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    loop = asyncio.get_running_loop()
    now = [loop.time()]
    sleeps: list[float] = []
    original_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        now[0] += delay
        await original_sleep(0)

    monkeypatch.setattr(loop, "time", lambda: now[0])
    from opensquilla.provider import retry_after

    monkeypatch.setattr(
        retry_after, "_provider_retry_after_cooldowns",
        retry_after.ProviderRetryAfterCooldowns(clock=loop.time),
    )
    monkeypatch.setattr("opensquilla.engine.agent.asyncio.sleep", fake_sleep)
    return sleeps


def test_provider_retry_delay_uses_larger_provider_hint() -> None:
    assert _provider_retry_delay_seconds(
        local_delay_s=2.0,
        provider_retry_after_s=8.0,
    ) == 8.0
    assert _provider_retry_delay_seconds(
        local_delay_s=12.0,
        provider_retry_after_s=8.0,
    ) == 12.0


@pytest.mark.parametrize("hint", [901.0, float("inf")])
def test_provider_retry_delay_does_not_clamp_an_excessive_hint_and_retry_early(hint: float) -> None:
    assert _provider_retry_delay_seconds(
        local_delay_s=1.0,
        provider_retry_after_s=hint,
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
async def test_agent_retries_rate_limit_on_same_deployment_after_provider_wait(
    retry_sleeps: list[float],
) -> None:
    provider = _SequenceProvider(
        [
            [ErrorEvent(message="synthetic rate limit", code="429", retry_after_s=8.0)],
            [TextDeltaEvent(text="ok"), DoneEvent(stop_reason="stop")],
        ]
    )
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
    assert retry_sleeps == [8.0]
    assert [event.phase for event in activity] == [
        "requesting",
        "retry_wait",
        "retrying",
        "requesting",
    ]
    assert activity[1].reason == ProviderFailureKind.RATE_LIMITED.value
    assert activity[1].retry_after_ms == 8_000
    assert activity[1].retry_attempt == activity[1].retry_limit == 1
    assert not any(isinstance(event, EngineErrorEvent) for event in events)
    assert not any("synthetic rate limit" in repr(event) for event in activity)


@pytest.mark.asyncio
async def test_agent_retries_same_deployment_provider_overload(
    retry_sleeps: list[float],
) -> None:
    provider = _SequenceProvider(
        [
            [ErrorEvent(message="synthetic overload", code="503", retry_after_s=8.0)],
            [TextDeltaEvent(text="ok"), DoneEvent(stop_reason="stop")],
        ]
    )
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
    assert retry_sleeps == [8.0]
    assert [event.phase for event in activity] == [
        "requesting",
        "retry_wait",
        "retrying",
        "requesting",
    ]
    assert activity[1].reason == ProviderFailureKind.PROVIDER_OVERLOADED.value
    assert activity[1].retry_after_ms == 8_000
    assert not any("synthetic overload" in repr(event) for event in activity)


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
    retry_sleeps: list[float],
) -> None:
    provider = _SequenceProvider(
        [[ErrorEvent(message="synthetic overload", code="503", retry_after_s=8.0)]]
    )
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
    assert retry_sleeps == []
    assert not any(
        isinstance(event, ProviderActivityEvent)
        and event.phase in {"retry_wait", "retrying"}
        for event in events
    )
    assert any(isinstance(event, EngineErrorEvent) and event.code == "503" for event in events)


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

    stream = _SelectorFallbackProvider(primary, _Selector()).chat(
        [Message(role="user", content="hi")]
    )
    first = await anext(stream)
    assert isinstance(first, ProviderDomainActivityEvent)
    assert (first.phase, first.from_model, first.model) == (
        "fallback", "primary/model", "fallback/model",
    )
    assert fallback.calls == 0  # Selection is observed before the target request begins.
    events = [first, *[event async for event in stream]]

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
async def test_selector_fallback_discards_failed_leg_tool_frames(
    retry_sleeps: list[float],
) -> None:
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
