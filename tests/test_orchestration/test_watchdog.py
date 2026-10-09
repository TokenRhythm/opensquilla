from __future__ import annotations

from opensquilla.orchestration.models import TaskOutcome
from opensquilla.orchestration.watchdog import terminal_subtree_interrupt_reason


def test_exact_repeat_interrupt_stops_only_current_activation() -> None:
    assert (
        terminal_subtree_interrupt_reason(
            TaskOutcome.FAILED,
            {"terminal_reason": "repeated_tool_call_blocked"},
        )
        is None
    )
    assert (
        terminal_subtree_interrupt_reason(
            TaskOutcome.TIMED_OUT,
            {"terminal_reason": "hard_deadline_exceeded"},
        )
        == "hard_deadline_exceeded"
    )
    assert terminal_subtree_interrupt_reason(TaskOutcome.FAILED, {"error": "boom"}) is None


def test_engine_error_codes_preserve_subtree_stop_semantics() -> None:
    assert (
        terminal_subtree_interrupt_reason(
            TaskOutcome.FAILED,
            {"terminal_reason": "error", "error_type": "subagent_max_iterations"},
        )
        == "max_iterations"
    )
    assert (
        terminal_subtree_interrupt_reason(
            TaskOutcome.FAILED,
            {"terminal_reason": "error", "error_type": "subagent_tool_failure_fallback"},
        )
        == "repeated_failure"
    )
    assert (
        terminal_subtree_interrupt_reason(
            TaskOutcome.FAILED,
            {"terminal_reason": "model_repetition_loop_detected"},
        )
        == "model_repetition_loop_detected"
    )
