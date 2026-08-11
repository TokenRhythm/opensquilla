from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
import structlog.testing

from opensquilla.provider.ensemble_observability import (
    log_ensemble_decision_failed,
    log_ensemble_decision_skipped,
    log_ensemble_decision_started,
    log_ensemble_decision_steps,
)


@pytest.mark.parametrize(
    "selection_mode",
    ["static_openrouter_b5", "static_tokenrhythm_b5", "custom_b5"],
)
def test_fixed_modes_log_each_selected_member(selection_mode: str) -> None:
    plan = {
        "strategy": selection_mode,
        "selected_P": ["provider:model-a", "provider:model-b"],
        "selected_A": "provider:aggregator",
    }

    with structlog.testing.capture_logs() as captured:
        log_ensemble_decision_steps(
            decision_id=f"decision-{selection_mode}",
            selection_mode=selection_mode,
            profile_name=selection_mode,
            selection_plan=plan,
        )

    assert [row["event"] for row in captured] == [
        "llm_ensemble.routing.proposer_step_recorded",
        "llm_ensemble.routing.proposer_step_recorded",
        "llm_ensemble.routing.aggregator_selected",
        "llm_ensemble.routing.decision_completed",
    ]
    assert [row["sequence"] for row in captured] == [1, 2, 3, 4]
    assert all(row["decision_id"] == f"decision-{selection_mode}" for row in captured)


def test_router_dynamic_logs_every_ranking_stage_in_sequence() -> None:
    plan: dict[str, Any] = {
        "strategy": "router_dynamic",
        "task_analyzer": {
            "source": "router_fallback",
            "schema_valid": False,
            "fallback_reason": "provider_unavailable",
        },
        "task_profile_hash": "task-hash",
        "request_context_hash": "request-hash",
        "effective_tier": 3,
        "N_min": 2,
        "N_max": 3,
        "bound_reasons": ["tier_3"],
        "top_l": 2,
        "quality_floor": 0.42,
        "session": {"intent": "new_task"},
        "candidate_pool": [
            {"identity": "provider:model-a", "model": "model-a"},
            {"identity": "provider:model-b", "model": "model-b"},
        ],
        "hard_filter": {
            "proposer_results": [{"identity": "provider:model-a", "eligible": True, "reasons": []}],
            "aggregator_results": [
                {"identity": "provider:model-b", "eligible": True, "reasons": []}
            ],
        },
        "model_scores": [{"identity": "provider:model-a", "S_base": 0.8}],
        "selection_steps": [{"step": 1, "selected": "provider:model-a", "marginal_gain": 0.7}],
        "aggregator": {"scores": [{"identity": "provider:model-b", "Score_agg": 0.9}]},
        "selected_P": ["provider:model-a"],
        "selected_A": "provider:model-b",
        "user_profile_enabled": False,
    }

    with structlog.testing.capture_logs() as captured:
        log_ensemble_decision_steps(
            decision_id="dynamic-decision",
            selection_mode="router_dynamic",
            profile_name="router_dynamic/c2",
            selection_plan=plan,
        )

    event_names = [row["event"] for row in captured]
    assert event_names == [
        "llm_ensemble.routing.task_analysis_recorded",
        "llm_ensemble.routing.proposer_bounds_recorded",
        "llm_ensemble.routing.session_adjustment_recorded",
        "llm_ensemble.routing.candidate_recorded",
        "llm_ensemble.routing.candidate_recorded",
        "llm_ensemble.routing.hard_filter_recorded",
        "llm_ensemble.routing.hard_filter_recorded",
        "llm_ensemble.routing.model_score_recorded",
        "llm_ensemble.routing.proposer_step_recorded",
        "llm_ensemble.routing.aggregator_score_recorded",
        "llm_ensemble.routing.aggregator_selected",
        "llm_ensemble.routing.decision_completed",
    ]
    assert [row["sequence"] for row in captured] == list(range(1, len(captured) + 1))
    assert all(row["decision_id"] == "dynamic-decision" for row in captured)
    assert captured[-1]["user_profile_enabled"] is False
    assert "llm_ensemble.routing.thinking_assignment_recorded" not in event_names
    assert not {
        "thinking_assignment",
        "assignment_reasons",
        "unsupported_level_fallbacks",
        "policy_versions",
    }.intersection(captured[-1])
    assert "role_reliability_coverage" not in captured[-1]


