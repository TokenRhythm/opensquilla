"""Hook protocol definitions for Agent + TurnRunner lifecycle.

These Protocols are structural — implementations do not need to inherit. A
concrete hook just needs to provide the named methods. Default no-op hooks live
in :mod:`opensquilla.engine.hooks.defaults`.

Value objects passed to hooks are intentionally narrow and frozen so a hook
cannot accidentally mutate caller state. Mutation paths must be explicit
(returning a value or going through a side-effect-bearing hook method).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from opensquilla.observability.trace import TraceContext
    from opensquilla.tool_boundary import ToolCall, ToolResult

# ---------------------------------------------------------------------------
# Turn lifecycle
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TurnHookContext:
    """Per-turn context handed to every TurnHook method.

    ``trace_context`` is optional because a turn may run without a trace context
    (e.g. unit tests). ``extra`` is a free-form bag of metadata; producers and
    consumers agree on field names out-of-band so the protocol stays loose.
    """

    session_key: str
    agent_id: str
    turn_id: str | None = None
    run_kind: str | None = None
    input_mode: str | None = None
    trace_context: TraceContext | None = None
    extra: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class TurnEvent:
    """A trace-bearing turn event emitted by the runtime.

    ``kind`` mirrors the ``_write_trace_event`` ``kind`` argument
    (``turn_start``, ``turn_end``, ``turn_error``, ``turn_cancelled``).
    """

    kind: str
    seq: int | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class TurnHookResult:
    """Result of a turn handed to ``TurnHook.after_turn``.

    Fields carry the data the transcript-persist path needs:
    final assistant text, structured turn segments, published artifacts, and
    any terminal error message captured during streaming.
    """

    final_text: str = ""
    turn_segments: list[dict] = field(default_factory=list)
    turn_artifacts: list[dict[str, Any]] = field(default_factory=list)
    error_message: str | None = None
    done_event: Any | None = None
    runtime_message: str = ""
    tool_context: Any | None = None
    input_provenance: dict[str, Any] | None = None
    no_memory_capture: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

@runtime_checkable
class TurnHook(Protocol):
    """Lifecycle observer for a single turn.

    Implementations should be cheap and side-effect-isolated; one slow hook
    must not stall the turn loop. The runtime invokes hooks in registration
    order and swallows exceptions raised by hooks (logging at WARN) so a buggy
    hook cannot break a turn.
    """

    name: str

    async def before_turn(self, ctx: TurnHookContext) -> None:
        """Fire once at the very start of a turn."""

    async def after_turn(self, ctx: TurnHookContext, result: TurnHookResult) -> None:
        """Fire after the turn finishes successfully."""

    async def on_error(self, ctx: TurnHookContext, exc: BaseException) -> None:
        """Fire when the turn terminates with an exception."""

    def on_event(self, ctx: TurnHookContext, event: TurnEvent) -> None:
        """Fire for each structured turn event (synchronous, observability only)."""

# ---------------------------------------------------------------------------
# Tool dispatch surround
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolHookCall:
    """The inputs presented to ``ToolHook.before_tool``.

    Carries the raw ``ToolCall`` plus the resolved :class:`ToolContext` so
    hooks can correlate to a session/agent without re-walking the call.
    """

    tool_call: ToolCall
    ctx: Any | None  # ToolContext | None — kept loose to avoid an import cycle

@dataclass(frozen=True)
class ToolHookResult:
    """The outcome presented to ``ToolHook.after_tool``.

    Either ``result`` is set (success) or ``exception`` is set (handler raised).
    Exactly one of the two is populated; both being ``None`` is reserved for
    short-circuited paths (policy denial after ``before_tool`` ran).
    """

    result: ToolResult | None = None
    exception: BaseException | None = None

@runtime_checkable
class ToolHook(Protocol):
    """Surround a single tool dispatch with before/after observability.

    ``before_tool`` runs after registry lookup but before the policy chain.
    ``after_tool`` runs just before result finalization.
    """

    name: str

    def before_tool(self, call: ToolHookCall) -> None: ...

    def after_tool(self, call: ToolHookCall, outcome: ToolHookResult) -> None: ...

# ---------------------------------------------------------------------------
# Compaction lifecycle. This hook is active only when supplied to TurnRunner.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CompactionState:
    """Snapshot of compaction inputs handed to ``CompactionHook.before_compact``.

    The runtime supplies the stable fields available at the pre-turn
    compaction call site. Hooks that need richer compaction telemetry should
    read it from ``extra`` when a specific producer documents that field.
    """

    session_key: str
    agent_id: str
    total_tokens: int = 0
    threshold_tokens: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

@runtime_checkable
class CompactionHook(Protocol):
    name: str

    async def before_compact(self, state: CompactionState) -> None: ...

    async def after_compact(self, state: CompactionState, outcome: Any) -> None: ...
