"""Stage object for the agent stream consumer loop.

Owns the per-turn slice between the attachment stage boundary
and the post-stream transcript-persist boundary: the
pre-loop accumulator declarations, the
``async for event in agent.run_turn(...)`` body, and the post-stream
sync-manager notify call.

Departs from the ``StageOutcome`` uniformity: this stage is an
async generator. Each ``AgentEvent`` the agent produces is forwarded
through the stage (after optional in-place rewrite) so downstream
consumers (WebUI, CLI, channels) keep their per-event streaming
contract. Terminal state for the post-stream surface flows
through the harness-owned ``_StreamState`` value object the stage
mutates in place.

Compaction refresh preservation: the ``_CompactionHandler`` fires the
supplied ``CompactionHook`` observers around the success-only three-step
``persist -> snapshot refresh -> system-prompt refresh`` sequence. The
``persist_compaction_result`` re-entrancy contract is preserved -- the
adapter forwards the call verbatim and the IN-TURN path remains the
only call site after the previous extraction landed.

No ``TurnHook`` is fired from inside the stream loop today.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, runtime_checkable

import structlog

from opensquilla.engine.hooks.types import CompactionState
from opensquilla.engine.route_plan import route_plan_snapshot
from opensquilla.observability.decision_log import build_vision_followup_gate_reason_code
from opensquilla.session.compaction_lifecycle import CompactionTimeoutError
from opensquilla.silent_reply import (
    SILENT_REPLY_NOT_ALLOWED_CODE,
    SILENT_REPLY_NOT_ALLOWED_MESSAGE,
    SILENT_REPLY_SENTINELS,
)

if TYPE_CHECKING:
    from opensquilla.contracts.turn_execution import TurnExecutionContext
    from opensquilla.engine.agent import Agent
    from opensquilla.engine.agent_injection import PendingInputProvider
    from opensquilla.engine.artifact_delivery import OmittedArtifactPublishResult
    from opensquilla.engine.hooks.types import CompactionHook
    from opensquilla.engine.types import (
        AgentEvent,
        ArtifactEvent,
        CompactionEvent,
        ControlTerminalEvent,
        DoneEvent,
        ErrorEvent,
        TextDeltaEvent,
        ToolResultEvent,
        ToolUseStartEvent,
        WarningEvent,
    )
    from opensquilla.silent_reply import SilentReplySegmentsNormalization

log = structlog.get_logger(__name__)

# Sentinel for "this event was consumed and should NOT be yielded
# downstream" (CompactionEvent + ErrorEvent take this path -- the
# loop continues instead of yielding).
_SUPPRESS: Final = object()
_CURRENT_SILENT_REPLY_TEXT_MARKER: Final = (
    "_opensquilla_current_silent_reply_text"
)
_HUMAN_SILENT_REPLY_PREFIX_MAX_CHARS: Final = 64


def _could_be_human_silent_reply_prefix(text: str) -> bool:
    """Hold only a short initial payload that can still be a silent sentinel.

    The character bound limits how much ordinary response text can be withheld.
    """

    if len(text) > _HUMAN_SILENT_REPLY_PREFIX_MAX_CHARS:
        return False
    candidate = text.strip()
    if not candidate:
        return True
    return any(sentinel.startswith(candidate) for sentinel in SILENT_REPLY_SENTINELS)


def _move_held_human_text_to_current_tail(
    state: _StreamState,
    held_text: str,
) -> bool:
    """Move withheld text after public non-text events in the durable timeline.

    Tool and activity events remain live while the short prefix is withheld. If
    the prefix later proves to be ordinary text, the client necessarily sees it
    after those events. Reposition the matching text carriers so a history reload
    preserves that same order. The rewrite is all-or-nothing and never changes
    non-text segments.
    """

    remaining = held_text
    rewritten_segments: list[dict[str, Any]] = []
    for raw_segment in state.turn_segments:
        segment = dict(raw_segment)
        if segment.get("type") != "text" or not remaining:
            rewritten_segments.append(segment)
            continue
        text = str(segment.get("text") or "")
        if remaining.startswith(text):
            remaining = remaining[len(text) :]
            continue
        if text.startswith(remaining):
            suffix = text[len(remaining) :]
            remaining = ""
            if suffix:
                segment["text"] = suffix
                rewritten_segments.append(segment)
            continue
        return False

    current_text = "".join(state.current_text_parts)
    if remaining:
        if not current_text.startswith(remaining):
            return False
        current_text = current_text[len(remaining) :]

    state.turn_segments[:] = rewritten_segments
    state.current_text_parts[:] = [held_text + current_text]
    return True

# ---------------------------------------------------------------------------
# Ports -- five narrow Protocols + one callable
# ---------------------------------------------------------------------------

@runtime_checkable
class AgentRunPort(Protocol):
    """Wrap ``agent.run_turn(turn_input, extra_messages=..., **kwargs)``.

    Returns the async iterator the agent produces; the stage consumes
    it via ``async for``. The port handles the ``_accepts_keyword_arg``
    introspection for ``semantic_message`` so the stage body has no
    ``inspect``-based branching. The agent is supplied per-call so a
    single stage instance can serve every turn.
    """

    def run_turn(
        self,
        agent: Agent,
        *,
        turn_input: str,
        extra_messages: list[Any] | None,
        semantic_message: str | None,
        pending_input_provider: PendingInputProvider | None = None,
    ) -> AsyncIterator[AgentEvent]: ...

@runtime_checkable
class CompactionPersistPort(Protocol):
    """Persist the IN-TURN ``CompactionEvent`` result + notify cache monitor.

    Wraps ``SessionManager.persist_compaction_result(session_key, summary,
    kept_entries)`` followed by a completed lifecycle notification. The
    stage handles persistence failures by emitting a failed lifecycle
    notification and preserving pre-refresh runtime state.

    Compaction refresh: This is the only remaining call site for
    ``persist_compaction_result``. The compaction contract requires the
    call to remain re-entrant -- the Agent has already mutated its
    in-memory message list before the runner sees ``CompactionEvent``,
    so the DB transcript may have more entries than ``kept_entries``.
    The stage forwards the call verbatim.
    """

    async def persist_and_notify(
        self,
        *,
        session_key: str,
        summary: str,
        kept_entries: list[Any],
        summary_payload: dict[str, Any] | None = None,
        summary_format: str = "text",
        coverage_status: str = "unknown",
        missing_obligations: list[str] | None = None,
        critical_carry_forward: list[str] | None = None,
        compaction_id: str | None = None,
        compaction_deadline_at_monotonic: float | None = None,
        compaction_timeout_seconds: float | None = None,
        removed_count: int = 0,
        source_entries: tuple[Any, ...] | None = None,
        source_preimage: tuple[tuple[Any, ...], ...] | None = None,
        source_boundary_message_id: str | None = None,
        source_boundary_entry_id: int | None = None,
    ) -> bool | None: ...

@runtime_checkable
class MemorySnapshotRefreshPort(Protocol):
    """Refresh ``_memory_snapshots[(agent_id, session_key)]`` after compaction.

    Wraps the resolve-workspace + load-memory-md + load-daily-notes +
    dict-write sequence. The adapter respects ``private_memory_allowed``:
    when false, the dict is not written.

    Compaction refresh: the frozen system-prompt snapshot lives in this dict;
    the design note "the in-turn path must keep emitting
    through CompactionEvent so TurnRunner can update the frozen
    system-prompt snapshot ... before the next turn" is satisfied by
    refreshing here, BEFORE the agent's next iteration runs.
    """

    def refresh_snapshot(
        self,
        *,
        agent_id: str,
        session_key: str,
        private_memory_allowed: bool,
    ) -> None: ...

@runtime_checkable
class SystemPromptRefreshPort(Protocol):
    """Rebuild + apply the cacheable system-prompt base after compaction.

    Wraps the ``_assemble_prompt(...)`` call + the tuple-vs-str extract
    + ``agent.refresh_system_prompt(refreshed_prompt)``.

    Compaction refresh: the post-compaction prompt rebuild MUST extract the
    cacheable base (not the full ``(base, dynamic_suffix)`` tuple) --
    feeding the tuple directly into ``agent.refresh_system_prompt``
    would smuggle volatile bytes into ``ChatConfig.system`` and raise
    ``ValidationError`` on the next turn. The adapter handles the
    extract; the stage cannot bypass it.
    """

    def refresh_system_prompt(
        self,
        *,
        agent: Agent,
        agent_id: str,
        tool_defs: list[Any],
        session_key: str,
        bootstrap_context_mode: str | None,
    ) -> None: ...

@runtime_checkable
class MemorySyncNotifyPort(Protocol):
    """Notify ``sync_manager.notify_message(byte_count)`` post-stream.

    Fires exactly once after the ``async for`` exits cleanly. The
    adapter handles the ``sync_manager is None`` guard so the stage
    body has no conditional.
    """

    def notify_message_bytes(
        self,
        sync_manager: Any | None,
        runtime_message: str,
    ) -> None: ...

# Callable signature for runtime warning transformation. Implemented as a
# plain callable rather than a Protocol because it is a single-method
# contract and the recording-fake discipline applies identically.
WarningTransformer = Callable[["WarningEvent"], "WarningEvent"]

# ---------------------------------------------------------------------------
# Stream state -- four owned + five pass-by-reference accumulators
# ---------------------------------------------------------------------------

@dataclass
class _StreamState:
    """Mutable accumulators shared across the stage's event handlers.

    Owned by the harness (created in ``_run_turn``, passed in as
    ``inp.state``), mutated by handler classes inside the stage. The
    stage does NOT mutate ``TurnContext`` directly -- it writes through
    this state object the harness owns.

    Stream-local fields move INTO this object from the inline body. Four
    accumulator fields STAY on the harness-level ``_run_turn`` body and are
    PASSED IN as mutable references -- the outer ``CancelledError``
    handler reads them after the stream loop. Wrapping each mutation
    into a yielded state-delta envelope would balloon LOC and harm
    readability for zero behavioral benefit.
    """

    # Moved INTO stage scope (declared inline before the stage extraction).
    current_text_parts: list[str] = field(default_factory=list)
    # Presentation for ``current_text_parts``. Persisted text segments carry
    # this additive field so history reloads can distinguish user-visible work
    # narration from the terminal answer. ``answer`` is the compatibility
    # default for legacy producers and rows that predate the field.
    current_text_presentation: Literal["intermediate", "answer"] = "answer"
    error_message: str | None = None
    pending_error_event: ErrorEvent | None = None
    done_event: DoneEvent | None = None
    # PASSED IN by the harness -- references, not copies.
    final_text_parts: list[str] = field(default_factory=list)
    reasoning_parts: list[str] = field(default_factory=list)
    turn_segments: list[dict] = field(default_factory=list)
    turn_artifacts: list[dict[str, Any]] = field(default_factory=list)
    artifact_delivery_failures: list[str] = field(default_factory=list)
    artifact_delivery_failures_by_target: dict[str, str] = field(default_factory=dict)
    completed_meta_skill_without_text: str | None = None
    reasoning: list[str] = field(default_factory=list)
    terminal_generation_reset: bool = False
    control_terminal_event: ControlTerminalEvent | None = None


@dataclass(frozen=True)
class _SilentReplyStateProjection:
    """Copy-on-write silent-reply projection of the live segment timeline."""

    raw_text: str
    canonical_text: str
    normalization: SilentReplySegmentsNormalization

# ---------------------------------------------------------------------------
# Stage I/O dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StreamConsumerStageInput:
    """Inputs the StreamConsumerStage needs at its boundary.

    Pulled from earlier-stage ``TurnContext`` accumulators by the
    harness. The ``state`` field is intentionally NOT frozen -- the
    stage mutates it in place.
    """

    # From AgentBootstrapStage
    agent: Agent
    agent_id: str
    sync_manager: Any  # MemorySyncManager | None
    private_memory_allowed: bool

    # From PromptAssemblerStage
    turn: Any  # post-pipeline pipeline.TurnContext
    tool_defs: list[Any]

    # From AttachmentStage
    turn_input: str
    extra_messages: list[Any] | None

    # From InputStage
    semantic_input: str
    effective_runtime_message: str

    # From _run_turn locals
    session_key: str
    run_kind: str
    heartbeat_ack_max_chars: int
    bootstrap_context_mode: str | None

    # From runner (read by handlers without needing a runner reference)
    router_cfg: Any | None
    session_manager_present: bool

    # Mutable shared accumulators (passed by reference)
    state: _StreamState

    # Effective tool context for runtime-generated delivery backstops.
    tool_context: Any | None = None
    # Original input provenance for runtime-generated disclosures.
    input_provenance: dict[str, Any] | None = None
    # In-process pending submitted-line provider for mid-turn injection.
    pending_input_provider: PendingInputProvider | None = None
    # Live delivery-ready authorization resolved on the event loop before the
    # blocking omitted-artifact publish enters its worker thread.
    attached_plan_run_ready: bool | None = None
    # Frozen durable prefix used by in-turn compaction persistence. The storage
    # adapter compares it atomically and preserves later append-only queue rows.
    compaction_source_entries: tuple[Any, ...] | None = None
    compaction_source_preimage: tuple[tuple[Any, ...], ...] | None = None
    compaction_source_boundary_message_id: str | None = None
    compaction_source_boundary_entry_id: int | None = None
    # Original ingress mode.  Internal Goal continuations and heartbeats use
    # ``system_event``; their text is held until the terminal snapshot can be
    # canonicalized so silent-reply protocol markers never flash on a client.
    input_mode: str = "user"
    execution_context: TurnExecutionContext | None = None


def _supports_realtime_generation(inp: StreamConsumerStageInput) -> bool:
    """Return whether this delivery surface can retract speculative output."""

    context = inp.execution_context
    if context is None:
        return True
    surface = context.surface
    return bool(
        surface.supports_streaming
        and surface.supports_edit
        and surface.supports_generation_reset
    )


def _supports_generation_reset(inp: StreamConsumerStageInput) -> bool:
    """Return whether a direct stream consumer declared reset support."""

    context = inp.execution_context
    if context is None:
        # Preserve the existing in-process API for callers that have not yet
        # adopted explicit surface capability negotiation.
        return True
    return bool(context.surface.supports_generation_reset)


def _control_reason_for_error_code(code: Any) -> Any | None:
    """Map only typed control/error codes; provider failures stay recoverable."""

    from opensquilla.engine.types import ControlTerminalReason

    normalized = str(code or "").strip().lower().replace("-", "_")
    return {
        "agent_runtime_timeout": ControlTerminalReason.HARD_DEADLINE,
        "hard_deadline": ControlTerminalReason.HARD_DEADLINE,
        "hard_deadline_exceeded": ControlTerminalReason.HARD_DEADLINE,
        "shutdown": ControlTerminalReason.SHUTDOWN,
        "gateway_shutdown": ControlTerminalReason.SHUTDOWN,
        "cancel": ControlTerminalReason.CANCEL,
        "cancelled": ControlTerminalReason.CANCEL,
        "canceled": ControlTerminalReason.CANCEL,
        "platform_validation": ControlTerminalReason.PLATFORM_VALIDATION,
        "platform_safety": ControlTerminalReason.PLATFORM_SAFETY,
        "safety_control": ControlTerminalReason.PLATFORM_SAFETY,
    }.get(normalized)

# ---------------------------------------------------------------------------
# Per-event handler classes
# ---------------------------------------------------------------------------

def _has_tool_boundary(state: _StreamState) -> bool:
    return any(
        segment.get("type") in {"tool_use", "tool_result"}
        for segment in state.turn_segments
    )


def _reset_generation_stream_state(state: _StreamState) -> None:
    """Discard answer state for a replaced generation, retaining completed tools."""

    completed_tool_ids = {
        str(segment.get("tool_use_id"))
        for segment in state.turn_segments
        if isinstance(segment, dict)
        and segment.get("type") == "tool_result"
        and segment.get("tool_use_id")
    }
    state.current_text_parts[:] = []
    state.final_text_parts[:] = []
    state.reasoning[:] = []
    state.reasoning_parts[:] = []
    state.error_message = None
    state.pending_error_event = None
    state.done_event = None
    state.turn_segments[:] = [
        segment
        for segment in state.turn_segments
        if not isinstance(segment, dict)
        or (
            segment.get("type") != "text"
            and (
                segment.get("type") != "tool_use"
                or str(segment.get("tool_use_id")) in completed_tool_ids
            )
        )
    ]


def _normalize_cumulative_text_delta(text: str, state: _StreamState) -> str:
    """Convert a post-tool cumulative text snapshot back into a delta."""

    if not text or not _has_tool_boundary(state):
        return text

    accumulated_text = "".join(state.final_text_parts)
    if not accumulated_text:
        return text
    if text == accumulated_text:
        return ""
    if text.startswith(accumulated_text):
        return text[len(accumulated_text) :]
    return text


def _normalize_text_presentation(value: object) -> Literal["intermediate", "answer"]:
    """Normalize optional or legacy presentation values to the answer default."""

    return "intermediate" if value == "intermediate" else "answer"


def _flush_current_text_segment(state: _StreamState) -> None:
    """Persist the current text run without losing its presentation metadata."""

    if not state.current_text_parts:
        return
    state.turn_segments.append(
        {
            "type": "text",
            "text": "".join(state.current_text_parts),
            "presentation": state.current_text_presentation,
        }
    )
    state.current_text_parts[:] = []


def _append_current_text(
    state: _StreamState,
    text: str,
    *,
    presentation: object,
) -> None:
    """Append text while keeping unlike presentation runs as separate segments."""

    if not text:
        return
    normalized = _normalize_text_presentation(presentation)
    if state.current_text_parts and state.current_text_presentation != normalized:
        _flush_current_text_segment(state)
    state.current_text_presentation = normalized
    state.current_text_parts.append(text)


class _TextDeltaHandler:
    """Accumulate streamed text deltas into the final-text and current-text buffers."""

    def handle(
        self,
        event: TextDeltaEvent,
        state: _StreamState,
    ) -> TextDeltaEvent:
        canonical_delta = _normalize_cumulative_text_delta(event.text, state)
        if canonical_delta:
            state.final_text_parts.append(canonical_delta)
            _append_current_text(
                state,
                canonical_delta,
                presentation=event.presentation,
            )
        return replace(event, text=canonical_delta)

class _ToolUseStartHandler:
    """Flush the canonical current text segment and append a tool-use segment."""

    def handle(
        self,
        event: ToolUseStartEvent,
        state: _StreamState,
    ) -> ToolUseStartEvent:
        _flush_current_text_segment(state)
        state.turn_segments.append(
            {
                "type": "tool_use",
                "tool_use_id": event.tool_use_id,
                "name": event.tool_name,
                "input": "",
            }
        )
        return event

def _clear_artifact_delivery_failure(state: _StreamState, target_key: str) -> None:
    failure_summary = state.artifact_delivery_failures_by_target.pop(target_key, None)
    if failure_summary is None:
        return
    try:
        state.artifact_delivery_failures.remove(failure_summary)
    except ValueError:
        pass


def _user_input_payload(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, dict) or value.get("kind") != "user_input":
        return None
    return dict(value)


def _pending_user_input_request(value: Any) -> dict[str, Any] | None:
    payload = _user_input_payload(value)
    if (
        payload is None
        or payload.get("status") != "input_required"
        or payload.get("paused") is not True
    ):
        return None
    return payload


def _is_terminal_user_input_outcome(value: Any) -> bool:
    payload = _user_input_payload(value)
    return bool(
        payload is not None
        and payload.get("status") in {"answered", "cancelled", "expired"}
        and payload.get("paused") is False
    )


class _ToolResultHandler:
    """Capture artifact-delivery failures and append the tool_result segment."""

    def handle(
        self,
        event: ToolResultEvent,
        state: _StreamState,
        *,
        tool_context: Any | None = None,
    ) -> ToolResultEvent:
        # Late imports keep the module import-cycle-free.
        from opensquilla.engine.runtime import (
            _artifact_delivery_failure_summary,
            _artifact_delivery_target_keys,
            _persisted_tool_result_segment,
            _persisted_tool_use_input,
        )

        failure_summary = _artifact_delivery_failure_summary(event)
        target_keys = _artifact_delivery_target_keys(
            event,
            tool_context=tool_context,
            include_publish_name=not event.is_error,
        )
        if failure_summary is not None:
            for target_key in target_keys:
                _clear_artifact_delivery_failure(state, target_key)
                state.artifact_delivery_failures_by_target[target_key] = failure_summary
            state.artifact_delivery_failures.append(failure_summary)
        elif not event.is_error:
            for target_key in target_keys:
                _clear_artifact_delivery_failure(state, target_key)
        if _is_completed_meta_invoke(event):
            state.completed_meta_skill_without_text = _meta_invoke_skill_name(event)
        if event.arguments is not None:
            for segment in reversed(state.turn_segments):
                if (
                    segment.get("type") == "tool_use"
                    and segment.get("tool_use_id") == event.tool_use_id
                ):
                    segment["name"] = event.tool_name
                    segment["input"] = _persisted_tool_use_input(
                        event.tool_name,
                        event.tool_use_id,
                        event.arguments,
                    )
                    break
        result_segment = _persisted_tool_result_segment(event)
        for index in range(len(state.turn_segments) - 1, -1, -1):
            segment = state.turn_segments[index]
            if (
                segment.get("type") == "tool_result"
                and segment.get("tool_use_id") == event.tool_use_id
            ):
                initial_user_input_request = segment.get("user_input_request")
                if initial_user_input_request is None:
                    initial_user_input_request = _pending_user_input_request(
                        segment.get("result")
                    )
                if (
                    initial_user_input_request is not None
                    and _is_terminal_user_input_outcome(result_segment.get("result"))
                ):
                    result_segment["user_input_request"] = initial_user_input_request
                state.turn_segments[index] = result_segment
                break
        else:
            state.turn_segments.append(result_segment)
        return event

class _ArtifactHandler:
    """Append an artifact payload to the per-turn artifact list."""

    def handle(
        self,
        event: ArtifactEvent,
        state: _StreamState,
    ) -> ArtifactEvent:
        from opensquilla.artifacts import artifact_payload

        state.turn_artifacts.append(artifact_payload(event))
        return event

class _ErrorHandler:
    """Rewrite timeout envelopes, drop unpaired tool_use, capture pending error.

    Returns ``_SUPPRESS`` -- the loop continues without
    yielding (the pending error event is yielded post-stream by the
    finalizer stage after transcript persist).
    """

    def handle(
        self,
        event: ErrorEvent,
        state: _StreamState,
    ) -> object:
        from opensquilla.engine.runtime import (
            _LLM_TIMEOUT_ENVELOPE,
            _drop_unpaired_tool_use_segments,
        )
        from opensquilla.engine.types import ErrorEvent as _ErrorEvent

        if event.code == "timeout":
            event = _ErrorEvent(
                message=_LLM_TIMEOUT_ENVELOPE["user_message"],
                code=_LLM_TIMEOUT_ENVELOPE["error_class"],
            )
        # A provider error is terminal for the current attempt.  Never persist a
        # tool_use that did not reach a matching tool_result: adapters use
        # provider-specific error codes (for example ``incomplete_stream`` and
        # ``incomplete_tool_call``), so keying this invariant to a small code
        # allowlist can leave an orphaned executable segment in the transcript.
        # Completed tool rounds remain intact because the helper preserves every
        # tool_use whose id has a paired tool_result.
        state.turn_segments[:] = _drop_unpaired_tool_use_segments(
            state.turn_segments
        )
        state.error_message = event.message or "Unknown error"
        state.pending_error_event = event
        return _SUPPRESS

class _WarningHandler:
    """Forward warnings to the runner's runtime-warning transformer."""

    def __init__(self, transformer: WarningTransformer) -> None:
        self._transformer = transformer

    def handle(self, event: WarningEvent, state: _StreamState) -> WarningEvent:
        if event.code in {
            "workspace_diff_recovery",
            "plan_run_reconciliation",
        }:
            self._discard_superseded_current_text(state)
        return self._transformer(event)

    @staticmethod
    def _discard_superseded_current_text(state: _StreamState) -> None:
        current_text = "".join(state.current_text_parts)
        if not current_text:
            return
        accumulated_text = "".join(state.final_text_parts)
        if accumulated_text.endswith(current_text):
            state.final_text_parts[:] = [accumulated_text[: -len(current_text)]]
        state.current_text_parts[:] = []
        state.current_text_presentation = "answer"

