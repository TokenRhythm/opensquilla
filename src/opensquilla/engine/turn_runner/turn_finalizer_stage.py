"""TurnRunner stage that finalizes a turn after the agent stream ends.

Drives the post-stream side effects between the "flush remaining text"
edge and the ``turn_call_logger.write("turn_end", ...)`` boundary:
heartbeat normalize, transcript ``append_message`` for the assistant
turn, ``_capture_turn_memory`` invocation, ``_persist_turn_error`` for
any pending error, and the ``Session.update(...)`` session-totals
rollup driven off the ``DoneEvent`` snapshot.

Returns ``StageOutcome[TurnFinalizerStageOutput]`` -- not a generator.
The agent stream has exhausted by the time this stage runs; the four
upstream accumulators are fully materialized. The stage emits no
``AgentEvent``s during its body. The ``pending_error_event`` is
surfaced in the stage output after its trace + decision-entry emit.

Side-effect order (load-bearing):

1. Heartbeat-normalize the accumulated text.
2. Transcript ``append_message`` (assistant turn) -- when
   ``(final_text or segments or artifacts)`` and a session manager is
   wired through the port.
3. ``capture_turn_memory`` -- wrapped in log-and-continue try/except.
4. ``persist_turn_error`` -- only when ``error_message`` is truthy.
   The helper owns its own internal try/except.
5. Session totals rollup -- wrapped in log-and-continue try/except,
   only when a DoneEvent is present.

Memory-after-transcript pairing is required (memory reads the
persisted ``final_text``). Errors are persisted before totals are
rolled up so the recorded cause is visible even when totals fail.

No ``TurnHook.after_turn`` fan-out today.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

import structlog

from opensquilla.contracts.turn_execution import (
    TurnExecutionContext,
    TurnPublicationLedger,
)
from opensquilla.observability.decision_log import build_vision_followup_gate_reason_code
from opensquilla.skills.meta.types import MetaPaused

if TYPE_CHECKING:
    from opensquilla.engine.turn_runner.outcome import StageOutcome
    from opensquilla.engine.types import DoneEvent, ErrorEvent
    from opensquilla.skills.meta.types import MetaResult
    from opensquilla.tools.types import ToolContext

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Paused MetaResult renderer
# ---------------------------------------------------------------------------


def _field_qualifier_text(field: Any) -> str:
    """Build the inline qualifier ("[1-14]", "[budget|mid|premium]" …)."""
    type_ = field.type
    if type_ == "int":
        lo, hi = field.min, field.max
        if lo is not None and hi is not None:
            return f"({lo}-{hi})"
        if lo is not None:
            return f"(>={lo})"
        if hi is not None:
            return f"(<={hi})"
    elif type_ == "enum" and field.choices:
        return "[" + "|".join(str(c) for c in field.choices) + "]"
    elif type_ == "string" and field.max_chars is not None:
        return f"(<={field.max_chars} chars)"
    return ""


def _schema_language(schema: Any, intro: str = "") -> str:
    text = "\n".join(
        [intro or getattr(schema, "intro", "")]
        + [getattr(field, "prompt", "") for field in getattr(schema, "fields", ())]
        + list(getattr(schema, "cancel_keywords", ()) or ())
    )
    return "zh" if any("\u4e00" <= ch <= "\u9fff" for ch in text) else "en"


def _field_flag_text(field: Any, *, language: str = "zh") -> str:
    """Render required / default / optional marker per spec §9.3."""
    if field.required:
        return "[必填]" if language == "zh" else "[required]"
    if field.default is not None:
        return (
            f"（默认 {field.default}）"
            if language == "zh"
            else f"(default {field.default})"
        )
    return "[可选]" if language == "zh" else "[optional]"


def render_paused_outcome(result: MetaResult) -> str:
    """Render a paused MetaResult into a plain-text form description.

    Matches the spec §9.3 IM-fallback layout:
      <intro>
      请回复以下字段：
        1) <name> — <prompt> <qualifier?> <flag>
        ...
      回复格式示例：
        <name>: <type-appropriate placeholder>
      或回复 <cancel-kw> 终止。

    Surface-specific rich rendering (Web card / CLI prompt-toolkit)
    rides on the synthetic ToolResultEvent's ``clarify_schema`` payload,
    not on this text. This rendering is what IM bots and any
    text-only fallback present to the user.
    """
    if not result.paused or result.paused_payload is None:
        return result.final_text or ""
    payload = result.paused_payload
    if not isinstance(payload, MetaPaused):
        return result.final_text or ""
    schema = payload.schema
    language = str(getattr(payload, "language", "") or "").lower()
    if language not in {"en", "zh"}:
        language = _schema_language(schema, payload.intro)
    lines: list[str] = []
    if payload.intro or schema.intro:
        lines.append(payload.intro or schema.intro)
        lines.append("")
    lines.append("请回复以下字段：" if language == "zh" else "Please reply with these fields:")
    for index, field in enumerate(schema.fields, start=1):
        flag = _field_flag_text(field, language=language)
        prompt = field.prompt or field.name
        qualifier = _field_qualifier_text(field)
        bits = [field.name, "—", prompt]
        if qualifier:
            bits.append(qualifier)
        bits.append(flag)
        lines.append(f"  {index}) {' '.join(bits)}")

    sample_fields = [f for f in schema.fields if f.required] or list(schema.fields)
    if sample_fields:
        lines.append("")
        lines.append("回复格式示例：" if language == "zh" else "Reply format example:")
        for field in sample_fields[:3]:
            placeholder = _field_sample_value(field)
            lines.append(f"  {field.name}: {placeholder}")

    if schema.cancel_keywords:
        kws = " / ".join(schema.cancel_keywords)
        lines.append("")
        if language == "zh":
            lines.append(f"或回复 {kws} 取消。")
        else:
            lines.append(f"Or reply {kws} to cancel.")
    return "\n".join(lines)


def _field_sample_value(field: Any) -> str:
    """Produce a placeholder reply value for the format-example block."""
    type_ = field.type
    if type_ == "enum" and field.choices:
        return str(field.choices[0])
    if type_ == "int":
        if field.min is not None:
            return str(field.min)
        if field.max is not None:
            return str(field.max)
        return "1"
    if type_ == "bool":
        return "true"
    if field.default is not None:
        return str(field.default)
    return "<value>"


# ---------------------------------------------------------------------------
# Ports -- four narrow Protocols
# ---------------------------------------------------------------------------

@runtime_checkable
class TranscriptAppendPort(Protocol):
    """Persist the assistant turn via ``SessionManager.append_message(...)``.

    Wraps the inline ``await self._session_manager.append_message(...)``.
    The adapter folds the
    ``_accepts_keyword_arg(..., "token_count")`` introspection so the
    stage body has no ``inspect`` dependency, and the
    ``session_manager is None`` guard so the stage body has no
    conditional on manager presence. Returns ``True`` if the append
    fired, ``False`` when the adapter declined (no manager configured).

    Exceptions propagate to the outer ``_run_turn`` terminal handler --
    no try/except wraps ``append_message``.
    """

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
        assistant_message_id: str | None = None,
    ) -> TranscriptAppendResult | bool: ...

@runtime_checkable
class TurnMemoryCapturePort(Protocol):
    """Wrap ``TurnRunner._capture_turn_memory(...)``.

    The call is wrapped in a log-and-continue try/except inside the
    stage body so the error-handling contract is visible. The adapter
    forwards verbatim without swallowing.
    """

    async def capture_turn(
        self,
        *,
        agent_id: str,
        session_key: str,
        runtime_message: str,
        final_text: str,
        input_mode: str,
        tool_context: ToolContext | None,
        input_provenance: dict[str, Any] | None,
        run_kind: str,
        no_memory_capture: bool,
    ) -> None: ...


def _turn_usage_payload(
    done_event: Any | None,
    *,
    resolved_model: str | None,
    persisted_text: str | None = None,
) -> dict[str, Any] | None:
    if done_event is None:
        return None
    model = done_event.model or resolved_model or ""
    payload = {
        "input_tokens": int(done_event.input_tokens or 0),
        "output_tokens": int(done_event.output_tokens or 0),
        "reasoning_tokens": int(done_event.reasoning_tokens or 0),
        "cached_tokens": int(done_event.cached_tokens or 0),
        "cache_write_tokens": int(done_event.cache_write_tokens or 0),
        "cost_usd": float(done_event.cost_usd or 0.0),
        "billed_cost": float(done_event.billed_cost or 0.0),
        "cost_source": done_event.cost_source or "none",
        "missing_cost_entries": int(
            getattr(done_event, "missing_cost_entries", 0) or 0
        ),
        "model": model,
        "routed_model": done_event.routed_model or "",
        "routed_tier": done_event.routed_tier or None,
        "routing_source": done_event.routing_source or "none",
        "routing_confidence": float(done_event.routing_confidence or 0.0),
        "routing_applied": bool(getattr(done_event, "routing_applied", True)),
        "rollout_phase": getattr(done_event, "rollout_phase", "full") or "full",
        "baseline_model": done_event.baseline_model or "",
        "savings_pct": float(done_event.savings_pct or 0.0),
        "savings_usd": float(done_event.savings_usd or 0.0),
        "cache_hit_active": bool(done_event.cache_hit_active),
        "total_savings_pct": float(done_event.total_savings_pct or 0.0),
        "total_savings_usd": float(done_event.total_savings_usd or 0.0),
        # Additive: quality label of the estimate behind cost_usd
        # ("cache_aware" | "cache_blind" | "free" | None).
        "estimate_basis": getattr(done_event, "estimate_basis", None),
        # Additive: V017 decision-record id so chat clients can attribute
        # feedback (router.feedback.submit) to this exact routing decision.
        # None when no decision was staged (router off / bypass / no writer).
        "decision_id": getattr(done_event, "decision_id", None),
        "route_plan": getattr(done_event, "route_plan", None),
        "execution_legs": list(getattr(done_event, "execution_legs", []) or []),
        "model_call_segments": _model_call_segments_for_persisted_text(
            done_event,
            persisted_text=persisted_text,
        ),
        "router_model_call_id": str(
            getattr(done_event, "router_model_call_id", "") or ""
        ),
        "router_iteration": int(getattr(done_event, "router_iteration", 0) or 0),
    }
    optional_fields = {
        "provider": getattr(done_event, "provider", None),
        "image_route_reason": getattr(done_event, "image_route_reason", None),
        "vision_followup_gate_decision": getattr(
            done_event,
            "vision_followup_gate_decision",
            None,
        ),
        "vision_followup_gate_confidence": getattr(
            done_event,
            "vision_followup_gate_confidence",
            None,
        ),
        "vision_followup_gate_reason": build_vision_followup_gate_reason_code(
            decision=getattr(done_event, "vision_followup_gate_decision", None),
            source=getattr(done_event, "vision_followup_gate_source", None),
            reason=getattr(done_event, "vision_followup_gate_reason", None),
            fallback=getattr(done_event, "vision_followup_fallback", None),
        ),
        "vision_followup_gate_source": getattr(
            done_event,
            "vision_followup_gate_source",
            None,
        ),
        "vision_followup_gate_model": getattr(
            done_event,
            "vision_followup_gate_model",
            None,
        ),
        "vision_followup_needs_image": getattr(
            done_event,
            "vision_followup_needs_image",
            None,
        ),
        "vision_followup_fallback": getattr(done_event, "vision_followup_fallback", None),
    }
    for key, value in optional_fields.items():
        if value is not None:
            payload[key] = value
    model_usage_breakdown = getattr(done_event, "model_usage_breakdown", None)
    if isinstance(model_usage_breakdown, list) and model_usage_breakdown:
        payload["model_usage_breakdown"] = [
            dict(row)
            for row in model_usage_breakdown
            if isinstance(row, dict)
        ]
    ensemble_trace = getattr(done_event, "ensemble_trace", None)
    if isinstance(ensemble_trace, dict) and ensemble_trace:
        payload["ensemble_trace"] = dict(ensemble_trace)
    return payload


def _model_call_segments_for_persisted_text(
    done_event: Any,
    *,
    persisted_text: str | None,
) -> list[dict[str, Any]]:
    """Rebase model-call codepoint ranges after persistence-only formatting."""

    raw_segments = [
        dict(segment)
        for segment in (getattr(done_event, "model_call_segments", []) or [])
        if isinstance(segment, dict)
    ]
    if not raw_segments:
        return []

    original_text = str(
        getattr(done_event, "text_snapshot", None)
        if getattr(done_event, "text_snapshot", None) is not None
        else getattr(done_event, "text", "")
    )
    target_text = original_text if persisted_text is None else persisted_text
    original_length = len(original_text)

    normalized: list[dict[str, Any]] = []
    seen_call_ids: set[str] = set()
    previous_end: int | None = None
    for segment in raw_segments:
        call_id = str(segment.get("model_call_id") or "").strip()
        raw_iteration = segment.get("iteration")
        raw_start = segment.get("start_codepoint")
        raw_end = segment.get("end_codepoint")
        if raw_iteration is None or raw_start is None or raw_end is None:
            return []
        try:
            iteration = int(raw_iteration)
            start = int(raw_start)
            end = int(raw_end)
        except (TypeError, ValueError):
            return []
        if (
            not call_id
            or call_id in seen_call_ids
            or iteration < 1
            or start < 0
            or end < start
            or end > original_length
            or (previous_end is not None and start != previous_end)
        ):
            return []
        seen_call_ids.add(call_id)
        previous_end = end
        normalized.append(
            {
                "model_call_id": call_id,
                "iteration": iteration,
                "start_codepoint": start,
                "end_codepoint": end,
            }
        )
    if normalized[-1]["end_codepoint"] != original_length:
        return []
    if target_text == original_text:
        return normalized

    def _rebase_boundary(boundary: int) -> int | None:
        original_index = 0
        target_index = 0
        while original_index < boundary:
            expected = original_text[original_index]
            while target_index < len(target_text) and target_text[target_index] != expected:
                target_index += 1
            if target_index >= len(target_text):
                return None
            original_index += 1
            target_index += 1
        return target_index

    # Persistence may add paragraph separators or a terminal notice, but it
    # must not delete/rewrite the model text for these ranges to stay causal.
    if _rebase_boundary(original_length) is None:
        return []
    rebased_starts: list[int] = []
    for segment in normalized:
        rebased_start = _rebase_boundary(int(segment["start_codepoint"]))
        if rebased_start is None:
            return []
        rebased_starts.append(rebased_start)
    return [
        {
            **segment,
            "start_codepoint": rebased_starts[index],
            "end_codepoint": (
                rebased_starts[index + 1]
                if index + 1 < len(rebased_starts)
                else len(target_text)
            ),
        }
        for index, segment in enumerate(normalized)
    ]

@runtime_checkable
class SessionTotalsPort(Protocol):
    """Roll up session token + cost + cache totals from a DoneEvent.

    Wraps the entire post-DoneEvent block: the
    ``get_session`` read, ``normalize_event_cost_source`` call, the
    four ``next_*`` accumulator computations, the ``rollup_cost_source``
    call, and the ``Session.update`` write. The adapter folds the
    ``session_manager is None`` guard and the ``current_session is None``
    early-return so the stage body has no conditional on manager
    presence.

    Returns ``CostRollupResult | None``. ``None`` when the adapter
    declined (no session manager or no current session row); a populated
    snapshot otherwise so the equivalence harness can pin the
    post-rollup ``Session`` row across modes.
    """

    async def rollup(
        self,
        *,
        session_key: str,
        done_event: DoneEvent,
        resolved_model: str,
    ) -> CostRollupResult | None: ...

@runtime_checkable
class TurnErrorPersistPort(Protocol):
    """Wrap ``TurnRunner._persist_turn_error(session_key, event)``.

    The helper owns its own log-and-continue try/except; the
    adapter forwards verbatim. The helper guards
    ``session_manager is None`` AND ``event is None`` internally -- the
    stage body has no None checks.
    """

    async def persist_error(
        self,
        *,
        session_key: str,
        event: ErrorEvent | None,
        append_transcript: bool = True,
    ) -> None: ...


@runtime_checkable
class UsageTelemetryPort(Protocol):
    """Best-effort local aggregation for a completed top-level turn."""

    async def record_turn(self, *, run_kind: str, done_event: DoneEvent | None) -> None: ...


class _NullUsageTelemetryPort:
    async def record_turn(self, *, run_kind: str, done_event: DoneEvent | None) -> None:
        return None

# ---------------------------------------------------------------------------
# Finalizer result values
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TranscriptAppendResult:
    """Assistant transcript persistence result from the concrete adapter."""

    appended: bool
    message_id: str | None = None


@dataclass(frozen=True)
class CostRollupResult:
    """Snapshot of the per-turn session-totals update.

    Exposed so the equivalence harness can pin the post-rollup
    ``Session`` row. Not consumed by
    ``TurnContext`` or any downstream stage directly.
    """

    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: float
    total_cost_usd: float
    billed_cost_usd: float
    estimated_cost_component_usd: float
    cost_source: str
    missing_cost_entries: int
    cache_read: int
    cache_write: int
    model_override: str | None
    # Physical provider paired with ``model_override``.  Kept separate from
    # the user's explicit ``provider_override`` so routing is not pinned.
    model_provider: str | None = None

# ---------------------------------------------------------------------------
# Stage I/O dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TurnFinalizerStageInput:
    """Inputs the TurnFinalizerStage needs at its boundary.

    Pulled from ``TurnContext`` accumulators by the harness plus the
    post-stream ``_run_turn``-body locals (the four
    ``stream_*`` mirrors plus the original ``runtime_message`` /
    ``input_mode`` / ``input_provenance``).
    """

    # From StreamConsumerStage
    final_text_parts: list[str]
    turn_segments: list[dict]
    turn_artifacts: list[dict[str, Any]]
    error_message: str | None
    pending_error_event: ErrorEvent | None
    done_event: DoneEvent | None

    # From InputStage -- the ORIGINAL runtime_message (used by
    # ``_capture_turn_memory`` for memory provenance), NOT the effective
    # post-pipeline string.
    runtime_message: str
    input_mode: str
    input_provenance: dict[str, Any] | None

    # From PromptAssemblerStage
    resolved_model: str

    # From AgentBootstrapStage
    agent_id: str

    # From _run_turn locals
    session_key: str
    tool_context: ToolContext | None
    run_kind: str
    heartbeat_ack_max_chars: int
    no_memory_capture: bool

    # Additive identity path.  ``None`` preserves direct/legacy stage callers;
    # an identity-aware TurnRunner supplies one id before streaming starts.
    assistant_message_id: str | None = None
    execution_context: TurnExecutionContext | None = None
    publication_ledger: TurnPublicationLedger | None = None
    # A terminal generation reset already persists its friendly canonical
    # assistant snapshot. Keep the structured turn_errors row, but do not add
    # the ordinary second system "Error:" transcript message on reload.
    terminal_generation_reset: bool = False

@dataclass(frozen=True)
class TurnFinalizerStageOutput:
    """Outputs the harness applies to ``TurnContext`` after the stage runs.

   Downstream consumers read ``final_text``, ``turn_segments``,
    ``turn_artifacts``, ``error_message``, ``pending_error_event``,
    ``done_event`` for its turn_end trace + decision entry. The
    ``cost_rollup`` snapshot is observability-only (pinned by the
    equivalence harness, not consumed downstream).
    """

    # Heartbeat-normalized final text (the harness writes this onto
    # TurnContext for to read).
    final_text: str
    # ``turn_segments`` may be EMPTIED by the heartbeat-empty edge; the
    # stage returns the post-empty value.
    turn_segments: list[dict]
    # Re-exposed unchanged so has its turn_end payload inputs.
    turn_artifacts: list[dict[str, Any]]
    error_message: str | None
    pending_error_event: ErrorEvent | None
    done_event: DoneEvent | None
    # Observability snapshot -- None when no DoneEvent or
    # SessionTotalsPort returned None.
    cost_rollup: CostRollupResult | None
    # Did the assistant turn actually persist?
    transcript_appended: bool
    # Exact assistant row and payload persisted for this turn. The gateway
    # stores these on channel tasks so delivery never has to infer ownership
    # from whichever assistant row happens to be newest.
    assistant_message_id: str | None
    assistant_message_content: str | None
    # Did the memory capture fire?
    memory_captured: bool


def _readable_tool_boundary_text(
    final_text: str,
    turn_segments: list[dict],
) -> str:
    """Preserve paragraph boundaries between separate assistant narrations."""

    text_segments = [
        str(segment.get("text") or "")
        for segment in turn_segments
        if isinstance(segment, dict)
        and segment.get("type") == "text"
        and str(segment.get("text") or "")
    ]
    if len(text_segments) < 2:
        return final_text
    compact = "".join(text_segments)
    if not final_text.startswith(compact):
        return final_text
    readable = text_segments[0]
    for segment in text_segments[1:]:
        if readable[-1:].isspace() or segment[:1].isspace():
            readable += segment
        else:
            readable += f"\n\n{segment}"
    return readable + final_text[len(compact) :]


# ---------------------------------------------------------------------------
# Outer stage class
# ---------------------------------------------------------------------------

class TurnFinalizerStage:
    """Persist the assistant turn, capture memory, roll up session totals.

    Stable boundary: runs ONCE per turn, after StreamConsumerStage
    exhausts (and after the harness flushes the trailing text segment),
   . The four ports execute in the original order:

    1. Heartbeat-normalize the accumulated text.
    2. ``TranscriptAppendPort.append_message`` (assistant turn).
    3. ``TurnMemoryCapturePort.capture_turn`` (memory write -- wrapped
       in log-and-continue try/except intentional).
    4. ``TurnErrorPersistPort.persist_error`` (pending error, only if
       ``error_message`` is truthy).
    5. ``SessionTotalsPort.rollup`` (DoneEvent-driven session.update --
       wrapped in log-and-continue try/except intentional).

    The order is load-bearing: transcript persistence MUST precede
    memory capture (memory capture reads ``final_text`` AS PERSISTED);
    error persist MUST precede totals rollup for diagnostic ordering
    that downstream observability relies on.

    Exception model: the stage does NOT wrap the ``append_message``
    call. Any exception there propagates to the outer ``_run_turn``
    terminal handler --. The memory-capture
    and totals-rollup ports each have their own log-and-continue
    try/except inside the stage body.

    No ``TurnHook.after_turn`` fan-out today; that wiring belongs in a separate
    production hook pass.
    """

    name = "turn_finalizer_stage"

    def __init__(
        self,
        *,
        transcript_append: TranscriptAppendPort,
        turn_memory_capture: TurnMemoryCapturePort,
        session_totals: SessionTotalsPort,
        turn_error_persist: TurnErrorPersistPort,
        usage_telemetry: UsageTelemetryPort | None = None,
    ) -> None:
        self._transcript_append = transcript_append
        self._turn_memory_capture = turn_memory_capture
        self._session_totals = session_totals
        self._turn_error_persist = turn_error_persist
        self._usage_telemetry = usage_telemetry or _NullUsageTelemetryPort()

    async def run(
        self,
        inp: TurnFinalizerStageInput,
    ) -> StageOutcome[TurnFinalizerStageOutput]:
        # Late imports keep the module import-cycle-free.
        import json as _json

        from opensquilla.engine.runtime import _is_deepseek_model_id
        from opensquilla.engine.silent_reply import (
            normalize_silent_reply,
            sanitize_silent_reply_segments,
        )
        from opensquilla.engine.turn_runner.outcome import StageOutcome
        from opensquilla.engine.turn_runner.runtime_notices import (
            with_unconfirmed_action_notice,
        )

        # 1. Normalize the shared silent-reply protocol.
        final_text = _readable_tool_boundary_text(
            "".join(inp.final_text_parts),
            inp.turn_segments,
        )
        normalization = normalize_silent_reply(
            final_text,
            run_kind=inp.run_kind,
            input_mode=inp.input_mode,
            heartbeat_ack_max_chars=inp.heartbeat_ack_max_chars,
        )
        final_text = normalization.text
        turn_segments = inp.turn_segments
        if normalization.changed:
            segment_normalization = sanitize_silent_reply_segments(
                turn_segments,
                run_kind=inp.run_kind,
                input_mode=inp.input_mode,
                heartbeat_ack_max_chars=inp.heartbeat_ack_max_chars,
            )
            turn_segments = segment_normalization.segments
            remaining_segment_text = _readable_tool_boundary_text(
                "".join(
                    str(segment.get("text") or "")
                    for segment in turn_segments
                    if isinstance(segment, dict) and segment.get("type") == "text"
                ),
                turn_segments,
            )
            if remaining_segment_text != final_text:
                # A sentinel may be split across provider iterations or a
                # heartbeat wrapper may span text segments. Preserve tool
                # lifecycle records and fall back to one canonical text block.
                turn_segments = [
                    segment
                    for segment in turn_segments
                    if not (isinstance(segment, dict) and segment.get("type") == "text")
                ]
                if final_text:
                    turn_segments.append(
                        {
                            "type": "text",
                            "text": final_text,
                            "presentation": "answer",
                        }
                    )

        final_text = with_unconfirmed_action_notice(final_text, turn_segments)

        done_event = inp.done_event
        runtime_notice_added = final_text != normalization.text
        if done_event is not None and (normalization.changed or runtime_notice_added):
            # A runtime-authored confirmation guard is visible even when the
            # model payload itself was a silent sentinel.
            delivery: Literal["visible", "suppressed"] = (
                "suppressed" if normalization.suppressed and not final_text else "visible"
            )
            done_event = replace(
                done_event,
                text=final_text,
                text_snapshot=final_text,
                delivery=delivery,
                suppression_reason=(
                    normalization.suppression_reason if delivery == "suppressed" else None
                ),
            )

        transcript_appended = False
        assistant_message_id = inp.assistant_message_id
        if assistant_message_id is None and inp.execution_context is not None:
            assistant_message_id = inp.execution_context.identity.assistant_message_id
        assistant_message_content: str | None = None
        memory_captured = False

        # 2. Transcript append + 3. memory capture (paired -- memory
        # only fires if transcript persisted).
        if final_text or turn_segments or inp.turn_artifacts:
            persisted_content = (
                _json.dumps(
                    {"text": final_text, "artifacts": inp.turn_artifacts},
                    ensure_ascii=False,
                )
                if inp.turn_artifacts
                else final_text
            )
            reasoning_content: str | None = None
            if (
                done_event is not None
                and done_event.reasoning_content
                and _is_deepseek_model_id(
                    done_event.model or inp.resolved_model or ""
                )
            ):
                reasoning_content = done_event.reasoning_content
            token_count = None
            if done_event is not None:
                message_output_tokens = getattr(
                    done_event,
                    "message_output_tokens",
                    None,
                )
                token_count = (
                    message_output_tokens
                    if message_output_tokens is not None
                    else done_event.output_tokens
                )
            append_kwargs: dict[str, Any] = {
                "role": "assistant",
                "content": persisted_content,
                "tool_calls": turn_segments if turn_segments else None,
                "reasoning_content": reasoning_content,
                "turn_usage": _turn_usage_payload(
                    done_event,
                    resolved_model=inp.resolved_model,
                    persisted_text=final_text,
                ),
                "token_count": token_count,
            }
            if assistant_message_id is not None:
                append_kwargs["assistant_message_id"] = assistant_message_id
            append_result = await self._transcript_append.append_message(
                inp.session_key,
                **append_kwargs,
            )
            if isinstance(append_result, TranscriptAppendResult):
                transcript_appended = append_result.appended
                if assistant_message_id is None:
                    assistant_message_id = append_result.message_id
            else:
                # Backward compatibility for third-party/direct stage adapters
                # that implement the original boolean port contract.
                transcript_appended = bool(append_result)
            if transcript_appended:
                assistant_message_content = persisted_content
                if inp.execution_context is not None:
                    inp.execution_context.publish_visible(
                        text=persisted_content,
                        generation_epoch=inp.execution_context.generation_epoch,
                    )
                elif inp.publication_ledger is not None:
                    next_sequence = inp.publication_ledger.last_sequence + 1
                    inp.publication_ledger.accept(
                        generation_epoch=inp.publication_ledger.generation_epoch,
                        sequence=next_sequence,
                        text=persisted_content,
                    )
            if transcript_appended and not inp.no_memory_capture:
                try:
                    await self._turn_memory_capture.capture_turn(
                        agent_id=inp.agent_id,
                        session_key=inp.session_key,
                        runtime_message=inp.runtime_message,
                        final_text=final_text,
                        input_mode=inp.input_mode,
                        tool_context=inp.tool_context,
                        input_provenance=inp.input_provenance,
                        run_kind=inp.run_kind,
                        no_memory_capture=inp.no_memory_capture,
                    )
                    memory_captured = True
                except Exception as exc:  # noqa: BLE001 - log-and-continue intentional
                    log.warning(
                        "turn_runner.capture_failed",
                        session_key=inp.session_key,
                        agent_id=inp.agent_id,
                        error=str(exc),
                    )

        if not (final_text or turn_segments or inp.turn_artifacts):
            if inp.execution_context is not None:
                inp.execution_context.release_reserved_unpublished("no_visible_output")

        # 4. Error persist (only when error_message is truthy; the
        # adapter folds the session-manager-None guard, and the helper
        # also guards event-is-None internally).
        if inp.error_message:
            await self._turn_error_persist.persist_error(
                session_key=inp.session_key,
                event=inp.pending_error_event,
                append_transcript=not inp.terminal_generation_reset,
            )

        # 5. Session totals rollup (only when DoneEvent present; the
        # adapter folds the session-manager-None and
        # current_session-None guards).
        cost_rollup: CostRollupResult | None = None
        if done_event is not None:
            try:
                cost_rollup = await self._session_totals.rollup(
                    session_key=inp.session_key,
                    done_event=done_event,
                    resolved_model=inp.resolved_model,
                )
            except Exception as exc:  # noqa: BLE001 - log-and-continue intentional
                log.warning(
                    "turn_runner.session_usage_persist_failed",
                    session_key=inp.session_key,
                    error=str(exc),
                )

        # 6. Aggregate telemetry governed by the unified privacy switch. The
        # port stores counters only; failures must never alter the turn result.
        try:
            await self._usage_telemetry.record_turn(
                run_kind=inp.run_kind,
                done_event=done_event,
            )
        except Exception as exc:  # noqa: BLE001 - log-and-continue intentional
            log.warning("turn_runner.usage_telemetry_persist_failed", error=str(exc))

        return StageOutcome.success(
            TurnFinalizerStageOutput(
                final_text=final_text,
                turn_segments=turn_segments,
                turn_artifacts=inp.turn_artifacts,
                error_message=inp.error_message,
                pending_error_event=inp.pending_error_event,
                done_event=done_event,
                cost_rollup=cost_rollup,
                transcript_appended=transcript_appended,
                assistant_message_id=assistant_message_id,
                assistant_message_content=assistant_message_content,
                memory_captured=memory_captured,
            )
        )