def test_router_dynamic_logs_selected_role_reliability_coverage() -> None:
    plan: dict[str, Any] = {
        "strategy": "router_dynamic",
        "ranking_parameters": {
            "role_reliability": {
                "penalty_weight": 0.4,
                "prior_success": 10,
                "prior_failure": 0,
            }
        },
        "task_analyzer": {},
        "session": {},
        "hard_filter": {},
        "model_scores": [
            {
                "identity": "provider:model-a",
                "role_reliability": {
                    "role": "proposer",
                    "success": 4,
                    "failure": 1,
                    "observed": 5,
                },
            },
            {
                "identity": "provider:model-b",
                "role_reliability": {
                    "role": "proposer",
                    "success": 0,
                    "failure": 0,
                    "observed": 0,
                },
            },
        ],
        "aggregator": {
            "selected": {
                "identity": "provider:model-c",
                "role_reliability": {
                    "role": "aggregator",
                    "success": 9,
                    "failure": 1,
                    "observed": 10,
                },
            }
        },
        "selected_P": ["provider:model-a", "provider:model-b"],
        "selected_A": "provider:model-c",
    }
    original = deepcopy(plan)

    with structlog.testing.capture_logs() as captured:
        log_ensemble_decision_steps(
            decision_id="reliability-coverage",
            selection_mode="router_dynamic",
            profile_name="router_dynamic/c2",
            selection_plan=plan,
        )

    assert plan == original
    coverage = captured[-1]["role_reliability_coverage"]
    assert coverage == {
        "schema": "opensquilla.role-reliability-coverage/v1",
        "coverage_unit": "selected_model_identity",
        "proposer": {
            "selected_count": 2,
            "observed_selected_count": 1,
            "zero_observation_selected_count": 1,
            "window_observed_attempt_count": 5,
            "coverage_basis_points": 5000,
        },
        "aggregator": {
            "selected_count": 1,
            "observed_selected_count": 1,
            "zero_observation_selected_count": 0,
            "window_observed_attempt_count": 10,
            "coverage_basis_points": 10_000,
        },
    }
    assert [row["sequence"] for row in captured] == list(
        range(1, len(captured) + 1)
    )
    assert captured[-1]["event"] == "llm_ensemble.routing.decision_completed"


def test_router_dynamic_omits_malformed_reliability_coverage_fail_open() -> None:
    plan: dict[str, Any] = {
        "strategy": "router_dynamic",
        "ranking_parameters": {"role_reliability": {"penalty_weight": 0.4}},
        "task_analyzer": {},
        "session": {},
        "hard_filter": {},
        "model_scores": [
            {
                "identity": "provider:model-a",
                "role_reliability": {
                    "role": "proposer",
                    "success": 1,
                    "failure": 0,
                    "observed": 2,
                },
            }
        ],
        "aggregator": {"selected": {"identity": "provider:model-b"}},
        "selected_P": ["provider:model-a"],
        "selected_A": "provider:model-b",
    }

    with structlog.testing.capture_logs() as captured:
        log_ensemble_decision_steps(
            decision_id="malformed-reliability-coverage",
            selection_mode="router_dynamic",
            profile_name="router_dynamic/c2",
            selection_plan=plan,
        )

    assert "role_reliability_coverage" not in captured[-1]
    assert not any(
        row["event"] == "llm_ensemble.routing.decision_log_failed"
        for row in captured
    )