@dataclass
class _DonePrePublish:
    """Carrier between the done handler's on-loop pre/post-publish phases.

    Holds the transformed ``DoneEvent``, the pre-publish ``extra_yields`` and
    the accumulated final text so the blocking artifact publish can run in a
    worker thread BETWEEN two on-loop phases -- without ever carrying a
    ``_StreamState`` mutation into that thread.
    """

    event: DoneEvent
    extra_yields: list[AgentEvent]
    accumulated_text: str


class _DoneHandler:
    """Apply routing-tier metadata, savings, normalize text, emit notices.

    Largest single handler. Returns ``(transformed_done_event,
    extra_yields)`` where ``extra_yields`` is the (possibly empty) list
    of events the outer stage must yield BEFORE the DoneEvent itself --
    the artifact-delivery-failure notice TextDelta and/or the
    hallucination Warning yield, in the original order.

    Split into three phases so the live stage can keep every
    ``_StreamState`` mutation on the event loop while offloading ONLY the
    blocking artifact publish to a worker thread: ``pre_publish`` (on loop)
    -> ``run_publish`` (offloadable, self-contained) -> ``post_publish`` (on
    loop, applied only after the publish completes). ``handle`` composes all
    three synchronously for callers that do not need the offload.
    """

    def handle(
        self,
        event: DoneEvent,
        inp: StreamConsumerStageInput,
        state: _StreamState,
    ) -> tuple[DoneEvent, list[AgentEvent]]:
        """Run all three phases synchronously on the calling thread.

        Convenience composition for callers that do not need to keep the
        blocking publish off a live event loop (focused unit tests). The
        live stage runs the phases separately -- see ``StreamConsumerStage``.
        """

        pre = self.pre_publish(event, inp, state)
        publish_result = self.run_publish(inp, pre.accumulated_text)
        return self.post_publish(pre, publish_result, inp, state)

    def pre_publish(
        self,
        event: DoneEvent,
        inp: StreamConsumerStageInput,
        state: _StreamState,
    ) -> _DonePrePublish:
        """Metadata, savings, text reconciliation and pre-publish notices.

        Every ``_StreamState`` mutation here runs on the caller's thread. The
        live stage calls this on the event loop so the shared, by-reference
        accumulators the CancelledError finalizer reads are never mutated
        concurrently with cancellation.
        """

        from opensquilla.engine.runtime import (
            _compute_comprehensive_turn_savings,
            _compute_route_input_savings_usd,
            _turn_used_ensemble,
        )
        from opensquilla.engine.silent_reply import normalize_silent_reply
        from opensquilla.engine.types import (
            done_text_snapshot,
        )

        turn = inp.turn
        metadata = turn.metadata

        snapshot_present, terminal_text = done_text_snapshot(event)
        accumulated_text = "".join(state.final_text_parts)
        buffer_system_event_text = (
            inp.input_mode == "system_event"
            and inp.run_kind in {"goal", "heartbeat"}
        )
        # Buffered internal turns may be produced by legacy adapters that leave
        # Done.text empty. Their accumulated deltas are still the canonical
        # terminal candidate and must be normalized before anything is shown.
        normalization_source = (
            terminal_text
            if snapshot_present
            else accumulated_text if buffer_system_event_text else terminal_text
        )
        silent_reply = normalize_silent_reply(
            normalization_source,
            run_kind=inp.run_kind,
            input_mode=inp.input_mode,
            heartbeat_ack_max_chars=inp.heartbeat_ack_max_chars,
        )

        state_projection: _SilentReplyStateProjection | None = None
        candidate = _silent_reply_state_projection(
            state,
            run_kind=inp.run_kind,
            input_mode=inp.input_mode,
            heartbeat_ack_max_chars=inp.heartbeat_ack_max_chars,
        )
        if (
            candidate.raw_text == accumulated_text
            and candidate.normalization.changed
            and (
                candidate.canonical_text == silent_reply.text
                or (
                    normalization_source == accumulated_text
                    and not silent_reply.changed
                )
            )
        ):
            # A producer may already publish the canonical terminal snapshot
            # even though its streamed timeline still contains an outer marker.
            # Exact canonical agreement lets us preserve text/tool chronology;
            # any other terminal mismatch remains the retry/recovery path.
            state_projection = candidate

        if state_projection is not None:
            segment_normalization = state_projection.normalization
            done_fallback_text = state_projection.canonical_text
            normalization_changed = True
            normalization_sentinel = silent_reply.sentinel or next(
                iter(segment_normalization.sentinels),
                None,
            )
            normalization_suppressed = segment_normalization.suppressed
            normalization_delivery = segment_normalization.delivery
            normalization_suppression_reason = (
                segment_normalization.suppression_reason
            )
        else:
            done_fallback_text = silent_reply.text
            normalization_changed = silent_reply.changed
            normalization_sentinel = silent_reply.sentinel
            normalization_suppressed = silent_reply.suppressed
            normalization_delivery = silent_reply.delivery
            normalization_suppression_reason = silent_reply.suppression_reason

        if normalization_sentinel is not None:
            metric = (
                "suppressed_reply_total"
                if normalization_suppressed
                else "mixed_sentinel_output_total"
            )
            # Labels are fixed protocol enums only. Never log model-authored
            # text from this path.
            log.info(
                metric,
                metric=metric,
                value=1,
                run_kind=inp.run_kind,
                sentinel=normalization_sentinel,
            )
        canonical_snapshot_present = snapshot_present or buffer_system_event_text
        routed_tier = metadata.get("routed_tier")
        routing_source = metadata.get("routing_source", "none")
        routing_confidence = float(metadata.get("routing_confidence") or 0.0)
        routing_applied = metadata.get("routing_applied")
        if routing_applied is None:
            routing_applied = True
        rollout_phase = str(metadata.get("rollout_phase") or "full")
        baseline_model = metadata.get("baseline_model", "")
        routed_model = metadata.get("routed_model", "") or event.model
        if _turn_used_ensemble(event, metadata):
            # Route-level savings share the single-model assumption the
            # comprehensive score rejects for ensemble turns: the input-price
            # delta would be applied to fan-out-multiplied token totals. Zero
            # both so channels and the decision log never attribute savings
            # to a turn that fanned out to several member models.
            savings_pct = 0.0
            savings_usd = 0.0
        else:
            savings_pct = float(metadata.get("savings_pct") or 0.0)
            _max_p = float(metadata.get("savings_max_price_per_m") or 0.0)
            _rte_p = float(metadata.get("savings_routed_price_per_m") or 0.0)
            savings_usd = _compute_route_input_savings_usd(
                _max_p,
                _rte_p,
                event.input_tokens,
            )
        router_cfg = inp.router_cfg
        squilla_router_tiers = getattr(router_cfg, "tiers", {})
        estimated_output_savings_pct = getattr(
            router_cfg,
            "estimated_output_savings_pct",
            0.03,
        )
        comprehensive = _compute_comprehensive_turn_savings(
            event,
            metadata,
            squilla_router_tiers,
            routed_model,
            estimated_output_savings_pct=estimated_output_savings_pct,
        )
        provider_cache_hit = (event.cached_tokens or 0) > 0
        opensquilla_cache_hit = metadata.get("cache_mode") == "hit"
        decision_id = metadata.get("router_decision_id")
        event = replace(
            event,
            text=done_fallback_text,
            text_snapshot=(
                done_fallback_text if canonical_snapshot_present else None
            ),
            delivery=normalization_delivery,
            suppression_reason=normalization_suppression_reason,
            decision_id=str(decision_id) if decision_id else None,
            routed_tier=routed_tier,
            routing_source=routing_source or "none",
            routing_confidence=routing_confidence,
            routing_applied=bool(routing_applied),
            rollout_phase=rollout_phase,
            baseline_model=baseline_model,
            routed_model=routed_model,
            savings_pct=savings_pct,
            savings_usd=savings_usd,
            cache_hit_active=provider_cache_hit or opensquilla_cache_hit,
            total_savings_pct=comprehensive.pct,
            total_savings_usd=comprehensive.usd,
            image_route_reason=metadata.get("image_route_reason"),
            vision_followup_gate_decision=metadata.get(
                "router_vision_followup_gate_decision"
            ),
            vision_followup_gate_confidence=metadata.get(
                "router_vision_followup_gate_confidence"
            ),
            vision_followup_gate_reason=build_vision_followup_gate_reason_code(
                decision=metadata.get("router_vision_followup_gate_decision"),
                source=metadata.get("router_vision_followup_gate_source"),
                reason=metadata.get("router_vision_followup_gate_reason"),
                fallback=metadata.get("router_vision_followup_fallback"),
            ),
            vision_followup_gate_source=metadata.get(
                "router_vision_followup_gate_source"
            ),
            vision_followup_gate_model=metadata.get("router_vision_followup_gate_model"),
            vision_followup_needs_image=metadata.get("router_vision_followup_needs_image"),
            vision_followup_fallback=metadata.get("router_vision_followup_fallback"),
            route_plan=route_plan_snapshot(turn),
            execution_legs=[
                dict(leg)
                for leg in metadata.get("execution_legs", [])
                if isinstance(leg, dict)
            ],
        )

        if normalization_changed and state_projection is not None:
            _apply_silent_reply_normalization_to_state(state, state_projection)
            canonical_text = done_fallback_text
            done_suffix_event = None
        else:
            canonical_text, done_suffix_event = _reconcile_done_text_snapshot(
                done_fallback_text,
                state,
                accumulated_text=accumulated_text,
                authoritative=canonical_snapshot_present,
            )
        accumulated_text = "".join(state.final_text_parts)
        event = replace(event, text=canonical_text)
        state.done_event = event
        extra_yields: list[AgentEvent] = []
        if done_suffix_event is not None:
            extra_yields.append(done_suffix_event)
        if not accumulated_text.strip() and state.completed_meta_skill_without_text:
            event, fallback_event = _append_done_notice_delta(
                event,
                state,
                _meta_completed_without_text_notice(
                    state.completed_meta_skill_without_text
                ),
                accumulated_text=accumulated_text,
            )
            extra_yields.append(fallback_event)
            accumulated_text = "".join(state.final_text_parts)

        return _DonePrePublish(
            event=event,
            extra_yields=extra_yields,
            accumulated_text=accumulated_text,
        )

    def run_publish(
        self,
        inp: StreamConsumerStageInput,
        accumulated_text: str,
    ) -> OmittedArtifactPublishResult:
        """Publish deliverables the model wrote but forgot to publish.

        The blocking phase: re-reads workspace files, validates, hashes and
        writes them through the ``ArtifactStore``. Self-contained -- it reads
        ``inp.tool_context`` and returns a result, and never touches
        ``_StreamState``. This is the ONLY phase the live stage offloads to a
        worker thread, so a cancelled turn cannot leave a store write racing
        the finalizer's reads of the shared stream accumulators.
        """

        from opensquilla.engine.artifact_delivery import (
            auto_publish_omitted_workspace_artifacts,
        )

        return auto_publish_omitted_workspace_artifacts(
            inp.tool_context,
            final_text=accumulated_text,
            attached_plan_run_ready=inp.attached_plan_run_ready,
        )

    def record_publish_result(
        self,
        publish_result: OmittedArtifactPublishResult,
        state: _StreamState,
    ) -> list[AgentEvent]:
        """Record a completed publish's side effects into shared turn state.

        The artifact-recording half of ``post_publish``: append published
        artifacts to ``state.turn_artifacts`` (the by-reference accumulator
        every finalizer -- including the CancelledError finalizer -- persists
        the transcript from), clear resolved delivery failures, and record
        new failure summaries. Runs on the event loop. Returns the
        ``ArtifactEvent``s for the outer stage to yield; the cancel path
        discards them because the stream is already unwinding, but the
        recording itself must still happen -- the store write and the
        ``ctx.published_artifacts`` append already took effect in the worker,
        so skipping it would orphan an artifact that exists on disk (and
        counts against the disk budget) without any transcript record.
        """

        from opensquilla.engine.types import ArtifactEvent as _ArtifactEvent

        artifact_events: list[AgentEvent] = []
        for artifact in publish_result.artifacts:
            artifact_event = _ArtifactEvent(**artifact)
            state.turn_artifacts.append(artifact)
            artifact_events.append(artifact_event)
        for target_key in publish_result.resolved_target_keys:
            _clear_artifact_delivery_failure(state, target_key)
        state.artifact_delivery_failures.extend(
            publish_result.failure_summaries
        )
        return artifact_events

    def post_publish(
        self,
        pre: _DonePrePublish,
        publish_result: OmittedArtifactPublishResult,
        inp: StreamConsumerStageInput,
        state: _StreamState,
    ) -> tuple[DoneEvent, list[AgentEvent]]:
        """Apply the publish result and post-publish notices to state.

        Runs on the caller's thread (the event loop in the live stage) and
        only AFTER ``run_publish`` has fully completed, so a cancelled turn
        never observes a half-applied result: on cancellation the stage
        records a completed publish through ``record_publish_result`` and
        re-raises before the notice phases run.
        """

        from opensquilla.engine.runtime import (
            _artifact_delivery_failure_notice,
            _claims_image_without_tool_use,
            _should_add_artifact_delivery_failure_notice,
        )
        from opensquilla.engine.types import (
            WarningEvent as _WarningEvent,
        )

        event = pre.event
        extra_yields = pre.extra_yields
        accumulated_text = pre.accumulated_text
        turn = inp.turn

        extra_yields.extend(self.record_publish_result(publish_result, state))

        if _should_add_artifact_delivery_failure_notice(
            failure_summaries=state.artifact_delivery_failures,
            turn_artifacts=state.turn_artifacts,
            final_text=accumulated_text,
        ):
            event, notice_event = _append_done_notice_delta(
                event,
                state,
                _artifact_delivery_failure_notice(partial=bool(state.turn_artifacts)),
                accumulated_text=accumulated_text,
            )
            extra_yields.append(notice_event)

        accumulated_text = "".join(state.final_text_parts)
        disclosure = _subagent_partial_failure_disclosure(inp.input_provenance)
        if disclosure is not None:
            event, notice_event = _append_done_notice_delta(
                event,
                state,
                disclosure,
                accumulated_text=accumulated_text,
            )
            extra_yields.append(notice_event)

        accumulated_text = "".join(state.final_text_parts)
        from opensquilla.engine.turn_runner.runtime_notices import (
            unconfirmed_action_notice,
        )

        confirmation_notice = unconfirmed_action_notice(
            accumulated_text,
            state.turn_segments,
        )
        if confirmation_notice is not None:
            event, notice_event = _append_done_notice_delta(
                event,
                state,
                confirmation_notice,
                accumulated_text=accumulated_text,
            )
            extra_yields.append(notice_event)

        accumulated_text = "".join(state.final_text_parts)
        if _claims_image_without_tool_use(
            accumulated_text, turn.tool_defs, state.turn_segments
        ):
            extra_yields.append(
                _WarningEvent(
                    code="image_generate_claimed_without_call",
                    message=(
                        "The assistant described a generated image but did not "
                        "call an image-generation tool. No image was produced."
                    ),
                )
            )
        return event, extra_yields


