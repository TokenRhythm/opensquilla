"""Parity corpus for provider failure classification.

This corpus was written against the branch-based ``classify_provider_error``
and encodes its exact behavior: every status-code branch, every substring
trigger, the raw-code text channel, the empty-response exact-match shapes,
order-dependent combinations, and inputs that must keep falling through to
UNKNOWN. The table-driven refactor must keep every case green unchanged.

All strings are synthetic; some mirror generic public provider error
phrasings because those phrasings are exactly what the classifier matches on.
"""

from __future__ import annotations

import pytest

from opensquilla.provider.failures import ProviderFailureKind, classify_provider_error

K = ProviderFailureKind

_UNREGISTERED = "totally-unknown-provider"
_GATEWAY_STATUSES = (499, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529)

Case = tuple[str, int | None, str, str, ProviderFailureKind]

# --- shared pre-pass: context overflow (family-independent, highest priority) ---
_CONTEXT_OVERFLOW_CASES: list[Case] = [
    ("openai", None, "", "context length exceeded", K.CONTEXT_OVERFLOW),
    ("anthropic", None, "", "this exceeds the context window", K.CONTEXT_OVERFLOW),
    ("ollama", None, "", "maximum context reached", K.CONTEXT_OVERFLOW),
    (_UNREGISTERED, None, "", "prompt is too long", K.CONTEXT_OVERFLOW),
    ("openrouter", None, "", "input is too long for this model", K.CONTEXT_OVERFLOW),
    ("openai", None, "", "input exceeds the allowed size", K.CONTEXT_OVERFLOW),
    (
        "openrouter",
        None,
        "provider_request_budget_exhausted",
        '{"fallback_reason":"provider_request_budget_exhausted"}',
        K.CONTEXT_OVERFLOW,
    ),
    ("anthropic", None, "", "too many tokens in request", K.CONTEXT_OVERFLOW),
    # Pre-pass outranks the family tables (a 401 would otherwise be AUTH_INVALID).
    ("openai", 401, "", "context length exceeded", K.CONTEXT_OVERFLOW),
    # Context overflow outranks policy refusal within the pre-pass.
    ("openai", None, "", "context window hit; flagged by moderation", K.CONTEXT_OVERFLOW),
]

# --- shared pre-pass: policy refusal ---
_POLICY_REFUSAL_CASES: list[Case] = [
    ("openai", None, "", "violates our content policy", K.POLICY_REFUSAL),
    ("anthropic", None, "", "policy violation detected", K.POLICY_REFUSAL),
    ("ollama", None, "", "safety policy triggered", K.POLICY_REFUSAL),
    (_UNREGISTERED, None, "", "flagged by moderation", K.POLICY_REFUSAL),
    ("openrouter", None, "", "refusal: cannot help with that", K.POLICY_REFUSAL),
    ("openai", None, "", "request blocked by policy", K.POLICY_REFUSAL),
    # Pre-pass outranks the family 429 branch.
    ("anthropic", 429, "", "flagged by moderation", K.POLICY_REFUSAL),
]

# --- shared pre-pass: empty response (exact-match shapes, not substrings) ---
_EMPTY_RESPONSE_CASES: list[Case] = [
    ("openrouter", None, "empty_response", "", K.EMPTY_RESPONSE),
    ("openrouter", None, "", "empty_response", K.EMPTY_RESPONSE),
    ("openai", None, "", "empty response", K.EMPTY_RESPONSE),
    ("anthropic", None, "", "Provider returned an empty response", K.EMPTY_RESPONSE),
    ("openai", None, "", "  EMPTY RESPONSE  ", K.EMPTY_RESPONSE),
    ("openai", None, " EMPTY_RESPONSE ", "", K.EMPTY_RESPONSE),
    # Pre-pass outranks the family 429 branch.
    ("openai", 429, "empty_response", "", K.EMPTY_RESPONSE),
    # Exact-match only: a longer message containing the phrase is NOT empty-response;
    # this one lands on the openai_compat gateway-status branch instead.
    (
        "openai",
        500,
        "",
        "OpenAI chat request failed (HTTP 500): empty response body",
        K.PROVIDER_OVERLOADED,
    ),
]

