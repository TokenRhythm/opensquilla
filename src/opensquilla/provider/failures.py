"""Provider failure classification and runtime recovery decisions.

Classification is table-driven: each ``failure_family`` maps to an ordered
tuple of :class:`FailureMatcher` rows, walked after a shared pre-pass and
before a shared generic tail. Adding coverage for a new backend's error text
is a data change (a new matcher row), not a new branch.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

import structlog

from opensquilla.http_retry import parse_retry_after
from opensquilla.redaction import redact_error_text

from .registry import UnknownProviderError, get_provider_spec

log = structlog.get_logger(__name__)


class ProviderFailureKind(StrEnum):
    RATE_LIMITED = "rate_limited"
    PROVIDER_OVERLOADED = "provider_overloaded"
    AUTH_INVALID = "auth_invalid"
    CONTEXT_OVERFLOW = "context_overflow"
    UNSUPPORTED_FEATURE = "unsupported_feature"
    INSUFFICIENT_CREDITS = "insufficient_credits"
    MODEL_NOT_FOUND = "model_not_found"
    TRANSPORT_TRANSIENT = "transport_transient"
    POLICY_REFUSAL = "policy_refusal"
    EMPTY_RESPONSE = "empty_response"
    MALFORMED_RESPONSE = "malformed_response"
    BAD_REQUEST = "bad_request"
    UNKNOWN = "unknown"


class ProviderRecoveryAction(StrEnum):
    RETRY = "retry"
    RETRY_THEN_FALLBACK = "retry_then_fallback"
    FALLBACK_PROVIDER = "fallback_provider"
    COMPACT_AND_RETRY = "compact_and_retry"
    FAIL_CONFIG = "fail_config"
    SURFACE = "surface"


def _failure_family(provider: str) -> str:
    """Return the provider's registry failure family, or '' if unknown.

    Keeps error classification in sync with the registry instead of a
    hand-maintained provider set: a new provider classifies correctly the
    moment its ProviderSpec declares a failure_family.
    """
    try:
        return get_provider_spec(provider).failure_family
    except UnknownProviderError:
        return ""

_GATEWAY_TRANSIENT_STATUS_CODES = frozenset(
    {499, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}
)
_GATEWAY_CODES = r"(?:499|500|502|503|504|520|521|522|523|524|529)"
_GATEWAY_CONTEXT = r"(?:cloudflare|openrouter|upstream|gateway|backend)"
_GATEWAY_ERROR_TERMS = (
    r"(?:error|returned|returning|failed|failure|unreachable|timeout|timed out|"
    r"overload(?:ed)?|bad gateway|origin)"
)
_GATEWAY_TRANSIENT_RE = re.compile(
    r"\b(?:http(?: status)?|status(?:[_ -]?code)?|error code|code)\s*[:=]?\s*"
    rf"{_GATEWAY_CODES}\b"
    rf"|\b{_GATEWAY_CONTEXT}\b[^\n]{{0,80}}\b{_GATEWAY_ERROR_TERMS}\b[^\n]{{0,80}}\b{_GATEWAY_CODES}\b"
    rf"|\b{_GATEWAY_CONTEXT}\b[^\n]{{0,80}}\b{_GATEWAY_CODES}\b[^\n]{{0,80}}\b{_GATEWAY_ERROR_TERMS}\b"
    rf"|\b{_GATEWAY_CODES}\b[^\n]{{0,80}}\b{_GATEWAY_CONTEXT}\b[^\n]{{0,80}}\b{_GATEWAY_ERROR_TERMS}\b"
)


def _joined(status_code: int | None, raw_code: str, message: str) -> str:
    return f"{status_code or ''} {raw_code or ''} {message or ''}".lower()


@dataclass(frozen=True)
class FailureSignal:
    """One provider error, pre-normalized for matcher evaluation."""

    status_code: int | None
    raw_code: str  # stripped + lowercased
    message: str  # raw, untouched
    text: str  # "{status} {raw_code} {message}" lowercased


@dataclass(frozen=True)
class FailureMatcher:
    """One data row of the classification tables.

    A matcher matches when every *declared* constraint holds (empty
    constraints are wildcards): ``status_codes``/``raw_codes`` are set
    membership, ``message_substrings`` is any-of over the joined lowercased
    text, ``message_substrings_all`` is all-of over the same text (for
    conjunctions like ollama's "pull" + "model"), and ``predicate`` is a
    named escape hatch for the few checks that are not expressible as
    substring/status data (exact-match empty-response shapes, the
    gateway-transient regex). An original ``status OR substring`` branch
    becomes two adjacent rows with the same kind.
    """

    kind: ProviderFailureKind
    status_codes: frozenset[int] = frozenset()
    raw_codes: frozenset[str] = frozenset()
    message_substrings: tuple[str, ...] = ()
    message_substrings_all: tuple[str, ...] = ()
    predicate: Callable[[FailureSignal], bool] | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not (
            self.status_codes
            or self.raw_codes
            or self.message_substrings
            or self.message_substrings_all
            or self.predicate is not None
        ):
            raise ValueError("FailureMatcher needs at least one constraint (would match all)")

    def matches(self, signal: FailureSignal) -> bool:
        if self.status_codes and signal.status_code not in self.status_codes:
            return False
        if self.raw_codes and signal.raw_code not in self.raw_codes:
            return False
        if self.message_substrings and not any(
            marker in signal.text for marker in self.message_substrings
        ):
            return False
        if self.message_substrings_all and not all(
            marker in signal.text for marker in self.message_substrings_all
        ):
            return False
        return self.predicate is None or self.predicate(signal)


def _is_empty_response(signal: FailureSignal) -> bool:
    """Named predicate: exact-match (not substring) empty-response shapes.

    A substring rule would misfire on transport noise such as
    "HTTP 500: empty response body", which must stay gateway-transient.
    """
    return signal.raw_code == "empty_response" or signal.message.strip().lower() in {
        "empty_response",
        "empty response",
        "provider returned an empty response",
    }


def _is_gateway_transient(signal: FailureSignal) -> bool:
    """Named predicate: gateway status codes scoped by context words.

    A regex (not substrings) so that unscoped numbers like "520 tokens" do
    not classify as transient.
    """
    return bool(_GATEWAY_TRANSIENT_RE.search(signal.text))


_MODEL_UNAVAILABLE_SUBSTRINGS = (
    "no endpoints found",
    "model not found",
    "model is not available",
    "model not available",
    "not available in your region",
    "not available in the requested region",
    # TokenRhythm's non-OpenAI envelope: top-level code MODEL_NOT_AVAILABLE
    # with a localized Chinese message ("模型不可用：<model>").
    "model_not_available",
    "模型不可用",
)

# Family-independent kinds checked before any family table.
_SHARED_PRE_MATCHERS: tuple[FailureMatcher, ...] = (
    # Local composite-provider validation.  This must classify before a
    # provider-family "does not support" matcher can turn it into
    # UNSUPPORTED_FEATURE: that kind deliberately hops to a fallback provider,
    # which would bypass Ensemble's text-only contract.
    FailureMatcher(
        ProviderFailureKind.BAD_REQUEST,
        raw_codes=frozenset({"ensemble_multimodal_unsupported"}),
    ),
    FailureMatcher(
        ProviderFailureKind.CONTEXT_OVERFLOW,
        message_substrings=(
            "context length",
            "context window",
            "maximum context",
            "prompt is too long",
            "input is too long",
            "input exceeds",
            "provider_request_budget_exhausted",
            "too many tokens",
        ),
    ),
    FailureMatcher(
        ProviderFailureKind.POLICY_REFUSAL,
        message_substrings=(
            "content policy",
            "policy violation",
            "safety policy",
            "moderation",
            "refusal",
            "blocked by policy",
        ),
    ),
    FailureMatcher(ProviderFailureKind.EMPTY_RESPONSE, predicate=_is_empty_response),
)

FAILURE_TABLES: dict[str, tuple[FailureMatcher, ...]] = {
    "openai_compat": (
        FailureMatcher(ProviderFailureKind.MODEL_NOT_FOUND, status_codes=frozenset({404})),
        FailureMatcher(
            ProviderFailureKind.MODEL_NOT_FOUND,
            message_substrings=_MODEL_UNAVAILABLE_SUBSTRINGS,
        ),
        FailureMatcher(ProviderFailureKind.AUTH_INVALID, status_codes=frozenset({401, 403})),
        FailureMatcher(
            ProviderFailureKind.AUTH_INVALID,
            message_substrings=("invalid api key", "unauthorized"),
        ),
        FailureMatcher(ProviderFailureKind.INSUFFICIENT_CREDITS, status_codes=frozenset({402})),
        FailureMatcher(
            ProviderFailureKind.INSUFFICIENT_CREDITS,
            message_substrings=("insufficient credits", "no credits"),
        ),
        FailureMatcher(ProviderFailureKind.RATE_LIMITED, status_codes=frozenset({429})),
        FailureMatcher(
            ProviderFailureKind.RATE_LIMITED,
            message_substrings=("rate limit", "rate_limit"),
        ),
        FailureMatcher(
            ProviderFailureKind.UNSUPPORTED_FEATURE,
            message_substrings=("does not support", "unsupported"),
        ),
        FailureMatcher(
            ProviderFailureKind.PROVIDER_OVERLOADED,
            status_codes=_GATEWAY_TRANSIENT_STATUS_CODES,
        ),
        FailureMatcher(ProviderFailureKind.PROVIDER_OVERLOADED, message_substrings=("overloaded",)),
        FailureMatcher(ProviderFailureKind.PROVIDER_OVERLOADED, predicate=_is_gateway_transient),
        FailureMatcher(ProviderFailureKind.BAD_REQUEST, status_codes=frozenset({400})),
        FailureMatcher(ProviderFailureKind.BAD_REQUEST, message_substrings=("invalid_request",)),
    ),
    "anthropic": (
        FailureMatcher(ProviderFailureKind.MODEL_NOT_FOUND, status_codes=frozenset({404})),
        FailureMatcher(
            ProviderFailureKind.MODEL_NOT_FOUND,
            message_substrings=("not_found_error", *_MODEL_UNAVAILABLE_SUBSTRINGS),
        ),
        FailureMatcher(ProviderFailureKind.AUTH_INVALID, status_codes=frozenset({401, 403})),
        FailureMatcher(
            ProviderFailureKind.AUTH_INVALID,
            message_substrings=("authentication_error",),
        ),
        FailureMatcher(ProviderFailureKind.INSUFFICIENT_CREDITS, status_codes=frozenset({402})),
        FailureMatcher(
            ProviderFailureKind.INSUFFICIENT_CREDITS,
            message_substrings=("credit balance",),
        ),
        FailureMatcher(ProviderFailureKind.RATE_LIMITED, status_codes=frozenset({429})),
        FailureMatcher(
            ProviderFailureKind.RATE_LIMITED,
            message_substrings=("rate_limit_error",),
        ),
        FailureMatcher(
            ProviderFailureKind.PROVIDER_OVERLOADED,
            status_codes=_GATEWAY_TRANSIENT_STATUS_CODES,
        ),
        FailureMatcher(
            ProviderFailureKind.PROVIDER_OVERLOADED,
            message_substrings=("overloaded_error",),
        ),
        FailureMatcher(
            ProviderFailureKind.BAD_REQUEST,
            message_substrings=("invalid_request_error",),
        ),
    ),
    "ollama": (
        FailureMatcher(
            ProviderFailureKind.MODEL_NOT_FOUND,
            message_substrings=("model not found",),
        ),
        FailureMatcher(
            ProviderFailureKind.MODEL_NOT_FOUND,
            message_substrings_all=("pull", "model"),
        ),
        # Ollama Cloud / secured remote hosts return standard auth statuses;
        # without these rows a 401 fell through to UNKNOWN.
        FailureMatcher(ProviderFailureKind.AUTH_INVALID, status_codes=frozenset({401, 403})),
        FailureMatcher(ProviderFailureKind.AUTH_INVALID, message_substrings=("unauthorized",)),
        FailureMatcher(
            ProviderFailureKind.TRANSPORT_TRANSIENT,
            message_substrings=(
                "connection refused",
                "connection error",
                "request error",
                "timeout",
            ),
        ),
    ),
}

# Generic tail: runs for every family (and for providers with no family)
# after the family table missed.
_SHARED_TAIL_MATCHERS: tuple[FailureMatcher, ...] = (
    FailureMatcher(ProviderFailureKind.RATE_LIMITED, status_codes=frozenset({429})),
    FailureMatcher(ProviderFailureKind.RATE_LIMITED, message_substrings=("rate limit",)),
    FailureMatcher(
        ProviderFailureKind.PROVIDER_OVERLOADED,
        status_codes=_GATEWAY_TRANSIENT_STATUS_CODES,
    ),
    FailureMatcher(ProviderFailureKind.PROVIDER_OVERLOADED, predicate=_is_gateway_transient),
    FailureMatcher(
        ProviderFailureKind.MALFORMED_RESPONSE,
        raw_codes=frozenset({"invalid_stream_frame", "invalid_stream_order"}),
    ),
    # Adapter-synthesized terminal-evidence codes: the stream ended without a
    # finish signal, or a native tool call arrived without usable arguments.
    # Both are transient from the runtime's point of view — a retry (and then
    # a fallback provider) can succeed where surfacing a terminal error
    # cannot. Must precede the "malformed" substring row: some adapters phrase
    # incomplete tool calls as "malformed tool calls", and MALFORMED_RESPONSE
    # would downgrade recovery to SURFACE.
    FailureMatcher(
        ProviderFailureKind.TRANSPORT_TRANSIENT,
        raw_codes=frozenset({"incomplete_stream", "incomplete_tool_call"}),
    ),
    FailureMatcher(
        ProviderFailureKind.MALFORMED_RESPONSE,
        message_substrings=("malformed", "invalid json"),
    ),
    FailureMatcher(
        ProviderFailureKind.TRANSPORT_TRANSIENT,
        message_substrings=("timeout", "request error"),
    ),
)


def classify_provider_error(
    provider_name: str,
    status_code: int | None,
    raw_code: str = "",
    message: str = "",
) -> ProviderFailureKind:
    """Classify a provider error into a stable runtime failure kind."""

    provider = (provider_name or "").lower()
    family = _failure_family(provider)
    signal = FailureSignal(
        status_code=status_code,
        raw_code=(raw_code or "").strip().lower(),
        message=message or "",
        text=_joined(status_code, raw_code, message),
    )

    for table in (_SHARED_PRE_MATCHERS, FAILURE_TABLES.get(family, ()), _SHARED_TAIL_MATCHERS):
        for matcher in table:
            if matcher.matches(signal):
                return matcher.kind

    # UNKNOWN downgrades recovery to SURFACE, so make every miss observable:
    # a field report of this event is exactly one new FailureMatcher row.
    log.warning(
        "provider_failure.unclassified",
        provider=provider,
        failure_family=family,
        status_code=status_code,
        raw_code=raw_code,
        message_head=redact_error_text(message),
    )
    return ProviderFailureKind.UNKNOWN


def retry_after_from_headers(
    status_code: int,
    headers: Mapping[str, str] | None,
) -> float | None:
    """``Retry-After`` seconds for a 429/5xx response, else ``None``.

    Guarded on the status code so a stray ``Retry-After`` on, say, a 404
    never feeds cooldown machinery; parsing itself is :func:`parse_retry_after`.
    Duck-typed and defensive: a headerless response object (``None``) or a
    non-mapping stand-in yields ``None`` instead of raising into the
    adapter's error path.
    """
    if status_code != 429 and status_code < 500:
        return None
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    try:
        value = getter("retry-after")
    except Exception:  # noqa: BLE001 — header access must never break error handling
        return None
    return parse_retry_after(value if isinstance(value, str) else None)


def decide_recovery_action(kind: ProviderFailureKind) -> ProviderRecoveryAction:
    """Map classified provider failure to the first runtime recovery action."""

    if kind is ProviderFailureKind.CONTEXT_OVERFLOW:
        return ProviderRecoveryAction.COMPACT_AND_RETRY
    if kind in {
        ProviderFailureKind.PROVIDER_OVERLOADED,
        ProviderFailureKind.TRANSPORT_TRANSIENT,
    }:
        return ProviderRecoveryAction.RETRY_THEN_FALLBACK
    if kind in {
        ProviderFailureKind.RATE_LIMITED,
        ProviderFailureKind.INSUFFICIENT_CREDITS,
        ProviderFailureKind.MODEL_NOT_FOUND,
        ProviderFailureKind.UNSUPPORTED_FEATURE,
    }:
        return ProviderRecoveryAction.FALLBACK_PROVIDER
    if kind is ProviderFailureKind.AUTH_INVALID:
        return ProviderRecoveryAction.FAIL_CONFIG
    return ProviderRecoveryAction.SURFACE