def _reconcile_done_text_snapshot(
    done_text: str,
    state: _StreamState,
    *,
    accumulated_text: str,
    authoritative: bool = False,
) -> tuple[str, AgentEvent | None]:
    """Reconcile streamed text with the Agent's authoritative final snapshot.

    Exact matches preserve the streamed segment layout. A strict extension promotes
    only the new suffix into the delta stream, preserving tool/text ordering (for
    example the artifact-ready notice after a terminal ``publish_artifact`` call).
    A conflicting authoritative snapshot supersedes stale retry/recovery text:
    remove text segments, retain tool lifecycle segments, and stage one canonical
    text segment after them. An empty legacy terminal value falls back to deltas;
    an explicitly present empty snapshot clears superseded text.
    """

    if not authoritative and not done_text:
        return accumulated_text, None

    if done_text == accumulated_text:
        return done_text, None

    if accumulated_text and done_text.startswith(accumulated_text):
        from opensquilla.engine.types import TextDeltaEvent as _TextDeltaEvent

        suffix = done_text[len(accumulated_text) :]
        state.final_text_parts.append(suffix)
        _append_current_text(state, suffix, presentation="answer")
        return done_text, _TextDeltaEvent(text=suffix, presentation="answer")

    # The terminal aggregate is a complete Agent-owned snapshot. A mismatch means
    # streamed text was intentionally superseded (retry, recovery, or a producer
    # that emitted a cumulative final value). Keep non-text segments so tool
    # execution history is never erased, but collapse text to one canonical value.
    if state.current_text_parts and state.current_text_presentation == "intermediate":
        _flush_current_text_segment(state)
    state.final_text_parts[:] = [done_text] if done_text else []
    state.turn_segments[:] = [
        segment
        for segment in state.turn_segments
        if not (
            isinstance(segment, dict)
            and segment.get("type") == "text"
            and _normalize_text_presentation(segment.get("presentation")) == "answer"
        )
    ]
    state.current_text_parts[:] = (
        [done_text] if done_text and state.turn_segments else []
    )
    state.current_text_presentation = "answer"
    return done_text, None


