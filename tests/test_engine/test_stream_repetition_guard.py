from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.engine import Agent, AgentConfig
from opensquilla.engine.repetition_guard import (
    MODEL_REPETITION_LOOP_CODE,
    ModelRepetitionLoopError,
    RepetitionDetection,
    RepetitionGuardPolicy,
    StreamingRepetitionGuard,
    guard_provider_text_stream,
)
from opensquilla.engine.usage_accounting import (
    UsageAccountingScope,
    UsageCallResult,
    UsageCallStart,
    UsageExecutionContext,
    account_provider_stream,
    bind_usage_accounting_scope,
)
from opensquilla.provider import ChatConfig, Message
from opensquilla.provider import DoneEvent as ProviderDone
from opensquilla.provider import TextDeltaEvent as ProviderText
from opensquilla.provider import ToolUseEndEvent as ProviderToolUseEnd
from opensquilla.provider import ToolUseStartEvent as ProviderToolUseStart


def _feed_in_chunks(
    text: str,
    chunk_size: int,
    *,
    policy: RepetitionGuardPolicy | None = None,
) -> tuple[str, RepetitionDetection | None, StreamingRepetitionGuard]:
    guard = StreamingRepetitionGuard(policy)
    emitted: list[str] = []
    detection: RepetitionDetection | None = None
    for start in range(0, len(text), chunk_size):
        accepted, detection = guard.feed(text[start : start + chunk_size])
        emitted.append(accepted)
        if detection is not None:
            break
    return "".join(emitted), detection, guard


def test_repetition_detection_is_chunk_invariant() -> None:
    phrase = "I am reading the file while checking the next section carefully. "
    payload = phrase * 300

    results = [_feed_in_chunks(payload, size)[:2] for size in (1, 7, 257, len(payload))]

    emitted_lengths = {len(emitted) for emitted, _ in results}
    detections = {detection for _, detection in results}
    assert len(emitted_lengths) == 1
    assert len(detections) == 1
    [detection] = list(detections)
    assert detection is not None
    assert detection.repeated_chars >= 4_096
    assert detection.repetitions >= 8
    assert detection.similarity >= 0.985
    assert detection.structured is False


def test_highly_similar_repetition_with_small_changing_field_is_detected() -> None:
    rows = [
        (
            f"Progress marker {chr(65 + index % 26)}: I am reading the file and "
            "checking the same section before continuing carefully. "
        )
        for index in range(240)
    ]

    _, detection, _ = _feed_in_chunks("".join(rows), 113)

    assert detection is not None
    assert detection.similarity >= 0.985


def test_short_repeated_unit_is_detected_via_a_larger_period() -> None:
    payload = "yes " * 3_000

    _, detection, _ = _feed_in_chunks(payload, 1)

    assert detection is not None
    assert detection.period_chars >= 48