# --- openai_compat family ---
_OPENAI_COMPAT_CASES: list[Case] = [
    # MODEL_NOT_FOUND: status 404 OR any model-unavailable marker.
    ("openai", 404, "", "", K.MODEL_NOT_FOUND),
    ("openrouter", None, "", "no endpoints found for this model", K.MODEL_NOT_FOUND),
    ("deepseek", None, "", "model not found", K.MODEL_NOT_FOUND),
    ("openai", None, "", "the model is not available right now", K.MODEL_NOT_FOUND),
    ("openrouter", None, "", "model not available", K.MODEL_NOT_FOUND),
    ("openrouter", None, "", "not available in the requested region", K.MODEL_NOT_FOUND),
    # Model-unavailable marker outranks the 403 auth branch.
    ("openai", 403, "", "This model is not available in your region.", K.MODEL_NOT_FOUND),
    # TokenRhythm's custom envelope arrives as HTTP 400 with a top-level
    # code + localized message; _http_error_body_text prefixes the code, and
    # either marker (code or Chinese text) must outrank BAD_REQUEST.
    ("tokenrhythm", 400, "", "MODEL_NOT_AVAILABLE: 模型不可用：some-model", K.MODEL_NOT_FOUND),
    ("tokenrhythm", 400, "", "模型不可用：some-model", K.MODEL_NOT_FOUND),
    # Status 404 outranks the auth substring branch.
    ("openai", 404, "", "unauthorized", K.MODEL_NOT_FOUND),
    # AUTH_INVALID: 401/403 OR "invalid api key" / "unauthorized".
    ("openai", 401, "", "", K.AUTH_INVALID),
    ("openrouter", 403, "", "HTTP 403: forbidden", K.AUTH_INVALID),
    ("deepseek", None, "", "invalid api key", K.AUTH_INVALID),
    ("tokenrhythm", 401, "", "UNAUTHORIZED: 未认证或登录已过期", K.AUTH_INVALID),
    ("openai", None, "", "Unauthorized", K.AUTH_INVALID),
    ("openai_responses", 401, "", "invalid api key", K.AUTH_INVALID),
    # INSUFFICIENT_CREDITS: 402 OR "insufficient credits" / "no credits".
    ("openai", 402, "", "", K.INSUFFICIENT_CREDITS),
    ("openrouter", None, "", "insufficient credits to complete request", K.INSUFFICIENT_CREDITS),
    ("openrouter", None, "", "no credits remaining", K.INSUFFICIENT_CREDITS),
    # 402 outranks the rate-limit substring branch.
    ("openrouter", 402, "", "rate limit will apply", K.INSUFFICIENT_CREDITS),
    # RATE_LIMITED: 429 OR "rate limit" / "rate_limit".
    ("openai", 429, "", "", K.RATE_LIMITED),
    ("openrouter", None, "", "rate limit exceeded", K.RATE_LIMITED),
    ("openai", 429, "", "Too Many Requests", K.RATE_LIMITED),
    # raw_code participates in the joined text channel.
    ("openai", None, "rate_limit_exceeded", "", K.RATE_LIMITED),
    # 429 outranks the unsupported branch.
    ("openai", 429, "", "does not support this", K.RATE_LIMITED),
    # UNSUPPORTED_FEATURE: "does not support" / "unsupported".
    ("openai", None, "", "model does not support tools", K.UNSUPPORTED_FEATURE),
    # Unsupported outranks the bare-400 branch.
    ("openrouter", 400, "", "unsupported parameter", K.UNSUPPORTED_FEATURE),
    # Unsupported outranks the gateway-status branch.
    ("openai", 503, "", "unsupported model configuration", K.UNSUPPORTED_FEATURE),
    # PROVIDER_OVERLOADED: "overloaded" OR gateway-transient text.
    ("openai", None, "", "server overloaded, try again", K.PROVIDER_OVERLOADED),
    ("openrouter", None, "", "Cloudflare returned 520", K.PROVIDER_OVERLOADED),
    ("openai", None, "", "HTTP 522", K.PROVIDER_OVERLOADED),
    ("deepseek", None, "", "status_code: 523", K.PROVIDER_OVERLOADED),
    ("openrouter", None, "", "upstream returned 522", K.PROVIDER_OVERLOADED),
    ("openrouter", None, "", "OpenRouter upstream error 520", K.PROVIDER_OVERLOADED),
    # BAD_REQUEST: 400 OR "invalid_request".
    ("openai", 400, "", "", K.BAD_REQUEST),
    ("openrouter", None, "invalid_request", "", K.BAD_REQUEST),
    ("openai", None, "", "invalid_request_error: bad payload", K.BAD_REQUEST),
    # Family miss falls through to the shared tail.
    ("openai", None, "", "malformed chunk received", K.MALFORMED_RESPONSE),
    (
        "tokenrhythm",
        None,
        "invalid_stream_order",
        "TokenRhythm stream mutated state after finish_reason",
        K.MALFORMED_RESPONSE,
    ),
    (
        "openai",
        None,
        "invalid_stream_frame",
        "Provider stream returned an invalid choice batch",
        K.MALFORMED_RESPONSE,
    ),
    ("openrouter", None, "", "request error: connection reset by peer", K.TRANSPORT_TRANSIENT),
    ("openai", None, "timeout", "Request timed out: ", K.TRANSPORT_TRANSIENT),
]