def test_router_dynamic_logs_thinking_assignment_audit_fields() -> None:
    assignment = {
        "proposers": {"provider:model-a": "high"},
        "aggregator": "highest",
        "thinking_policy_version": "thinking-policy-v1",
    }
    plan: dict[str, Any] = {
        "strategy": "router_dynamic",
        "task_analyzer": {},
        "session": {},
        "hard_filter": {},
        "aggregator": {},
        "selected_P": ["provider:model-a"],
        "selected_A": "provider:model-b",
        "thinking_assignment": assignment,
        "assignment_reasons": {
            "proposers": {"provider:model-a": ["tier_3_base_high"]},
            "aggregator": ["aggregator_level_step"],
        },
        "unsupported_level_fallbacks": [
            {
                "identity": "provider:model-b",
                "requested_level": "highest",
                "effective_level": "high",
            }
        ],
        "policy_versions": {
            "thinking": "thinking-policy-v1",
        },
    }

    with structlog.testing.capture_logs() as captured:
        log_ensemble_decision_steps(
            decision_id="thinking-decision",
            selection_mode="router_dynamic",
            profile_name="router_dynamic/c2",
            selection_plan=plan,
        )

    assignment_event = next(
        row
        for row in captured
        if row["event"]
        == "llm_ensemble.routing.thinking_assignment_recorded"
    )
    completed = captured[-1]
    assert assignment_event["thinking_assignment"] == assignment
    assert assignment_event["assignment_reasons"] == plan["assignment_reasons"]
    assert assignment_event["unsupported_level_fallbacks"] == (
        plan["unsupported_level_fallbacks"]
    )
    assert completed["thinking_assignment"] == assignment
    assert completed["policy_versions"]["thinking"] == "thinking-policy-v1"


def test_router_tree_baseline_logs_pool_slots_and_aggregator_scores() -> None:
    plan = {
        "strategy": "router_tree_baseline",
        "candidate_pool": [
            {"identity": "provider:model-a", "model": "model-a"},
            {"identity": "provider:model-b", "model": "model-b"},
        ],
        "slots": [
            {"slot": "anchor", "selected": {"identity": "provider:model-a"}},
            {
                "slot": "contrast",
                "selected": {"identity": "provider:model-b"},
                "score": 0.8,
            },
        ],
        "aggregator": {
            "top_candidates": [
                {"identity": "provider:model-b", "score": 0.9},
                {"identity": "provider:model-a", "score": 0.7},
            ]
        },
        "selected_P": ["provider:model-a", "provider:model-b"],
        "selected_A": "provider:model-b",
    }

    with structlog.testing.capture_logs() as captured:
        log_ensemble_decision_steps(
            decision_id="tree-decision",
            selection_mode="router_tree_baseline",
            profile_name="router_tree_baseline/c2",
            selection_plan=plan,
        )

    event_names = [row["event"] for row in captured]
    assert event_names.count("llm_ensemble.routing.candidate_recorded") == 2
    assert event_names.count("llm_ensemble.routing.proposer_step_recorded") == 2
    assert event_names.count("llm_ensemble.routing.aggregator_score_recorded") == 2
    assert event_names[-2:] == [
        "llm_ensemble.routing.aggregator_selected",
        "llm_ensemble.routing.decision_completed",
    ]


def test_decision_lifecycle_logs_local_route_and_failure_correlation() -> None:
    with structlog.testing.capture_logs() as captured:
        log_ensemble_decision_started(
            decision_id="failed-decision",
            selection_mode="router_dynamic",
            turn_metadata={
                "routed_tier": "c2",
                "routed_model": "provider:model",
                "routing_confidence": 0.91,
                "routing_extra": {
                    "route_class": "complex",
                    "base_tier": "c1",
                    "final_tier": "c2",
                },
            },
            user_profile_enabled=False,
        )
        log_ensemble_decision_failed(
            decision_id="failed-decision",
            selection_mode="router_dynamic",
            reason="ranking_unavailable",
            error=ValueError("synthetic failure"),
        )

    assert [row["event"] for row in captured] == [
        "llm_ensemble.routing.decision_started",
        "llm_ensemble.routing.decision_failed",
    ]
    assert [row["sequence"] for row in captured] == [0, 1]
    assert all(row["decision_id"] == "failed-decision" for row in captured)
    assert captured[0]["route_class"] == "complex"
    assert captured[1]["error_type"] == "ValueError"


def test_readiness_skip_closes_the_decision_lifecycle() -> None:
    with structlog.testing.capture_logs() as captured:
        log_ensemble_decision_started(
            decision_id="skipped-decision",
            selection_mode="static_openrouter_b5",
            turn_metadata={},
            user_profile_enabled=None,
        )
        log_ensemble_decision_skipped(
            decision_id="skipped-decision",
            selection_mode="static_openrouter_b5",
            reason="static_openrouter_b5_no_credential",
        )

    assert [row["event"] for row in captured] == [
        "llm_ensemble.routing.decision_started",
        "llm_ensemble.routing.decision_skipped",
    ]
    assert [row["sequence"] for row in captured] == [0, 1]
    assert all(row["decision_id"] == "skipped-decision" for row in captured)
