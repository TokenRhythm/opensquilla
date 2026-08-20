"""Human-readable terminal replies for task and stream terminal events."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opensquilla.session.models import AgentTaskStatus
from opensquilla.silent_reply import (
    SILENT_REPLY_NOT_ALLOWED_CODE,
    SILENT_REPLY_NOT_ALLOWED_MESSAGE,
)

CONTEXT_PAYLOAD_TOO_LARGE_CODE = "provider_request_too_large"
ENSEMBLE_MULTIMODAL_UNSUPPORTED_CODE = "ensemble_multimodal_unsupported"
ENSEMBLE_MULTIMODAL_UNSUPPORTED_MESSAGE = (
    "Ensemble does not support image input yet. "
    "Switch to a single-model routing mode and try again."
)
IMAGE_INPUT_UNSUPPORTED_CODE = "image_input_unsupported"
IMAGE_INPUT_UNSUPPORTED_MESSAGE = (
    "The selected model cannot process image input. Choose an image-capable "
    "model or remove the image and try again."
)
_REASONING_ONLY_OUTPUT_BUDGET_ERROR_MESSAGE = (
    "the provider used the configured output budget for reasoning without returning a visible "
    "answer. increase llm.max_tokens or choose another model or provider."
)
_REASONING_ONLY_OUTPUT_BUDGET_TERMINAL_MESSAGE = (
    "The model used its output budget for reasoning without returning a visible answer. "
    "Increase llm.max_tokens or choose another model or provider."
)
_REASONING_ONLY_EMPTY_ERROR_MESSAGE = (
    "the provider returned reasoning without a visible answer. try again or choose another "
    "model or provider."
)
_REASONING_ONLY_EMPTY_TERMINAL_MESSAGE = (
    "The model returned reasoning without a visible answer. "
    "Try again or choose another model or provider."
)

# Non-context budget-exhaustion codes. Context-window exhaustion has its own,
# more specific message via ``is_context_payload_too_large`` and is intentionally
# excluded here.
_BUDGET_CLASSES = frozenset(
    {
        "tool_run_budget_exhausted",
        "llm_budget_exhausted",
        "turn_llm_call_budget_exceeded",
        "turn_input_token_budget_exceeded",
        "turn_output_token_budget_exceeded",
        "turn_billed_cost_budget_exceeded",
    }
)

_SAFE_PROVIDER_FAILURE_MESSAGES = {
    "rate_limited": "The model provider is rate-limiting requests. Try again later.",
    "provider_overloaded": (
        "The model provider is temporarily overloaded. Try again later."
    ),
    "auth_invalid": "The model provider rejected the configured credentials.",
    "context_overflow": "The request exceeds the model provider's context window.",
    "unsupported_feature": "The model provider does not support this request.",
    "insufficient_credits": "The model provider account has insufficient credits.",
    "model_not_found": "The configured model is unavailable from the provider.",
    "transport_transient": (
        "The connection to the model provider was interrupted. Try again."
    ),
    "policy_refusal": "The model provider refused this request under its policy.",
    "empty_response": "The model provider returned an empty response.",
    "malformed_response": "The model provider returned an invalid response.",
    "bad_request": "The model provider rejected the request.",
}
_SAFE_PROVIDER_TERMINAL_CODES = frozenset(
    {
        "cancelled",
        "context_length_exceeded",
        "context_overflow",
        "empty_response",
        "ensemble_multimodal_unsupported",
        "image_input_unsupported",
        "incomplete_stream",
        "incomplete_tool_call",
        "incomplete_tool_stream",
        "invalid_json",
        "invalid_response",
        "invalid_response_status",
        "invalid_stream_frame",
        "invalid_stream_order",
        "model_repetition_loop_detected",
        "provider_protocol_error",
        "provider_output_truncated",
        "provider_pretext_buffer_exhausted",
        "provider_retry_after_deadline",
        "provider_request_budget_exhausted",
        "provider_request_too_large",
        "request_error",
        "response_incomplete",
        "synthetic_upstream_failure",
        "timeout",
        "usage_limit_reached",
    }
)


def safe_provider_failure_message(failure_kind: str | None) -> str:
    """Project a stable provider failure kind to allowlisted user text.

    This is a defense-in-depth boundary for Gateway producers other than the
    current Agent. Raw upstream prose can echo prompts, response bodies, or
    credentials and must never reach a client or durable terminal record.
    """

    normalized = str(failure_kind or "").strip().lower().replace("-", "_")
    return _SAFE_PROVIDER_FAILURE_MESSAGES.get(
        normalized,
        "The model provider request failed.",
    )


def safe_provider_failure_code(raw_code: str | None, failure_kind: str | None) -> str:
    """Return a bounded terminal code without relaying provider-controlled text."""

    normalized_code = str(raw_code or "").strip().lower().replace("-", "_")
    if normalized_code.isascii() and normalized_code.isdigit() and len(normalized_code) <= 3:
        return normalized_code
    if normalized_code in _SAFE_PROVIDER_TERMINAL_CODES:
        return normalized_code
    normalized_kind = str(failure_kind or "").strip().lower().replace("-", "_")
    if normalized_kind in _SAFE_PROVIDER_FAILURE_MESSAGES:
        return f"provider_{normalized_kind}"
    return "provider_error"


def build_terminal_reply(
    record_or_payload: Any,
    *,
    surface: str | None = None,
    locale: str | None = None,
) -> str:
    """Return an additive human-readable message for a terminal payload.

    The returned string is intended for user-facing terminal surfaces. Existing
    technical fields such as ``terminal_reason`` and ``error_message`` remain
    the source of machine/debug detail; this helper deliberately avoids exposing
    raw timeout internals in the normal reply text.
    """

    del surface, locale  # Reserved for future surface/locale-specific phrasing.

    existing = _read_value(record_or_payload, "terminal_message")
    if (
        isinstance(existing, str)
        and existing.strip()
        and not _contains_context_payload_marker(existing)
    ):
        return existing.strip()

    status = _normalize(_read_value(record_or_payload, "status"))
    reason = _normalize(_read_value(record_or_payload, "terminal_reason"))
    error_class = _normalize(_read_value(record_or_payload, "error_class"))
    error_message = _normalize(_read_value(record_or_payload, "error_message"))

    if (
        status == AgentTaskStatus.TIMEOUT.value
        or reason == "timeout"
        or error_class == "iteration_timeout"
        or "timeouterror" in error_class
        or "iteration_timeout" in error_message
        or "stream idle" in error_message
    ):
        return "The task timed out before it could finish."
    if is_context_payload_too_large(record_or_payload) or (
        isinstance(existing, str) and _contains_context_payload_marker(existing)
    ):
        return (
            "The request is too large for the provider context window after "
            "automatic context compaction and payload reduction. OpenSquilla "
            "preserved the recoverable state; retry with a narrower request "
            "or a larger-context model."
        )
    if (
        error_class == "empty_response"
        and error_message == _REASONING_ONLY_OUTPUT_BUDGET_ERROR_MESSAGE
    ):
        return _REASONING_ONLY_OUTPUT_BUDGET_TERMINAL_MESSAGE
    if (
        error_class == "empty_response"
        and error_message == _REASONING_ONLY_EMPTY_ERROR_MESSAGE
    ):
        return _REASONING_ONLY_EMPTY_TERMINAL_MESSAGE
    if reason == "output_truncated" or error_class == "provider_output_truncated":
        return "The provider stopped because the output limit was reached before the task finished."
    if (
        reason == "model_repetition_loop_detected"
        or error_class == "model_repetition_loop_detected"
    ):
        return "The model began repeating the same output, so OpenSquilla stopped the task."
    if status == AgentTaskStatus.CANCELLED.value or reason.startswith("cancelled"):
        return "The task was cancelled before it finished."
    if status == AgentTaskStatus.ABANDONED.value or reason == "shutdown_timeout":
        return "The task stopped before it could finish."
    # Per-code phrasing for the non-provider terminal codes the agent loop emits,
    # so each surfaces a specific, actionable cause instead of the generic
    # "failed" sentence below.
    if (
        error_class == ENSEMBLE_MULTIMODAL_UNSUPPORTED_CODE
        or reason == ENSEMBLE_MULTIMODAL_UNSUPPORTED_CODE
    ):
        return ENSEMBLE_MULTIMODAL_UNSUPPORTED_MESSAGE
    if error_class == IMAGE_INPUT_UNSUPPORTED_CODE or reason == IMAGE_INPUT_UNSUPPORTED_CODE:
        return IMAGE_INPUT_UNSUPPORTED_MESSAGE
    if error_class == "sandbox_threshold_exceeded" or reason == "sandbox_threshold_exceeded":
        return (
            "Automatic execution paused after repeated sandbox denials. Approve the "
            "requested access (or widen the sandbox policy) and resume to continue."
        )
    if error_class in _BUDGET_CLASSES or reason in _BUDGET_CLASSES:
        return (
            "The task stopped because it reached a configured budget limit "
            "(tokens, tool calls, or cost) before it could finish."
        )
    if error_class == "max_iterations" or reason == "max_iterations":
        return (
            "The task stopped after reaching its maximum number of steps before it could finish."
        )
    if error_class == "tool_policy_denied" or reason == "tool_policy_denied":
        return (
            "The task was blocked because a tool it needed is not permitted by the "
            "current policy."
        )
    if (
        error_class == SILENT_REPLY_NOT_ALLOWED_CODE
        or reason == SILENT_REPLY_NOT_ALLOWED_CODE
    ):
        return SILENT_REPLY_NOT_ALLOWED_MESSAGE
    if error_class in {
        "usage_accounting_busy",
        "usage_accounting_unavailable",
    } or reason in {
        "usage_accounting_busy",
        "usage_accounting_unavailable",
    }:
        usage_call_index = _read_value(record_or_payload, "usage_call_index")
        replay_safe = (
            isinstance(usage_call_index, int)
            and not isinstance(usage_call_index, bool)
            and usage_call_index == 1
            and _read_value(record_or_payload, "no_prior_provider_dispatch") is True
            and _read_value(record_or_payload, "replay_safe") is True
        )
        if replay_safe:
            return (
                "Usage accounting is temporarily unavailable. The provider request was not "
                "sent and no usage was billed, so it is safe to retry this turn."
            )
        return (
            "Usage accounting is temporarily unavailable. This provider request was not "
            "sent. Earlier work in this turn may already have run or been billed."
        )
    if status == AgentTaskStatus.FAILED.value or reason in {"error", "tool_error"}:
        return "The task failed before it could finish."
    if status == AgentTaskStatus.SUCCEEDED.value or reason in {"completed", "done"}:
        return "The task completed."
    return "The task ended before it could finish."


def sanitize_agent_error(
    record_or_payload: Any,
    *,
    fallback_error_class: str | None = None,
    fallback_error_message: str = "Agent error",
) -> tuple[str | None, str]:
    if is_context_payload_too_large(record_or_payload):
        return CONTEXT_PAYLOAD_TOO_LARGE_CODE, build_terminal_reply(record_or_payload)
    if _is_provider_output_truncated(record_or_payload):
        return "provider_output_truncated", build_terminal_reply(
            {
                "status": "failed",
                "terminal_reason": "output_truncated",
                "error_class": "provider_output_truncated",
                "error_message": (
                    record_or_payload
                    if isinstance(record_or_payload, str)
                    else _read_value(record_or_payload, "error_message")
                ),
            }
        )
    if _is_timeout_error(record_or_payload):
        raw_timeout_class = (
            None
            if isinstance(record_or_payload, str)
            else _read_value(record_or_payload, "error_class")
        )
        timeout_error_class = (
            raw_timeout_class.strip()
            if isinstance(raw_timeout_class, str) and raw_timeout_class.strip()
            else fallback_error_class or "iteration_timeout"
        )
        return timeout_error_class, build_terminal_reply(
            {
                "status": "timeout",
                "terminal_reason": "timeout",
                "error_class": timeout_error_class,
                "error_message": (
                    record_or_payload
                    if isinstance(record_or_payload, str)
                    else _read_value(record_or_payload, "error_message")
                    or _read_value(record_or_payload, "message")
                ),
            }
        )

    raw_message = (
        record_or_payload
        if isinstance(record_or_payload, str)
        else (
            _read_value(record_or_payload, "error_message")
            or _read_value(record_or_payload, "message")
            or _read_value(record_or_payload, "terminal_message")
        )
    )
    if isinstance(raw_message, str) and raw_message.strip():
        if _contains_context_payload_marker(raw_message):
            payload = {"status": "failed", "error_message": raw_message}
            return CONTEXT_PAYLOAD_TOO_LARGE_CODE, build_terminal_reply(payload)
        message = raw_message.strip()
    else:
        message = fallback_error_message

    raw_error_class = (
        None
        if isinstance(record_or_payload, str)
        else _read_value(record_or_payload, "error_class")
    )
    error_class = (
        raw_error_class.strip()
        if isinstance(raw_error_class, str) and raw_error_class.strip()
        else fallback_error_class
    )
    return error_class, message


def is_context_payload_too_large(record_or_payload: Any) -> bool:
    """Return whether a terminal payload represents provider context exhaustion."""

    reason = _normalize(_read_value(record_or_payload, "terminal_reason"))
    error_class = _normalize(_read_value(record_or_payload, "error_class"))
    error_message = _normalize(_read_value(record_or_payload, "error_message"))
    terminal_message = _normalize(_read_value(record_or_payload, "terminal_message"))
    combined = f"{reason} {error_class} {error_message} {terminal_message}"
    return _contains_context_payload_marker(combined)


def _is_provider_output_truncated(record_or_payload: Any) -> bool:
    if isinstance(record_or_payload, str):
        return "provider output limit reached before completion" in _normalize(
            record_or_payload
        )
    reason = _normalize(_read_value(record_or_payload, "terminal_reason"))
    error_class = _normalize(_read_value(record_or_payload, "error_class"))
    message = _normalize(_read_value(record_or_payload, "error_message"))
    terminal_message = _normalize(_read_value(record_or_payload, "terminal_message"))
    combined = f"{reason} {error_class} {message} {terminal_message}"
    return (
        reason == "output_truncated"
        or error_class == "provider_output_truncated"
        or "provider output limit reached before completion" in combined
    )


def _is_timeout_error(record_or_payload: Any) -> bool:
    if isinstance(record_or_payload, str):
        normalized = _normalize(record_or_payload)
        return (
            "iteration_timeout" in normalized
            or "timeouterror" in normalized
            or "stream idle" in normalized
        )
    status = _normalize(_read_value(record_or_payload, "status"))
    reason = _normalize(_read_value(record_or_payload, "terminal_reason"))
    error_class = _normalize(_read_value(record_or_payload, "error_class"))
    message = _normalize(_read_value(record_or_payload, "error_message"))
    event_message = _normalize(_read_value(record_or_payload, "message"))
    combined = f"{reason} {error_class} {message} {event_message}"
    return (
        status == AgentTaskStatus.TIMEOUT.value
        or reason == "timeout"
        or error_class == "iteration_timeout"
        or "timeouterror" in error_class
        or "iteration_timeout" in combined
        or "stream idle" in combined
    )


def _contains_context_payload_marker(value: str) -> bool:
    normalized = _normalize(value)
    return any(
        marker in normalized
        for marker in (
            "provider_request_too_large",
            "provider_request_budget_exhausted",
            "current_turn_context_exhausted",
            "context overflow is in the current turn",
            "history compaction cannot reduce it",
        )
    )


def _read_value(record_or_payload: Any, field: str) -> Any:
    if isinstance(record_or_payload, Mapping):
        return record_or_payload.get(field)
    return getattr(record_or_payload, field, None)


def _normalize(value: Any) -> str:
    if isinstance(value, AgentTaskStatus):
        return value.value
    if isinstance(value, str):
        return value.strip().lower()
    return ""


def append_error_ref(message: str, error_id: str | None) -> str:
    """Append ``(ref: <error_id>)`` to a user-facing error message.

    Idempotent: a message already carrying this ref is returned unchanged, so
    gateway-side and client-side normalization passes cannot double-suffix.
    """
    if not error_id:
        return message
    suffix = f"(ref: {error_id})"
    if suffix in message:
        return message
    return f"{message} {suffix}"
