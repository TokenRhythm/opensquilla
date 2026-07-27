"""Stable, data-safe reason codes for incomplete usage accounting.

Provider error codes are third-party input.  They may contain URLs, request
identifiers, credentials, or arbitrary text, so the usage ledger must never
persist them verbatim.  This module is intentionally dependency-free so every
layer that can write an ``unknown_reason`` can enforce the same closed
taxonomy.
"""

from __future__ import annotations

_INTERNAL_USAGE_UNKNOWN_REASONS = frozenset(
    {
        "cancelled",
        "cancelled_before_provider_request",
        "direct_request_failed",
        "iteration_timeout",
        "missing_or_invalid_usage_receipt",
        "process_restarted",
        "provider_error",
        "provider_exception",
        "provider_stream_ended_without_usage",
        "total_timeout",
        "usage_unknown",
    }
)

# Exact provider values only.  Never retain a merely well-formed third-party
# string: its apparent code may still embed customer- or request-specific data.
_PROVIDER_ERROR_CODE_ALIASES = {
    "authentication_error": "authentication",
    "bad_request": "invalid_request",
    "canceled": "cancelled",
    "cancelled": "cancelled",
    "connection_error": "transport",
    "content_filter": "policy",
    "content_policy_violation": "policy",
    "context_length_exceeded": "context_limit",
    "context_window_exceeded": "context_limit",
    "deadline_exceeded": "timeout",
    "ensemble_aggregator_error": "internal",
    "ensemble_aggregator_incomplete": "incomplete_stream",
    "ensemble_aggregator_timeout": "timeout",
    "ensemble_fallback_incomplete": "incomplete_stream",
    "ensemble_fallback_timeout": "timeout",
    "forbidden": "permission",
    "incomplete_stream": "incomplete_stream",
    "incomplete_tool_call": "protocol_error",
    "invalid_api_key": "authentication",
    "invalid_json": "invalid_response",
    "invalid_request": "invalid_request",
    "invalid_request_error": "invalid_request",
    "invalid_response": "invalid_response",
    "invalid_response_status": "invalid_response",
    "invalid_stream_frame": "protocol_error",
    "invalid_stream_order": "protocol_error",
    "model_not_found": "not_found",
    "not_found": "not_found",
    "overloaded": "unavailable",
    "overloaded_error": "unavailable",
    "permission_error": "permission",
    "provider_internal": "internal",
    "provider_protocol_error": "protocol_error",
    "provider_request_budget_exhausted": "request_budget",
    "rate_limit_error": "rate_limit",
    "rate_limit_exceeded": "rate_limit",
    "rate_limited": "rate_limit",
    "request_error": "transport",
    "request_timeout": "timeout",
    "service_unavailable": "unavailable",
    "stream_incomplete": "incomplete_stream",
    "timeout": "timeout",
    "too_many_requests": "rate_limit",
    "unauthorized": "authentication",
    "unavailable": "unavailable",
}


def provider_error_usage_reason(code: object) -> str:
    """Map an untrusted provider error code to the closed ledger taxonomy."""

    if isinstance(code, bool):
        return "provider_error"
    if isinstance(code, int):
        return f"provider_error:{code}" if 100 <= code <= 599 else "provider_error"
    if not isinstance(code, str) or not code or code != code.strip():
        return "provider_error"
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in code):
        return "provider_error"
    if len(code) == 3 and code.isascii() and code.isdigit():
        status = int(code)
        if 100 <= status <= 599:
            return f"provider_error:{code}"
    category = _PROVIDER_ERROR_CODE_ALIASES.get(code.lower())
    return f"provider_error:{category}" if category is not None else "provider_error"


def normalize_usage_unknown_reason(reason: object) -> str:
    """Return one safe reason code suitable for durable ledger storage."""

    if not isinstance(reason, str):
        return "usage_unknown"
    if reason in _INTERNAL_USAGE_UNKNOWN_REASONS:
        return reason
    if reason.startswith("provider_error:"):
        return provider_error_usage_reason(reason.removeprefix("provider_error:"))
    if reason.startswith("raised:"):
        return "provider_exception"
    return "usage_unknown"


__all__ = [
    "normalize_usage_unknown_reason",
    "provider_error_usage_reason",
]
