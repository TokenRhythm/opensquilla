from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.session.models import AgentTaskStatus
from opensquilla.session.terminal_reply import build_terminal_reply, sanitize_agent_error

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
