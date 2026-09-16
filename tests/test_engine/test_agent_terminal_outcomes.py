from __future__ import annotations

import pytest

from opensquilla.engine.outcome import outcome_from_error

STOP_SURFACE_INVENTORY = {
    "max_iterations": {
        "desired_kind": "partial",
        "recovery_first": True,
        "interactive_terminal_by_default": False,
    },
    "provider_request_budget_exhausted": {
        "desired_kind": "budgetLimited",
        "recovery_first": True,
        "interactive_terminal_by_default": False,
    },
    "turn_llm_call_budget_exceeded": {
        "desired_kind": "budgetLimited",
        "recovery_first": True,
        "interactive_terminal_by_default": False,
    },
    "turn_tool_error_budget_exceeded": {
        "desired_kind": "partial",
        "recovery_first": True,
        "interactive_terminal_by_default": False,
    },
    "sandbox_threshold_exceeded": {
        "desired_kind": "blocked",
        "recovery_first": True,
        "interactive_terminal_by_default": False,
    },
    "iteration_timeout": {
        "desired_kind": "interrupted",
        "recovery_first": True,
        "interactive_terminal_by_default": False,
    },
    "compaction_refused_flush_timeout": {
        "desired_kind": "blocked",
        "recovery_first": True,
        "interactive_terminal_by_default": False,
    },
    "compaction_refused_memory_flush": {
        "desired_kind": "blocked",
        "recovery_first": True,
        "interactive_terminal_by_default": False,
    },
    "compaction_refused_empty_summary": {
        "desired_kind": "blocked",
        "recovery_first": True,
        "interactive_terminal_by_default": False,
    },
    "tool_failure_loop_exhausted": {
        "desired_kind": "partial",
        "recovery_first": True,
        "interactive_terminal_by_default": False,
    },
}


@pytest.mark.parametrize(
    "code, expected",
    [(k, v["desired_kind"]) for k, v in STOP_SURFACE_INVENTORY.items()],
)
def test_stop_surface_codes_have_normalized_outcomes(code: str, expected: str) -> None:
    assert outcome_from_error(code=code, message="stop").kind == expected


def test_stop_surface_inventory_tracks_recovery_first_contract() -> None:
    assert all(item["recovery_first"] for item in STOP_SURFACE_INVENTORY.values())
    assert not any(
        item["interactive_terminal_by_default"]
        for item in STOP_SURFACE_INVENTORY.values()
    )
