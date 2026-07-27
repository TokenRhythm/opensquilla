from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from opensquilla.cli.chat.turn import UsageSummary
from opensquilla.cli.chat.turn_stream import (
    default_turn_stream_dependencies,
    stream_response_gateway,
    stream_response_turnrunner,
)
from opensquilla.engine.types import DoneEvent, EnsembleProgressEvent
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

    async def send_message(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        for event in self._events:
            yield event

    async def resolve_approval(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def abort_session(self, _key: str) -> None:
        return None


class _TurnRunner:
    def __init__(self, events: list[object]) -> None:
        self._events = events

    async def run(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[object]:
        for event in self._events:
            yield event


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
