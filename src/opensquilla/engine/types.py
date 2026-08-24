"""Engine type definitions: AgentState, AgentEvent, AgentConfig."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from opensquilla.contracts.turn_execution import (
    AnswerGenerationResetEvent,
)
from opensquilla.execution_status import ExecutionStatus
from opensquilla.session.compaction_lifecycle import (
    DEFAULT_FLUSH_TRIGGERS,
    normalize_flush_triggers_strict,
)
from opensquilla.tool_boundary import ToolCall as ToolCall
from opensquilla.tool_boundary import ToolEffectOutcome as ToolEffectOutcome
from opensquilla.tool_boundary import ToolResult as ToolResult

if TYPE_CHECKING:
    from opensquilla.engine.usage import SessionTotalsSnapshot


class ThinkingLevel(StrEnum):
    OFF = "off"
    MINIMAL = "minimal"  # 1024 tokens
    LOW = "low"  # 4096 tokens
    MEDIUM = "medium"  # 10000 tokens
    HIGH = "high"  # 20000 tokens
    XHIGH = "xhigh"  # 50000 tokens
    ADAPTIVE = "adaptive"  # auto-scale based on prompt


THINKING_BUDGETS: dict[ThinkingLevel, int] = {
    ThinkingLevel.OFF: 0,
    ThinkingLevel.MINIMAL: 1024,
    ThinkingLevel.LOW: 4096,
    ThinkingLevel.MEDIUM: 10000,
    ThinkingLevel.HIGH: 20000,
    ThinkingLevel.XHIGH: 50000,
}


class AgentState(StrEnum):
    IDLE = "idle"
    THINKING = "thinking"
    TOOL_CALLING = "tool_calling"
    STREAMING = "streaming"
    ERROR = "error"
    DONE = "done"


class ControlTerminalReason(StrEnum):
    """Control-owned terminal reasons; none of these are provider failures."""

    CANCEL = "cancel"
    SHUTDOWN = "shutdown"
    HARD_DEADLINE = "hard_deadline"
    PLATFORM_VALIDATION = "platform_validation"
    PLATFORM_SAFETY = "platform_safety"


# ---------------------------------------------------------------------------
# Agent events
# ---------------------------------------------------------------------------


@dataclass
class ThinkingStartEvent:
    kind: Literal["thinking_start"] = field(default="thinking_start", init=False)
    block_id: str = ""
    block_index: int = 0
    started_at: int = 0
    content_kind: Literal["summary", "reasoning"] = "reasoning"
    generation_epoch: int = 0


@dataclass
class ThinkingEvent:
    kind: Literal["thinking"] = field(default="thinking", init=False)
    text: str = ""
    started_at: int = 0
    # Optional block identity keeps the existing thinking delta wire contract
    # compatible while allowing newer clients to preserve provider-call
    # boundaries. Empty/-1 remain the legacy single-block representation.
    block_id: str = ""
    block_index: int = -1
    generation_epoch: int = 0
    # Physical provider-call identity for stable presentation ownership.
    # Additive defaults preserve legacy producers and positional construction.
    model_call_id: str = ""
    iteration: int = 0


@dataclass
class ThinkingEndEvent:
    kind: Literal["thinking_end"] = field(default="thinking_end", init=False)
    block_id: str = ""
    block_index: int = 0
    status: Literal["completed", "interrupted", "error"] = "completed"
    ended_at: int = 0
    generation_epoch: int = 0


@dataclass
class TextDeltaEvent:
    kind: Literal["text_delta"] = field(default="text_delta", init=False)
    text: str = ""
    # Whether this text is the turn's final answer (render as a card) or
    # intermediate narration between tool calls (render as a lightweight purple
    # ✻ line). Decided by the agent from whether the producing provider call
    # ended up making tool calls — see agent.py. Defaults to "answer" so any
    # producer that does not set it keeps the pre-existing card behavior.
    presentation: Literal["intermediate", "answer"] = "answer"
    generation_epoch: int = 0
    # Physical provider-call identity for stable presentation ownership.
    # Additive defaults preserve legacy producers and positional construction.
    model_call_id: str = ""
    iteration: int = 0


@dataclass
class RunHeartbeatEvent:
    kind: Literal["run_heartbeat"] = field(default="run_heartbeat", init=False)
    phase: str = "agent"
    elapsed_ms: int = 0
    idle_ms: int = 0
    message: str = ""
    generation_epoch: int = 0


@dataclass
class ProviderActivityEvent:
    """Public, provider-neutral progress for long model operations.

    Fields are deliberately closed enums and counters: provider error bodies
    and failed-leg reasoning must never cross this boundary.
    """

    kind: Literal["provider_activity"] = field(default="provider_activity", init=False)
    schema_version: int = 1
    activity_id: str = ""
    phase: Literal["requesting", "reasoning", "retry_wait", "retrying", "fallback"] = (
        "requesting"
    )
    reason: Literal[
        "initial",
        "rate_limited",
        "provider_overloaded",
        "transport_transient",
        "reasoning_only",
        "empty_response",
        "stream_incomplete",
        "invalid_response",
        "context_overflow",
        "unknown",
    ] = "initial"
    retry_attempt: int = 0
    retry_limit: int = 0
    retry_after_ms: int = 0
    started_at: int = 0
    heartbeat: bool = False


@dataclass
class ToolUseStartEvent:
    kind: Literal["tool_use_start"] = field(default="tool_use_start", init=False)
    tool_use_id: str = ""
    tool_name: str = ""
    synthetic_from_text: bool = False
    # Server wall-clock start time in epoch milliseconds, stamped when the tool
    # call begins. Lets a client show a stable elapsed timer that survives
    # page switches / stream replay instead of restarting from a fresh local
    # clock every time the component remounts (see issue #329). 0 means
    # "unstamped" — clients fall back to their own clock.
    started_at: int = 0
    generation_epoch: int = 0


@dataclass
class ToolUseDeltaEvent:
    kind: Literal["tool_use_delta"] = field(default="tool_use_delta", init=False)
    tool_use_id: str = ""
    json_fragment: str = ""
    generation_epoch: int = 0


@dataclass
class ToolUseEndEvent:
    """Committed end of one provider tool-call declaration.

    Like start/delta, this event is buffered inside the Agent until the
    provider call reaches a legal DoneEvent.  ``arguments`` is the validated,
    authoritative object supplied by the provider boundary.
    """

    kind: Literal["tool_use_end"] = field(default="tool_use_end", init=False)
    tool_use_id: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    synthetic_from_text: bool = False
    generation_epoch: int = 0


@dataclass
class ToolResultEvent:
    kind: Literal["tool_result"] = field(default="tool_result", init=False)
    tool_use_id: str = ""
    tool_name: str = ""
    result: str = ""
    is_error: bool = False
    arguments: dict[str, Any] | None = None
    execution_status: ExecutionStatus | None = None
    effect_outcome: ToolEffectOutcome | None = None
    generation_epoch: int = 0


@dataclass
class RouterControlReplayEvent:
    kind: Literal["router_control_replay"] = field(default="router_control_replay", init=False)
    action: str = ""
    target_tier: str | None = None
    target_model: str | None = None
    target_provider: str | None = None
    target_id: str | None = None
    replay_depth: int = 0


@dataclass
class ArtifactEvent:
    kind: Literal["artifact"] = "artifact"
    id: str = ""
    sha256: str = ""
    name: str = ""
    mime: str = ""
    size: int = 0
    session_id: str = ""
    session_key: str = ""
    source: str = ""
    created_at: str = ""
    download_url: str = ""
    store: str = "artifacts"
    has_thumbnail: bool = False
    generation_epoch: int = 0


@dataclass
class StateChangeEvent:
    kind: Literal["state_change"] = field(default="state_change", init=False)
    from_state: AgentState = AgentState.IDLE
    to_state: AgentState = AgentState.IDLE


@dataclass
class ErrorEvent:
    kind: Literal["error"] = field(default="error", init=False)
    message: str = ""
    code: str = ""
    # Short reference id joining this error to its durable turn_errors row.
    # Appended last with a default: positional construction elsewhere must not
    # shift (same hazard the DoneEvent comment in this file documents).
    error_id: str = ""
    # Stable provider taxonomy for terminal consumers.  The provider's raw
    # ``code`` remains available for diagnostics and wire compatibility.
    failure_kind: str = ""
    generation_epoch: int = 0
    # Optional bounded retry hint for errors that prove no provider dispatch.
    # Appended for positional-construction compatibility.
    retry_after_ms: int | None = None
    # Usage-ledger admission evidence. ``retryable`` describes the transient
    # error class; these fields separately prove whether replaying the whole
    # user turn cannot duplicate an earlier provider dispatch or side effect.
    usage_call_index: int | None = None
    no_prior_provider_dispatch: bool | None = None
    replay_safe: bool | None = None


@dataclass
class ControlTerminalEvent:
    """Public terminal event owned by turn control, not by a provider."""

    kind: Literal["control_terminal"] = field(default="control_terminal", init=False)
    turn_id: str = ""
    assistant_message_id: str = ""
    sequence: int = 0
    reason: ControlTerminalReason = ControlTerminalReason.CANCEL
    preserve_completed_tools: bool = True
    terminal: bool = True

    def __post_init__(self) -> None:
        self.reason = ControlTerminalReason(self.reason)
        if not self.preserve_completed_tools:
            raise ValueError("control terminal events must preserve completed tools")
        if not self.terminal:
            raise ValueError("control terminal events must be terminal")


@dataclass
class DoneEvent:
    kind: Literal["done"] = field(default="done", init=False)
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    iterations: int = 0
    cost_usd: float = 0.0
    billed_cost: float = 0.0
    cost_source: str = "none"
    model: str = ""
    runtime_context_hash: str | None = None
    runtime_context_chars: int = 0
    routed_tier: str | None = None
    routing_source: str = "none"
    routing_confidence: float = 0.0
    baseline_model: str = ""
    routed_model: str = ""
    savings_pct: float = 0.0
    savings_usd: float = 0.0
    cache_hit_active: bool = False
    # Comprehensive per-turn savings score derived from token counts and model prices.
    # Route-only compatibility remains in savings_pct/savings_usd.
    total_savings_pct: float = 0.0
    total_savings_usd: float = 0.0
    # New fields appended at the end so positional construction in tests
    # (notably DoneEvent(...) without kwargs) does not silently shift earlier
    # args.
    cache_write_tokens: int = 0
    reasoning_content: str | None = None
    session_totals: SessionTotalsSnapshot | None = None
    routing_applied: bool = True
    rollout_phase: str = "full"
    image_route_reason: str | None = None
    vision_followup_gate_decision: str | None = None
    vision_followup_gate_confidence: float | None = None
    vision_followup_gate_reason: str | None = None
    vision_followup_gate_source: str | None = None
    vision_followup_gate_model: str | None = None
    vision_followup_needs_image: bool | None = None
    vision_followup_fallback: str | None = None
    model_usage_breakdown: list[dict[str, Any]] = field(default_factory=list)
    ensemble_trace: dict[str, Any] | None = None
    # Quality label of the estimate component behind cost_usd:
    # "cache_aware" | "cache_blind" | "free" | None (None when the reported
    # cost has no estimated component, e.g. fully provider-billed turns).
    estimate_basis: str | None = None
    # V017 router-decision record id for this turn, when one was staged.
    # Lets chat clients attribute feedback (router.feedback.submit) to the
    # exact routing decision. None when the router is disabled, the turn
    # bypassed classification, or no decision writer is registered.
    decision_id: str | None = None
    # Explicit presence distinguishes an authoritative empty final answer from
    # legacy/partial producers whose empty ``text`` means "fall back to the
    # streamed deltas".  Appended last for positional-construction compatibility.
    text_snapshot: str | None = None
    # Configured registry identity that served the terminal model response.
    # Appended for compatibility with existing positional construction.
    provider: str = ""
    # Historical output-token count for the parent assistant message itself.
    # Aggregated output_tokens may also include completed in-process subagents.
    message_output_tokens: int | None = None
    # Number of non-free usage components whose cost could not be determined.
    missing_cost_entries: int = 0
    # Immutable logical router choice plus append-only physical provider calls.
    # Appended for wire and positional-construction compatibility.
    route_plan: dict[str, Any] | None = None
    execution_legs: list[dict[str, Any]] = field(default_factory=list)
    # Output ranges produced by model calls that applied a same-turn steer.
    # Offsets are Unicode codepoint indexes into ``text_snapshot``/``text``.
    # Appended for wire and positional-construction compatibility.
    model_call_segments: list[dict[str, Any]] = field(default_factory=list)
    # Typed presentation contract for silent replies. Appended so existing
    # positional DoneEvent construction keeps its historical field order.
    delivery: Literal["visible", "suppressed"] = "visible"
    suppression_reason: Literal["no_reply", "heartbeat_ack"] | None = None
    # Authoritative document side-effect fact for restricted annotation turns.
    # Presentation text may be model-generated; this receipt is runtime-owned.
    document_mutation_outcome: dict[str, Any] | None = None
    generation_epoch: int = 0
    # First physical provider call that emitted visible output for the route
    # plan. Clients use this to keep its route card on the same answer segment.
    router_model_call_id: str = ""
    router_iteration: int = 0

    @property
    def upstream_cost_usd(self) -> float:
        """Backward-compatible alias for earlier OpenRouter cost consumers."""
        return self.billed_cost


def done_text_snapshot(event_or_mapping: object) -> tuple[bool, str]:
    """Resolve an engine Done event's terminal-text presence and value.

    New producers set ``text_snapshot`` even when the authoritative value is
    empty.  Legacy producers remain compatible: a nonempty ``text`` is treated
    as an authoritative snapshot, while an empty value means consumers should
    retain whatever text deltas they already collected.
    """

    if isinstance(event_or_mapping, Mapping):
        explicit = event_or_mapping.get("text_snapshot")
        legacy = event_or_mapping.get("text")
    else:
        explicit = getattr(event_or_mapping, "text_snapshot", None)
        legacy = getattr(event_or_mapping, "text", None)
    if isinstance(explicit, str):
        return True, explicit
    if isinstance(legacy, str) and legacy:
        return True, legacy
    return False, ""


@dataclass
class RouterDecisionEvent:
    """Squilla router's decision for this turn, emitted once after the
    pre-turn pipeline resolves the tier/model. Frontend uses this to drive
    the router HUD (tier pill, tier-shift highlight, scanner popover).

    Routing fires once per logical turn and the plan sticks across the
    agent loop. Provider fallback is reported through the terminal
    ``execution_legs`` payload and never creates a second routing decision.
    """

    kind: Literal["router_decision"] = field(default="router_decision", init=False)
    tier: str = ""
    tier_index: int = -1
    model: str = ""
    baseline_model: str = ""
    source: str = "none"
    confidence: float = 0.0
    probs: list[float] = field(default_factory=list)
    savings_pct: float = 0.0
    fallback: bool = False
    thinking_mode: str = ""
    prompt_policy: str = ""
    routing_applied: bool = True
    rollout_phase: str = "full"
    context_window: int | None = None


@dataclass
class EnsembleProgressEvent:
    """One LLM-ensemble member started or finished mid-turn. Emitted from the
    ensemble provider and forwarded so the frontend can reveal ensemble members
    incrementally ahead of the terminal DoneEvent breakdown."""

    kind: Literal["ensemble_progress"] = field(default="ensemble_progress", init=False)
    event_type: str = "proposer_start"
    proposer_index: int = -1
    proposer_label: str = ""
    proposer_model: str = ""
    proposer_provider: str = ""
    sample_index: int = 0
    elapsed_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error: str = ""
    generation_epoch: int = 0


@dataclass
class MetaPreflightEvent:
    """Emitted before a MetaSkill run begins when the plan declares a
    ``request_template``. This is a non-blocking preview of the interpreted
    request and declared assumptions; the scheduler continues after emitting it.
    """

    kind: Literal["meta_preflight"] = field(default="meta_preflight", init=False)
    run_id: str = ""
    meta_skill_name: str = ""
    request_template: dict[str, Any] = field(default_factory=dict)
    interpreted_request: str = ""
    missing_fields: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    can_skip: bool = True
    requires_confirmation: bool = False


@dataclass
class MetaRunAnnouncedEvent:
    """Emitted once when a MetaSkill run starts and its plan has been
    compiled. WebUI uses this to seed the step ribbon with all declared
    step ids, labels, kinds, and dependency edges. `parent_run_id` is
    reserved for nested meta-skill rollouts (always None today).
    """

    kind: Literal["meta_run_announced"] = field(default="meta_run_announced", init=False)
    run_id: str = ""
    meta_skill_name: str = ""
    language: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    parent_run_id: str | None = None


@dataclass
class MetaStepStateEvent:
    """One state transition for a single MetaSkill step within a run.

    `state` is one of pending / running / succeeded / failed / skipped /
    substituted. `status_text` is an optional short human-readable label
    shown under the active chip; `error` carries the failure message when
    `state == "failed"`; `substitute_for` is set on the substitute step
    yielded after an `on_failure` branch fires.
    """

    kind: Literal["meta_step_state"] = field(default="meta_step_state", init=False)
    run_id: str = ""
    step_id: str = ""
    state: Literal["pending", "running", "succeeded", "failed", "skipped", "substituted"] = (
        "pending"
    )
    status_text: str | None = None
    error: str | None = None
    substitute_for: str | None = None
    rescue: dict[str, Any] = field(default_factory=dict)


@dataclass
class MetaRunCompletedEvent:
    """Terminal event for a MetaSkill run. `outcome` is one of
    ok / failed / cancelled. The three step-id lists let the WebUI freeze
    the final ribbon state without scanning back through the stream.
    `recovered_steps` keeps the audit trail for failed steps whose
    on-failure substitute completed successfully.
    """

    kind: Literal["meta_run_completed"] = field(default="meta_run_completed", init=False)
    run_id: str = ""
    outcome: Literal["ok", "failed", "cancelled"] = "ok"
    completed_steps: list[str] = field(default_factory=list)
    failed_steps: list[str] = field(default_factory=list)
    recovered_steps: list[str] = field(default_factory=list)
    skipped_steps: list[str] = field(default_factory=list)


@dataclass
class WarningEvent:
    """Non-persistent user-facing warning surfaced at the end of a turn.

    Unlike ErrorEvent, warnings do not terminate the turn. They flow through
    ``session.event.warning`` to the frontend, which typically shows a toast.
    Not written to the transcript — warnings should never enter LLM context.
    """

    kind: Literal["warning"] = field(default="warning", init=False)
    code: str = ""
    message: str = ""


@dataclass
class CompactionEvent:
    """Emitted when Agent completes inline compaction. Captured by TurnRunner for DB persistence."""

    kind: Literal["compaction"] = field(default="compaction", init=False)
    compaction_id: str | None = None
    compaction_deadline_at_monotonic: float | None = None
    compaction_timeout_seconds: float | None = None
    summary: str = ""
    kept_entries: list[dict] = field(default_factory=list)
    kept_count: int = 0
    removed_count: int = 0
    summary_payload: dict[str, Any] | None = None
    summary_format: str = "text"
    coverage_status: str = "unknown"
    missing_obligations: list[str] | None = None
    critical_carry_forward: list[str] | None = None


@dataclass
class CompactionOutcome:
    """Return type for _check_context_overflow — carries compaction metadata."""

    messages: list = field(default_factory=list)
    compacted: bool = False
    summary: str = ""
    summary_payload: dict[str, Any] | None = None
    summary_format: str = "text"
    coverage_status: str = "unknown"
    missing_obligations: list[str] | None = None
    critical_carry_forward: list[str] | None = None
    kept_entries: list[dict] = field(default_factory=list)
    removed_count: int = 0
    compaction_id: str | None = None
    compaction_deadline_at_monotonic: float | None = None
    compaction_timeout_seconds: float | None = None
    request_context_insert_index: int | None = None
    runtime_context_insert_index: int | None = None
    protected_turn_start_index: int | None = None
    # Request-scoped projection only. The canonical turn transcript remains
    # raw and no CompactionEvent may be persisted for this outcome.
    ephemeral_only: bool = False
    # Runtime-only owner for a multi-stage overflow recovery. Durable and
    # request-scoped projections share its absolute deadline and physical LLM
    # call counter; it must never enter persistence, logs, or event reprs.
    runtime_compaction_config: Any | None = field(
        default=None,
        repr=False,
        compare=False,
    )


AgentEvent = (
    ThinkingStartEvent
    | ThinkingEvent
    | ThinkingEndEvent
    | TextDeltaEvent
    | RunHeartbeatEvent
    | ProviderActivityEvent
    | ToolUseStartEvent
    | ToolUseDeltaEvent
    | ToolUseEndEvent
    | ToolResultEvent
    | RouterControlReplayEvent
    | ArtifactEvent
    | StateChangeEvent
    | ErrorEvent
    | AnswerGenerationResetEvent
    | ControlTerminalEvent
    | DoneEvent
    | CompactionEvent
    | WarningEvent
    | RouterDecisionEvent
    | EnsembleProgressEvent
    | MetaPreflightEvent
    | MetaRunAnnouncedEvent
    | MetaStepStateEvent
    | MetaRunCompletedEvent
)


# ---------------------------------------------------------------------------
# Agent config (internal — @dataclass)
# ---------------------------------------------------------------------------


_THINKING_BUDGET_DEFAULT = -1  # sentinel: "not explicitly set"

# Tokens used for ADAPTIVE level when no prompt length is provided
_ADAPTIVE_DEFAULT_TOKENS = 10000

# Characters-per-token estimate for adaptive scaling
_CHARS_PER_TOKEN = 4


@dataclass
class AgentConfig:
    # Model/tool loop budget. 0 = unlimited; explicit positive values are
    # bounded operator budgets for CI, benchmarks, and constrained runs.
    max_iterations: int = 0
    # Total turn wall-clock budget (seconds; 0 = disabled)
    # 30 min — see iteration_timeout note below; outer turn budget for
    # meta-skill DAGs (paper-write / arxiv-deck run 5-7 min commonly).
    timeout: float = 1800.0
    # Per-iteration timeout: one LLM call + its tool executions
    # 30 min — single iteration may be the whole meta DAG when the soft
    # path treats meta_invoke as a single tool call.
    iteration_timeout: float = 1800.0
    # HTTP-level timeout for a single LLM API request
    request_timeout: float = 120.0
    # Per-tool execution timeout
    tool_timeout: float = 60.0
    # Upper bound for same-turn safe tool execution. Safe tools can overlap, but
    # unbounded fan-out can overload local/network resources.
    max_safe_tool_concurrency: int = 6
    max_tokens: int = 16384
    # Optional per-turn operator budgets. 0 disables the corresponding budget.
    max_turn_llm_calls: int = 0
    max_turn_input_tokens: int = 0
    max_turn_output_tokens: int = 0
    max_turn_billed_cost_usd: float = 0.0
    # Estimate-backed cousin of max_turn_billed_cost_usd: falls back to
    # estimate_cost() for calls with no provider-reported billed_cost, so the
    # gate works even on providers/paths that never report real dollars.
    max_turn_cost_usd: float = 0.0
    max_turn_tool_errors: int = 0
    temperature: float | None = None
    top_p: float | None = None
    thinking: bool | ThinkingLevel = False
    thinking_budget_tokens: int = _THINKING_BUDGET_DEFAULT
    system_prompt: str | None = None
    extra_system_prompt: str | None = None
    workspace_dir: str | None = None
    model_id: str | None = None
    # CONFIGURED provider id (e.g. "vllm", "lm_studio"), sourced from
    # config.llm.provider — NOT the adapter class name (openai_compat
    # deployments all report provider_name == "openai"). Threaded into usage
    # tracking so the layered price resolver can treat local runtimes as free.
    provider_id: str = ""
    stop_sequences: list[str] = field(default_factory=list)
    context_window_tokens: int = 200000
    # Positive only when the gateway operator explicitly configured the global
    # ``llm.context_window_tokens`` value. Keep it separate from the resolved
    # active-model window so selector fallback can apply the same precedence to
    # its own physical deployment. Zero preserves catalog-derived rebinding.
    context_window_tokens_global_override: int = 0
    context_overflow_threshold: float = 0.85  # trigger at 85%
    max_overflow_retries: int = 2
    max_history_turns: int = 0  # 0 = unlimited; compaction handles oversized history
    preserve_historical_images: bool = False
    materialize_historical_attachments: bool = True
    # Retry policy for transient LLM errors (429, 500, 503)
    max_provider_retries: int = 3
    length_capped_continuations: int = 3
    retry_base_backoff_ms: int = 1000
    retry_max_backoff_ms: int = 30_000
    # Prompt caching breakpoints (list of {"text": ..., "cache": "true"})
    cache_breakpoints: list[dict[str, str]] | None = None
    cache_mode: Literal["off", "auto", "on"] = "off"
    # Optional final-answer JSON schema for provider-specific structured output.
    output_json_schema: dict[str, Any] | None = None
    output_json_schema_strict: bool = True
    # Per-turn volatile request context injected after persisted history
    # and before the current user turn. It is not persisted to history.
    request_context_prompt: str | None = None
    # Per-turn user-role skill context injected after persisted history
    # and before the current user turn. The agent persists each turn's
    # skill context in history so provider KV-cache prefixes stay stable.
    skills_context_prompt: str | None = None
    # Pre-compaction memory flush
    flush_enabled: bool = False
    flush_triggers: list[str] = field(default_factory=lambda: list(DEFAULT_FLUSH_TRIGGERS))
    flush_pre_compaction: bool = False
    flush_timeout_seconds: float = 15.0
    flush_background_timeout_seconds: float = 120.0
    flush_backoff_initial_seconds: float = 30.0
    flush_backoff_max_seconds: float = 300.0
    flush_archive_max_bytes: int = 800_000
    flush_compaction_requires_safe_receipt: bool = False
    flush_compaction_safety_mode: Literal["protect", "best_effort", "block", "off"] = "protect"
    compaction_profile: Literal["conversation", "coding", "research", "support"] = "conversation"
    compaction_protected_recent_messages: int = 0
    compaction_total_timeout_seconds: float = 120.0
    compaction_heartbeat_interval_seconds: float = 15.0
    # Explicit per-turn authority boundary. Restricted turns (currently
    # PromptAnnotation edits) must never send persisted history to an
    # auxiliary compaction/flush model before or during the primary request.
    # The runtime derives this from ToolContext.exclusive_tools; it is not a
    # user-configurable inference from individual tool names.
    restricted_turn: bool = False
    # Frozen runtime-only single-deployment chain for auxiliary compaction.
    # Kept opaque here to avoid coupling engine types to session internals.
    compaction_execution_plan: Any | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    # Re-resolve the selector's current physical leg at the start of each
    # compaction operation, then freeze the returned plan for that operation.
    compaction_execution_plan_factory: Callable[[], Any | None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    repair_enabled: bool = True
    repair_interval_seconds: float = 60.0
    repair_max_items_per_tick: int = 5
    flush_workspace_dir: str | None = None
    model_capabilities: Any | None = None  # ModelCapabilities from provider.types
    # Active-deployment tool capability provenance for diagnostics and routing.
    # Tool authorization remains owned by the projected registry surface and
    # dispatch/side-effect boundaries; unknown provenance does not remove tools.
    model_tools_capability_verified: bool = False
    # Runtime-only tri-state evidence; synthesized capability defaults remain
    # ``unknown`` instead of becoming an authoritative vision denial.
    model_vision_support: Literal["supported", "unsupported", "unknown"] = "unknown"
    # Tokenjuice projection: project eligible fresh tool results before the
    # next LLM turn. This is not user-selectable behavior.
    # Legacy compression knobs remain as compatibility shims for meta_invoke
    # tests and embedded callers; the runtime's default path uses Tokenjuice.
    tool_result_compression_enabled: bool = True
    tool_result_compression_mode: Literal["off", "truncate", "summarize"] | None = None
    tool_result_compression_max_share: float = 0.25
    tool_result_compression_summary_model: str | None = None
    tool_result_compression_summary_max_tokens: int = 1024
    tool_result_compression_summary_timeout_seconds: float = 20.0
    tool_result_compression_summary_input_max_chars: int = 60_000
    tool_result_projection_max_inline_chars: int = 60_000
    # Fresh diagnostic delivery is experimental; unattended profiles should opt
    # in only after model-specific validation.
    tool_result_fresh_diagnostic_policy_enabled: bool = False
    tool_result_diagnostic_retrieval_gate_enabled: bool = False
    # When the fresh diagnostic policy is enabled, keep bounded failures intact
    # for the immediate handoff, then let older history/replay compaction handle
    # long-term context pressure.
    tool_result_fresh_diagnostic_inline_max_chars: int = 64_000
    # Dispatch-layer tool result caps. 0 disables the cap. These run before
    # provider-request projection and are intended for unattended automation
    # profiles that must keep broad local command output bounded.
    tool_result_dispatch_max_chars: int = 0
    tool_result_dispatch_turn_max_chars: int = 0
    tool_result_provider_request_max_chars: int = 0
    provider_request_proof_max_chars: int = 0
    # ``None`` means infer an explicit operator limit from a positive value.
    # Internal callers that bind a cap derived for a concrete child deployment
    # set this to ``False`` so selector fallback can re-plan independently.
    provider_request_proof_max_chars_explicit: bool | None = None
    tool_use_argument_provider_request_max_chars: int = 0
    tool_use_argument_projection_enabled: bool = False
    tool_result_external_keep_recent: int = 2
    tool_failure_loop_block_threshold: int = 3
    repeated_tool_call_recovery_threshold: int = 0
    # Extra tool names covered by repeated-identical-call recovery, on top of
    # the built-in read-only set. Set via OPENSQUILLA_TOOL_REPEAT_NUDGE_TOOLS
    # (comma-separated); the threshold is tunable via
    # OPENSQUILLA_TOOL_REPEAT_NUDGE_THRESHOLD.
    repeated_tool_call_recovery_extra_tools: tuple[str, ...] = ()
    progress_watchdog_mode: Literal["off", "log", "warn_model", "block"] = "off"
    progress_watchdog_repeated_tool_error_threshold: int = 3
    progress_watchdog_repeated_provider_failure_threshold: int = 2
    progress_watchdog_repeated_failure_anchor_threshold: int = 3
    post_write_convergence_enabled: bool = False
    post_write_convergence_warn_threshold: int = 3
    post_write_convergence_finalize_after_warning: int = 3
    patch_evidence_ledger_path: str | None = None
    # Finalize-time red-evidence gate (see engine.finalize_evidence_gate).
    # Off by default; enabled per run via OPENSQUILLA_FINALIZE_EVIDENCE_GATE.
    finalize_evidence_gate_enabled: bool = False
    # Strict mode for the finalize-time evidence gate: adds the
    # zero_verification trigger (a change shipped without ever running a
    # verification-level command draws one bounded challenge). Red-first
    # state stays report-only. Implies the gate itself (strict on activates
    # the tracker even when the base flag is off). Off by default; enabled
    # per run via OPENSQUILLA_FINALIZE_EVIDENCE_STRICT.
    finalize_evidence_strict: bool = False
    # Review-on-submit checkpoint (see engine.submit_review). Surfaces a
    # ``submit`` tool and, when the model finishes with a non-empty diff, shows
    # it a general hygiene checklist plus its own diff exactly once before the
    # turn finalizes. Off by default; enabled per run via
    # OPENSQUILLA_SUBMIT_REVIEW. ``submit_review_diff_max_chars`` bounds the diff
    # body echoed into context (the per-file summary is always shown in full).
    submit_review_enabled: bool = False
    submit_review_diff_max_chars: int = 20000
    # Finalize-time patch hygiene hard block. "off" (default) leaves patch
    # hygiene as warn-only text; "test_paths" challenges a finalizing response
    # while the live workspace diff still touches test-classified paths, so
    # test edits are reverted before the patch is collected;
    # "protected_paths" instead challenges while the diff touches paths
    # matching the deployment's workspace write-deny globs — the same
    # configured policy the write gates enforce, with no built-in path
    # taxonomy. Set via OPENSQUILLA_PATCH_HYGIENE_BLOCK.
    patch_hygiene_block_mode: Literal["off", "test_paths", "protected_paths"] = "off"
    # Scratch verify-mirror for deny-blocked in-package test edits. When on,
    # workspace write-deny rejections point the model at a writable mirror
    # under <scratch>/verify-mirror/<workspace-relative-path>, and the
    # finalize-time evidence gate credits executions that reference mirror
    # files ONLY while every mirror copy hash-matches its workspace original
    # (a diverged mirror would otherwise let weakened tests count as
    # verification). Off by default; set via
    # OPENSQUILLA_SCRATCH_VERIFY_MIRROR.
    scratch_verify_mirror: bool = False
    # Finalize-time variant-sweep challenge. When on, the first finalizing
    # response after source edits receives ONE uniform challenge turn asking
    # the model to enumerate the distinct input/construct classes reachable
    # by its changed code paths and run its verification against each. Fires
    # at most once per turn and never spends the last LLM call or deadline
    # slack. Off by default; set via OPENSQUILLA_FINALIZE_VARIANT_CHALLENGE.
    finalize_variant_challenge: bool = False
    # Keep rejection feedback visible when blocked compacted-placeholder tool
    # calls are projected out of provider requests: the blocked tool_use keeps
    # a placeholder input and its error tool_result stays in the projection.
    # Off by default; enabled via OPENSQUILLA_PROVIDER_CONTEXT_BLOCK_FEEDBACK.
    provider_context_block_feedback: bool = False
    # Byte-identical provider-request loop breaker. 0 = off. At N consecutive
    # identical projected payloads the request is perturbed with a loop nudge;
    # at 2N the turn aborts. Set via OPENSQUILLA_IDENTICAL_REQUEST_LOOP_BREAK.
    identical_request_loop_break_threshold: int = 0
    # Escalating recovery directive for repeated compacted-placeholder tool-call
    # offenses within one turn. 0 = off. From the Nth iteration that blocks a
    # placeholder reuse onward, a stronger directive is appended after the tool
    # results so the model rebuilds arguments from fresh file/command output
    # instead of re-offending until the wall clock expires. Set via
    # OPENSQUILLA_PLACEHOLDER_ESCALATION_THRESHOLD.
    placeholder_escalation_threshold: int = 0
    # Pre-deadline wrap-up nudge. 0 = off. When positive and a total turn
    # timeout is configured, the wrap-up directive arms once when remaining
    # wall-clock time drops below this many seconds, then is rebuilt each
    # iteration (so the remaining-minutes figure stays current) and spliced
    # into every subsequent provider request; only the arming log event is
    # one-shot. Unlike the max_iterations finalization, tools stay available
    # so the model can still apply and verify its final changes. Set via
    # OPENSQUILLA_DEADLINE_WRAPUP_MARGIN_SECONDS.
    deadline_wrapup_margin_seconds: int = 0
    # Retry the reasoning-only provider failure with thinking disabled instead
    # of re-requesting visible content with thinking still enabled. Off by
    # default (the retry keeps thinking on). Set via
    # OPENSQUILLA_REASONING_ONLY_THINKING_FALLBACK.
    reasoning_only_thinking_fallback: bool = False
    # Retry provider errors mentioning thinking/reasoning with thinking
    # disabled. Historical default on; strict benchmark arms can turn it off
    # with OPENSQUILLA_PROVIDER_ERROR_THINKING_FALLBACK so every request keeps
    # the frozen thinking contract.
    provider_error_thinking_fallback: bool = True
    # Force thinking off for every provider call once remaining wall-clock
    # time drops below this many seconds. 0 = off. Complements the wrap-up
    # directive: the nudge alone leaves thinking enabled, so the model can
    # still spend the entire margin inside a single reasoning stream. Set via
    # OPENSQUILLA_DEADLINE_THINKING_OFF_MARGIN_SECONDS.
    deadline_thinking_off_margin_seconds: int = 0
    # Preempt a runaway reasoning-only stream once its streamed reasoning text
    # exceeds this many characters. 0 = off. The partial reasoning is
    # discarded and the call retries immediately with thinking disabled for
    # that retry only (the next iteration re-enables thinking), so the budget
    # goes to tool calls instead of one unbounded reasoning stream. One
    # preempt per iteration; attempts that already emitted user-visible text
    # or tool calls are never preempted. Set via
    # OPENSQUILLA_REASONING_STREAM_CHAR_CAP.
    reasoning_stream_char_cap: int = 0
    # Re-apply captured source-diff candidates whose paths end the turn with
    # no live workspace diff (that path's earlier work would otherwise be
    # missing from the collected patch). Off by default. Runs once per turn
    # end — normal finalization and terminal errors alike — applying the
    # newest candidate per path, each guarded by `git apply --check`. Set via
    # OPENSQUILLA_FINAL_DIFF_SALVAGE.
    final_diff_salvage: bool = False
    # Freeze workspace-reverting git commands (restore, checkout paths or
    # branches, reset --hard, clean -fd, stash) in the shell tools once
    # remaining wall-clock time drops below this many seconds. 0 = off.
    # Unlike source_diff_preservation_mode="block", the freeze blocks the
    # operations outright — no protected-path intersection — so a last-minute
    # revert cannot empty the collected diff. Set via
    # OPENSQUILLA_ENDGAME_GIT_FREEZE_MARGIN_SECONDS.
    endgame_git_freeze_margin_seconds: int = 0
    # Let the iteration cap yield to remaining wall-clock time. 0 = off. When
    # positive and a total turn timeout is configured, hitting max_iterations
    # does NOT enter finalization while more than this many seconds remain;
    # the loop keeps running normal iterations until the deadline margin is
    # reached, then the cap applies as usual. Set via
    # OPENSQUILLA_MAX_ITERATIONS_DEADLINE_EXTEND_SECONDS.
    max_iterations_deadline_extend_seconds: int = 0
    # Veto for final-diff salvage: skip candidates the agent explicitly
    # reverted (marked lost) and candidates whose patch only adds diagnostic
    # print/log lines — both re-apply abandoned or throwaway edits into the
    # scored patch. Off by default; only meaningful with final_diff_salvage.
    # Set via OPENSQUILLA_FINAL_DIFF_SALVAGE_VETO.
    final_diff_salvage_veto: bool = False
    # Endgame git freeze exemption: a frozen revert whose targeted diff is
    # instrumentation-only (added print/log lines, nothing removed) is allowed
    # through — cleaning up diagnostic output is what the wrap-up window is
    # for. Off by default; only meaningful with the freeze margin. Set via
    # OPENSQUILLA_ENDGAME_GIT_FREEZE_INSTRUMENTATION_EXEMPT.
    endgame_git_freeze_instrumentation_exempt: bool = False
    # Make the wrap-up preempt's thinking-off sticky: when the wrap-up
    # directive preempts a reasoning stream, disable thinking for every
    # remaining provider call this turn instead of the next call only. Off by
    # default. Set via OPENSQUILLA_DEADLINE_WRAPUP_STICKY_THINKING_OFF.
    deadline_wrapup_sticky_thinking_off: bool = False
    # One-shot endgame fix directive. 0 = off. When positive, a total turn
    # timeout is configured, and remaining wall-clock time drops below this
    # many seconds while the workspace still shows no source change beyond
    # diagnostic instrumentation, a single user message directs the model to
    # commit to its best-supported fix now. Set via
    # OPENSQUILLA_ENDGAME_FIX_DIRECTIVE_MARGIN_SECONDS.
    endgame_fix_directive_margin_seconds: int = 0
    # Inject an act-now user message when a provider response is reasoning
    # only (no visible text, no tool calls) and grant one extra retry for that
    # failure kind. Off by default (the bare retry re-requests with nothing
    # added). Set via OPENSQUILLA_REASONING_ONLY_ACT_NOW.
    reasoning_only_act_now: bool = False
    # Mid-budget progress nudges. Off by default. When enabled and the turn
    # has a wall-clock budget (timeout > 0), a one-shot user message is
    # appended after tool results the first time elapsed time crosses 50% and
    # again at 75% of the budget while the workspace shows no change yet (no
    # write receipts, no captured diff candidates, empty live workspace
    # diff). Set via OPENSQUILLA_MID_BUDGET_NO_DIFF_NUDGE.
    mid_budget_no_diff_nudge: bool = False
    # Provider-view dedup of byte-identical repeated tool results. Off by
    # default. When enabled, older duplicate tool_result payloads (same content
    # emitted N+ times across iterations) are replaced in the provider request
    # projection with a compact back-reference to the surviving newest copy;
    # persisted history is never mutated. Set via
    # OPENSQUILLA_PROVIDER_HISTORY_DEDUP.
    provider_history_dedup_enabled: bool = False
    # Minimum number of byte-identical copies of a tool result before dedup
    # elides the older ones (keeps the newest copy full). Set via
    # OPENSQUILLA_PROVIDER_HISTORY_DEDUP_MIN_REPEATS.
    provider_history_dedup_min_repeats: int = 2
    # Append a failure-signal scan header to tool-result projection notices:
    # the omitted region of the original output is scanned for failure-pattern
    # lines and the notice gains a signal_scan summary plus a ready-to-copy
    # retrieve_tool_result call for the first match. Off by default; enabled
    # via OPENSQUILLA_PROJECTION_SIGNAL_HINTS.
    projection_signal_hints: bool = False
    tool_loop_observer_mode: Literal["off", "log"] = "off"
    runtime_recovery_mode: Literal["off", "log", "warn_model"] = "log"
    runtime_recovery_source_loop_max_nudges: int = 1
    final_diff_contract_mode: Literal["off", "log", "warn_model"] = "log"
    source_diff_preservation_mode: Literal["off", "log", "block"] = "log"
    source_diff_candidate_mode: Literal["off", "log", "warn_model"] = "log"
    runtime_state_capsule_mode: Literal["off", "log", "inject"] = "off"
    post_tool_empty_recovery_mode: Literal["off", "log", "warn_model"] = "log"
    text_only_tool_recovery_mode: Literal["off", "log", "warn_model"] = "off"
    reasoning_prefill_recovery_mode: Literal["off", "log", "recover"] = "log"
    runtime_events_path: str | None = None
    tool_result_store_dir: str | None = None
    tool_result_store_session_id: str | None = None
    tool_result_store_session_key: str | None = None
    tool_result_store_agent_id: str | None = None
    tool_result_store_full_trace: bool = False
    tool_result_store_max_bytes: int | None = 8 * 1024 * 1024
    tool_result_store_disk_budget_bytes: int | None = 256 * 1024 * 1024
    tool_result_store_retention_seconds: int | None = 7 * 24 * 60 * 60
    # Optional gateway-injected observer invoked once per provider call with
    # keyword args (provider_id, model, ttft_ms, duration_ms, ok,
    # failure_kind). The agent loop swallows observer errors so the engine
    # stays gateway-agnostic and a broken observer can never affect a turn.
    provider_call_observer: Callable[..., None] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.flush_triggers = list(normalize_flush_triggers_strict(self.flush_triggers))
        if self.provider_request_proof_max_chars_explicit is None:
            self.provider_request_proof_max_chars_explicit = (
                int(self.provider_request_proof_max_chars or 0) > 0
            )
        self.compaction_protected_recent_messages = max(
            0,
            int(self.compaction_protected_recent_messages or 0),
        )
        if float(self.compaction_total_timeout_seconds or 0) <= 0:
            self.compaction_total_timeout_seconds = 120.0
        if float(self.compaction_heartbeat_interval_seconds or 0) <= 0:
            self.compaction_heartbeat_interval_seconds = 15.0

    def resolve_thinking(self, prompt: str | None = None) -> tuple[bool, int]:
        """Return (enabled, budget_tokens) based on the thinking field.

        Rules:
        - False          → (False, 0)
        - True           → same as ThinkingLevel.MEDIUM → (True, 10000)
        - ThinkingLevel.OFF      → (False, 0)
        - ThinkingLevel.ADAPTIVE → estimate from prompt length
        - Other levels   → (True, THINKING_BUDGETS[level])

        If thinking_budget_tokens is explicitly set (not the default sentinel),
        it overrides the resolved budget (but not the enabled flag).
        """
        thinking = self.thinking

        if thinking is False:
            return (False, 0)

        if thinking is True:
            enabled, budget = True, THINKING_BUDGETS[ThinkingLevel.MEDIUM]
        elif thinking == ThinkingLevel.OFF:
            return (False, 0)
        elif thinking == ThinkingLevel.ADAPTIVE:
            enabled = True
            if prompt is not None:
                estimated_tokens = len(prompt) // _CHARS_PER_TOKEN
                # Clamp between MINIMAL and XHIGH
                budget = max(
                    THINKING_BUDGETS[ThinkingLevel.MINIMAL],
                    min(estimated_tokens, THINKING_BUDGETS[ThinkingLevel.XHIGH]),
                )
            else:
                budget = _ADAPTIVE_DEFAULT_TOKENS
        else:
            enabled = True
            budget = THINKING_BUDGETS[thinking]

        # Override budget if explicitly set by caller
        if self.thinking_budget_tokens != _THINKING_BUDGET_DEFAULT:
            budget = self.thinking_budget_tokens

        return (enabled, budget)
