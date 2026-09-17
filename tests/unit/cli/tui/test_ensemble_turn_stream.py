from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.cli.chat.turn import UsageCounter, UsageSummary
from opensquilla.cli.chat.turn_stream import (
    default_turn_stream_dependencies,
    stream_response_gateway,
    stream_response_turnrunner,
)
from opensquilla.engine.types import (
    AnswerGenerationResetEvent,
    DoneEvent,
    EnsembleProgressEvent,
    TextDeltaEvent,
)
from opensquilla.tools.types import CallerKind, ToolContext

PROGRESS_PAYLOAD: dict[str, Any] = {
    "event_type": "proposer_finish",
    "proposer_index": 1,
    "proposer_label": "critic",
    "proposer_model": "z-ai/glm-5.2",
    "proposer_provider": "openrouter",
    "sample_index": 0,
    "elapsed_ms": 321,
    "input_tokens": 120,
    "output_tokens": 24,
    "cost_usd": 0.003,
    "error": "",
}

MODEL_USAGE_BREAKDOWN: list[dict[str, Any]] = [
    {
        "role": "proposer",
        "label": "critic",
        "provider": "openrouter",
        "model": "z-ai/glm-5.2",
        "input_tokens": 120,
        "output_tokens": 24,
    },
    {
        "role": "aggregator",
        "label": "aggregator",
        "provider": "openrouter",
        "model": "openai/gpt-5.4",
        "input_tokens": 160,
        "output_tokens": 32,
    },
]

ENSEMBLE_TRACE: dict[str, Any] = {
    "mode": "b5_fusion",
    "profile": "static_openrouter_b5",
    "llm_request_count": 5,
    "fallback_used": False,
}


