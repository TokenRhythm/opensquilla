"""Rendering, batching, and the streaming provider call.

Rendering preserves the transcript entries and truncates head+tail so a long
session still fits a bounded prompt; batching drops a session that alone blows
the budget rather than showing the model a half-session it will still cite. The
streaming call mirrors the task analyzer's loop and must fail open — a batch
that errors, times out, or ends early becomes a failed batch, never a raise.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from opensquilla.provider.types import DoneEvent, ErrorEvent, TextDeltaEvent
from opensquilla.squilla_router.user_profile import extractor
from opensquilla.squilla_router.user_profile.schema import SessionTranscript


@dataclass
class _Row:
    role: str
    content: str | None


def test_render_preserves_all_nonempty_transcript_roles() -> None:
    rows = [
        _Row("system", "you are helpful"),
        _Row("user", "write a function"),
        _Row("tool", "{...}"),
        _Row("assistant", "here you go"),
    ]
    text = extractor.render_transcript("s1", rows, per_session_max_chars=10_000).text
    assert "user: write a function" in text
    assert "assistant: here you go" in text
    assert "system: you are helpful" in text
    assert "tool: {...}" in text


def test_render_truncates_head_and_tail_with_a_marker() -> None:
    rows = [_Row("user", "A" * 500 + "B" * 500)]
    rendered = extractor.render_transcript("s1", rows, per_session_max_chars=120)
    assert len(rendered.text) <= 120
    assert "truncated" in rendered.text


def test_batching_groups_by_size() -> None:
    sessions = [SessionTranscript(f"s{i}", "x" * 10) for i in range(25)]
    batches = extractor.batch_sessions(sessions, batch_size=10, batch_input_max_chars=100_000)
    assert [len(b) for b in batches] == [10, 10, 5]


def test_a_session_over_the_batch_budget_is_dropped_whole() -> None:
    sessions = [
        SessionTranscript("small", "x" * 10),
        SessionTranscript("huge", "x" * 1000),
    ]
    batches = extractor.batch_sessions(sessions, batch_size=10, batch_input_max_chars=100)
    kept = [s.session_id for b in batches for s in b]
    assert kept == ["small"]  # huge dropped, not split


def test_char_budget_forces_a_new_batch() -> None:
    sessions = [SessionTranscript(f"s{i}", "x" * 60) for i in range(3)]
    batches = extractor.batch_sessions(sessions, batch_size=10, batch_input_max_chars=100)
    # Each session is 60 chars; two would exceed 100, so one per batch.
    assert [len(b) for b in batches] == [1, 1, 1]


class _FakeProvider:
    """A provider whose ``chat`` yields a scripted event stream."""

    def __init__(self, events, *, delay: float = 0.0) -> None:
        self._events = events
        self._delay = delay
        self.closed = False

    def chat(self, messages, tools=None, config=None):  # noqa: ANN001
        provider = self

        async def _stream() -> AsyncIterator[object]:
            try:
                for event in provider._events:
                    if provider._delay:
                        await asyncio.sleep(provider._delay)
                    yield event
            finally:
                provider.closed = True

        return _stream()


def _batch() -> list[SessionTranscript]:
    return [SessionTranscript("s1", "user: hi")]


def _stream_factory(
    *,
    provider,
    user_prompt: str,
    system_prompt: str,
    max_output_tokens: int,
    temperature: float,
    timeout: float,
):
    del user_prompt, system_prompt, max_output_tokens, temperature, timeout
    return provider.chat([], tools=None, config=None)


_GOOD_JSON = (
    '{"session_labels":[{"session_id":"s1","capability":"reasoning",'
    '"confidence":0.8}],"quality_latency_tradeoff":{"value":"quality_first",'
    '"confidence":0.7,"session_ids":["s1"]},"model_mentions":[]}'
)


async def test_a_clean_stream_parses_into_an_ok_batch() -> None:
    provider = _FakeProvider([TextDeltaEvent(text=_GOOD_JSON), DoneEvent()])
    analysis = await extractor.extract_batch(
        provider=provider,
        stream_factory=_stream_factory,
        batch=_batch(),
        max_output_tokens=500,
        temperature=0.0,
        timeout=5.0,
        response_max_chars=48_000,
    )
    assert analysis.ok
    assert analysis.session_labels[0].capability == "reasoning"
    assert provider.closed  # stream always closed


async def test_an_error_event_fails_the_batch_open() -> None:
    provider = _FakeProvider([TextDeltaEvent(text="{"), ErrorEvent(message="boom", code="500")])
    analysis = await extractor.extract_batch(
        provider=provider,
        stream_factory=_stream_factory,
        batch=_batch(),
        max_output_tokens=500,
        temperature=0.0,
        timeout=5.0,
        response_max_chars=48_000,
    )
    assert analysis.ok is False
    assert analysis.session_ids == ("s1",)


async def test_a_stream_ending_before_done_fails_open() -> None:
    provider = _FakeProvider([TextDeltaEvent(text=_GOOD_JSON)])  # no DoneEvent
    analysis = await extractor.extract_batch(
        provider=provider,
        stream_factory=_stream_factory,
        batch=_batch(),
        max_output_tokens=500,
        temperature=0.0,
        timeout=5.0,
        response_max_chars=48_000,
    )
    assert analysis.ok is False


async def test_a_timeout_fails_open() -> None:
    provider = _FakeProvider([TextDeltaEvent(text=_GOOD_JSON), DoneEvent()], delay=0.2)
    analysis = await extractor.extract_batch(
        provider=provider,
        stream_factory=_stream_factory,
        batch=_batch(),
        max_output_tokens=500,
        temperature=0.0,
        timeout=0.01,
        response_max_chars=48_000,
    )
    assert analysis.ok is False


async def test_an_oversized_response_fails_open() -> None:
    provider = _FakeProvider([TextDeltaEvent(text="x" * 100), DoneEvent()])
    analysis = await extractor.extract_batch(
        provider=provider,
        stream_factory=_stream_factory,
        batch=_batch(),
        max_output_tokens=500,
        temperature=0.0,
        timeout=5.0,
        response_max_chars=10,
    )
    assert analysis.ok is False