# --- anthropic family ---
_ANTHROPIC_CASES: list[Case] = [
    ("anthropic", 404, "", "", K.MODEL_NOT_FOUND),
    ("anthropic", None, "not_found_error", "", K.MODEL_NOT_FOUND),
    ("anthropic", None, "", '{"type":"not_found_error","message":"model: x"}', K.MODEL_NOT_FOUND),
    ("anthropic", None, "", "model not available", K.MODEL_NOT_FOUND),
    ("minimax", 401, "authentication_error", "", K.AUTH_INVALID),
    ("anthropic", 403, "", "", K.AUTH_INVALID),
    ("anthropic", None, "", "authentication_error: bad key", K.AUTH_INVALID),
    ("anthropic", 402, "", "", K.INSUFFICIENT_CREDITS),
    # "credit balance" outranks everything a 400 could otherwise become.
    (
        "anthropic",
        400,
        "",
        "Your credit balance is too low to access the Anthropic API.",
        K.INSUFFICIENT_CREDITS,
    ),
    ("anthropic", 429, "", "", K.RATE_LIMITED),
    ("anthropic", None, "rate_limit_error", "", K.RATE_LIMITED),
    # Family branch only matches "rate_limit_error"; plain "rate limit" is
    # caught by the shared tail instead.
    ("anthropic", None, "", "rate limit hit", K.RATE_LIMITED),
    ("anthropic", None, "overloaded_error", "", K.PROVIDER_OVERLOADED),
    # Family miss on text; the shared-tail gateway regex catches it.
    ("anthropic", None, "", "HTTP 520", K.PROVIDER_OVERLOADED),
    ("anthropic", None, "invalid_request_error", "", K.BAD_REQUEST),
    # anthropic has no bare-400 branch: a naked 400 stays UNKNOWN.
    ("anthropic", 400, "", "", K.UNKNOWN),
    # openai_compat-only substrings do not apply to the anthropic family.
    ("anthropic", None, "", "unsupported parameter", K.UNKNOWN),
    ("anthropic", None, "", "invalid api key", K.UNKNOWN),
    # "invalid_request" without the "_error" suffix misses the anthropic branch.
    ("anthropic", None, "", "invalid_request in body", K.UNKNOWN),
]

# --- ollama family ---
_OLLAMA_CASES: list[Case] = [
    ("ollama", None, "", "model not found", K.MODEL_NOT_FOUND),
    # Conjunction: "pull" AND "model" together classify as model-not-found.
    ("ollama", None, "", 'try "ollama pull llama3" to download the model', K.MODEL_NOT_FOUND),
    # The pull+model conjunction outranks the timeout transport branch.
    ("ollama", None, "", "timeout while pulling model manifest", K.MODEL_NOT_FOUND),
    ("ollama", 401, "", "", K.AUTH_INVALID),
    ("ollama", 403, "", "", K.AUTH_INVALID),
    ("ollama", None, "", "HTTP 401: unauthorized", K.AUTH_INVALID),
    ("ollama", None, "", "connection refused", K.TRANSPORT_TRANSIENT),
    ("ollama", None, "", "connection error while contacting host", K.TRANSPORT_TRANSIENT),
    ("ollama", None, "", "request error: boom", K.TRANSPORT_TRANSIENT),
    ("ollama", None, "", "read timeout", K.TRANSPORT_TRANSIENT),
    # Family misses fall through to the shared tail.
    ("ollama", 429, "", "", K.RATE_LIMITED),
    ("ollama", 500, "", "", K.PROVIDER_OVERLOADED),
    ("ollama", None, "", "invalid json in response", K.MALFORMED_RESPONSE),
    # ollama has no 404 status branch.
    ("ollama", 404, "", "", K.UNKNOWN),
    # "pull" without "model" (and vice versa) does not classify.
    ("ollama", None, "", "pull the latest image", K.UNKNOWN),
    ("ollama", None, "", "model exploded", K.UNKNOWN),
]

# --- shared/generic tail (unregistered provider => no family table) ---
_GENERIC_TAIL_CASES: list[Case] = [
    (_UNREGISTERED, 429, "", "", K.RATE_LIMITED),
    (_UNREGISTERED, None, "", "rate limit exceeded", K.RATE_LIMITED),
    # The underscore variant is an openai_compat-family substring only.
    (_UNREGISTERED, None, "", "rate_limit_exceeded", K.UNKNOWN),
    (_UNREGISTERED, None, "", "upstream returned 522", K.PROVIDER_OVERLOADED),
    (_UNREGISTERED, None, "", "malformed response", K.MALFORMED_RESPONSE),
    (_UNREGISTERED, None, "", "invalid json payload", K.MALFORMED_RESPONSE),
    (_UNREGISTERED, None, "", "timeout after 30s", K.TRANSPORT_TRANSIENT),
    (_UNREGISTERED, None, "", "request error: peer reset", K.TRANSPORT_TRANSIENT),
    # Tail order: malformed is checked before timeout.
    (_UNREGISTERED, None, "", "malformed response after timeout", K.MALFORMED_RESPONSE),
]

