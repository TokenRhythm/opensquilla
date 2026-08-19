from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.session.models import AgentTaskStatus
from opensquilla.session.terminal_reply import (
    build_terminal_reply,
    safe_provider_failure_code,
    safe_provider_failure_message,
    sanitize_agent_error,
)
from opensquilla.silent_reply import (
    SILENT_REPLY_NOT_ALLOWED_CODE,
    SILENT_REPLY_NOT_ALLOWED_MESSAGE,
)

RAW_INTERNAL_STRINGS = (
    "Gateway task timeout",
    "Stream idle for more than",
    "Context overflow is in the current turn",
    "current_turn_context_exhausted",
    "Provider output limit reached before completion",
)


@pytest.mark.parametrize(
    ("payload", "expected_fragment"),
    [
        (
            {
                "status": "timeout",
                "terminal_reason": "timeout",
                "error_class": "TimeoutError",
                "error_message": "Gateway task timeout: Stream idle for more than 60s",
            },
            "timed out",
        ),
        (
            {
                "status": "failed",
                "terminal_reason": "error",
                "error_class": "RuntimeError",
                "error_message": "boom",
            },
            "failed",
        ),
        (
            {
                "status": "abandoned",
                "terminal_reason": "shutdown_timeout",
            },
            "stopped",
        ),
        (
            {
                "status": "cancelled",
                "terminal_reason": "cancelled",
            },
            "cancelled",
        ),
        (
            {
                "status": "failed",
                "terminal_reason": "provider_request_budget_exhausted",
                "error_class": "provider_request_budget_exhausted",
                "error_message": '{"fallback_reason":"provider_request_budget_exhausted"}',
            },
            "automatic context compaction",
        ),
        (
            {
                "status": "failed",
                "terminal_reason": "error",
                "error_class": "current_turn_context_exhausted",
                "error_message": (
                    "Context overflow is in the current turn's recent tool calls "
                    "or reasoning tail; history compaction cannot reduce it."
                ),
            },
            "too large",
        ),
        (
            {
                "status": "failed",
                "terminal_reason": "provider_request_too_large",
                "error_class": "provider_request_too_large",
                "error_message": "provider request too large",
            },
            "too large",
        ),
        (
            {
                "status": "failed",
                "terminal_message": (
                    "Context overflow is in the current turn's recent tool calls "
                    "or reasoning tail; history compaction cannot reduce it."
                ),
            },
            "too large",
        ),
        (
            {
                "status": "failed",
                "terminal_reason": "output_truncated",
                "error_class": "provider_output_truncated",
                "error_message": "Provider output limit reached before completion",
            },
            "output limit",
        ),
        (
            {
                "status": "failed",
                "terminal_reason": "sandbox_threshold_exceeded",
                "error_class": "sandbox_threshold_exceeded",
                "error_message": "Autonomous execution paused after repeated sandbox denials.",
            },
            "paused",
        ),
        (
            {
                "status": "failed",
                "terminal_reason": "max_iterations",
                "error_class": "max_iterations",
            },
            "maximum number of steps",
        ),
        (
            {
                "status": "failed",
                "terminal_reason": "turn_output_token_budget_exceeded",
                "error_class": "turn_output_token_budget_exceeded",
            },
            "budget limit",
        ),
        (
            {
                "status": "failed",
                "terminal_reason": "error",
                "error_class": "ensemble_multimodal_unsupported",
                "error_message": "internal provider validation detail",
            },
            "single-model routing mode",
        ),
    ],
)
def test_build_terminal_reply_returns_user_readable_messages(
    payload: dict[str, Any],
    expected_fragment: str,
) -> None:
    message = build_terminal_reply(payload)

    assert message
    assert expected_fragment in message.lower()
    assert not message.startswith("terminal_reason=")
    for raw in RAW_INTERNAL_STRINGS:
        assert raw not in message


def test_sandbox_pause_reply_is_distinct_from_generic_failure() -> None:
    # The denial-pause code must not collapse into the generic "failed" sentence:
    # it needs its own actionable, resume-oriented phrasing.
    paused = build_terminal_reply(
        {
            "status": "failed",
            "terminal_reason": "sandbox_threshold_exceeded",
            "error_class": "sandbox_threshold_exceeded",
        }
    )
    generic = build_terminal_reply(
        {"status": "failed", "terminal_reason": "error", "error_class": "RuntimeError"}
    )
    assert paused != generic
    assert "resume" in paused.lower()


