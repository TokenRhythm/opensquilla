"""Optional conversation-continuity question for the native JEV request."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class ContextRestartAssessment:
    completed_turns: int
    history_tokens: int
    context_window_tokens: int
    threshold_tokens: int


@dataclass(frozen=True)
class ContextRestartDecision:
    action: Literal["keep_context", "new_session"]
    confidence: float


def context_restart_state(assessment: ContextRestartAssessment) -> dict[str, int | bool]:
    """Expose measured history separately from the size of the current input."""
    return {
        "completed_conversation_turns": assessment.completed_turns,
        "history_tokens": assessment.history_tokens,
        "context_window_tokens": assessment.context_window_tokens,
        "history_threshold_tokens": assessment.threshold_tokens,
        "history_pressure": assessment.history_tokens >= assessment.threshold_tokens,
    }


def context_restart_question(assessment: ContextRestartAssessment) -> dict[str, Any]:
    """Ask about handoff benefits after conversation age and capacity gates pass."""
    return {
        "type": "choice",
        "instructions": (
            "Independently assess conversation continuity, using session_continuity in the "
            "state and the current task evidence. This question does not choose an execution "
            "model. This question is eligible only when both the completed conversation "
            "count and the measured history-pressure threshold have been reached for the "
            "current model's known context window. Both are necessary, but neither is an "
            "instruction to restart. Many turns or a topic change with plenty of context "
            "remaining must not cause a restart suggestion. A large current input, "
            "tool-call iterations, or a request quoted inside tool output is not a reason to "
            "restart. Prefer keeping context when exact earlier details are still needed, "
            "the same task is progressing coherently, or the benefit of a summary is unclear. "
            "Select new_session only when a concise handoff of goals, verified findings, "
            "preferences, file references and unfinished work would improve the remaining "
            "conversation despite losing verbatim detail. This is a suggestion requiring "
            "explicit user confirmation, never permission to interrupt a tool or move data."
        ),
        "criteria": {
            "keep_context": (
                "Continue in the existing conversation. Its history is still useful for the "
                "remaining task, exact context matters, or there is insufficient evidence "
                "that a summarized continuation would help."
            ),
            "new_session": (
                f"The conversation already has {assessment.completed_turns} completed turns "
                f"and its accumulated history has reached the {assessment.threshold_tokens:,} "
                "token pressure threshold of the current model's known context window. "
                "Recommend an optional summarized continuation only if it would free "
                "needed capacity while retaining the goals, verified facts and unfinished "
                "work. A topic change alone is insufficient. The source "
                "conversation remains accessible, and the user must choose whether to move."
            ),
        },
    }


def _probability(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("invalid_context_probability")
    if isinstance(value, str) and len(value) > 64:
        raise ValueError("invalid_context_probability")
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ValueError("invalid_context_probability") from None
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise ValueError("invalid_context_probability")
    return number


def parse_context_restart_answer(
    answer: object,
    assessment: ContextRestartAssessment,
    *,
    threshold: float,
) -> ContextRestartDecision:
    """Validate the optional native answer without interpreting model advice."""
    cutoff = _probability(threshold)
    if (
        assessment.completed_turns < 2
        or assessment.threshold_tokens < 1
        or assessment.context_window_tokens < 1
        or assessment.history_tokens < assessment.threshold_tokens
        or not isinstance(answer, Mapping)
        or answer.get("type") != "choice"
    ):
        raise ValueError("invalid_context_choice")
    choices = {"keep_context", "new_session"}
    choice = answer.get("choice")
    distribution = answer.get("probabilities")
    if (
        not isinstance(choice, str) or choice not in choices
        or not isinstance(distribution, Mapping) or set(distribution) != choices
    ):
        raise ValueError("invalid_context_choice")
    scores = {key: _probability(distribution[key]) for key in choices}
    if (
        abs(sum(scores.values()) - 1) > 0.02
        or scores[choice] + 1e-6 < max(scores.values())
    ):
        raise ValueError("inconsistent_context_choice")
    if answer.get("confidence") is not None:
        _probability(answer["confidence"])
    confidence = scores[choice]
    return ContextRestartDecision(
        action=(
            "new_session" if choice == "new_session" and confidence >= cutoff else "keep_context"
        ),
        confidence=confidence,
    )