class _GatewayClient:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events
        self.drained = False

    async def send_message(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        for event in self._events:
            yield event
        self.drained = True

    async def resolve_approval(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def abort_session(self, _key: str) -> None:
        return None


class _TurnRunner:
    def __init__(self, events: list[object]) -> None:
        self._events = events
        self.drained = False

    async def run(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[object]:
        for event in self._events:
            yield event
        self.drained = True


class _RecordingRenderer:
    def __init__(self) -> None:
        self.buffer = ""
        self.ensemble_progress: list[dict[str, Any]] = []
        self.final_usage: UsageSummary | None = None

    def __enter__(self) -> _RecordingRenderer:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> bool:
        return False

    async def aturn_started(self) -> None:
        return None

    async def aappend_text(self, delta: str, **_kwargs: Any) -> None:
        self.buffer += delta

    async def aensemble_progress(self, payload: dict[str, Any]) -> None:
        self.ensemble_progress.append(payload)

    async def afinalize(
        self,
        usage: UsageSummary | None = None,
        *,
        cancelled: bool = False,
    ) -> None:
        del cancelled
        self.final_usage = usage

    async def aclose(self) -> None:
        return None

    def pulse(self) -> None:
        return None


class _LegacyRenderer:
    """Renderer predating the optional ensemble hook."""

    def __init__(self) -> None:
        self.buffer = ""

    def __enter__(self) -> _LegacyRenderer:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> bool:
        return False

    async def aappend_text(self, delta: str, **_kwargs: Any) -> None:
        self.buffer += delta

    async def afinalize(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def aclose(self) -> None:
        return None


def _deps(renderer: object):
    return default_turn_stream_dependencies(
        renderer_factory=lambda **_kwargs: renderer,
        stream_wrapper=lambda stream, _svc: stream,
    )


def _tool_context() -> ToolContext:
    return ToolContext(
        caller_kind=CallerKind.CLI,
        channel_kind="cli",
        channel_id="cli:chat",
    )


@pytest.mark.asyncio
async def test_gateway_stream_forwards_real_ensemble_progress_and_preserves_done_trace() -> None:
    renderer = _RecordingRenderer()
    events = [
        {
            "event": "session.event.ensemble_progress",
            "session_id": "session-id",
            "turn_id": "turn-id",
            **PROGRESS_PAYLOAD,
        },
        {"event": "session.event.text_delta", "text": "fused answer"},
        {
            "event": "session.event.done",
            "model": "openai/gpt-5.4",
            "input_tokens": 280,
            "output_tokens": 56,
            "model_usage_breakdown": MODEL_USAGE_BREAKDOWN,
            "ensemble_trace": ENSEMBLE_TRACE,
        },
    ]

    result = await stream_response_gateway(
        _GatewayClient(events),
        "agent:main:test",
        "question",
        deps=_deps(renderer),
    )

    assert renderer.ensemble_progress == [PROGRESS_PAYLOAD]
    assert result.text == "fused answer"
    assert result.usage is renderer.final_usage
    assert result.usage is not None
    assert result.usage.model_usage_breakdown == MODEL_USAGE_BREAKDOWN
    assert result.usage.model_usage_breakdown is not MODEL_USAGE_BREAKDOWN
    assert result.usage.ensemble_trace == ENSEMBLE_TRACE
    assert result.usage.ensemble_trace is not ENSEMBLE_TRACE


@pytest.mark.asyncio
async def test_turnrunner_stream_forwards_matching_ensemble_progress_and_done_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = _RecordingRenderer()
    progress = EnsembleProgressEvent(**PROGRESS_PAYLOAD)
    done = DoneEvent(
        text="fused answer",
        model="openai/gpt-5.4",
        input_tokens=280,
        output_tokens=56,
        model_usage_breakdown=MODEL_USAGE_BREAKDOWN,
        ensemble_trace=ENSEMBLE_TRACE,
    )
    turn_runner = _TurnRunner([progress, done])
    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", _TurnRunner)

    result = await stream_response_turnrunner(
        turn_runner,
        "agent:main:test",
        _tool_context(),
        "question",
        deps=_deps(renderer),
    )

    assert renderer.ensemble_progress == [PROGRESS_PAYLOAD]
    assert result.usage is renderer.final_usage
    assert result.usage is not None
    assert result.usage.model_usage_breakdown == MODEL_USAGE_BREAKDOWN
    assert result.usage.ensemble_trace == ENSEMBLE_TRACE


@pytest.mark.asyncio
async def test_ensemble_progress_is_compatible_with_renderer_without_optional_hook() -> None:
    renderer = _LegacyRenderer()

    result = await stream_response_gateway(
        _GatewayClient(
            [
                {"event": "session.event.ensemble_progress", **PROGRESS_PAYLOAD},
                {"event": "session.event.done", "model": "single-model"},
            ]
        ),
        "agent:main:test",
        "question",
        deps=_deps(renderer),
    )

    assert result.usage is not None
    assert result.usage.model == "single-model"


def test_usage_summary_does_not_infer_ensemble_without_actual_done_trace() -> None:
    gateway_usage = UsageSummary.from_gateway_payload({"model": "single-model"})
    runner_usage = UsageSummary.from_done_event(DoneEvent(model="single-model"))

    assert gateway_usage.model_usage_breakdown == []
    assert gateway_usage.ensemble_trace == {}
    assert runner_usage.model_usage_breakdown == []
    assert runner_usage.ensemble_trace == {}


def test_gateway_session_snapshot_drives_cli_usage_counter() -> None:
    usage = UsageSummary.from_gateway_payload(
        {
            "input_tokens": 1070,
            "output_tokens": 207,
            "cost_usd": 0.57,
            "session_totals": {
                "input_tokens": 1070,
                "output_tokens": 207,
                "cache_read_tokens": 50,
                "cache_write_tokens": 25,
                "cost_usd": 0.57,
                "billed_cost": 0.57,
            },
        }
    )
    counter = UsageCounter()

    counter.apply(usage)

    assert counter.input_tokens == 1070
    assert counter.output_tokens == 207
    assert counter.cached_tokens == 50
    assert counter.cost_usd == pytest.approx(0.57)


@pytest.mark.asyncio
async def test_turnrunner_terminal_reset_replaces_partial_and_returns_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal_text = "The fixed model could not complete this answer."
    renderer = _RecordingRenderer()
    turn_runner = _TurnRunner(
        [
            TextDeltaEvent(text="superseded partial"),
            AnswerGenerationResetEvent(
                old_generation_epoch=1,
                new_generation_epoch=2,
                safe_reason="fixed provider final failure",
                terminal=True,
                terminal_text_snapshot=terminal_text,
            ),
        ]
    )
    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", _TurnRunner)

    result = await stream_response_turnrunner(
        turn_runner,
        "agent:main:test",
        _tool_context(),
        "question",
        deps=_deps(renderer),
    )

    assert result.text == terminal_text
    assert result.error == terminal_text
    assert "superseded partial" not in result.text
    assert turn_runner.drained is True


@pytest.mark.asyncio
async def test_gateway_terminal_reset_replaces_partial_and_returns_failure() -> None:
    terminal_text = "The fixed model could not complete this answer."
    renderer = _RecordingRenderer()

    client = _GatewayClient(
        [
            {"event": "session.event.text_delta", "text": "superseded partial"},
            {
                "event": "session.event.answer_generation_reset",
                "old_generation_epoch": 1,
                "new_generation_epoch": 2,
                "safe_reason": "fixed provider final failure",
                "terminal": True,
                "terminal_text_snapshot": terminal_text,
            },
        ]
    )
    result = await stream_response_gateway(
        client,
        "agent:main:test",
        "question",
        deps=_deps(renderer),
    )

    assert result.text == terminal_text
    assert result.error == terminal_text
    assert "superseded partial" not in result.text
    assert client.drained is True
