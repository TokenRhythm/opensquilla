"""Default hook implementations that reproduce the original inline behavior.

These hooks let the runtime be wired with an explicit hook surface while
preserving the existing observability + persistence side-effects bit-for-bit.
The default hook implementations exist so the registry never sees ``None``;
hook path with these defaults registered.
"""

from __future__ import annotations

import structlog

from opensquilla.engine.hooks.types import (
    CompactionHook,
    CompactionState,
    ToolHook,
    ToolHookCall,
    ToolHookResult,
    TurnEvent,
    TurnHook,
    TurnHookContext,
    TurnHookResult,
)
from opensquilla.observability.trace import TraceEvent, write_trace_event

log = structlog.get_logger("opensquilla.engine.hooks")

# ---------------------------------------------------------------------------
# No-op hooks — used in tests + as the safe registration default
# ---------------------------------------------------------------------------

class NoopTurnHook:
    """TurnHook that does nothing. Useful as a base class or test double."""

    name = "noop_turn"

    async def before_turn(self, ctx: TurnHookContext) -> None:  # noqa: D401
        return None

    async def after_turn(self, ctx: TurnHookContext, result: TurnHookResult) -> None:
        return None

    async def on_error(self, ctx: TurnHookContext, exc: BaseException) -> None:
        return None

    def on_event(self, ctx: TurnHookContext, event: TurnEvent) -> None:
        return None

class NoopToolHook:
    """ToolHook that does nothing."""

    name = "noop_tool"

    def before_tool(self, call: ToolHookCall) -> None:
        return None

    def after_tool(self, call: ToolHookCall, outcome: ToolHookResult) -> None:
        return None

class NoopCompactionHook:
    """CompactionHook that does nothing."""

    name = "noop_compaction"

    async def before_compact(self, state: CompactionState) -> None:
        return None

    async def after_compact(self, state: CompactionState, outcome: object) -> None:
        return None

# ---------------------------------------------------------------------------
# Default trace emitter — reproduces TurnRunner._write_trace_event verbatim
# ---------------------------------------------------------------------------

class DefaultTraceEmitterHook:
    """``TurnHook.on_event`` implementation that emits a trace event.

    Reproduces ``TurnRunner._write_trace_event``: builds an ``operational``
    privacy ``TraceEvent`` and pushes it through ``write_trace_event``. Any
    exception raised by the trace sink is swallowed at DEBUG so observability
    never breaks a turn.
    """

    name = "default_trace_emitter"

    async def before_turn(self, ctx: TurnHookContext) -> None:
        return None

    async def after_turn(self, ctx: TurnHookContext, result: TurnHookResult) -> None:
        return None

    async def on_error(self, ctx: TurnHookContext, exc: BaseException) -> None:
        return None

    def on_event(self, ctx: TurnHookContext, event: TurnEvent) -> None:
        trace_context = ctx.trace_context
        if trace_context is None:
            return
        try:
            write_trace_event(
                TraceEvent(
                    kind=event.kind,
                    context=trace_context,
                    privacy="operational",
                    seq=event.seq,
                    attrs=dict(event.attrs),
                    payload=dict(event.payload),
                )
            )
        except Exception as exc:  # pragma: no cover — observability must not break turns
            log.debug("trace_event.write_failed", kind=event.kind, error=str(exc))

# ---------------------------------------------------------------------------
# Default transcript hook — placeholder; production persistence stays inline
# ---------------------------------------------------------------------------

class DefaultTranscriptHook:
    """``TurnHook.after_turn`` implementation reserved for transcript persist.

    the transcript persist branch stays inline because the code
    is deeply intertwined with done-event bookkeeping, error-event persistence,
    and capture services. Wiring is exposed as a hook so TurnRunner stage
    decomposition can move the body without changing the public contract. The
    default behavior is a no-op so inline persistence remains the source of truth.
    """

    name = "default_transcript"

    async def before_turn(self, ctx: TurnHookContext) -> None:
        return None

    async def after_turn(self, ctx: TurnHookContext, result: TurnHookResult) -> None:
        return None

    async def on_error(self, ctx: TurnHookContext, exc: BaseException) -> None:
        return None

    def on_event(self, ctx: TurnHookContext, event: TurnEvent) -> None:
        return None

# ---------------------------------------------------------------------------
# Default memory flush hook — placeholder; production flush stays inline
# ---------------------------------------------------------------------------

class DefaultMemoryFlushHook:
    """``TurnHook.after_turn`` implementation reserved for memory flush.

    The actual flush task lives in :mod:`opensquilla.engine.agent` and depends
    on per-Agent compaction state. This hook is currently reserved; inline
    memory-flush behavior remains the source of truth.
    """

    name = "default_memory_flush"

    async def before_turn(self, ctx: TurnHookContext) -> None:
        return None

    async def after_turn(self, ctx: TurnHookContext, result: TurnHookResult) -> None:
        return None

    async def on_error(self, ctx: TurnHookContext, exc: BaseException) -> None:
        return None

    def on_event(self, ctx: TurnHookContext, event: TurnEvent) -> None:
        return None

# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_default_turn_hooks() -> tuple[TurnHook, ...]:
    """Return the canonical default ``TurnHook`` chain.

    The order is observational only: trace emitter first so events reach the
    sink before later hooks observe them, then transcript and flush hooks
    which currently no-op (the inline code remains the source of truth in
    prior runtime path). A later runtime extraction can move the inline bodies into these hooks.
    """

    return (
        DefaultTraceEmitterHook(),
        DefaultTranscriptHook(),
        DefaultMemoryFlushHook(),
    )

__all__ = [
    "DefaultMemoryFlushHook",
    "DefaultTraceEmitterHook",
    "DefaultTranscriptHook",
    "NoopCompactionHook",
    "NoopToolHook",
    "NoopTurnHook",
    "build_default_turn_hooks",
]

# Static checks: defaults satisfy the protocols.
_check_turn: TurnHook = NoopTurnHook()
_check_trace_turn: TurnHook = DefaultTraceEmitterHook()
_check_transcript_turn: TurnHook = DefaultTranscriptHook()
_check_memory_turn: TurnHook = DefaultMemoryFlushHook()
_check_tool: ToolHook = NoopToolHook()
_check_compact: CompactionHook = NoopCompactionHook()
del (
    _check_turn,
    _check_trace_turn,
    _check_transcript_turn,
    _check_memory_turn,
    _check_tool,
    _check_compact,
)
