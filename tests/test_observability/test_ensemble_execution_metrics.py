from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest
import structlog.testing

from opensquilla.observability import ensemble_execution_metrics as metrics_module
from opensquilla.observability.ensemble_execution_metrics import (
    TRACE_SIZE_CAP_BYTES,
    TRACE_SIZE_VISIT_CAP,
    build_ensemble_execution_metrics,
    log_ensemble_execution_metrics,
    log_ensemble_execution_metrics_once,
)


def test_terminal_trace_projects_content_free_bounded_stage_metrics() -> None:
    private_text = "sk-private-never-emit user prompt and hidden reasoning"
    trace: dict[str, Any] = {
        "mode": "b5_fusion",
        "selection_strategy": "router_dynamic",
        "fallback_used": False,
        "llm_request_count": 4,
        "physical_request_count": 4,
        "usage_missing_count": 1,
        "selection_plan": {
            "strategy": "router_dynamic",
            "request_context": private_text,
        },
        "candidates": [
            {
                "request_started": True,
                "elapsed_ms": 120,
                "text": private_text,
                "execution": {
                    "admission": {
                        "outcome": "admitted",
                        "wait_ms": 7,
                    }
                },
            },
            {
                "request_started": False,
                "elapsed_ms": 999,
                "execution": {
                    "admission": {
                        "outcome": "timeout",
                        "wait_ms": 50,
                    }
                },
            },
            {
                "request_started": True,
                "elapsed_ms": 250,
            },
        ],
        "proposer_quorum": {
            "quorum_reached": True,
            "time_to_quorum_ms": 115,
            "grace_elapsed_ms": 9,
            "pending_at_quorum": 2,
            "cancellation": {"requested_task_count": 1},
            "cleanup": {
                "awaited_task_count": 1,
                "completed_task_count": 1,
                "lingering_task_count": 0,
                "stream_close_proven_count": 1,
                "stream_close_unproven_count": 0,
            },
        },
        "proposer_recovery": {
            "additional_physical_requests_started": 2,
        },
        "final_request": {
            "execution": {
                "admission": {
                    "outcome": "admitted",
                    "wait_ms": 3,
                }
            },
            "output": private_text,
        },
    }
    before = deepcopy(trace)

    metrics = build_ensemble_execution_metrics(
        trace,
        terminal_outcome="completed",
    )

    assert trace == before
    assert metrics == {
        "schema": "opensquilla.ensemble-execution-metrics/v1",
        "terminal_outcome": "completed",
        "execution_status": "success",
        "selection_family": "router_dynamic",
        "fallback_used_observed": True,
        "fallback_used": False,
        "task_analyzer_observed": False,
        "aggregator_recovery_observed": False,
        "aggregator_stage_observed": False,
        "trace_size_observed": True,
        "trace_compact_json_bytes": len(
            json.dumps(
                trace,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ),
        "trace_compact_json_bytes_capped": False,
        "trace_compact_json_bytes_cap": TRACE_SIZE_CAP_BYTES,
        "trace_compact_json_visit_cap": TRACE_SIZE_VISIT_CAP,
        "proposer_candidates_observed": True,
        "proposer_candidate_count": 3,
        "proposer_candidate_scan_count": 3,
        "proposer_candidate_scan_capped": False,
        "proposer_elapsed_observation_count": 2,
        "proposer_candidate_elapsed_ms_max": 250,
        "proposer_candidate_elapsed_ms_total": 370,
        "admission_observation_count": 3,
        "admission_wait_observation_count": 3,
        "admission_timeout_count": 1,
        "admission_rejected_count": 0,
        "admission_wait_ms_max": 50,
        "admission_wait_ms_total": 60,
        "quorum_observed": True,
        "quorum_reached_observed": True,
        "quorum_reached": True,
        "time_to_quorum_ms": 115,
        "quorum_grace_elapsed_ms": 9,
        "pending_at_quorum": 2,
        "cleanup_observed": True,
        "quorum_cancel_requested_task_count": 1,
        "cleanup_awaited_task_count": 1,
        "cleanup_completed_task_count": 1,
        "cleanup_lingering_task_count": 0,
        "cleanup_stream_close_proven_count": 1,
        "cleanup_stream_close_unproven_count": 0,
        "proposer_recovery_observed": True,
        "proposer_recovery_calls": 2,
        "physical_request_count_observed": True,
        "physical_request_count": 4,
        "unknown_usage_count_observed": True,
        "unknown_usage_count": 1,
    }
    assert private_text not in json.dumps(metrics, sort_keys=True)


def test_terminal_trace_projects_analyzer_and_aggregator_recovery() -> None:
    private_text = "sk-private model/user/reasoning/error text"
    trace: dict[str, Any] = {
        "selection_plan": {
            "task_analyzer": {
                "source": "llm_provider",
                "schema_valid": True,
                "provider": private_text,
                "model": private_text,
                "fallback_reason": private_text,
                "chain": {
                    "attempt_outcomes": [
                        {
                            "outcome": "failed",
                            "physical_request_count": 1,
                            "reason": private_text,
                        },
                        {
                            "outcome": "success",
                            "physical_request_count": 1,
                            "model": private_text,
                        },
                    ],
                    "selected_index": 1,
                    "exhausted": False,
                    "deadline": {
                        "configured_seconds": 2.0,
                        "elapsed_seconds": 0.1254,
                        "remaining_seconds": 1.8746,
                        "expired": False,
                    },
                },
            }
        },
        "aggregator_recovery": {
            "attempts": [
                {
                    "kind": "primary",
                    "request_started": True,
                    "physical_request_count": 1,
                    "outcome": "failed",
                    "code": private_text,
                },
                {
                    "kind": "continuation",
                    "request_started": True,
                    "physical_request_count": 1,
                    "outcome": "succeeded",
                    "trigger": private_text,
                },
            ],
            "success": True,
            "selected_kind": "continuation",
            "fallback_index": 0,
            "continuation_count": 1,
            "same_model_recovery_count": 0,
            "exhausted": False,
            "degraded": False,
        },
    }
    before = deepcopy(trace)

    metrics = build_ensemble_execution_metrics(
        trace,
        terminal_outcome="completed",
    )

    assert trace == before
    assert metrics["task_analyzer_observed"] is True
    assert metrics["task_analyzer_source_family"] == "live_provider"
    assert metrics["task_analyzer_schema_valid"] is True
    assert metrics["task_analyzer_chain_attempt_count"] == 2
    assert metrics["task_analyzer_chain_attempt_scan_count"] == 2
    assert metrics["task_analyzer_chain_attempt_scan_capped"] is False
    assert metrics["task_analyzer_chain_success_count"] == 1
    assert metrics["task_analyzer_chain_failed_count"] == 1
    assert metrics["task_analyzer_chain_physical_request_count"] == 2
    assert metrics["task_analyzer_selected"] is True
    assert metrics["task_analyzer_selected_index"] == 1
    assert metrics["task_analyzer_exhausted"] is False
    assert metrics["task_analyzer_deadline_configured_ms"] == 2_000
    assert metrics["task_analyzer_elapsed_ms"] == 125
    assert metrics["task_analyzer_deadline_remaining_ms"] == 1_875
    assert metrics["task_analyzer_deadline_expired"] is False
    assert metrics["aggregator_recovery_observed"] is True
    assert metrics["aggregator_stage_observed"] is True
    assert metrics["aggregator_recovery_attempt_count"] == 2
    assert metrics["aggregator_recovery_attempt_scan_capped"] is False
    assert metrics["aggregator_primary_attempt_count"] == 1
    assert metrics["aggregator_continuation_attempt_count"] == 1
    assert metrics["aggregator_request_started_count"] == 2
    assert metrics["aggregator_physical_request_count"] == 2
    assert metrics["aggregator_succeeded_attempt_count"] == 1
    assert metrics["aggregator_failed_attempt_count"] == 1
    assert metrics["aggregator_abandoned_attempt_count"] == 0
    assert metrics["aggregator_unsuccessful_attempt_count"] == 1
    assert metrics["aggregator_unavailable_attempt_count"] == 0
    assert metrics["aggregator_unknown_outcome_attempt_count"] == 0
    assert metrics["aggregator_selected_kind"] == "continuation"
    assert metrics["aggregator_fallback_index"] == 0
    assert metrics["aggregator_recovery_success"] is True
    assert metrics["aggregator_recovery_exhausted"] is False
    assert metrics["aggregator_recovery_degraded"] is False
    assert metrics["aggregator_continuation_count"] == 1
    assert metrics["aggregator_same_model_recovery_count"] == 0
    assert private_text not in json.dumps(metrics, sort_keys=True)


def test_preinitialized_recovery_block_does_not_enter_aggregator_denominator() -> None:
    private_text = "private/provider:model"
    trace = {
        "aggregator_recovery": {
            "schema": "opensquilla.ensemble-aggregator-recovery/v1",
            "mode": "serving",
            "candidate_count": 3,
            "candidate_ids": [private_text],
            "max_tokens_cap": 8_192,
            "visible_answer_reserve_tokens": 1_024,
            "attempts": [],
            "proposer_reused": True,
            "success": False,
        },
        "run_outcome": "proposer_quorum_failed",
    }

    metrics = build_ensemble_execution_metrics(
        trace,
        terminal_outcome="failed",
    )

    assert metrics["aggregator_recovery_observed"] is True
    assert metrics["aggregator_recovery_attempts_observed"] is True
    assert metrics["aggregator_recovery_attempt_count"] == 0
    assert metrics["aggregator_stage_observed"] is False
    assert metrics["aggregator_recovery_success_observed"] is False
    assert "aggregator_recovery_success" not in metrics
    assert metrics["aggregator_recovery_exhausted_observed"] is False
    assert metrics["aggregator_recovery_degraded_observed"] is False
    assert metrics["aggregator_continuation_count_observed"] is False
    assert metrics["aggregator_same_model_recovery_count_observed"] is False
    assert private_text not in json.dumps(metrics, sort_keys=True)


def test_abandoned_and_unknown_aggregator_outcomes_are_not_hidden() -> None:
    private_text = "private failure code and model"
    metrics = build_ensemble_execution_metrics(
        {
            "aggregator_recovery": {
                "attempts": [
                    {
                        "kind": "primary",
                        "request_started": True,
                        "physical_request_count": 1,
                        "outcome": "abandoned",
                        "code": private_text,
                    },
                    {
                        "kind": "future_recovery_kind",
                        "request_started": True,
                        "physical_request_count": 1,
                        "outcome": "future_terminal_outcome",
                        "requested_model": private_text,
                    },
                    {
                        "kind": "model_fallback",
                        "request_started": True,
                        "physical_request_count": 1,
                        "outcome": "member_unavailable",
                    },
                    {
                        "kind": "model_fallback",
                        "physical_request_count": 0,
                        "outcome": "provider_build_failed",
                    },
                    {
                        "kind": "model_fallback",
                        "request_started": False,
                        "physical_request_count": 0,
                        "outcome": "runtime_health_deferred",
                    },
                ],
                "success": False,
                "exhausted": True,
            }
        },
        terminal_outcome="failed",
    )

    assert metrics["aggregator_stage_observed"] is True
    assert metrics["aggregator_failed_attempt_count"] == 0
    assert metrics["aggregator_abandoned_attempt_count"] == 1
    assert metrics["aggregator_unsuccessful_attempt_count"] == 1
    assert metrics["aggregator_unavailable_attempt_count"] == 1
    assert metrics["aggregator_unknown_outcome_attempt_count"] == 3
    assert metrics["aggregator_unknown_kind_attempt_count"] == 1
    assert (
        metrics["aggregator_succeeded_attempt_count"]
        + metrics["aggregator_failed_attempt_count"]
        + metrics["aggregator_abandoned_attempt_count"]
        + metrics["aggregator_unavailable_attempt_count"]
        + metrics["aggregator_unknown_outcome_attempt_count"]
        == metrics["aggregator_recovery_attempt_scan_count"]
    )
    assert metrics["aggregator_recovery_success"] is False
    assert metrics["aggregator_recovery_exhausted"] is True
    assert private_text not in json.dumps(metrics, sort_keys=True)


def test_stage_projection_is_bounded_and_malformed_values_are_omitted() -> None:
    private_text = "private model, provider, code, prompt, and reasoning"
    analyzer_attempts = [
        {
            "outcome": "failed",
            "physical_request_count": 1,
            "reason": private_text,
        }
        for _ in range(10)
    ]
    aggregator_attempts = [
        {
            "kind": "model_fallback",
            "request_started": False,
            "physical_request_count": 0,
            "outcome": "member_unavailable",
            "requested_model": private_text,
        }
        for _ in range(18)
    ]
    trace: dict[str, Any] = {
        "selection_plan": {
            "task_analyzer": {
                "source": private_text,
                "schema_valid": 1,
                "chain": {
                    "attempt_outcomes": analyzer_attempts,
                    "selected_index": None,
                    "exhausted": True,
                    "deadline": {
                        "configured_seconds": 10**1000,
                        "elapsed_seconds": -1.0,
                        "remaining_seconds": True,
                        "expired": True,
                    },
                },
            }
        },
        "aggregator_recovery": {
            "attempts": aggregator_attempts,
            "success": False,
            "selected_kind": private_text,
            "fallback_index": True,
            "continuation_count": -1,
            "same_model_recovery_count": float("nan"),
            "exhausted": True,
            "degraded": True,
        },
    }
    before = deepcopy(trace)

    metrics = build_ensemble_execution_metrics(
        trace,
        terminal_outcome="completed",
    )

    assert trace == before
    assert metrics["execution_status"] == "degraded"
    assert metrics["task_analyzer_source_family"] == "unknown"
    assert metrics["task_analyzer_schema_valid_observed"] is False
    assert metrics["task_analyzer_chain_attempt_count"] == 10
    assert metrics["task_analyzer_chain_attempt_scan_count"] == 8
    assert metrics["task_analyzer_chain_attempt_scan_capped"] is True
    assert metrics["task_analyzer_chain_failed_count"] == 8
    assert metrics["task_analyzer_chain_physical_request_count"] == 8
    assert metrics["task_analyzer_selected"] is False
    assert "task_analyzer_selected_index" not in metrics
    assert metrics["task_analyzer_exhausted"] is True
    assert "task_analyzer_deadline_configured_ms" not in metrics
    assert "task_analyzer_elapsed_ms" not in metrics
    assert "task_analyzer_deadline_remaining_ms" not in metrics
    assert metrics["task_analyzer_deadline_expired"] is True
    assert metrics["aggregator_recovery_attempt_count"] == 18
    assert metrics["aggregator_recovery_attempt_scan_count"] == 16
    assert metrics["aggregator_recovery_attempt_scan_capped"] is True
    assert metrics["aggregator_model_fallback_attempt_count"] == 16
    assert metrics["aggregator_stage_observed"] is True
    assert metrics["aggregator_abandoned_attempt_count"] == 0
    assert metrics["aggregator_unsuccessful_attempt_count"] == 0
    assert metrics["aggregator_unavailable_attempt_count"] == 16
    assert metrics["aggregator_unknown_outcome_attempt_count"] == 0
    assert metrics["aggregator_request_started_count"] == 0
    assert metrics["aggregator_physical_request_count"] == 0
    assert metrics["aggregator_selected_kind"] == "unknown"
    assert metrics["aggregator_fallback_index_observed"] is False
    assert "aggregator_fallback_index" not in metrics
    assert metrics["aggregator_continuation_count_observed"] is False
    assert "aggregator_continuation_count" not in metrics
    assert metrics["aggregator_same_model_recovery_count_observed"] is False
    assert "aggregator_same_model_recovery_count" not in metrics
    assert private_text not in json.dumps(metrics, sort_keys=True)


def test_trace_size_matches_compact_json_for_escapes_and_unicode() -> None:
    trace = {
        "controls": "quote=\" slash=\\ newline=\n tab=\t nul=\x00",
        "unicode": "界🙂\u2028",
        "values": [None, True, False, -7, 1.25],
    }

    metrics = build_ensemble_execution_metrics(
        trace,
        terminal_outcome="completed",
    )

    expected = len(
        json.dumps(
            trace,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    assert metrics["trace_compact_json_bytes"] == expected
    assert metrics["trace_compact_json_bytes_capped"] is False
    assert metrics["trace_compact_json_visit_cap"] == TRACE_SIZE_VISIT_CAP


def test_trace_size_measurement_is_clamped_before_full_string_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_dumps = metrics_module.json.dumps
    measured_string_chunks: list[int] = []

    def counting_dumps(value: Any, *args: Any, **kwargs: Any) -> str:
        if isinstance(value, str):
            measured_string_chunks.append(len(value))
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(metrics_module.json, "dumps", counting_dumps)
    trace = {
        "selection_plan": {
            "request_context": "界" * (TRACE_SIZE_CAP_BYTES * 2),
        }
    }

    metrics = build_ensemble_execution_metrics(
        trace,
        terminal_outcome="completed",
    )

    assert "trace_compact_json_bytes" not in metrics
    assert (
        metrics["trace_compact_json_bytes_lower_bound"]
        == TRACE_SIZE_CAP_BYTES
    )
    assert metrics["trace_compact_json_bytes_capped"] is True
    assert metrics["trace_compact_json_bytes_cap"] == TRACE_SIZE_CAP_BYTES
    assert metrics["trace_compact_json_bytes_cap_reason"] == "byte_limit"
    assert measured_string_chunks
    assert max(measured_string_chunks) <= 4_096
    assert sum(measured_string_chunks) < TRACE_SIZE_CAP_BYTES * 2


def test_malformed_fields_are_omitted_without_inventing_observations() -> None:
    metrics = build_ensemble_execution_metrics(
        {
            "selection_strategy": "arbitrary-custom-mode",
            "candidates": [
                {
                    "request_started": True,
                    "elapsed_ms": True,
                    "execution": {
                        "admission": {
                            "outcome": "timeout",
                            "wait_ms": -1,
                        }
                    },
                },
                {
                    "request_started": True,
                    "elapsed_ms": (1 << 63) - 1,
                    "execution": {
                        "admission": {
                            "outcome": "admitted",
                            "wait_ms": (1 << 63) - 1,
                        }
                    },
                },
                {
                    "request_started": True,
                    "elapsed_ms": (1 << 63) - 1,
                    "execution": {
                        "admission": {
                            "outcome": "admitted",
                            "wait_ms": (1 << 63) - 1,
                        }
                    },
                },
            ],
            "proposer_quorum": {"time_to_quorum_ms": float("nan")},
            "proposer_recovery": {
                "additional_physical_requests_started": True,
            },
            "usage_missing_count": "7",
            "physical_request_count": 10**1000,
            "not_json": float("nan"),
        },
        terminal_outcome="failed",
    )

    assert metrics["selection_family"] == "fixed"
    assert metrics["execution_status"] == "failed"
    assert metrics["fallback_used_observed"] is False
    assert "fallback_used" not in metrics
    assert metrics["trace_size_observed"] is False
    assert metrics["proposer_candidates_observed"] is True
    assert metrics["proposer_elapsed_observation_count"] == 2
    assert metrics["proposer_candidate_elapsed_ms_max"] == (1 << 63) - 1
    assert "proposer_candidate_elapsed_ms_total" not in metrics
    assert metrics["admission_observation_count"] == 3
    assert metrics["admission_wait_observation_count"] == 2
    assert metrics["admission_timeout_count"] == 1
    assert metrics["admission_wait_ms_max"] == (1 << 63) - 1
    assert "admission_wait_ms_total" not in metrics
    assert metrics["quorum_observed"] is True
    assert metrics["quorum_reached_observed"] is False
    assert "quorum_reached" not in metrics
    assert "time_to_quorum_ms" not in metrics
    assert metrics["proposer_recovery_observed"] is False
    assert metrics["physical_request_count_observed"] is False
    assert "physical_request_count" not in metrics
    assert metrics["unknown_usage_count_observed"] is False
    assert "unknown_usage_count" not in metrics


@pytest.mark.parametrize("float_value", [0.0, 1.0, 1.9])
def test_integer_trace_metrics_reject_float_values(float_value: float) -> None:
    metrics = build_ensemble_execution_metrics(
        {
            "candidates": [
                {
                    "request_started": True,
                    "elapsed_ms": float_value,
                    "execution": {
                        "admission": {
                            "outcome": "admitted",
                            "wait_ms": float_value,
                        }
                    },
                }
            ],
            "proposer_quorum": {
                "time_to_quorum_ms": float_value,
                "grace_elapsed_ms": float_value,
                "pending_at_quorum": float_value,
                "cancellation": {
                    "requested_task_count": float_value,
                },
                "cleanup": {
                    "awaited_task_count": float_value,
                    "completed_task_count": float_value,
                    "lingering_task_count": float_value,
                    "stream_close_proven_count": float_value,
                    "stream_close_unproven_count": float_value,
                },
            },
            "proposer_recovery": {
                "additional_physical_requests_started": float_value,
            },
            "physical_request_count": float_value,
            "usage_missing_count": float_value,
        },
        terminal_outcome="completed",
    )

    assert metrics["proposer_elapsed_observation_count"] == 0
    assert "proposer_candidate_elapsed_ms_max" not in metrics
    assert "proposer_candidate_elapsed_ms_total" not in metrics
    assert metrics["admission_wait_observation_count"] == 0
    assert "admission_wait_ms_max" not in metrics
    assert "admission_wait_ms_total" not in metrics
    for key in (
        "time_to_quorum_ms",
        "quorum_grace_elapsed_ms",
        "pending_at_quorum",
        "quorum_cancel_requested_task_count",
        "cleanup_awaited_task_count",
        "cleanup_completed_task_count",
        "cleanup_lingering_task_count",
        "cleanup_stream_close_proven_count",
        "cleanup_stream_close_unproven_count",
    ):
        assert key not in metrics
    assert metrics["proposer_recovery_observed"] is False
    assert "proposer_recovery_calls" not in metrics
    assert metrics["physical_request_count_observed"] is False
    assert "physical_request_count" not in metrics
    assert metrics["unknown_usage_count_observed"] is False
    assert "unknown_usage_count" not in metrics


def test_trace_size_wide_structure_stops_at_visit_cap() -> None:
    trace = {"wide": [None] * (TRACE_SIZE_VISIT_CAP + 1)}

    metrics = build_ensemble_execution_metrics(
        trace,
        terminal_outcome="completed",
    )

    assert metrics["trace_compact_json_bytes_capped"] is True
    assert metrics["trace_compact_json_bytes_cap_reason"] == "visit_limit"
    assert "trace_compact_json_bytes" not in metrics
    assert metrics["trace_compact_json_bytes_lower_bound"] > 0


@pytest.mark.parametrize(
    ("terminal_outcomes", "expected_outcome"),
    [
        (["failed"], "failed"),
        (["failed", "failed"], "failed"),
        (["completed", "failed"], "completed"),
    ],
)
def test_per_call_metrics_gate_emits_only_the_first_terminal_event(
    terminal_outcomes: list[str],
    expected_outcome: str,
) -> None:
    emitted = False
    with structlog.testing.capture_logs() as captured:
        for terminal_outcome in terminal_outcomes:
            emitted = log_ensemble_execution_metrics_once(
                {"fallback_used": False},
                terminal_outcome=terminal_outcome,
                already_emitted=emitted,
            )

    rows = [
        row
        for row in captured
        if row.get("event") == "llm_ensemble.execution.metrics"
    ]
    assert emitted is True
    assert len(rows) == 1
    assert rows[0]["terminal_outcome"] == expected_outcome


def test_invalid_outcome_is_rejected_by_builder_but_logging_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="terminal_outcome"):
        build_ensemble_execution_metrics({}, terminal_outcome="cancelled")

    class BrokenLogger:
        def info(self, *_: Any, **__: Any) -> None:
            raise RuntimeError("broken info processor")

        def warning(self, *_: Any, **__: Any) -> None:
            raise RuntimeError("broken warning processor")

    monkeypatch.setattr(metrics_module, "log", BrokenLogger())
    log_ensemble_execution_metrics({}, terminal_outcome="cancelled")
