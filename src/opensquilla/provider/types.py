"""Provider type definitions: stream events, model info, config."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

from opensquilla.execution_status import ExecutionStatus

if TYPE_CHECKING:
    from opensquilla.provider.failures import ProviderFailureKind

# Provider APIs use all three literals for the same terminal condition: the
# completion budget was exhausted before any visible answer was emitted.
# Keeping the vocabulary here prevents inner-ensemble recovery and outer
# campaign retry/audit code from drifting apart.
REASONING_ONLY_LENGTH_STOP_REASONS = frozenset(
    {"length", "max_tokens", "max_output_tokens"}
)
OPERATIONAL_ERROR_SCHEMA_VERSION = "opensquilla.operational-error/v1"


def make_operational_error(
    code: str,
    *,
    retryable: bool,
) -> dict[str, Any]:
    """Build the exact machine-readable terminal error contract."""

    normalized_code = code.strip()
    if not normalized_code:
        raise ValueError("operational error code must not be empty")
    return {
        "schema_version": OPERATIONAL_ERROR_SCHEMA_VERSION,
        "code": normalized_code,
        "retryable": bool(retryable),
        "terminal": True,
    }

# ---------------------------------------------------------------------------
# Stream event dataclasses
# ---------------------------------------------------------------------------


@dataclass
class TextDeltaEvent:
    """A chunk of assistant text."""

    kind: Literal["text_delta"] = field(default="text_delta", init=False)
    text: str = ""


@dataclass
class ReasoningDeltaEvent:
    """A chunk of model reasoning/thinking, streamed in real time.

    Distinct from TextDeltaEvent: reasoning is the model's private thinking,
    not the final answer. Emitting it as its own event lets every layer keep
    the two apart from the source, so the renderer never has to guess a block's
    identity after the fact. The concatenation of these deltas equals
    DoneEvent.reasoning_content, which remains the source of truth for non-TUI
    consumers (signature replay, persistence, compaction, cost).
    """

    kind: Literal["reasoning_delta"] = field(default="reasoning_delta", init=False)
    text: str = ""


@dataclass
class ToolUseStartEvent:
    """LLM begins a tool call."""

    kind: Literal["tool_use_start"] = field(default="tool_use_start", init=False)
    tool_use_id: str = ""
    tool_name: str = ""
    synthetic_from_text: bool = False


@dataclass
class ToolUseDeltaEvent:
    """Streaming fragment of tool call arguments (JSON)."""

    kind: Literal["tool_use_delta"] = field(default="tool_use_delta", init=False)
    tool_use_id: str = ""
    json_fragment: str = ""


@dataclass
class ToolUseEndEvent:
    """Tool call argument stream complete."""

    kind: Literal["tool_use_end"] = field(default="tool_use_end", init=False)
    tool_use_id: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    synthetic_from_text: bool = False


@dataclass(frozen=True)
class ProviderBillingReceipt:
    """Provider-native billing receipt attached to one physical request.

    Amounts use integer nanos so adapters can preserve an upstream bill without
    routing it through binary floating-point arithmetic. ``billed_cost`` on the
    enclosing :class:`DoneEvent` remains the canonical USD-compatible value for
    existing callers.
    """

    currency: str
    status: Literal["confirmed", "pending"]
    amount_nanos: int | None
    usd_equivalent_nanos: int | None
    fx_native_per_usd_nanos: int
    schema_version: int = 1


@dataclass
class DoneEvent:
    """Stream finished successfully."""

    kind: Literal["done"] = field(default="done", init=False)
    stop_reason: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_content: str | None = None
    thinking_signature: str | None = None
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    billed_cost: float = 0.0
    model: str = ""
    # New fields appended at the end so positional construction in callers and
    # tests does not silently shift earlier args.
    cache_write_tokens: int = 0
    cost_source: str = "none"
    model_usage_breakdown: list[dict[str, Any]] = field(default_factory=list)
    ensemble_trace: dict[str, Any] | None = None
    # Number of physical ensemble legs that ended without a usage receipt.
    # Appended for source compatibility with positional constructors.
    usage_missing_count: int = 0
    # Physical provider identity reported for this response.  Keep it empty
    # when the adapter has no authoritative evidence; configured/requested
    # identity belongs in ``requested_provider`` below.
    provider: str = ""
    # Provider-native receipt for this physical request. Ensemble envelopes do
    # not carry a synthetic receipt; their physical breakdown rows do.
    billing_receipt: ProviderBillingReceipt | None = None
    # Raw, non-secret provider evidence associated with this physical request.
    # OpenRouter benchmark runs use this for explicit BYOK and router-metadata
    # attestation; callers must still treat unknown additive keys permissively.
    provider_usage: dict[str, Any] = field(default_factory=dict)
    # Requested/configured identity is not physical response evidence. Keep it
    # separate so callers never need to fill an absent actual model/provider
    # with configuration values.
    requested_model: str = ""
    requested_provider: str = ""
    @property
    def upstream_cost_usd(self) -> float:
        """Backward-compatible alias for earlier OpenRouter cost consumers."""
        return self.billed_cost


@dataclass(frozen=True)
class ProviderMessageCountProjection:
    """Pure projection of one provider request's final wire-message count.

    ``additional_messages`` lets wrappers account for provider-local synthetic
    messages (for example, an ensemble aggregator's candidate bundle) without
    manufacturing ``Message`` objects or duplicating an adapter's expansion
    rules.
    """

    actual_wire_messages: int
    logical_messages: int
    system_messages: int
    tool_result_messages: int
    additional_messages: int = 0
    provider_kind: str = ""
    model: str = ""
    base_host: str = ""


@dataclass(frozen=True)
class ProviderMessageLimitProof:
    """Structured proof of an upstream wire-message cardinality rejection."""

    actual_wire_messages: int
    limit: int
    logical_messages: int
    system_messages: int
    tool_result_messages: int
    provider_kind: str = ""
    model: str = ""
    base_host: str = ""


@dataclass
class ErrorEvent:
    """Stream error.

    ``retry_after_s`` is the provider's ``Retry-After`` hint (parsed to
    seconds) for 429/5xx responses, when the adapter saw one; ``None``
    otherwise. Additive: consumers that ignore it behave exactly as before.
    """

    kind: Literal["error"] = field(default="error", init=False)
    message: str = ""
    code: str = ""
    retry_after_s: float | None = None
    # Appended so positional construction of the long-standing fields remains
    # source-compatible.  Only populated when an adapter has exact structured
    # evidence for a provider request limit.
    message_limit_proof: ProviderMessageLimitProof | None = None
    # Ensemble wrappers may have consumed billable legs before the terminal
    # failure.  Carry their known receipts so outer accounting can finalize a
    # partial envelope instead of discarding it as wholly unknown.
    model_usage_breakdown: list[dict[str, Any]] = field(default_factory=list)
    usage_missing_count: int = 0
    # Optional exact usage evidence for a physical request that was billed but
    # must still fail (for example, missing required OpenRouter router
    # metadata).  Runners can account for that request without treating it as
    # a successful completion.
    diagnostic_done: DoneEvent | None = None
    # Optional ensemble execution trace for terminal composite failures.  Kept
    # at the end for positional-construction compatibility.
    ensemble_trace: dict[str, Any] | None = None
    # Physical-request evidence for failures that happen before a DoneEvent.
    # ``None`` means the adapter cannot distinguish local preflight failure
    # from an upstream request. Explicit ``False``/``0`` proves that no
    # billable request started; fallback wrappers may report counts > 1.
    request_started: bool | None = None
    physical_request_count: int | None = None
    # Exact terminal contract consumed by automation. Generic provider errors
    # leave this unset; only callers that deliberately opt into the v1
    # operational-error protocol may populate it.
    operational_error: dict[str, Any] | None = None

@dataclass
class ProviderHeartbeatEvent:
    """Provider-side liveness signal while no user-visible tokens are ready."""

    kind: Literal["provider_heartbeat"] = field(
        default="provider_heartbeat", init=False
    )
    phase: str = "provider"
    message: str = ""


@dataclass
class EnsembleProgressEvent:
    """Mid-turn LLM-ensemble lifecycle signal — one proposer/aggregator started
    or finished. Lets the UI reveal ensemble members incrementally before the
    terminal ``DoneEvent`` lands with the full breakdown."""

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


@dataclass
class QuotaStatus:
    """Quota snapshot returned by ``quota_hook``.

    ``-1`` on either counter is the sentinel for "unlimited / not enforced";
    ``abort_reason`` is user-facing and surfaces verbatim in the graceful
    abort payload when the caller chooses to short-circuit the turn.
    """

    tokens_remaining: int = -1
    tool_calls_remaining: int = -1
    abort_reason: str | None = None


@dataclass(frozen=True)
class ModelCapabilities:
    """Per-model capability flags resolved from ModelCatalog."""

    supports_reasoning: bool = False
    supports_tools: bool = True
    supports_streaming: bool = True
    supports_vision: bool = False
    reasoning_format: str = "none"
    # "none" | "openrouter" | "openrouter_explicit_off" | "deepseek" |
    # "gemini" | "zai" | "dashscope" | "moonshot" | "volcengine" |
    # "tencent_tokenhub" | "think_tags"


StreamEvent = (
    TextDeltaEvent
    | ReasoningDeltaEvent
    | ToolUseStartEvent
    | ToolUseDeltaEvent
    | ToolUseEndEvent
    | DoneEvent
    | ErrorEvent
    | ProviderHeartbeatEvent
    | EnsembleProgressEvent
)


# ---------------------------------------------------------------------------
# Tool definition (Pydantic BaseModel — external API boundary)
# ---------------------------------------------------------------------------

from pydantic import BaseModel, ConfigDict, Field  # noqa: E402


class ToolParam(BaseModel):
    """Single parameter in a tool schema."""

    type: str
    description: str = ""
    enum: list[str] | None = None


class ToolInputSchema(BaseModel):
    """JSON schema for tool inputs."""

    model_config = ConfigDict(populate_by_name=True)

    type: Literal["object"] = "object"
    properties: dict[str, Any] = {}
    required: list[str] = []
    additional_properties: bool | dict[str, Any] | None = Field(
        default=None,
        alias="additionalProperties",
    )


class ToolDefinition(BaseModel):
    """Tool definition passed to the LLM."""

    name: str
    description: str
    input_schema: ToolInputSchema
    execution_timeout_seconds: float | None = None
    execution_timeout_argument: str | None = None
    execution_timeout_padding: float = 0.0


# ---------------------------------------------------------------------------
# Model info (Pydantic BaseModel — registry / API responses)
# ---------------------------------------------------------------------------


class ModelInfo(BaseModel):
    """Metadata about an available model."""

    provider: str
    model_id: str
    display_name: str = ""
    context_window: int = 0
    max_output_tokens: int = 0
    supports_reasoning: bool = False
    supports_tools: bool = True
    supports_streaming: bool = True
    supports_vision: bool = False
    input_cost_per_1k: float = 0.0
    output_cost_per_1k: float = 0.0


# ---------------------------------------------------------------------------
# Chat config (Pydantic BaseModel — call-time settings)
# ---------------------------------------------------------------------------


class ChatConfig(BaseModel):
    """Runtime options for a single chat call."""

    max_tokens: int = 16384
    temperature: float | None = None
    top_p: float | None = None
    system: str | None = None
    stop_sequences: list[str] = []
    thinking: bool = False
    thinking_budget_tokens: int = 5000
    thinking_budget_explicit: bool | None = None
    timeout: float = 120.0
    # Prompt caching: when set, system prompt is split into cached/dynamic blocks
    cache_breakpoints: list[dict[str, str]] | None = None
    cache_mode: Literal["off", "auto", "on"] = "off"
    output_json_schema: dict[str, Any] | None = None
    output_json_schema_strict: bool = True
    model_capabilities: ModelCapabilities | None = None
    thinking_level: Any | None = None
    provider_request_max_chars: int = 0
    tool_choice: Any | None = None
    candidate_output_mode: Literal["normal", "inert_artifact"] = Field(
        default="normal",
        exclude=True,
        repr=False,
    )
    ensemble_execution_mode: Literal["full", "aggregator_only"] = Field(
        default="full",
        exclude=True,
        repr=False,
    )
    # Ensemble-internal graceful-finalization controls.  These are deliberately
    # excluded from adapter payloads and are consumed only by a full
    # EnsembleProvider call; downstream member requests receive the defaults.
    ensemble_soft_deadline_seconds: float = Field(
        default=0.0,
        exclude=True,
        repr=False,
    )
    ensemble_soft_deadline_disable_tools: bool = Field(
        default=False,
        exclude=True,
        repr=False,
    )
    ensemble_soft_deadline_disable_thinking: bool = Field(
        default=False,
        exclude=True,
        repr=False,
    )
    # Ensemble-internal physical-request guard. Some OpenAI-compatible
    # adapters transparently retry an empty/timed-out stream as a second
    # non-stream request. Serving recovery disables that adapter-level retry
    # so its one-extra-request budget remains a real network-request bound.
    allow_provider_stream_fallback: bool = Field(
        default=True,
        exclude=True,
        repr=False,
    )

    def model_post_init(self, __context: Any) -> None:
        if self.thinking_budget_explicit is None:
            self.thinking_budget_explicit = (
                "thinking_budget_tokens" in self.model_fields_set
            )


# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------


class ContentBlockText(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ContentBlockToolUse(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]


class ContentBlockToolResult(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str | list[Any]
    is_error: bool = False
    execution_status: ExecutionStatus | None = None


class ContentBlockImage(BaseModel):
    type: Literal["image"] = "image"
    source_type: Literal["base64", "url"] = "base64"
    media_type: str  # "image/png", "image/jpeg", etc.
    data: str  # base64 data or URL


class ContentBlockDocument(BaseModel):
    type: Literal["document"] = "document"
    source_type: Literal["base64"] = "base64"
    media_type: Literal["application/pdf"]
    data: str
    title: str | None = None


class ContentBlockThinking(BaseModel):
    type: Literal["thinking"] = "thinking"
    thinking: str = ""
    signature: str | None = None


class ContentBlockCompaction(BaseModel):
    type: Literal["compaction"] = "compaction"
    content: str | None = None
    cache_control: dict[str, Any] | None = None


MessageContent = (
    str
    | list[
        ContentBlockText
        | ContentBlockToolUse
        | ContentBlockToolResult
        | ContentBlockImage
        | ContentBlockDocument
        | ContentBlockThinking
        | ContentBlockCompaction
    ]
)


class Message(BaseModel):
    """A single conversation message."""

    role: Literal["user", "assistant"]
    content: MessageContent
    reasoning_content: str | None = None


# ---------------------------------------------------------------------------
# Offline failure injection (test-only seam)
# ---------------------------------------------------------------------------


def synthetic_failure_event(kind: ProviderFailureKind) -> ErrorEvent:
    """Build the canonical synthetic ``ErrorEvent`` for an injected failure kind.

    Every shape is a synthetic dummy (never copied from real provider
    traffic) chosen so that ``classify_provider_error`` maps it back to
    ``kind`` for providers in the ``openai_compat`` failure family — the
    round trip is pinned by tests/test_provider/test_failure_injection.py.
    Family-scoped kinds (AUTH_INVALID, BAD_REQUEST, INSUFFICIENT_CREDITS)
    only round-trip when the probed provider name resolves a failure family
    in the registry; fakes should declare a registered ``provider_name``
    such as ``"openai"``.
    """
    # Local import: this module is the lowest layer of the provider package
    # and must not depend on ``failures`` (→ ``registry``) at import time.
    from opensquilla.provider.failures import ProviderFailureKind as _Kind

    shapes: dict[_Kind, tuple[str, str]] = {
        _Kind.RATE_LIMITED: ("429", "injected rate limit"),
        _Kind.PROVIDER_OVERLOADED: ("529", "injected upstream overloaded"),
        _Kind.AUTH_INVALID: ("401", "injected invalid api key"),
        _Kind.CONTEXT_OVERFLOW: ("", "injected context window overflow"),
        _Kind.UNSUPPORTED_FEATURE: ("", "injected unsupported feature"),
        _Kind.INSUFFICIENT_CREDITS: ("402", "injected insufficient credits"),
        _Kind.MODEL_NOT_FOUND: ("404", "injected model not found"),
        _Kind.TRANSPORT_TRANSIENT: ("", "injected connection timeout"),
        _Kind.POLICY_REFUSAL: ("", "injected policy violation"),
        _Kind.EMPTY_RESPONSE: ("empty_response", "empty_response"),
        _Kind.MALFORMED_RESPONSE: ("", "injected malformed response payload"),
        _Kind.BAD_REQUEST: ("400", "injected invalid_request"),
        _Kind.UNKNOWN: ("", "injected unclassified failure"),
    }
    code, message = shapes[_Kind(kind)]
    return ErrorEvent(message=message, code=code)


@dataclass
class FailureInjector:
    """Scripted provider-failure seam for offline retry/fallback-chain tests.

    Test-only by contract: nothing in the runtime constructs one. The agent
    loop consults an injector only when a caller explicitly passes an
    instance (every constructor default is ``None``), so the production call
    path is untouched unless a test opts in — no environment variable or
    process-global can activate it, and each injector instance owns its own
    script, so nothing leaks between tests.

    ``script`` is consumed front-to-back, one entry per provider chat call:

    - ``"succeed"`` — delegate the call to the real provider untouched.
    - ``ProviderFailureKind`` — emit one synthetic ``ErrorEvent`` (see
      :func:`synthetic_failure_event`) without calling the provider.
    - ``Exception`` instance — raise it from the stream, exercising the
      transport-exception path.

    An exhausted script delegates every further call, so a test scripts only
    the hops it wants to drive. ``consumed`` records applied outcomes for
    assertions.
    """

    script: list[Literal["succeed"] | ProviderFailureKind | Exception] = field(
        default_factory=list
    )
    consumed: list[Literal["succeed"] | ProviderFailureKind | Exception] = field(
        default_factory=list, init=False
    )

    def next_outcome(self) -> Literal["succeed"] | ProviderFailureKind | Exception:
        """Pop the next scripted outcome; an exhausted script always succeeds."""
        if not self.script:
            return "succeed"
        outcome = self.script.pop(0)
        self.consumed.append(outcome)
        return outcome

    def chat(
        self,
        provider: Any,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: ChatConfig | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Apply the next scripted outcome to one provider chat call.

        ``"succeed"`` (and script exhaustion) returns ``provider.chat(...)``
        with identical arguments; a scripted failure yields exactly one
        synthetic ``ErrorEvent`` or raises the scripted exception instead of
        contacting the provider.
        """
        outcome = self.next_outcome()
        if outcome == "succeed":
            stream: AsyncIterator[StreamEvent] = provider.chat(
                messages, tools=tools, config=config
            )
            return stream
        # Equality above rules out the "succeed" literal (no failure kind
        # shares that value); help the type checker over the StrEnum overlap.
        failure = cast("ProviderFailureKind | Exception", outcome)
        return self._injected_stream(failure)

    @staticmethod
    async def _injected_stream(
        outcome: ProviderFailureKind | Exception,
    ) -> AsyncIterator[StreamEvent]:
        if isinstance(outcome, Exception):
            raise outcome
        yield synthetic_failure_event(outcome)