@pytest.mark.parametrize(
    "unit",
    [
        pytest.param("for item in items:\n    print(item)\n", id="code"),
        pytest.param("print(item)\n", id="code-call"),
        pytest.param("| model | status |\n| --- | --- |\n", id="markdown-table"),
        pytest.param(
            "2026-08-13T12:00:00 INFO provider stream remains active\n",
            id="log",
        ),
    ],
)
def test_structured_repetition_uses_slow_threshold(unit: str) -> None:
    payload = (unit * (16_000 // len(unit) + 1))[:16_000]

    emitted, detection, _ = _feed_in_chunks(payload, 97)

    assert detection is None
    assert emitted == payload


def test_long_structured_loop_is_not_permanently_exempt() -> None:
    unit = "for item in items:\n    print(item)\n"
    payload = (unit * (50_000 // len(unit) + 1))[:50_000]

    _, detection, _ = _feed_in_chunks(payload, 211)

    assert detection is not None
    assert detection.structured is True
    assert detection.repeated_chars >= 32_768
    assert detection.repetitions >= 32


def test_nonperiodic_code_table_and_log_output_does_not_trigger() -> None:
    payload = "".join(
        f"2026-08-13T12:{index // 60:02d}:{index % 60:02d} INFO row | {index} | "
        f"value_{index * 17}\n"
        for index in range(2_000)
    )

    emitted, detection, guard = _feed_in_chunks(payload, 173)

    assert detection is None
    assert emitted == payload
    assert guard.buffered_chars <= 65_536


class _RepeatingIterator:
    def __init__(self, *, block_close: bool = False) -> None:
        self.close_calls = 0
        self.block_close = block_close
        self.close_started = asyncio.Event()

    def __aiter__(self) -> _RepeatingIterator:
        return self

    async def __anext__(self) -> ProviderText:
        await asyncio.sleep(0)
        return ProviderText(
            text="I am reading the file while checking the next section carefully. "
        )

    async def aclose(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        if self.block_close:
            await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_guard_closes_upstream_once_before_raising() -> None:
    upstream = _RepeatingIterator()
    emitted: list[str] = []

    with pytest.raises(ModelRepetitionLoopError):
        async for event in guard_provider_text_stream(upstream):
            emitted.append(event.text)

    assert upstream.close_calls == 1
    assert 4_096 <= len("".join(emitted)) <= 5_120


@pytest.mark.asyncio
async def test_guard_close_is_bounded_when_upstream_ignores_close() -> None:
    upstream = _RepeatingIterator(block_close=True)
    policy = RepetitionGuardPolicy(close_timeout_seconds=0.01)

    async def consume() -> None:
        async for _ in guard_provider_text_stream(upstream, policy=policy):
            pass

    with pytest.raises(ModelRepetitionLoopError):
        await asyncio.wait_for(consume(), timeout=0.25)

    assert upstream.close_started.is_set()
    assert upstream.close_calls == 1


@pytest.mark.asyncio
async def test_tool_boundary_resets_repetition_budget() -> None:
    phrase = "I am reading the file while checking the next section carefully. "

    async def stream() -> AsyncIterator[Any]:
        yield ProviderText(text=(phrase * 60)[:3_000])
        yield ProviderToolUseStart(tool_use_id="tool-1", tool_name="read")
        yield ProviderToolUseEnd(
            tool_use_id="tool-1",
            tool_name="read",
            arguments={},
        )
        yield ProviderText(text=(phrase * 60)[:3_000])
        yield ProviderDone()

    events = [event async for event in guard_provider_text_stream(stream())]

    assert [event.kind for event in events] == [
        "text_delta",
        "tool_use_start",
        "tool_use_end",
        "text_delta",
        "done",
    ]


class _RecordingSink:
    def __init__(self) -> None:
        self.started: list[UsageCallStart] = []
        self.finalized: list[tuple[UsageCallStart, UsageCallResult]] = []
        self.unknown: list[tuple[UsageCallStart, str]] = []

    async def start(self, call: UsageCallStart) -> None:
        self.started.append(call)

    async def finalize(self, call: UsageCallStart, result: UsageCallResult) -> None:
        self.finalized.append((call, result))

    async def mark_unknown(self, call: UsageCallStart, reason: str) -> None:
        self.unknown.append((call, reason))


class _RepeatingProvider:
    provider_name = "synthetic"

    def __init__(self) -> None:
        self.calls = 0
        self.streams: list[_RepeatingIterator] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[Any]:
        del messages, tools, config
        self.calls += 1
        stream = _RepeatingIterator()
        self.streams.append(stream)
        return stream

    async def list_models(self) -> list[Any]:
        return []


@pytest.mark.asyncio
async def test_agent_stops_without_retry_and_marks_usage_unknown_once() -> None:
    sink = _RecordingSink()
    provider = _RepeatingProvider()
    observer_calls: list[dict[str, Any]] = []
    agent = Agent(
        provider=provider,
        config=AgentConfig(
            max_iterations=1,
            max_provider_retries=3,
            retry_base_backoff_ms=0,
            retry_max_backoff_ms=0,
            provider_id="synthetic",
            model_id="looping-model",
            provider_call_observer=lambda **kwargs: observer_calls.append(kwargs),
        ),
        usage_event_sink=sink,
        usage_execution_context=UsageExecutionContext(
            execution_id="execution-949",
            agent_run_id="run-949",
            turn_id="turn-949",
        ),
    )

    events = [event async for event in agent.run_turn("read the file")]

    errors = [event for event in events if event.kind == "error"]
    assert [event.code for event in errors] == [MODEL_REPETITION_LOOP_CODE]
    assert not any(event.kind == "done" for event in events)
    assert provider.calls == 1
    assert provider.streams[0].close_calls == 1
    assert len(sink.started) == 1
    assert sink.finalized == []
    assert [(call.event_id, reason) for call, reason in sink.unknown] == [
        (sink.started[0].event_id, MODEL_REPETITION_LOOP_CODE)
    ]
    assert len(observer_calls) == 1
    assert observer_calls[0]["ok"] is False
    assert observer_calls[0]["failure_kind"] == MODEL_REPETITION_LOOP_CODE


@pytest.mark.asyncio
async def test_physical_usage_wrapper_closes_as_unknown_exactly_once() -> None:
    sink = _RecordingSink()
    upstream = _RepeatingIterator()
    scope = UsageAccountingScope(
        sink=sink,
        context=UsageExecutionContext(
            execution_id="execution-physical-949",
            agent_run_id="run-physical-949",
        ),
    )

    async def close_propagating_stream() -> AsyncIterator[Any]:
        try:
            async for event in upstream:
                yield event
        finally:
            await upstream.aclose()

    with bind_usage_accounting_scope(scope):
        accounted = account_provider_stream(
            close_propagating_stream,
            provider="synthetic",
            model="looping-model",
        )
        with pytest.raises(ModelRepetitionLoopError):
            async for _ in guard_provider_text_stream(accounted):
                pass

    assert upstream.close_calls == 1
    assert len(sink.started) == 1
    assert sink.finalized == []
    assert [(call.event_id, reason) for call, reason in sink.unknown] == [
        (sink.started[0].event_id, "provider_stream_ended_without_usage")
    ]
