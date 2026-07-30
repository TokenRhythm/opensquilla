from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from opensquilla.cli.chat.turn_stream import (
    TurnStreamDependencies,
    handle_image_command_turnrunner,
    stream_response_gateway,
    stream_response_turnrunner,
)
from opensquilla.cli.tui.backend.domain_events import (
    KIND_DONE,
    KIND_ROUTER_DECISION,
    KIND_TEXT_FLUSH,
    TuiDomainEvent,
)
from opensquilla.cli.tui.backend.streaming import StreamingFlushPolicy, StreamingPlane
from opensquilla.engine.types import DoneEvent, RouterDecisionEvent, TextDeltaEvent
from opensquilla.tools.types import CallerKind, ToolContext


class MutableClock:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> int:
        return self.value

    def advance(self, milliseconds: int) -> None:
        self.value += milliseconds


def test_streaming_plane_flushes_when_delay_budget_expires() -> None:
    clock = MutableClock()
    plane = StreamingPlane(
        policy=StreamingFlushPolicy(max_delay_ms=33, max_chars=100),
        clock_ms=clock,
    )

    assert plane.append("abc") is None
    clock.advance(34)
    flush = plane.append("def")

    assert flush is not None
    assert flush.text == "abcdef"
    assert flush.reason == "delay"
    assert plane.delta_count == 2
    assert plane.flush_count == 1
    assert plane.text_chars == 6
    assert plane.max_buffer_chars == 6


def test_streaming_plane_flushes_when_size_budget_is_reached() -> None:
    plane = StreamingPlane(policy=StreamingFlushPolicy(max_chars=10))

    assert plane.append("12345") is None
    flush = plane.append("67890")

    assert flush is not None
    assert flush.text == "1234567890"
    assert flush.reason == "size"
    assert plane.flush_count == 1


def test_streaming_plane_flushes_newline_chunks_after_minimum_size() -> None:
    plane = StreamingPlane(
        policy=StreamingFlushPolicy(max_chars=100, newline_min_chars=5),
    )

    assert plane.append("abcd") is None
    flush = plane.append("e\n")

    assert flush is not None
    assert flush.text == "abcde\n"
    assert flush.reason == "newline"


def test_streaming_plane_finish_flushes_tail_text() -> None:
    plane = StreamingPlane(policy=StreamingFlushPolicy(max_chars=100))

    assert plane.append("tail") is None
    flush = plane.finish()

    assert flush is not None
    assert flush.text == "tail"
    assert flush.reason == "finish"
    assert plane.finish() is None


def test_streaming_plane_emits_text_flush_domain_events() -> None:
    events: list[TuiDomainEvent] = []
    plane = StreamingPlane(
        policy=StreamingFlushPolicy(max_chars=4),
        event_sink=events.append,
        source="turn_runner",
        turn_id="turn-1",
    )

    flush = plane.append("abcd")

    assert flush is not None
    assert [event.kind for event in events] == [KIND_TEXT_FLUSH]
    assert events[0].source == "turn_runner"
    assert events[0].turn_id == "turn-1"
    assert events[0].payload["text"] == "abcd"
    assert events[0].payload["chars"] == 4
    assert events[0].payload["reason"] == "size"