def test_ensemble_multimodal_reply_is_actionable_and_stable() -> None:
    reply = build_terminal_reply(
        {
            "status": "failed",
            "terminal_reason": "error",
            "error_class": "ensemble_multimodal_unsupported",
            "error_message": "raw detail must not win",
        }
    )

    assert reply == (
        "Ensemble does not support image input yet. "
        "Switch to a single-model routing mode and try again."
    )


def test_image_input_unsupported_reply_is_actionable_and_stable() -> None:
    reply = build_terminal_reply(
        {
            "status": "failed",
            "terminal_reason": "error",
            "error_class": "image_input_unsupported",
            "error_message": "provider-specific detail must not win",
        }
    )

    assert reply == (
        "The selected model cannot process image input. Choose an image-capable "
        "model or remove the image and try again."
    )


def test_human_silent_reply_failure_has_actionable_terminal_message() -> None:
    reply = build_terminal_reply(
        {
            "status": "failed",
            "terminal_reason": "error",
            "error_class": SILENT_REPLY_NOT_ALLOWED_CODE,
        }
    )

    assert reply == SILENT_REPLY_NOT_ALLOWED_MESSAGE


@pytest.mark.parametrize(
    "proof",
    [
        {"replay_safe": True},
        {
            "usage_call_index": "1",
            "no_prior_provider_dispatch": True,
            "replay_safe": True,
        },
        {
            "usage_call_index": 2,
            "no_prior_provider_dispatch": True,
            "replay_safe": True,
        },
    ],
)
def test_usage_barrier_reply_requires_complete_native_replay_proof(
    proof: dict[str, object],
) -> None:
    reply = build_terminal_reply(
        {
            "status": "failed",
            "error_class": "usage_accounting_busy",
            **proof,
        }
    )

    assert "earlier work" in reply.lower()
    assert "no usage was billed" not in reply.lower()
    assert "safe to retry" not in reply.lower()


def test_usage_barrier_reply_accepts_complete_native_replay_proof() -> None:
    reply = build_terminal_reply(
        {
            "status": "failed",
            "error_class": "usage_accounting_busy",
            "usage_call_index": 1,
            "no_prior_provider_dispatch": True,
            "replay_safe": True,
        }
    )

    assert "no usage was billed" in reply.lower()
    assert "safe to retry" in reply.lower()


def test_build_terminal_reply_accepts_agent_task_record_like_objects() -> None:
    record = SimpleNamespace(
        status=AgentTaskStatus.TIMEOUT,
        terminal_reason="timeout",
        error_class="TimeoutError",
        error_message="Gateway task timeout: Stream idle for more than 120s",
    )

    message = build_terminal_reply(record, surface="terminal", locale="en")

    assert "timed out" in message.lower()
    for raw in RAW_INTERNAL_STRINGS:
        assert raw not in message


def test_repetition_loop_has_stable_code_and_specific_terminal_reply() -> None:
    code = safe_provider_failure_code(
        "model_repetition_loop_detected",
        "unknown",
    )
    message = build_terminal_reply(
        {
            "status": "failed",
            "terminal_reason": code,
            "error_class": code,
        }
    )

    assert code == "model_repetition_loop_detected"
    assert "repeating" in message.lower()
    assert "stopped" in message.lower()


def test_sanitize_agent_error_rewrites_raw_provider_output_limit_message() -> None:
    error_class, message = sanitize_agent_error(
        "Provider output limit reached before completion",
        fallback_error_class="provider_output_truncated",
    )

    assert error_class == "provider_output_truncated"
    assert "output limit" in message.lower()
    assert "Provider output limit reached before completion" not in message


def test_sanitize_agent_error_rewrites_raw_iteration_timeout_message() -> None:
    error_class, message = sanitize_agent_error(
        "Iteration 1 exceeded iteration_timeout",
        fallback_error_class="iteration_timeout",
    )

    assert error_class == "iteration_timeout"
    assert "timed out" in message.lower()
    assert "Iteration 1 exceeded iteration_timeout" not in message


def test_safe_provider_failure_message_is_allowlisted() -> None:
    assert safe_provider_failure_message("rate_limited") == (
        "The model provider is rate-limiting requests. Try again later."
    )
    assert safe_provider_failure_message("unrecognized-private-provider-body") == (
        "The model provider request failed."
    )
    assert safe_provider_failure_code("429", "rate_limited") == "429"
    assert safe_provider_failure_code(
        "PRIVATE_PROVIDER_CODE_BODY",
        "rate_limited",
    ) == "provider_rate_limited"