# --- shared tail: adapter-synthesized terminal-evidence codes ---
# These raw codes are emitted locally when a stream ends without terminal
# evidence or a native tool call arrives without usable arguments. They must
# classify as retryable for every family and for unregistered providers.
_TERMINAL_EVIDENCE_CASES: list[Case] = [
    (
        "openai",
        None,
        "incomplete_stream",
        "SomeBackend stream ended before a finish reason",
        K.TRANSPORT_TRANSIENT,
    ),
    (
        "anthropic",
        None,
        "incomplete_stream",
        "Anthropic stream ended before message_stop",
        K.TRANSPORT_TRANSIENT,
    ),
    (_UNREGISTERED, None, "incomplete_stream", "", K.TRANSPORT_TRANSIENT),
    (
        "openai",
        None,
        "incomplete_tool_call",
        "SomeBackend returned invalid native tool arguments",
        K.TRANSPORT_TRANSIENT,
    ),
    (
        "anthropic",
        None,
        "incomplete_tool_call",
        "Anthropic response ended with an incomplete tool call",
        K.TRANSPORT_TRANSIENT,
    ),
    # The raw-code row outranks the tail's "malformed" substring row, which
    # would otherwise downgrade recovery to SURFACE.
    (
        "ollama",
        None,
        "incomplete_tool_call",
        "Ollama stream contained malformed tool calls",
        K.TRANSPORT_TRANSIENT,
    ),
]

# --- inputs that must keep falling through to UNKNOWN ---
_UNKNOWN_CASES: list[Case] = [
    (_UNREGISTERED, 401, "", "", K.UNKNOWN),
    (_UNREGISTERED, 400, "", "", K.UNKNOWN),
    (_UNREGISTERED, 418, "", "", K.UNKNOWN),
    (_UNREGISTERED, None, "", "", K.UNKNOWN),
    ("", None, "", "", K.UNKNOWN),
    (_UNREGISTERED, None, "", "invalid api key", K.UNKNOWN),
    (_UNREGISTERED, None, "", "something inexplicable happened", K.UNKNOWN),
    # Unscoped gateway-looking numbers stay unclassified.
    (_UNREGISTERED, None, "", "line 520", K.UNKNOWN),
    (_UNREGISTERED, None, "", "the provider sent 520 tokens", K.UNKNOWN),
    # A bare "429 Too Many Requests" body without a status code has no trigger.
    (_UNREGISTERED, None, "", "429 Too Many Requests", K.UNKNOWN),
    # Near-miss on the exact-match empty-response shapes.
    (_UNREGISTERED, None, "", "the empty response body was discarded", K.UNKNOWN),
]

CORPUS: list[Case] = (
    _CONTEXT_OVERFLOW_CASES
    + _POLICY_REFUSAL_CASES
    + _EMPTY_RESPONSE_CASES
    + _OPENAI_COMPAT_CASES
    + _ANTHROPIC_CASES
    + _OLLAMA_CASES
    + _GENERIC_TAIL_CASES
    + _TERMINAL_EVIDENCE_CASES
    + _UNKNOWN_CASES
)

# Every gateway-transient status code classifies PROVIDER_OVERLOADED for the
# openai_compat and anthropic families and via the shared tail (ollama and
# unregistered providers have no family status branch and fall through).
for _status in _GATEWAY_STATUSES:
    for _provider in ("openrouter", "anthropic", "ollama", _UNREGISTERED):
        CORPUS.append((_provider, _status, "", "", K.PROVIDER_OVERLOADED))


@pytest.mark.parametrize(
    ("provider", "status_code", "raw_code", "message", "expected"),
    CORPUS,
    ids=[
        f"{i:03d}-{provider or 'blank'}-{status_code}-{expected.value}"
        for i, (provider, status_code, raw_code, message, expected) in enumerate(CORPUS)
    ],
)
def test_classification_parity(
    provider: str,
    status_code: int | None,
    raw_code: str,
    message: str,
    expected: ProviderFailureKind,
) -> None:
    assert (
        classify_provider_error(provider, status_code, raw_code=raw_code, message=message)
        is expected
    )


def test_corpus_covers_every_failure_kind_reachable_from_classification() -> None:
    reachable = {expected for *_ignored, expected in CORPUS}
    assert reachable == set(ProviderFailureKind)