class _FakeGatewayClient:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    async def send_message(
        self,
        session_key: str,
        message: str,
        attachments: list[dict] | None = None,
        elevated: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        del session_key, message, attachments, elevated
        for event in self._events:
            yield event

    async def resolve_approval(
        self,
        approval_id: str,
        approved: bool,
        *,
        choice: str | None = None,
    ) -> Any:
        del approval_id, approved, choice
        return None

    async def abort_session(self, key: str) -> Any:
        del key
        return None


class _FakeRenderer:
    def __init__(self) -> None:
        self.buffer = ""
        self.append_calls: list[str] = []
        self.finalized = False

    def __enter__(self) -> _FakeRenderer:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> Literal[False]:
        del exc_type, exc, tb
        return False

    async def aappend_text(self, delta: str) -> None:
        self.append_calls.append(delta)
        self.buffer += delta

    async def afinalize(self, usage: Any | None = None, *, cancelled: bool = False) -> None:
        del usage, cancelled
        self.finalized = True

    async def aclose(self) -> None:
        return None


class _RendererFactory:
    def __init__(self) -> None:
        self.created: list[_FakeRenderer] = []

    def __call__(self, **_kwargs: Any) -> _FakeRenderer:
        renderer = _FakeRenderer()
        self.created.append(renderer)
        return renderer


class _FakeOutputHandle:
    @property
    def approval_surface(self) -> object:
        return object()

    async def write_through(self, payload: str) -> None:
        del payload

    @asynccontextmanager
    async def stream_output(self):
        def write(_payload: str) -> None:
            return None

        yield write


def _text_delta_events(count: int, chunk: str) -> list[dict[str, Any]]:
    return [
        {"event": "session.event.text_delta", "text": chunk}
        for _ in range(count)
    ] + [{"event": "session.event.done"}]


class _ImageTurnRunner:
    def __init__(self, events: list[object]) -> None:
        self._events = events

    async def run(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[object]:
        for event in self._events:
            yield event


def test_turn_stream_coalesces_many_text_deltas_for_tui_output() -> None:
    renderer_factory = _RendererFactory()
    events: list[TuiDomainEvent] = []
    deps = TurnStreamDependencies(
        renderer_factory=renderer_factory,
        stream_wrapper=lambda stream, _svc: stream,
        approval_handler=lambda *_args, **_kwargs: asyncio.sleep(0),
        cancel_clearer=lambda: None,
        image_attachment_builder=lambda _command: ("", []),
        output_console=object(),
        error_panel_factory=lambda message: message,
        tui_event_sink=events.append,
    )

    result = asyncio.run(
        stream_response_gateway(
            _FakeGatewayClient(_text_delta_events(4_000, "abcd")),
            "session-1",
            "hello",
            tui_output=_FakeOutputHandle(),
            deps=deps,
        )
    )

    renderer = renderer_factory.created[0]
    assert result.text == "abcd" * 4_000
    assert renderer.buffer == result.text
    assert len(renderer.append_calls) < 100
    assert len(renderer.append_calls) == sum(
        1 for event in events if event.kind == KIND_TEXT_FLUSH
    )


def test_turn_stream_keeps_one_delta_per_append_without_tui_streaming_surface() -> None:
    renderer_factory = _RendererFactory()
    deps = TurnStreamDependencies(
        renderer_factory=renderer_factory,
        stream_wrapper=lambda stream, _svc: stream,
        approval_handler=lambda *_args, **_kwargs: asyncio.sleep(0),
        cancel_clearer=lambda: None,
        image_attachment_builder=lambda _command: ("", []),
        output_console=object(),
        error_panel_factory=lambda message: message,
    )

    result = asyncio.run(
        stream_response_gateway(
            _FakeGatewayClient(_text_delta_events(12, "x")),
            "session-1",
            "hello",
            deps=deps,
        )
    )

    renderer = renderer_factory.created[0]
    assert result.text == "x" * 12
    assert renderer.append_calls == ["x"] * 12


def test_image_command_turnrunner_uses_tui_streaming_plane_and_events(
    monkeypatch,
) -> None:
    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", _ImageTurnRunner)
    renderer_factory = _RendererFactory()
    events: list[TuiDomainEvent] = []
    deps = TurnStreamDependencies(
        renderer_factory=renderer_factory,
        stream_wrapper=lambda stream, _svc: stream,
        approval_handler=lambda *_args, **_kwargs: asyncio.sleep(0),
        cancel_clearer=lambda: None,
        image_attachment_builder=lambda _command: (
            "describe image",
            [{"path": "/tmp/image.png"}],
        ),
        output_console=object(),
        error_panel_factory=lambda message: message,
        tui_event_sink=events.append,
    )
    tool_ctx = ToolContext(
        caller_kind=CallerKind.CLI,
        channel_kind="cli",
        channel_id="cli:chat",
    )
    router_event = RouterDecisionEvent(
        tier="t2",
        tier_index=2,
        model="anthropic/claude-sonnet-4.6",
        baseline_model="anthropic/claude-opus-4.7",
        source="router",
        confidence=0.71,
        savings_pct=64.0,
    )
    turn_runner = _ImageTurnRunner(
        [router_event]
        + [TextDeltaEvent(text="abcd") for _ in range(4_000)]
        + [DoneEvent(model="anthropic/claude-sonnet-4.6")]
    )

    result = asyncio.run(
        handle_image_command_turnrunner(
            turn_runner,
            "agent:main:image",
            tool_ctx,
            "/image /tmp/image.png",
            tui_output=_FakeOutputHandle(),
            deps=deps,
        )
    )

    renderer = renderer_factory.created[0]
    assert result.text == "abcd" * 4_000
    assert renderer.buffer == result.text
    assert len(renderer.append_calls) < 100
    assert [event.kind for event in events if event.kind != KIND_TEXT_FLUSH] == [
        KIND_ROUTER_DECISION,
        KIND_DONE,
    ]
    assert sum(1 for event in events if event.kind == KIND_TEXT_FLUSH) == len(
        renderer.append_calls
    )


def test_gateway_done_snapshot_replaces_streamed_preview() -> None:
    renderer_factory = _RendererFactory()
    deps = TurnStreamDependencies(
        renderer_factory=renderer_factory,
        stream_wrapper=lambda stream, _svc: stream,
        approval_handler=lambda *_args, **_kwargs: asyncio.sleep(0),
        cancel_clearer=lambda: None,
        image_attachment_builder=lambda _command: ("", []),
        output_console=object(),
        error_panel_factory=lambda message: message,
    )

    result = asyncio.run(
        stream_response_gateway(
            _FakeGatewayClient(
                [
                    {"event": "session.event.text_delta", "text": "stale"},
                    {
                        "event": "session.event.done",
                        "text": "canonical",
                        "text_snapshot": "canonical",
                    },
                ]
            ),
            "session-1",
            "hello",
            deps=deps,
        )
    )

    assert result.text == "canonical"
    assert renderer_factory.created[0].buffer == "canonical"


def test_local_done_snapshot_can_authoritatively_clear_streamed_preview(
    monkeypatch,
) -> None:
    events = [
        TextDeltaEvent(text="stale"),
        DoneEvent(text="", text_snapshot=""),
    ]
    turn_runner = _ImageTurnRunner(events)
    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", _ImageTurnRunner)
    renderer_factory = _RendererFactory()
    deps = TurnStreamDependencies(
        renderer_factory=renderer_factory,
        stream_wrapper=lambda stream, _svc: stream,
        approval_handler=lambda *_args, **_kwargs: asyncio.sleep(0),
        cancel_clearer=lambda: None,
        image_attachment_builder=lambda _command: ("", []),
        output_console=object(),
        error_panel_factory=lambda message: message,
    )
    tool_ctx = ToolContext(
        caller_kind=CallerKind.CLI,
        channel_kind="cli",
        channel_id="cli:chat",
    )

    result = asyncio.run(
        stream_response_turnrunner(
            turn_runner,
            "agent:main:test",
            tool_ctx,
            "hello",
            deps=deps,
        )
    )

    assert result.text == ""
    assert renderer_factory.created[0].buffer == ""


def test_image_done_snapshot_replaces_streamed_preview(monkeypatch) -> None:
    events = [
        TextDeltaEvent(text="stale"),
        DoneEvent(text="canonical", text_snapshot="canonical"),
    ]
    turn_runner = _ImageTurnRunner(events)
    monkeypatch.setattr("opensquilla.engine.runtime.TurnRunner", _ImageTurnRunner)
    renderer_factory = _RendererFactory()
    deps = TurnStreamDependencies(
        renderer_factory=renderer_factory,
        stream_wrapper=lambda stream, _svc: stream,
        approval_handler=lambda *_args, **_kwargs: asyncio.sleep(0),
        cancel_clearer=lambda: None,
        image_attachment_builder=lambda _command: ("describe image", []),
        output_console=object(),
        error_panel_factory=lambda message: message,
    )
    tool_ctx = ToolContext(
        caller_kind=CallerKind.CLI,
        channel_kind="cli",
        channel_id="cli:chat",
    )

    result = asyncio.run(
        handle_image_command_turnrunner(
            turn_runner,
            "agent:main:image",
            tool_ctx,
            "/image /tmp/image.png",
            deps=deps,
        )
    )

    assert result.text == "canonical"
    assert renderer_factory.created[0].buffer == "canonical"
