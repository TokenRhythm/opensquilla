"""Regression tests for canonical text ownership at the shared turn boundary."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.engine.turn_runner.stream_consumer_stage import (
    StreamConsumerStageInput,
    _DoneHandler,
    _StreamState,
    _TextDeltaHandler,
    _ToolUseStartHandler,
)
from opensquilla.engine.turn_runner.turn_finalizer_stage import (
    TurnFinalizerStage,
    TurnFinalizerStageInput,
)
from opensquilla.engine.types import (
    DoneEvent,
    TextDeltaEvent,
    ToolUseStartEvent,
    done_text_snapshot,
)


class _RecordingTranscriptAppend:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def append_message(
        self,
        session_key: str,
        *,
        role: str,
        content: str,
        tool_calls: list[Any] | None,
        reasoning_content: str | None,
        turn_usage: dict[str, Any] | None,
        token_count: int | None,
    ) -> bool:
        self.calls.append(
            {
                "session_key": session_key,
                "role": role,
                "content": content,
                "tool_calls": tool_calls,
                "reasoning_content": reasoning_content,
                "turn_usage": turn_usage,
                "token_count": token_count,
            }
        )
        return True


class _NoopMemoryCapture:
    async def capture_turn(self, **_kwargs: Any) -> None:
        return None


class _NoopSessionTotals:
    async def rollup(self, **_kwargs: Any) -> None:
        return None


class _NoopErrorPersist:
    async def persist_error(self, **_kwargs: Any) -> None:
        return None


def _make_state() -> _StreamState:
    return _StreamState(
        current_text_parts=[],
        final_text_parts=[],
        turn_segments=[],
        turn_artifacts=[],
        artifact_delivery_failures=[],
    )


def _make_stream_input(state: _StreamState) -> StreamConsumerStageInput:
    return StreamConsumerStageInput(
        agent=SimpleNamespace(),
        agent_id="agent:main",
        sync_manager=None,
        private_memory_allowed=True,
        turn=SimpleNamespace(metadata={}, tool_defs=[]),
        tool_defs=[],
        turn_input="test",
        extra_messages=None,
        semantic_input="test",
        effective_runtime_message="test",
        session_key="agent:main:test",
        run_kind="default",
        heartbeat_ack_max_chars=300,
        bootstrap_context_mode=None,
        router_cfg=None,
        session_manager_present=True,
        state=state,
    )


async def _finalize(
    state: _StreamState,
    done_event: DoneEvent,
) -> tuple[str, _RecordingTranscriptAppend]:
    transcript = _RecordingTranscriptAppend()
    finalizer = TurnFinalizerStage(
        transcript_append=transcript,
        turn_memory_capture=_NoopMemoryCapture(),
        session_totals=_NoopSessionTotals(),
        turn_error_persist=_NoopErrorPersist(),
    )
    outcome = await finalizer.run(
        TurnFinalizerStageInput(
            final_text_parts=state.final_text_parts,
            turn_segments=state.turn_segments,
            turn_artifacts=state.turn_artifacts,
            error_message=None,
            pending_error_event=None,
            done_event=done_event,
            runtime_message="test",
            input_mode="user",
            input_provenance=None,
            resolved_model="synthetic/model",
            agent_id="agent:main",
            session_key="agent:main:test",
            tool_context=None,
            run_kind="default",
            heartbeat_ack_max_chars=300,
            no_memory_capture=False,
        )
    )
    return outcome.require_output().final_text, transcript


@pytest.mark.parametrize(
    "literal_text",
    [
        "Literal example: <tool_calls>not a real call</tool_calls>.",
        "Documentation may show `<tool_calls>...</tool_calls>`; keep the trailing note.",
        (
            "Literal DSML: <|DSML|tool_calls><|DSML|invoke name=\"echo\">"
            "not a real call</|DSML|invoke></|DSML|tool_calls>."
        ),
        "<details><summary>View areas around line 42</summary>literal text</details>",
    ],
)
@pytest.mark.asyncio
async def test_literal_protocol_like_text_is_canonical_across_stream_done_and_persist(
    literal_text: str,
) -> None:
    state = _make_state()
    text_handler = _TextDeltaHandler()

    if "`" in literal_text:
        opening_backtick_end = literal_text.index("`") + 1
        closing_angle = literal_text.index(">", opening_backtick_end)
        chunks = [
            literal_text[:opening_backtick_end],
            literal_text[opening_backtick_end:closing_angle],
            literal_text[closing_angle : closing_angle + 1],
            literal_text[closing_angle + 1 :],
        ]
    else:
        closing_angle = literal_text.index(">")
        chunks = [
            literal_text[:closing_angle],
            literal_text[closing_angle : closing_angle + 1],
            literal_text[closing_angle + 1 :],
        ]
    streamed_text = "".join(
        text_handler.handle(TextDeltaEvent(text=chunk), state).text for chunk in chunks
    )

    _ToolUseStartHandler().handle(
        ToolUseStartEvent(
            tool_use_id="tool-1",
            tool_name="echo",
            synthetic_from_text=True,
        ),
        state,
    )
    done_event, extra = _DoneHandler().handle(
        DoneEvent(text=literal_text),
        _make_stream_input(state),
        state,
    )

    assert extra == []
    assert streamed_text == literal_text
    assert done_event.text == literal_text
    assert "".join(state.final_text_parts) == literal_text
    assert state.turn_segments[0] == {"type": "text", "text": literal_text}

    final_text, transcript = await _finalize(state, done_event)

    assert final_text == literal_text
    assert transcript.calls[0]["content"] == literal_text
    assert transcript.calls[0]["tool_calls"][0] == {
        "type": "text",
        "text": literal_text,
    }


def test_partial_protocol_like_suffix_is_immediately_available_for_cancellation() -> None:
    state = _make_state()
    payload = "ordinary explanation ending with <tool_call"

    transformed = _TextDeltaHandler().handle(TextDeltaEvent(text=payload), state)

    assert transformed.text == payload
    assert "".join(state.final_text_parts) == payload
    assert "".join(state.current_text_parts) == payload


def test_done_text_is_the_fallback_when_only_empty_text_delta_was_produced() -> None:
    state = _make_state()
    literal_text = "Literal <tool_calls> documentation example </tool_calls>."
    transformed = _TextDeltaHandler().handle(TextDeltaEvent(text=""), state)

    done_event, extra = _DoneHandler().handle(
        DoneEvent(text=literal_text),
        _make_stream_input(state),
        state,
    )

    assert extra == []
    assert transformed.text == ""
    assert done_event.text == literal_text
    assert "".join(state.final_text_parts) == literal_text


def test_explicit_empty_done_snapshot_discards_superseded_streamed_text() -> None:
    state = _make_state()
    _TextDeltaHandler().handle(TextDeltaEvent(text="discarded recovery answer"), state)

    done_event, extra = _DoneHandler().handle(
        DoneEvent(text="", text_snapshot=""),
        _make_stream_input(state),
        state,
    )

    assert extra == []
    assert done_event.text == ""
    assert done_event.text_snapshot == ""
    assert state.final_text_parts == []
    assert state.current_text_parts == []


def test_legacy_empty_done_without_snapshot_preserves_partial_streamed_text() -> None:
    state = _make_state()
    _TextDeltaHandler().handle(TextDeltaEvent(text="authoritative partial"), state)

    done_event, extra = _DoneHandler().handle(
        DoneEvent(text=""),
        _make_stream_input(state),
        state,
    )

    assert extra == []
    assert done_event.text == "authoritative partial"
    assert done_event.text_snapshot is None
    assert state.final_text_parts == ["authoritative partial"]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (DoneEvent(text="legacy"), (True, "legacy")),
        (DoneEvent(text="", text_snapshot=""), (True, "")),
        ({"text": "legacy mapping"}, (True, "legacy mapping")),
        (
            {"text": "legacy mapping", "text_snapshot": None},
            (True, "legacy mapping"),
        ),
        ({"text": "stale", "text_snapshot": ""}, (True, "")),
        (DoneEvent(text=""), (False, "")),
    ],
)
def test_done_text_snapshot_presence_contract(
    payload: object,
    expected: tuple[bool, str],
) -> None:
    assert done_text_snapshot(payload) == expected


def test_done_strict_extension_is_reemitted_as_a_canonical_suffix_delta() -> None:
    state = _make_state()
    text_handler = _TextDeltaHandler()
    prefix = "Preparing the generated file."
    suffix = "\n\nThe generated file is ready: report.pptx"

    text_handler.handle(TextDeltaEvent(text=prefix), state)
    done_event, extra = _DoneHandler().handle(
        DoneEvent(text=prefix + suffix),
        _make_stream_input(state),
        state,
    )

    assert [event.text for event in extra if isinstance(event, TextDeltaEvent)] == [
        suffix
    ]
    assert done_event.text == prefix + suffix
    assert "".join(state.final_text_parts) == prefix + suffix


def test_conflicting_done_aggregate_cannot_resurrect_discarded_retry_text() -> None:
    state = _make_state()
    text_handler = _TextDeltaHandler()
    text_handler.handle(TextDeltaEvent(text="discarded retry"), state)
    text_handler.handle(TextDeltaEvent(text="kept answer"), state)

    done_event, extra = _DoneHandler().handle(
        # Agent-side retry state contains only the successful attempt, while
        # the outer stream consumer has already observed both attempts.
        DoneEvent(text="kept answer"),
        _make_stream_input(state),
        state,
    )

    assert extra == []
    assert done_event.text == "kept answer"
    assert "".join(state.final_text_parts) == "kept answer"


def test_conflicting_done_snapshot_preserves_tool_events_and_replaces_only_text() -> None:
    state = _make_state()
    text_handler = _TextDeltaHandler()
    text_handler.handle(TextDeltaEvent(text="stale pre-tool narration"), state)
    _ToolUseStartHandler().handle(
        ToolUseStartEvent(tool_use_id="tool-1", tool_name="search"),
        state,
    )
    state.turn_segments.append(
        {
            "type": "tool_result",
            "tool_use_id": "tool-1",
            "name": "search",
            "content": "ok",
        }
    )
    text_handler.handle(TextDeltaEvent(text="stale retry answer"), state)

    done_event, extra = _DoneHandler().handle(
        DoneEvent(text="canonical successful answer"),
        _make_stream_input(state),
        state,
    )

    assert extra == []
    assert done_event.text == "canonical successful answer"
    assert "".join(state.final_text_parts) == "canonical successful answer"
    assert [segment["type"] for segment in state.turn_segments] == [
        "tool_use",
        "tool_result",
    ]
    assert "".join(state.current_text_parts) == "canonical successful answer"


def test_cumulative_meta_final_snapshot_does_not_duplicate_streamed_prefix() -> None:
    state = _make_state()
    text_handler = _TextDeltaHandler()
    text_handler.handle(TextDeltaEvent(text="Planning complete. "), state)
    text_handler.handle(
        TextDeltaEvent(text="Planning complete. Final plan is ready."),
        state,
    )

    done_event, extra = _DoneHandler().handle(
        DoneEvent(text="Planning complete. Final plan is ready."),
        _make_stream_input(state),
        state,
    )

    assert extra == []
    assert done_event.text == "Planning complete. Final plan is ready."
    assert "".join(state.final_text_parts) == done_event.text
