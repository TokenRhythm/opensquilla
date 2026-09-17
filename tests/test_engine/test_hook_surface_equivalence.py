"""Equivalence harness for the engine hook surface.

The harness compares two execution paths for trace emission:

* **Legacy** — call ``TurnRunner._write_trace_event`` directly, exactly as the
  inline call sites at ``runtime.py:1465``, ``:1518``, ``:2256``, ``:2365``,
  ``:2393`` currently do.
* **Hook** — drive the same emission through ``DefaultTraceEmitterHook`` via
  the ``TurnHook.on_event`` Protocol.

The two paths must produce identical ``TraceEvent`` records observed at the
sink. The harness also covers the no-op ``DefaultTranscriptHook``, which
reserves a future hook without moving the inline body.

Coverage gate: every hook method on every default hook is exercised at least
once, so the protocols are wired end-to-end before production code moves to
call site.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from opensquilla.engine.hooks import (
    CompactionState,
    DefaultTraceEmitterHook,
    DefaultTranscriptHook,
    NoopCompactionHook,
    NoopToolHook,
    NoopTurnHook,
    ToolHookCall,
    ToolHookResult,
    TurnEvent,
    TurnHookContext,
    TurnHookResult,
    build_default_turn_hooks,
)
from opensquilla.engine.hooks.types import CompactionHook, ToolHook, TurnHook
from opensquilla.engine.runtime import TurnRunner
from opensquilla.observability.trace import TraceContext, TraceEvent
from opensquilla.tool_boundary import ToolCall, ToolResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def trace_context() -> TraceContext:
    return TraceContext.new(
        trace_id="trace-equiv-test",
        session_key="agent:main:test",
        session_id="sess-1",
        turn_id="turn-1",
        agent_id="agent:main",
    )


@pytest.fixture
def captured_events(monkeypatch: pytest.MonkeyPatch) -> list[TraceEvent]:
    """Replace ``write_trace_event`` in BOTH callers' modules with a capture.

    Both ``opensquilla.engine.runtime`` and ``opensquilla.engine.hooks.defaults``
    import the symbol by name, so we patch both bindings to make sure the
    legacy and hook paths route through the same sink.
    """

    captured: list[TraceEvent] = []

    def _capture(event: TraceEvent) -> None:
        captured.append(event)

    monkeypatch.setattr(
        "opensquilla.engine.runtime.write_trace_event",
        _capture,
    )
    monkeypatch.setattr(
        "opensquilla.engine.hooks.defaults.write_trace_event",
        _capture,
    )
    return captured


# ---------------------------------------------------------------------------
# Trace-emission equivalence for hook protocol wiring
# ---------------------------------------------------------------------------


def _legacy_emit(
    kind: str,
    ctx: TraceContext,
    *,
    seq: int | None,
    attrs: dict[str, Any] | None,
    payload: dict[str, Any] | None,
) -> None:
    """Direct call to the static legacy emitter — matches the inline path."""

    TurnRunner._write_trace_event(
        kind,
        ctx,
        seq=seq,
        attrs=attrs,
        payload=payload,
    )


def _hook_emit(
    hook: DefaultTraceEmitterHook,
    hook_ctx: TurnHookContext,
    event: TurnEvent,
) -> None:
    """Drive the same emission through the hook seam."""

    hook.on_event(hook_ctx, event)


@pytest.mark.parametrize(
    ("kind", "seq", "attrs", "payload"),
    [
        ("turn_start", 1, {"input_mode": "user", "run_kind": "default"},
         {"message_chars": 12, "attachment_count": 0}),
        ("turn_error", 2, None,
         {"error_type": "ProviderResolutionError", "error_code": "no_provider", "error_chars": 19}),
        ("turn_end", 2, {"provider": "openrouter", "model": "gpt-x"},
         {"final_text_chars": 5, "segment_count": 0, "artifact_count": 0,
          "error": False, "tool_projection_applied": False,
          "tool_projection_calls": 0, "tool_projection_tokens_saved": 0}),
        ("turn_cancelled", 2, None, {"partial_text_chars": 3}),
    ],
)
def test_trace_emission_legacy_matches_hook(
    trace_context: TraceContext,
    captured_events: list[TraceEvent],
    kind: str,
    seq: int,
    attrs: dict[str, Any] | None,
    payload: dict[str, Any],
) -> None:
    """Legacy emit and hook emit must produce identical TraceEvent payloads."""

    _legacy_emit(kind, trace_context, seq=seq, attrs=attrs, payload=payload)
    legacy = captured_events.pop()

    hook = DefaultTraceEmitterHook()
    hook_ctx = TurnHookContext(
        session_key="agent:main:test",
        agent_id="agent:main",
        turn_id="turn-1",
        trace_context=trace_context,
    )
    _hook_emit(hook, hook_ctx, TurnEvent(kind=kind, seq=seq, attrs=attrs or {}, payload=payload))
    hooked = captured_events.pop()

    assert legacy.kind == hooked.kind
    assert legacy.context == hooked.context
    assert legacy.privacy == hooked.privacy
    assert legacy.seq == hooked.seq
    assert legacy.attrs == hooked.attrs
    assert legacy.payload == hooked.payload
    assert legacy.schema_version == hooked.schema_version


def test_trace_emission_hook_no_trace_context_is_noop(
    captured_events: list[TraceEvent],
) -> None:
    """Hook with ``trace_context=None`` must skip emission silently.

    Mirrors the inline guard at ``runtime.py:2364`` (``if trace_context is not None``).
    """

    hook = DefaultTraceEmitterHook()
    hook_ctx = TurnHookContext(
        session_key="agent:main:test",
        agent_id="agent:main",
        trace_context=None,
    )
    hook.on_event(hook_ctx, TurnEvent(kind="turn_end", seq=2))
    assert captured_events == []


# ---------------------------------------------------------------------------
# Defaults satisfy their Protocols
# ---------------------------------------------------------------------------


def test_default_turn_hooks_satisfy_protocol() -> None:
    for hook in build_default_turn_hooks():
        assert isinstance(hook, TurnHook)


def test_noop_hooks_satisfy_protocols() -> None:
    assert isinstance(NoopTurnHook(), TurnHook)
    assert isinstance(NoopToolHook(), ToolHook)
    assert isinstance(NoopCompactionHook(), CompactionHook)


def test_default_chain_order_is_stable() -> None:
    """Trace emitter must come first so events reach the sink before observers."""

    chain = build_default_turn_hooks()
    assert chain[0].name == "default_trace_emitter"


# ---------------------------------------------------------------------------
# Lifecycle no-ops complete without raising
# ---------------------------------------------------------------------------


def _ctx() -> TurnHookContext:
    return TurnHookContext(session_key="s", agent_id="a")


def _result() -> TurnHookResult:
    return TurnHookResult(final_text="ok")


def test_default_transcript_lifecycle_runs_clean() -> None:
    hook = DefaultTranscriptHook()
    asyncio.run(hook.before_turn(_ctx()))
    asyncio.run(hook.after_turn(_ctx(), _result()))
    asyncio.run(hook.on_error(_ctx(), RuntimeError("boom")))
    hook.on_event(_ctx(), TurnEvent(kind="turn_end"))


def test_noop_turn_hook_lifecycle_runs_clean() -> None:
    hook = NoopTurnHook()
    asyncio.run(hook.before_turn(_ctx()))
    asyncio.run(hook.after_turn(_ctx(), _result()))
    asyncio.run(hook.on_error(_ctx(), RuntimeError("x")))
    hook.on_event(_ctx(), TurnEvent(kind="turn_start", seq=1))


def test_noop_tool_hook_runs_clean() -> None:
    hook = NoopToolHook()
    call = ToolHookCall(
        tool_call=ToolCall(tool_use_id="u", tool_name="t", arguments={}),
        ctx=None,
    )
    hook.before_tool(call)
    hook.after_tool(
        call,
        ToolHookResult(
            result=ToolResult(
                tool_use_id="u",
                tool_name="t",
                content="",
                is_error=False,
            )
        ),
    )
    hook.after_tool(call, ToolHookResult(exception=RuntimeError("err")))


def test_noop_compaction_hook_runs_clean() -> None:
    hook = NoopCompactionHook()
    state = CompactionState(session_key="s", agent_id="a", total_tokens=10, threshold_tokens=20)
    asyncio.run(hook.before_compact(state))
    asyncio.run(hook.after_compact(state, outcome={"summary": ""}))


# ---------------------------------------------------------------------------
# Runtime feature flag: OPENSQUILLA_HOOKS=new vs legacy must match
# ---------------------------------------------------------------------------


def _make_runtime_with_default_hook() -> TurnRunner:
    return TurnRunner(provider_selector=None)


def _emit_through_runtime(
    runner: TurnRunner,
    trace_context: TraceContext,
    *,
    kind: str,
    seq: int | None,
    attrs: dict[str, Any] | None,
    payload: dict[str, Any] | None,
) -> None:
    runner._emit_turn_event(
        kind,
        trace_context,
        session_key="agent:main:test",
        agent_id="agent:main",
        turn_id="turn-1",
        run_kind="default",
        input_mode="user",
        seq=seq,
        attrs=attrs,
        payload=payload,
    )


@pytest.mark.parametrize(
    ("kind", "seq", "attrs", "payload"),
    [
        ("turn_start", 1, {"input_mode": "user", "run_kind": "default"},
         {"message_chars": 12, "attachment_count": 0}),
        ("turn_end", 2, {"provider": "p", "model": "m"},
         {"final_text_chars": 5}),
        ("turn_cancelled", 2, None, {"partial_text_chars": 3}),
        ("turn_error", 2, None,
         {"error_type": "RuntimeError", "error_chars": 7}),
    ],
)
def test_emit_turn_event_legacy_matches_new(
    monkeypatch: pytest.MonkeyPatch,
    trace_context: TraceContext,
    captured_events: list[TraceEvent],
    kind: str,
    seq: int,
    attrs: dict[str, Any] | None,
    payload: dict[str, Any],
) -> None:
    """OPENSQUILLA_HOOKS=legacy and =new must produce identical TraceEvents."""

    runner = _make_runtime_with_default_hook()

    monkeypatch.setenv("OPENSQUILLA_HOOKS", "legacy")
    _emit_through_runtime(
        runner, trace_context, kind=kind, seq=seq, attrs=attrs, payload=payload
    )
    legacy = captured_events.pop()

    monkeypatch.setenv("OPENSQUILLA_HOOKS", "new")
    _emit_through_runtime(
        runner, trace_context, kind=kind, seq=seq, attrs=attrs, payload=payload
    )
    new = captured_events.pop()

    assert legacy.kind == new.kind
    assert legacy.context == new.context
    assert legacy.privacy == new.privacy
    assert legacy.seq == new.seq
    assert legacy.attrs == new.attrs
    assert legacy.payload == new.payload
    assert legacy.schema_version == new.schema_version


def test_emit_turn_event_with_none_context_is_noop(
    captured_events: list[TraceEvent],
) -> None:
    runner = _make_runtime_with_default_hook()
    runner._emit_turn_event(
        "turn_start",
        None,
        session_key="s",
        agent_id="a",
        seq=1,
    )
    assert captured_events == []


def test_emit_turn_event_unknown_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
    trace_context: TraceContext,
    captured_events: list[TraceEvent],
) -> None:
    """An unrecognised env value must resolve to the active default (new)."""

    monkeypatch.setenv("OPENSQUILLA_HOOKS", "garbage")
    runner = _make_runtime_with_default_hook()
    _emit_through_runtime(
        runner, trace_context, kind="turn_end", seq=2, attrs={"x": 1}, payload={"y": 2}
    )
    event = captured_events.pop()
    assert event.kind == "turn_end"
    assert event.attrs == {"x": 1}
    assert event.payload == {"y": 2}


def test_runtime_hook_failure_does_not_break_emission(
    monkeypatch: pytest.MonkeyPatch,
    trace_context: TraceContext,
    captured_events: list[TraceEvent],
) -> None:
    """A misbehaving hook must be logged and skipped, never raised."""

    class _RaisingHook:
        name = "raising"

        async def before_turn(self, ctx):  # type: ignore[no-untyped-def]
            return None

        async def after_turn(self, ctx, result):  # type: ignore[no-untyped-def]
            return None

        async def on_error(self, ctx, exc):  # type: ignore[no-untyped-def]
            return None

        def on_event(self, ctx, event):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    monkeypatch.setenv("OPENSQUILLA_HOOKS", "new")
    runner = TurnRunner(
        provider_selector=None,
        turn_hooks=(_RaisingHook(), DefaultTraceEmitterHook()),
    )
    _emit_through_runtime(
        runner, trace_context, kind="turn_start", seq=1, attrs=None, payload=None
    )
    # The default emitter still wrote the event despite the prior hook raising.
    event = captured_events.pop()
    assert event.kind == "turn_start"