def _silent_reply_state_projection(
    state: _StreamState,
    *,
    run_kind: str,
    input_mode: str | None,
    heartbeat_ack_max_chars: int,
) -> _SilentReplyStateProjection:
    """Normalize the ordered timeline, including the unflushed text carrier."""

    from opensquilla.engine.silent_reply import sanitize_silent_reply_segments

    current_text = "".join(state.current_text_parts)
    combined_segments = [dict(segment) for segment in state.turn_segments]
    if current_text:
        combined_segments.append(
            {
                "type": "text",
                "text": current_text,
                "presentation": state.current_text_presentation,
                _CURRENT_SILENT_REPLY_TEXT_MARKER: True,
            }
        )
    normalized = sanitize_silent_reply_segments(
        combined_segments,
        run_kind=run_kind,
        input_mode=input_mode,
        heartbeat_ack_max_chars=heartbeat_ack_max_chars,
    )
    raw_text = "".join(
        str(segment.get("text") or "")
        for segment in combined_segments
        if segment.get("type") == "text"
    )
    normalized_text = "".join(
        str(segment.get("text") or "")
        for segment in normalized.segments
        if segment.get("type") == "text"
    )
    return _SilentReplyStateProjection(
        raw_text=raw_text,
        canonical_text=normalized_text,
        normalization=normalized,
    )


