from __future__ import annotations

import pytest
import structlog

from opensquilla.engine.fallback import FallbackPolicy
from opensquilla.provider.failures import (
    FailureMatcher,
    ProviderFailureKind,
    ProviderRecoveryAction,
    classify_provider_error,
    decide_recovery_action,
)
from opensquilla.provider.openai import _http_error_body_text


def test_http_error_body_text_prefixes_top_level_code() -> None:
    """Non-OpenAI envelopes ({"code","message","traceId"} — TokenRhythm)
    carry the machine-readable kind in a top-level code; it must ride along
    with the localized message so classification substrings can match."""
    body = '{"code": "MODEL_NOT_AVAILABLE", "message": "模型不可用：xyz", "traceId": "trace_0"}'
    assert _http_error_body_text(body.encode()) == "MODEL_NOT_AVAILABLE: 模型不可用：xyz"
    # OpenAI envelopes keep their message untouched.
    assert _http_error_body_text(b'{"error": {"message": "boom", "code": "x"}}') == "boom"
    # A top-level message without a code stays bare.
    assert _http_error_body_text(b'{"message": "plain"}') == "plain"


def test_provider_request_budget_exhausted_is_context_overflow() -> None:
    assert (
        classify_provider_error(
            provider_name="openrouter",
            status_code=None,
            raw_code="provider_request_budget_exhausted",
            message='{"fallback_reason":"provider_request_budget_exhausted"}',
        )
        is ProviderFailureKind.CONTEXT_OVERFLOW
    )


@pytest.mark.parametrize("provider_name", ["ensemble", "openrouter"])
def test_ensemble_multimodal_rejection_is_surfaced_without_fallback(
    provider_name: str,
) -> None:
    kind = classify_provider_error(
        provider_name=provider_name,
        status_code=None,
        raw_code="ensemble_multimodal_unsupported",
        message=(
            "Ensemble does not support image input yet. "
            "Switch to a single-model routing mode and try again."
        ),
    )

    assert kind is ProviderFailureKind.BAD_REQUEST
    assert decide_recovery_action(kind) is ProviderRecoveryAction.SURFACE
    assert FallbackPolicy().should_retry(kind, attempt=0) is False


def test_image_input_rejection_is_surfaced_without_fallback() -> None:
    kind = classify_provider_error(
        provider_name="openrouter",
        status_code=None,
        raw_code="image_input_unsupported",
        message="The selected model cannot process image input.",
    )

    assert kind is ProviderFailureKind.BAD_REQUEST
    assert decide_recovery_action(kind) is ProviderRecoveryAction.SURFACE
    assert FallbackPolicy().should_retry(kind, attempt=0) is False


@pytest.mark.parametrize(
    ("provider_name", "raw_code", "message"),
    [
        ("openai", "incomplete_stream", "SomeBackend stream ended before a finish reason"),
        ("anthropic", "incomplete_stream", "Anthropic stream ended before message_stop"),
        ("ollama", "incomplete_stream", "Ollama stream ended before done=true"),
        ("openai", "incomplete_tool_call", "SomeBackend returned invalid native tool arguments"),
        ("ollama", "incomplete_tool_call", "Ollama stream contained malformed tool calls"),
    ],
)
def test_terminal_evidence_codes_are_retryable_with_provider_fallback(
    provider_name: str,
    raw_code: str,
    message: str,
) -> None:
    """A truncated stream or unusable native tool call must not be terminal:
    the runtime retries the call and then falls back to another provider."""
    with structlog.testing.capture_logs() as captured:
        kind = classify_provider_error(
            provider_name=provider_name,
            status_code=None,
            raw_code=raw_code,
            message=message,
        )

    assert kind is ProviderFailureKind.TRANSPORT_TRANSIENT
    assert decide_recovery_action(kind) is ProviderRecoveryAction.RETRY_THEN_FALLBACK
    assert FallbackPolicy().should_retry(kind, attempt=0) is True
    # Classified: no unclassified-fingerprint noise on every truncated stream.
    assert not [e for e in captured if e["event"] == "provider_failure.unclassified"]


def test_unknown_classification_emits_redacted_fingerprint_event() -> None:
    with structlog.testing.capture_logs() as captured:
        kind = classify_provider_error(
            "openrouter",
            None,
            raw_code="strange_code",
            message="novel backend exploded: Bearer abc123def456",
        )

    assert kind is ProviderFailureKind.UNKNOWN
    events = [entry for entry in captured if entry["event"] == "provider_failure.unclassified"]
    assert len(events) == 1
    event = events[0]
    assert event["provider"] == "openrouter"
    assert event["failure_family"] == "openai_compat"
    assert event["status_code"] is None
    assert event["raw_code_chars"] == len("strange_code")
    assert event["message_chars"] == len(
        "novel backend exploded: Bearer abc123def456"
    )
    assert "raw_code" not in event
    assert "message_head" not in event
    assert "strange_code" not in repr(event)
    assert "novel backend exploded" not in repr(event)
    assert "abc123def456" not in repr(event)


def test_classified_errors_do_not_emit_the_unclassified_event() -> None:
    with structlog.testing.capture_logs() as captured:
        kind = classify_provider_error("openrouter", 429, message="rate limit")

    assert kind is ProviderFailureKind.RATE_LIMITED
    assert not [e for e in captured if e["event"] == "provider_failure.unclassified"]


def test_constraint_free_matcher_rows_are_rejected() -> None:
    # A row with no constraints would match every error; the table refuses it.
    with pytest.raises(ValueError, match="at least one constraint"):
        FailureMatcher(ProviderFailureKind.UNKNOWN)
