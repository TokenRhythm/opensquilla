from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig, ToolResult
from opensquilla.engine.agent import _provider_retry_delay_seconds
from opensquilla.engine.pipeline import TurnContext
from opensquilla.engine.routing.health import ProviderHealthLedger
from opensquilla.engine.runtime import (
    _SELECTOR_REASONING_TRUNCATED_NOTICE,
    _report_credential_pool_failure,
    _SelectorFallbackProvider,
    _SelectorPreTextBuffer,
    _trace_routing_decision_payload,
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


@pytest.mark.asyncio
async def test_call_duration_excludes_context_preparation(monkeypatch: pytest.MonkeyPatch) -> None:
    import time

    now = [10.0]
    monkeypatch.setattr(
        "opensquilla.engine.agent.time",
        SimpleNamespace(
            monotonic=lambda: now[0], time=time.time, time_ns=time.time_ns,
            perf_counter=time.perf_counter,
        ),
    )

    class TimedLog(_CapturingTurnLog):
        def write(self, kind: str, payload: dict[str, Any]) -> None:
            if kind == "context_stage" and payload.get("stage") == "stream:context":
                now[0] += 2.0
            super().write(kind, payload)

    class TimedProvider(_SequenceProvider):
        async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
            now[0] += 0.25
            async for event in super()._stream(events):
                yield event

    turn_log = TimedLog()
    agent = Agent(
        provider=TimedProvider([[TextDeltaEvent(text="done"), DoneEvent(stop_reason="stop")]]),
        config=AgentConfig(max_provider_retries=0),
        turn_call_logger=turn_log,  # type: ignore[arg-type]
    )
    _ = [event async for event in agent.run_turn("hello")]
    response = next(row for row in turn_log.records if row["kind"] == "llm_response")
    assert now[0] == 12.25
    assert response["payload"]["duration_ms"] == 250


@pytest.mark.asyncio
async def test_agent_persists_bounded_progress_before_provider_finishes(monkeypatch) -> None:
    import time

    now = [20.0]
    synthetic_time = SimpleNamespace(**vars(time))
    synthetic_time.monotonic = lambda: now[0]
    monkeypatch.setattr("opensquilla.engine.agent.time", synthetic_time)
    turn_log = _CapturingTurnLog()

    class LiveProvider(_SequenceProvider):
        async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
            yield ReasoningDeltaEvent(text="inspect")
            assert [row["kind"] for row in turn_log.records].count("llm_progress") == 1
            now[0] += 0.25
            yield TextDeltaEvent(text="first")
            assert [row["kind"] for row in turn_log.records].count("llm_progress") == 1
            now[0] += 1.0
            yield TextDeltaEvent(text=" second")
            assert not any(row["kind"] == "llm_response" for row in turn_log.records)
            progress = [row for row in turn_log.records if row["kind"] == "llm_progress"]
            assert len(progress) == 2
            assert progress[-1]["payload"]["text"] == "first second"
            assert progress[-1]["payload"]["reasoning_content"] == "inspect"
            assert "messages" not in progress[-1]["payload"]
            yield DoneEvent(stop_reason="stop", reasoning_content="inspect")

    agent = Agent(
        provider=LiveProvider([[]]),
        config=AgentConfig(max_provider_retries=0),
        turn_call_logger=turn_log,  # type: ignore[arg-type]
    )
    _ = [event async for event in agent.run_turn("hello")]
    call_rows = [row for row in turn_log.records if row["kind"].startswith("llm_")]
    assert [row["kind"] for row in call_rows] == [
        "llm_request", "llm_progress", "llm_progress", "llm_response",
    ]
    assert len({row["payload"]["call_id"] for row in call_rows}) == 1
    assert call_rows[-1]["payload"]["text"] == "first second"
    assert call_rows[-1]["payload"]["reasoning_content"] == "inspect"


@pytest.mark.asyncio
async def test_agent_captures_tool_arguments_while_model_is_still_generating(monkeypatch) -> None:
    import time

    now = [30.0]
    synthetic_time = SimpleNamespace(**vars(time))
    synthetic_time.monotonic = lambda: now[0]
    monkeypatch.setattr("opensquilla.engine.agent.time", synthetic_time)
    turn_log = _CapturingTurnLog()

    class LiveToolProvider(_SequenceProvider):
        async def _stream(self, events: list[Any]) -> AsyncIterator[Any]:
            if self.calls > 1:
                yield TextDeltaEvent(text="done")
                yield DoneEvent(stop_reason="stop")
                return
            yield ToolUseStartEvent(tool_use_id="tool-a", tool_name="echo")
            now[0] += 1.0
            yield ToolUseDeltaEvent(tool_use_id="tool-a", json_fragment='{"value": "par')
            partial = [row for row in turn_log.records if row["kind"] == "llm_progress"][-1]
            assert partial["payload"]["tool_calls"][0]["arguments_text"] == '{"value": "par'
            assert not any(
                row["kind"] in {"llm_response", "tool_request"} for row in turn_log.records
            )
            yield ToolUseDeltaEvent(tool_use_id="tool-a", json_fragment='tial"}')
            yield ToolUseEndEvent(
                tool_use_id="tool-a", tool_name="echo", arguments={"value": "partial"}
            )
            yield DoneEvent(stop_reason="tool_use")

    async def tool_handler(call: Any) -> ToolResult:
        return ToolResult(tool_use_id=call.tool_use_id, tool_name=call.tool_name, content="ok")

    agent = Agent(
        provider=LiveToolProvider([[]]),
        config=AgentConfig(max_provider_retries=0),
        tool_definitions=[ToolDefinition(
            name="echo", description="Echo a value.",
            input_schema=ToolInputSchema(
                properties={"value": {"type": "string"}}, required=["value"]
            ),
        )],
        tool_handler=tool_handler,
        turn_call_logger=turn_log,  # type: ignore[arg-type]
    )
    _ = [event async for event in agent.run_turn("use echo")]
    final = next(row for row in turn_log.records if row["kind"] == "llm_response")
    assert final["payload"]["tool_calls"][0]["arguments"] == {"value": "partial"}



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
    monkeypatch.setattr("opensquilla.engine.agent.asyncio.sleep", fake_sleep)
    return sleeps


@pytest.mark.parametrize(
    ("router_enabled", "ensemble_enabled", "metadata", "requested", "effective"),
    [
        (False, False, {}, "direct", "direct"),
        (True, False, {"routing_applied": True, "routing_source": "classifier"},
         "router", "router"),
        (True, True, {"routing_applied": True, "ensemble_enabled": True},
         "ensemble", "ensemble"),
        (True, True, {"routing_applied": True,
                     "ensemble_wrap_skipped_reason": "no_eligible_members"},
         "ensemble", "direct"),
    ],
)
def test_trace_route_keeps_requested_mode_and_actual_execution_separate(
    router_enabled: bool,
    ensemble_enabled: bool,
    metadata: dict[str, Any],
    requested: str,
    effective: str,
) -> None:
    config = SimpleNamespace(
        squilla_router=SimpleNamespace(enabled=router_enabled, rollout_phase="full"),
        llm_ensemble=SimpleNamespace(enabled=ensemble_enabled),
    )
    turn = TurnContext(
        message="synthetic", session_key="synthetic", config=config, provider=None,
        model="selected-model", tool_defs=[], system_prompt="", metadata=metadata,
    )
    payload = _trace_routing_decision_payload(
        turn, config, requested_mode=requested, requested_model="requested-model",
        selected_model="selected-model", provider="synthetic-provider",
    )
    assert payload["requested_mode"] == requested
    assert payload["effective_mode"] == effective
    assert payload["requested_model"] == "requested-model"
    assert payload["selected_model"] == "selected-model"
    assert payload["router_enabled"] is router_enabled
    assert "duration_ms" not in payload
    if "ensemble_wrap_skipped_reason" in metadata:
        assert payload["reason"] == "no_eligible_members"


def test_trace_disabled_router_is_a_direct_selection_without_classifier_evidence() -> None:
    config = SimpleNamespace(squilla_router=SimpleNamespace(enabled=False))
    turn = TurnContext(
        message="synthetic", session_key="synthetic", config=config, provider=None,
        model="fixed-model", tool_defs=[], system_prompt="",
    )
    payload = _trace_routing_decision_payload(
        turn, config, requested_mode=None, requested_model=None,
        selected_model="fixed-model", provider="synthetic-provider",
    )
    assert payload["requested_mode"] == payload["effective_mode"] == "direct"
    assert payload["reason"] == "router_disabled"
    assert payload["routing_applied"] is False
    assert "routing_confidence" not in payload
    assert "routed_tier" not in payload


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
    turn_log = _CapturingTurnLog()
    agent = Agent(
        provider=provider,
        turn_call_logger=turn_log,  # type: ignore[arg-type]
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

    boundaries = [row for row in turn_log.records if row["kind"] == "provider_retry"]
    assert [row["payload"]["phase"] for row in boundaries] == ["retry_wait", "retrying"]
    assert boundaries[0]["payload"]["retry_after_ms"] == 8_000
    kinds = [row["kind"] for row in turn_log.records]
    assert kinds.index("llm_error") < kinds.index("provider_retry")
    assert kinds.index("provider_retry") < kinds.index("llm_response")
    assert all("duration_ms" not in row["payload"] for row in boundaries)


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