def _apply_silent_reply_normalization_to_state(
    state: _StreamState,
    projection: _SilentReplyStateProjection,
) -> None:
    """Commit a validated deletion-only projection without crossing tools."""

    turn_segments: list[dict[str, Any]] = []
    normalized_current = ""
    normalized_current_presentation: Literal["intermediate", "answer"] = "answer"
    for raw_segment in projection.normalization.segments:
        segment = dict(raw_segment)
        if segment.pop(_CURRENT_SILENT_REPLY_TEXT_MARKER, False):
            normalized_current += str(segment.get("text") or "")
            normalized_current_presentation = _normalize_text_presentation(
                segment.get("presentation")
            )
            continue
        turn_segments.append(segment)
    state.turn_segments[:] = turn_segments
    state.current_text_parts[:] = [normalized_current] if normalized_current else []
    state.current_text_presentation = normalized_current_presentation
    state.final_text_parts[:] = (
        [projection.canonical_text] if projection.canonical_text else []
    )


def _unreleased_text_suffix(
    canonical_text: str,
    released_text: str | None,
) -> str:
    """Return only a safe append after an Error already released held text."""

    if released_text is None:
        return canonical_text
    if canonical_text.startswith(released_text):
        return canonical_text[len(released_text) :]
    # A conflicting terminal snapshot is authoritative, but a delta cannot
    # retract the already-released prefix. Let Done.text_snapshot reconcile it.
    return ""


def _append_done_notice_delta(
    event: DoneEvent,
    state: _StreamState,
    notice: str,
    *,
    accumulated_text: str,
) -> tuple[DoneEvent, AgentEvent]:
    from opensquilla.engine.types import TextDeltaEvent as _TextDeltaEvent

    separator = "\n\n" if accumulated_text.strip() else ""
    notice_delta = separator + notice
    state.final_text_parts.append(notice_delta)
    _append_current_text(state, notice_delta, presentation="answer")
    final_text = "".join(state.final_text_parts)
    event = replace(
        event,
        text=final_text,
        text_snapshot=final_text,
        delivery="visible",
        suppression_reason=None,
    )
    state.done_event = event
    return event, _TextDeltaEvent(text=notice_delta, presentation="answer")


def _is_completed_meta_invoke(event: ToolResultEvent) -> bool:
    if event.tool_name != "meta_invoke" or event.is_error:
        return False
    result = event.result.strip().lower()
    return "meta-skill" in result and "completed" in result


def _meta_invoke_skill_name(event: ToolResultEvent) -> str:
    arguments = event.arguments if isinstance(event.arguments, dict) else {}
    candidate = arguments.get("name")
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    return "meta skill"


def _meta_completed_without_text_notice(skill_name: str) -> str:
    return (
        f"Meta skill `{skill_name}` 已完成，但这次流程没有生成可展示的最终回答。"
        "请查看上方步骤结果和产物；如果需要，可以补充更明确的输出要求后重新运行。"
    )


def _subagent_partial_failure_disclosure(
    input_provenance: dict[str, Any] | None,
) -> str | None:
    if not isinstance(input_provenance, dict):
        return None
    required = input_provenance.get("runtime_partial_failure_disclosure_required") is True
    outcome = input_provenance.get("subagent_group_outcome")
    if not isinstance(outcome, dict):
        if required:
            raise RuntimeError(
                "subagent partial failure disclosure required but outcome metadata is missing"
            )
        return None
    non_success = _int_value(outcome.get("non_success"))
    if non_success <= 0:
        return None
    total = _int_value(outcome.get("total"))
    succeeded = _int_value(outcome.get("succeeded"))
    failures = _format_subagent_failure_children(outcome.get("failed_children"))
    suffix = f"; failures: {failures}" if failures else ""
    return f"Subagents: {succeeded}/{total} succeeded{suffix}."


def _format_subagent_failure_children(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for child in value[:3]:
        if not isinstance(child, dict):
            continue
        child_id = _first_text(
            child.get("child_session_key"),
            child.get("task_id"),
            child.get("agent_id"),
            default="unknown child",
        )
        status = _first_text(child.get("status"), default="non-success")
        reason = _first_text(child.get("terminal_reason"), default="")
        detail = f"{child_id} {status}"
        if reason:
            detail = f"{detail} ({reason})"
        error = _format_subagent_failure_error(child)
        if error:
            detail = f"{detail}: {error}"
        parts.append(detail)
    remaining = len(value) - len(parts)
    if remaining > 0:
        parts.append(f"{remaining} more")
    return "; ".join(parts)


def _format_subagent_failure_error(child: dict[str, Any]) -> str:
    error_class = _first_text(child.get("error_class"), default="")
    error_message = _first_text(child.get("error_message"), default="")
    if error_class in {"current_turn_context_exhausted", "provider_request_too_large"}:
        return "provider_request_too_large"
    if error_message and len(error_message) > 160:
        error_message = f"{error_message[:157]}..."
    if error_class and error_message:
        return f"{error_class}: {error_message}"
    return error_message or error_class


def _first_text(*values: Any, default: str) -> str:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return default


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0

class _CompactionHandler:
    """Persist compaction and refresh memory snapshot + system prompt.

    The success order persist -> snapshot -> prompt is load-bearing per the
    compaction contract (the Agent has already mutated its
    in-memory history; the DB transcript must be brought into sync
    first, then the next turn's snapshot dependencies refreshed). The
    persist call is wrapped so durable failures emit failed lifecycle feedback
    and stop before snapshot/prompt refresh can manufacture a false compacted
    runtime view.

    Does NOT yield -- ``CompactionEvent`` is internal-only.
    """

    def __init__(
        self,
        *,
        persist: CompactionPersistPort,
        memory_snapshot: MemorySnapshotRefreshPort,
        system_prompt: SystemPromptRefreshPort,
        compaction_hooks: tuple[CompactionHook, ...] = (),
    ) -> None:
        self._persist = persist
        self._memory_snapshot = memory_snapshot
        self._system_prompt = system_prompt
        self._compaction_hooks = compaction_hooks

    async def handle(
        self,
        event: CompactionEvent,
        inp: StreamConsumerStageInput,
    ) -> None:
        state = CompactionState(
            session_key=inp.session_key,
            agent_id=inp.agent_id,
            extra={
                "phase": "in_turn_stream",
                "summary": event.summary,
                "kept_entries": event.kept_entries,
            },
        )
        await self._fire_before_compact(state)
        if inp.session_manager_present:
            try:
                persist_kwargs: dict[str, Any] = {
                    "session_key": inp.session_key,
                    "summary": event.summary,
                    "kept_entries": event.kept_entries,
                    "summary_payload": event.summary_payload,
                    "summary_format": event.summary_format,
                    "coverage_status": event.coverage_status,
                    "missing_obligations": event.missing_obligations,
                    "critical_carry_forward": event.critical_carry_forward,
                    "compaction_id": event.compaction_id,
                    "removed_count": event.removed_count,
                    "source_entries": inp.compaction_source_entries,
                    "source_preimage": inp.compaction_source_preimage,
                    "source_boundary_message_id": (
                        inp.compaction_source_boundary_message_id
                    ),
                    "source_boundary_entry_id": (
                        inp.compaction_source_boundary_entry_id
                    ),
                }
                if event.compaction_deadline_at_monotonic is not None:
                    persist_kwargs["compaction_deadline_at_monotonic"] = (
                        event.compaction_deadline_at_monotonic
                    )
                if event.compaction_timeout_seconds is not None:
                    persist_kwargs["compaction_timeout_seconds"] = (
                        event.compaction_timeout_seconds
                    )
                installed = await self._persist.persist_and_notify(**persist_kwargs)
                if installed is False:
                    await self._fire_after_compact(
                        state,
                        {
                            "status": "skipped",
                            "reason": "stale_preimage",
                        },
                    )
                    return
            except asyncio.CancelledError:
                from opensquilla.engine.cache_break_monitor import notify_compaction
                from opensquilla.session.compaction_lifecycle import (
                    COMPACTION_TRIGGERED_EVENT,
                    compaction_effect_payload,
                    compaction_lifecycle_payload,
                    new_compaction_id,
                )

                compaction_id = event.compaction_id or new_compaction_id()
                notify_compaction(
                    inp.session_key,
                    source="automatic",
                    phase="agent_inline_overflow",
                    status="cancelled",
                    reason="cancelled",
                    **compaction_effect_payload(status="cancelled"),
                    **compaction_lifecycle_payload(
                        compaction_id,
                        COMPACTION_TRIGGERED_EVENT,
                    ),
                )
                await self._fire_after_compact(
                    state,
                    {"status": "cancelled", "reason": "cancelled"},
                )
                raise
            except CompactionTimeoutError as exc:
                from opensquilla.engine.cache_break_monitor import notify_compaction
                from opensquilla.session.compaction_lifecycle import (
                    COMPACTION_TRIGGERED_EVENT,
                    compaction_effect_payload,
                    compaction_lifecycle_payload,
                    new_compaction_id,
                )

                compaction_id = event.compaction_id or new_compaction_id()
                notify_compaction(
                    inp.session_key,
                    source="automatic",
                    phase=exc.phase,
                    status="timed_out",
                    reason="compaction_deadline_exceeded",
                    **compaction_effect_payload(status="timed_out"),
                    **compaction_lifecycle_payload(
                        compaction_id,
                        COMPACTION_TRIGGERED_EVENT,
                    ),
                )
                await self._fire_after_compact(
                    state,
                    {
                        "status": "timed_out",
                        "reason": "compaction_deadline_exceeded",
                    },
                )
                return
            except Exception as exc:  # noqa: BLE001 - preserve turn recoverability
                log.warning("compaction_persist_failed", error=str(exc))
                from opensquilla.engine.cache_break_monitor import notify_compaction
                from opensquilla.session.compaction_lifecycle import (
                    COMPACTION_TRIGGERED_EVENT,
                    compaction_effect_payload,
                    compaction_lifecycle_payload,
                    new_compaction_id,
                )

                compaction_id = event.compaction_id or new_compaction_id()

                notify_compaction(
                    inp.session_key,
                    source="automatic",
                    phase="agent_inline_overflow",
                    status="failed",
                    reason="persist_failed",
                    message=str(exc),
                    kept_count=len(event.kept_entries),
                    summary_len=len(event.summary or ""),
                    **compaction_effect_payload(status="failed", reason="persist_failed"),
                    **compaction_lifecycle_payload(
                        compaction_id,
                        COMPACTION_TRIGGERED_EVENT,
                    ),
                )
                await self._fire_after_compact(
                    state,
                    {
                        "status": "failed",
                        "reason": "persist_failed",
                        "message": str(exc),
                    },
                )
                return

        self._memory_snapshot.refresh_snapshot(
            agent_id=inp.agent_id,
            session_key=inp.session_key,
            private_memory_allowed=inp.private_memory_allowed,
        )
        self._system_prompt.refresh_system_prompt(
            agent=inp.agent,
            agent_id=inp.agent_id,
            tool_defs=inp.tool_defs,
            session_key=inp.session_key,
            bootstrap_context_mode=inp.bootstrap_context_mode,
        )
        await self._fire_after_compact(
            state,
            {
                "status": "completed",
                "summary": event.summary,
                "kept_entries": event.kept_entries,
            },
        )

    async def _fire_before_compact(self, state: CompactionState) -> None:
        for hook in self._compaction_hooks:
            try:
                await hook.before_compact(state)
            except Exception:  # noqa: BLE001 - hook isolation contract
                pass

    async def _fire_after_compact(
        self,
        state: CompactionState,
        outcome: dict[str, Any],
    ) -> None:
        for hook in self._compaction_hooks:
            try:
                await hook.after_compact(state, outcome)
            except Exception:  # noqa: BLE001 - hook isolation contract
                pass

# ---------------------------------------------------------------------------
# Outer stage class
# ---------------------------------------------------------------------------

class StreamConsumerStage:
    """Consume the agent stream and yield events; persist mid-stream side effects.

    Stable boundary: runs ONCE per turn, after AttachmentStage and
    before TurnFinalizerStage. The five ports execute as follows:

    1. ``AgentRunPort.run_turn`` -- one async iterator started; the
       stage owns the lifetime of the consumer loop.
    2. Per ``CompactionEvent``, conditionally:
       a. ``CompactionPersistPort.persist_and_notify``.
       b. ``MemorySnapshotRefreshPort.refresh_snapshot``.
       c. ``SystemPromptRefreshPort.refresh_system_prompt``.
    3. Post-stream: ``MemorySyncNotifyPort.notify_message_bytes`` -- one
       call after the loop exits cleanly.

    Async-generator signature -- the FIRST TurnRunner stage that does NOT
    return ``StageOutcome``. Each yielded value is forwarded by the
    harness to its own caller. Terminal state for flows
    through ``inp.state`` (the harness-owned ``_StreamState``).

    Exception model: the stream loop propagates ``CancelledError``
    unchanged (the outer ``_run_turn`` CancelledError handler owns the
    partial-persist behavior). Other exceptions propagate to the outer
    terminal handler. The ``_CompactionHandler`` wraps its persist call
    in a log-and-continue try/except so transient persistence failures
    do not abort the surrounding turn.

    ``CompactionHook`` observers supplied by the runner fire around
    in-turn ``CompactionEvent`` handling only; hook exceptions are
    isolated and do not change the stream outcome.
    """

    name = "stream_consumer_stage"

    def __init__(
        self,
        *,
        agent_run: AgentRunPort,
        compaction_persist: CompactionPersistPort,
        memory_snapshot_refresh: MemorySnapshotRefreshPort,
        system_prompt_refresh: SystemPromptRefreshPort,
        memory_sync_notify: MemorySyncNotifyPort,
        warning_transformer: WarningTransformer,
        compaction_hooks: tuple[CompactionHook, ...] = (),
    ) -> None:
        self._agent_run = agent_run
        self._memory_sync_notify = memory_sync_notify

        self._text_delta_handler = _TextDeltaHandler()
        self._tool_use_start_handler = _ToolUseStartHandler()
        self._tool_result_handler = _ToolResultHandler()
        self._artifact_handler = _ArtifactHandler()
        self._error_handler = _ErrorHandler()
        self._warning_handler = _WarningHandler(warning_transformer)
        self._done_handler = _DoneHandler()
        self._compaction_handler = _CompactionHandler(
            persist=compaction_persist,
            memory_snapshot=memory_snapshot_refresh,
            system_prompt=system_prompt_refresh,
            compaction_hooks=compaction_hooks,
        )

    async def _adopt_generated_artifact(
        self,
        event: ArtifactEvent,
        tool_context: Any | None,
    ) -> None:
        """Best-effort canonicalization before an artifact becomes public.

        The artifact has already been recorded in the shared turn state before
        this hook runs, so cancellation or adoption failure can never orphan a
        successfully published deliverable from transcript persistence.
        """

        adopter = getattr(tool_context, "generated_artifact_adopter", None)
        if not callable(adopter):
            return
        try:
            await adopter(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - artifact delivery must survive adoption
            log.warning(
                "generated_artifact_adoption_failed",
                artifact_id=str(getattr(event, "id", "") or ""),
                mime=str(getattr(event, "mime", "") or ""),
                error_type=type(exc).__name__,
            )

    async def run(
        self,
        inp: StreamConsumerStageInput,
    ) -> AsyncIterator[AgentEvent]:
        # Late imports keep the module import-cycle-free.
        from opensquilla.engine.types import (
            AnswerGenerationResetEvent,
            ArtifactEvent,
            CompactionEvent,
            ControlTerminalEvent,
            DoneEvent,
            EnsembleProgressEvent,
            ErrorEvent,
            TextDeltaEvent,
            ThinkingEvent,
            ToolResultEvent,
            ToolUseDeltaEvent,
            ToolUseEndEvent,
            ToolUseStartEvent,
            WarningEvent,
            done_text_snapshot,
        )
        from opensquilla.provider.types import (
            EnsembleProgressEvent as ProviderEnsembleProgressEvent,
        )

        state = inp.state
        buffer_system_event_text = (
            inp.input_mode == "system_event"
            and inp.run_kind in {"goal", "heartbeat"}
        )
        gate_human_silent_reply_prefix = inp.input_mode != "system_event"
        human_silent_reply_prefix_open = gate_human_silent_reply_prefix
        held_human_silent_reply_text = ""
        human_prefix_crossed_public_boundary = False
        buffered_text_observed = False
        released_system_event_text: str | None = None
        released_system_event_source_text: str | None = None
        released_suppressed_source_text: str | None = None
        realtime_generation = _supports_realtime_generation(inp)
        supports_generation_reset = _supports_generation_reset(inp)
        deferred_generation_events: list[AgentEvent] = []

        def _is_deferred_generation_event(candidate: object) -> bool:
            return isinstance(
                candidate,
                (
                    TextDeltaEvent,
                    ThinkingEvent,
                    ToolUseStartEvent,
                    ToolUseDeltaEvent,
                    ToolUseEndEvent,
                    ToolResultEvent,
                    ArtifactEvent,
                ),
            )

        def _drop_speculative_text() -> None:
            deferred_generation_events[:] = [
                candidate
                for candidate in deferred_generation_events
                if not isinstance(candidate, (TextDeltaEvent, ThinkingEvent))
            ]

        async for event in self._agent_run.run_turn(
            inp.agent,
            turn_input=inp.turn_input,
            extra_messages=inp.extra_messages,
            semantic_message=inp.semantic_input,
            pending_input_provider=inp.pending_input_provider,
        ):
            transformed: AgentEvent | object
            extra_yields: list[AgentEvent] = []
            if isinstance(event, AnswerGenerationResetEvent):
                _reset_generation_stream_state(state)
                if not realtime_generation:
                    _drop_speculative_text()
                terminal_snapshot = ""
                if event.terminal:
                    terminal_snapshot = str(
                        event.terminal_text_snapshot
                        or event.authoritative_text_snapshot
                        or "The model could not complete this answer."
                    )
                    if terminal_snapshot:
                        state.current_text_parts.append(terminal_snapshot)
                        state.final_text_parts.append(terminal_snapshot)
                    state.terminal_generation_reset = True
                    # A terminal replacement is the only public failure
                    # payload, but finalization still needs the same typed
                    # failure state as an ordinary ErrorEvent so it can write
                    # turn_errors and a failed durable outcome.
                    self._error_handler.handle(
                        ErrorEvent(
                            message=(
                                event.terminal_error_message
                                or "The model provider request failed."
                            ),
                            code=(
                                event.terminal_error_code
                                or "ensemble_fixed_error"
                            ),
                            failure_kind=(
                                event.terminal_failure_kind or "unknown"
                            ),
                            generation_epoch=event.new_generation_epoch,
                        ),
                        state,
                    )
                if supports_generation_reset:
                    # Keep the typed failure taxonomy on the trusted
                    # in-process event until TaskRuntime has classified the
                    # durable outcome. Gateway serializers scrub these fields
                    # at the public transport boundary.
                    transformed = event
                elif event.terminal:
                    transformed = ErrorEvent(
                        message=terminal_snapshot,
                        code="ensemble_fixed_error",
                        generation_epoch=event.new_generation_epoch,
                    )
                else:
                    transformed = _SUPPRESS
            elif isinstance(event, ControlTerminalEvent):
                state.control_terminal_event = event
                if not realtime_generation:
                    _drop_speculative_text()
                transformed = event
            elif isinstance(event, ThinkingEvent):
                if event.text:
                    state.reasoning.append(event.text)
                    state.reasoning_parts.append(event.text)
                transformed = event
            elif isinstance(event, TextDeltaEvent):
                transformed = self._text_delta_handler.handle(event, state)
                if buffer_system_event_text:
                    if event.text and not buffered_text_observed:
                        log.info(
                            "system_event_text_buffered_total",
                            metric="system_event_text_buffered_total",
                            value=1,
                            run_kind=inp.run_kind,
                        )
                        buffered_text_observed = True
                    transformed = _SUPPRESS
                elif human_silent_reply_prefix_open:
                    held_human_silent_reply_text += transformed.text
                    if _could_be_human_silent_reply_prefix(
                        held_human_silent_reply_text
                    ):
                        transformed = _SUPPRESS
                    else:
                        if human_prefix_crossed_public_boundary:
                            _move_held_human_text_to_current_tail(
                                state,
                                held_human_silent_reply_text,
                            )
                        transformed = replace(
                            transformed,
                            text=held_human_silent_reply_text,
                        )
                        held_human_silent_reply_text = ""
                        human_silent_reply_prefix_open = False
                        human_prefix_crossed_public_boundary = False
            elif isinstance(event, ToolUseStartEvent):
                transformed = self._tool_use_start_handler.handle(event, state)
            elif isinstance(event, ToolUseDeltaEvent):
                transformed = event
            elif isinstance(event, ToolUseEndEvent):
                transformed = event
            elif isinstance(event, ToolResultEvent):
                transformed = self._tool_result_handler.handle(
                    event,
                    state,
                    tool_context=inp.tool_context,
                )
            elif isinstance(event, ArtifactEvent):
                transformed = self._artifact_handler.handle(event, state)
                await self._adopt_generated_artifact(event, inp.tool_context)
            elif isinstance(event, ErrorEvent):
                transformed = self._error_handler.handle(event, state)
                if (
                    human_silent_reply_prefix_open
                    and held_human_silent_reply_text
                ):
                    # A provider failure can terminate while a sentinel is only
                    # partially streamed. Do not persist or reveal that control
                    # prefix as a user-visible partial answer.
                    _reconcile_done_text_snapshot(
                        "",
                        state,
                        accumulated_text="".join(state.final_text_parts),
                        authoritative=True,
                    )
                    held_human_silent_reply_text = ""
                    human_silent_reply_prefix_open = False
                    human_prefix_crossed_public_boundary = False
                if buffer_system_event_text:
                    from opensquilla.engine.silent_reply import (
                        is_silent_reply_prefix,
                        normalize_silent_reply,
                    )

                    buffered_text = "".join(state.final_text_parts)
                    normalized = normalize_silent_reply(
                        buffered_text,
                        run_kind=inp.run_kind,
                        input_mode=inp.input_mode,
                        heartbeat_ack_max_chars=inp.heartbeat_ack_max_chars,
                    )
                    state_projection: _SilentReplyStateProjection | None = None
                    candidate = _silent_reply_state_projection(
                        state,
                        run_kind=inp.run_kind,
                        input_mode=inp.input_mode,
                        heartbeat_ack_max_chars=inp.heartbeat_ack_max_chars,
                    )
                    if (
                        candidate.raw_text == buffered_text
                        and candidate.normalization.changed
                        and (
                            not normalized.changed
                            or candidate.canonical_text == normalized.text
                        )
                    ):
                        state_projection = candidate
                    if is_silent_reply_prefix(buffered_text):
                        canonical_partial = ""
                        state_projection = None
                    elif state_projection is not None:
                        canonical_partial = state_projection.canonical_text
                    else:
                        canonical_partial = normalized.text

                    if state_projection is not None:
                        _apply_silent_reply_normalization_to_state(
                            state,
                            state_projection,
                        )
                    else:
                        _reconcile_done_text_snapshot(
                            canonical_partial,
                            state,
                            accumulated_text=buffered_text,
                            authoritative=True,
                        )
                    # An Error has no authoritative Done snapshot. Settle the
                    # held text once so useful partial output remains visible,
                    # while an exact or distinctive incomplete marker never
                    # escapes merely because the provider failed mid-token.
                    release_delta = _unreleased_text_suffix(
                        canonical_partial,
                        released_system_event_text,
                    )
                    if release_delta:
                        extra_yields.append(TextDeltaEvent(text=release_delta))
                    if (
                        released_system_event_source_text is not None
                        and released_system_event_text is not None
                        and buffered_text.startswith(released_system_event_text)
                    ):
                        released_system_event_source_text += buffered_text[
                            len(released_system_event_text) :
                        ]
                    else:
                        released_system_event_source_text = buffered_text
                    released_system_event_text = canonical_partial
                    released_suppressed_source_text = (
                        buffered_text
                        if (
                            normalized.suppressed
                            or (
                                state_projection is not None
                                and state_projection.normalization.suppressed
                            )
                        )
                        else None
                    )
            elif isinstance(event, WarningEvent):
                transformed = self._warning_handler.handle(event, state)
            elif isinstance(event, DoneEvent):
                if state.terminal_generation_reset:
                    # Agent may emit one accounting-only DoneEvent after a
                    # terminal generation replacement when failed physical
                    # attempts carried usage. Preserve that receipt for the
                    # finalizer, but the terminal reset remains the only public
                    # outcome and its friendly snapshot remains authoritative.
                    terminal_snapshot = "".join(state.final_text_parts)
                    state.done_event = replace(
                        event,
                        text=terminal_snapshot,
                        text_snapshot=terminal_snapshot,
                    )
                    transformed = _SUPPRESS
                    continue
                if (
                    buffer_system_event_text
                    and released_system_event_source_text is not None
                    and released_system_event_text is not None
                ):
                    snapshot_present, terminal_text = done_text_snapshot(event)
                    accumulated_text = "".join(state.final_text_parts)
                    if (
                        snapshot_present
                        and not terminal_text
                        and released_suppressed_source_text is not None
                    ):
                        # A canonical-empty usage Done can follow an Error that
                        # carried a complete silent token. Re-present that raw
                        # token only to the shared normalizer so the terminal
                        # delivery remains explicitly suppressed.
                        event = replace(
                            event,
                            text=released_suppressed_source_text,
                            text_snapshot=released_suppressed_source_text,
                        )
                        terminal_text = released_suppressed_source_text
                    if (
                        snapshot_present
                        and bool(released_system_event_text)
                        and terminal_text == released_system_event_source_text
                        and accumulated_text.startswith(released_system_event_text)
                    ):
                        # Some providers emit a usage-bearing Done after Error
                        # with the same pre-normalization aggregate. Map only
                        # that exact raw payload back to the canonical text we
                        # already released; a genuinely conflicting retry
                        # snapshot remains authoritative.
                        canonical_terminal = released_system_event_text
                        event = replace(
                            event,
                            text=canonical_terminal,
                            text_snapshot=canonical_terminal,
                        )
                # The done handler may auto-publish forgotten workspace
                # deliverables, which re-reads and fully validates them
                # (PPTX inflation plus deck parse). Keep every _StreamState
                # mutation on the event loop (pre/post-publish) and offload
                # ONLY that blocking publish to a worker thread. The worker
                # cannot be cancelled, so on cancellation we wait for it to
                # finish before unwinding: otherwise a steered follow-up turn
                # could start finalizing (reading the shared, by-reference
                # stream accumulators) while the worker was still writing to
                # the ArtifactStore -- yielding a torn transcript or an
                # artifact persisted without a transcript record.
                pre = self._done_handler.pre_publish(event, inp, state)
                from opensquilla.engine.artifact_delivery import (
                    attached_plan_run_ready_for_auto_publish,
                )

                publish_inp = replace(
                    inp,
                    attached_plan_run_ready=(
                        await attached_plan_run_ready_for_auto_publish(inp.tool_context)
                    ),
                )
                publish_task = asyncio.ensure_future(
                    asyncio.to_thread(
                        self._done_handler.run_publish,
                        publish_inp,
                        pre.accumulated_text,
                    )
                )
                try:
                    publish_result = await asyncio.shield(publish_task)
                except asyncio.CancelledError:
                    # Let the in-flight store write drain before the
                    # CancelledError finalizer runs. The worker thread cannot
                    # be interrupted, so absorb REPEATED cancels too: a second
                    # cancel arriving during the drain must not unwind the
                    # coroutine while the worker is still writing to the
                    # ArtifactStore, or the finalizer would race that write.
                    while not publish_task.done():
                        try:
                            await asyncio.shield(asyncio.wait({publish_task}))
                        except asyncio.CancelledError:
                            continue
                    if (
                        not publish_task.cancelled()
                        and publish_task.exception() is None
                    ):
                        # The publish COMPLETED: its bytes are already in the
                        # ArtifactStore and in ctx.published_artifacts, so
                        # record the result into the shared turn state the
                        # cancel finalizer persists from. Recording is the
                        # loss-free choice -- dropping it would orphan a file
                        # that exists on disk (and counts against the disk
                        # budget) with no transcript record. The notice
                        # phases of post_publish stay skipped.
                        self._done_handler.record_publish_result(
                            publish_task.result(), state
                        )
                    raise
                transformed, extra_yields = self._done_handler.post_publish(
                    pre, publish_result, inp, state
                )
                for extra_event in extra_yields:
                    if isinstance(extra_event, ArtifactEvent):
                        await self._adopt_generated_artifact(
                            extra_event,
                            inp.tool_context,
                        )
                if buffer_system_event_text:
                    # Text deltas generated by reconciliation/notices are
                    # replaced with one canonical terminal answer.  Non-text
                    # events (artifacts, warnings, tool lifecycle) remain live
                    # and in their original relative order.
                    extra_yields = [
                        extra_event
                        for extra_event in extra_yields
                        if not isinstance(extra_event, TextDeltaEvent)
                    ]
                    snapshot_present, canonical_text = done_text_snapshot(transformed)
                    if not snapshot_present:
                        canonical_text = transformed.text
                    release_delta = _unreleased_text_suffix(
                        canonical_text,
                        released_system_event_text,
                    )
                    if release_delta:
                        extra_yields.insert(0, TextDeltaEvent(text=release_delta))
                    released_system_event_text = canonical_text
                elif (
                    human_silent_reply_prefix_open
                    and held_human_silent_reply_text
                ):
                    # A short initial candidate was withheld while it could
                    # still be a control token. Once Done proves the payload is
                    # visible, release one canonical delta and discard any
                    # notice/reconciliation deltas that would duplicate it.
                    extra_yields = [
                        extra_event
                        for extra_event in extra_yields
                        if not isinstance(extra_event, TextDeltaEvent)
                    ]
                    snapshot_present, canonical_text = done_text_snapshot(transformed)
                    if not snapshot_present:
                        canonical_text = transformed.text
                    if canonical_text and human_prefix_crossed_public_boundary:
                        _move_held_human_text_to_current_tail(
                            state,
                            held_human_silent_reply_text,
                        )
                    if canonical_text:
                        extra_yields.insert(0, TextDeltaEvent(text=canonical_text))
                    held_human_silent_reply_text = ""
                    human_silent_reply_prefix_open = False
                    human_prefix_crossed_public_boundary = False

                human_silent_reply_disallowed = (
                    inp.input_mode != "system_event"
                    and transformed.delivery == "suppressed"
                    and transformed.suppression_reason
                    in {"no_reply", "heartbeat_ack"}
                    and not transformed.text.strip()
                )
                if human_silent_reply_disallowed:
                    # Keep the usage-bearing DoneEvent in state for transcript
                    # usage/session totals, but replace the public success
                    # terminal with the existing error path. The outer runner
                    # emits pending_error_event only after finalization.
                    extra_yields = [
                        extra_event
                        for extra_event in extra_yields
                        if not isinstance(extra_event, TextDeltaEvent)
                    ]
                    if state.pending_error_event is None:
                        self._error_handler.handle(
                            ErrorEvent(
                                message=SILENT_REPLY_NOT_ALLOWED_MESSAGE,
                                code=SILENT_REPLY_NOT_ALLOWED_CODE,
                            ),
                            state,
                        )
                    transformed = _SUPPRESS
            elif isinstance(event, CompactionEvent):
                await self._compaction_handler.handle(event, inp)
                transformed = _SUPPRESS
            elif isinstance(event, ProviderEnsembleProgressEvent):
                transformed = EnsembleProgressEvent(
                    event_type=event.event_type,
                    proposer_index=event.proposer_index,
                    proposer_label=event.proposer_label,
                    proposer_model=event.proposer_model,
                    proposer_provider=event.proposer_provider,
                    sample_index=event.sample_index,
                    elapsed_ms=event.elapsed_ms,
                    input_tokens=event.input_tokens,
                    output_tokens=event.output_tokens,
                    cost_usd=event.cost_usd,
                    error=event.error,
                    generation_epoch=event.generation_epoch,
                )
            else:
                transformed = event

            if (
                human_silent_reply_prefix_open
                and bool(held_human_silent_reply_text)
                and not isinstance(event, (TextDeltaEvent, ErrorEvent, DoneEvent))
                and (bool(extra_yields) or transformed is not _SUPPRESS)
            ):
                human_prefix_crossed_public_boundary = True

            if realtime_generation:
                for extra_event in extra_yields:
                    yield extra_event
                if transformed is not _SUPPRESS:
                    yield transformed  # type: ignore[misc]
                continue

            for extra_event in extra_yields:
                if _is_deferred_generation_event(extra_event):
                    deferred_generation_events.append(extra_event)
                else:
                    yield extra_event

            if transformed is _SUPPRESS:
                continue
            if _is_deferred_generation_event(transformed):
                deferred_generation_events.append(transformed)  # type: ignore[arg-type]
                continue
            if isinstance(
                transformed,
                (DoneEvent, ErrorEvent, ControlTerminalEvent),
            ) or (
                isinstance(transformed, AnswerGenerationResetEvent)
                and transformed.terminal
            ):
                for deferred_event in deferred_generation_events:
                    yield deferred_event
                deferred_generation_events.clear()
            yield transformed  # type: ignore[misc]

        # Post-stream: notify sync manager once.
        self._memory_sync_notify.notify_message_bytes(
            inp.sync_manager,
            inp.effective_runtime_message,
        )
