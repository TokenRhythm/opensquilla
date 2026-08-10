from __future__ import annotations

from copy import deepcopy

from opensquilla.eval.draco_task_analyzer_execution import (
    TASK_ANALYZER_EXECUTION_CONTRACT_SCHEMA_V3,
    build_task_analyzer_execution_contract,
    task_analyzer_execution_contract_from_g1_registry,
    validate_task_analyzer_execution_trace,
    validated_task_analyzer_execution_contract,
)

ROUTES = [
    {
        "provider": "openrouter",
        "model": "anthropic/claude-opus-4.8",
        "upstream_provider": "anthropic",
        "max_attempts": 1,
    },
    {
        "provider": "openrouter",
        "model": "openai/gpt-5.6-sol",
        "upstream_provider": "azure",
        "max_attempts": 1,
    },
    {
        "provider": "openrouter",
        "model": "google/gemini-3.1-pro-preview",
        "upstream_provider": "google-ai-studio",
        "max_attempts": 1,
    },
]


def contract(*, repairs: int = 0):
    return build_task_analyzer_execution_contract(
        routes=ROUTES,
        schema_repair_max_retries=repairs,
        total_timeout_seconds=60.0,
        route_source="test",
        source_payload={"routes": ROUTES},
    )


def attempt(index: int, route_index: int):
    route = ROUTES[route_index]
    attempt_id = f"{index:032x}"
    return {
        "attempt": index,
        "physical_attempt_id": attempt_id,
        "provider": route["provider"],
        "model": route["model"],
        "requested_provider": route["provider"],
        "requested_model": route["model"],
        "input_tokens": 10,
        "output_tokens": 1,
        "reasoning_tokens": 0,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "billed_cost": 0.001,
        "provider_usage": {
            "provider": route["provider"],
            "model": route["model"],
            "physical_attempt_id": attempt_id,
        },
    }


def analyzer_trace(
    *,
    counts: list[int],
    selected_index: int | None,
    include_new_fields: bool = True,
    repairs: int = 0,
):
    outcomes = []
    physical_attempts = []
    ordinal = 0
    for index, count in enumerate(counts):
        route = ROUTES[index]
        success = selected_index == index
        outcomes.append(
            {
                "candidate_index": index,
                "provider": route["provider"],
                "model": route["model"],
                "upstream_provider": route["upstream_provider"],
                "outcome": "success" if success else "failed",
                "reason": "" if success else "transient",
                "physical_request_count": count,
            }
        )
        for _ in range(count):
            ordinal += 1
            physical_attempts.append(attempt(ordinal, index))
    chain = {
        "protocol": "opensquilla.task-analyzer-fallback-chain/v1",
        "configured_routes": [
            {
                key: route[key]
                for key in ("provider", "model", "upstream_provider")
            }
            for route in ROUTES
        ],
        "attempt_outcomes": outcomes,
        "selected_index": selected_index,
        "exhausted": selected_index is None,
    }
    if include_new_fields:
        chain.update(
            {
                "schema_repair_max_retries": repairs,
                "deadline": {
                    "configured_seconds": 60.0,
                    "elapsed_seconds": 1.0,
                    "remaining_seconds": 59.0,
                    "expired": False,
                },
            }
        )
    result_route = (
        ROUTES[selected_index]
        if selected_index is not None
        else ROUTES[len(counts) - 1]
    )
    return {
        "source": "llm_provider" if selected_index is not None else "router_fallback",
        "schema_valid": selected_index is not None,
        "provider": result_route["provider"],
        "model": result_route["model"],
        "fallback_reason": "" if selected_index is not None else "transient",
        "usage": {
            "attempt_count": len(physical_attempts),
            "physical_attempts": physical_attempts,
            "input_tokens": 10 * len(physical_attempts),
            "output_tokens": len(physical_attempts),
            "reasoning_tokens": 0,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "billed_cost": 0.001 * len(physical_attempts),
        },
        "chain": chain,
    }


def test_g1_live_chain_is_frozen_separately_from_historical_ranking() -> None:
    historical_policy = {
        "provider": "openrouter",
        "model": ROUTES[0]["model"],
        "upstream_provider": ROUTES[0]["upstream_provider"],
        "timeout_seconds": 20.0,
        "max_retries": 3,
    }
    g1 = {
        "task_analyzer": historical_policy,
        "live_task_analyzer_chain": deepcopy(ROUTES),
    }

    frozen = task_analyzer_execution_contract_from_g1_registry(g1)

    assert frozen["schema"] == TASK_ANALYZER_EXECUTION_CONTRACT_SCHEMA_V3
    assert frozen["routes"] == ROUTES
    assert frozen["schema_repair_max_retries"] == 0
    assert frozen["total_timeout_seconds"] == 60.0
    assert validated_task_analyzer_execution_contract(frozen) == frozen
    assert "fallback_chain" not in historical_policy


def test_opus_failure_gpt_success_authenticates_order_and_usage() -> None:
    trace = analyzer_trace(counts=[1, 1], selected_index=1)

    validated, reasons = validate_task_analyzer_execution_trace(
        execution_contract=contract(),
        analyzer_trace=trace,
    )

    assert reasons == []
    assert validated is not None
    assert validated["selected_route"]["model"] == "openai/gpt-5.6-sol"
    assert [route["model"] for route in validated["physical_routes"]] == [
        "anthropic/claude-opus-4.8",
        "openai/gpt-5.6-sol",
    ]


def test_legacy_trace_without_repair_or_deadline_is_single_attempt_only() -> None:
    trace = analyzer_trace(
        counts=[1, 1],
        selected_index=1,
        include_new_fields=False,
    )
    validated, reasons = validate_task_analyzer_execution_trace(
        execution_contract=contract(),
        analyzer_trace=trace,
    )
    assert reasons == []
    assert validated is not None and validated["legacy_trace"] is True

    trace["chain"]["attempt_outcomes"][0]["physical_request_count"] = 2
    trace["usage"]["physical_attempts"].insert(1, attempt(2, 0))
    trace["usage"]["physical_attempts"][2]["attempt"] = 3
    trace["usage"]["physical_attempts"][2]["physical_attempt_id"] = f"{3:032x}"
    trace["usage"]["attempt_count"] = 3
    assert validate_task_analyzer_execution_trace(
        execution_contract=contract(),
        analyzer_trace=trace,
    )[1] == ["invalid_task_analyzer_chain_trace"]


def test_common_chain_has_only_one_global_schema_repair() -> None:
    trace = analyzer_trace(counts=[2, 1], selected_index=1, repairs=1)
    validated, reasons = validate_task_analyzer_execution_trace(
        execution_contract=contract(repairs=1),
        analyzer_trace=trace,
    )
    assert reasons == []
    assert validated is not None

    invalid = analyzer_trace(counts=[2, 2], selected_index=1, repairs=1)
    assert validate_task_analyzer_execution_trace(
        execution_contract=contract(repairs=1),
        analyzer_trace=invalid,
    )[1] == ["task_analyzer_repair_budget_exceeded"]


def test_exhausted_prefix_requires_exact_expired_deadline_and_usage_prefix() -> None:
    trace = analyzer_trace(counts=[1], selected_index=None)
    trace["chain"]["deadline"] = {
        "configured_seconds": 0.01,
        "elapsed_seconds": 0.01,
        "remaining_seconds": 0,
        "expired": True,
    }
    validated, reasons = validate_task_analyzer_execution_trace(
        execution_contract=contract(),
        analyzer_trace=trace,
    )
    assert reasons == []
    assert validated is not None and validated["exhausted"] is True

    for mutation in ("missing", "remaining", "expired", "usage"):
        forged = deepcopy(trace)
        if mutation == "missing":
            forged["chain"].pop("deadline")
        elif mutation == "remaining":
            forged["chain"]["deadline"]["remaining_seconds"] = 0.1
        elif mutation == "expired":
            forged["chain"]["deadline"]["expired"] = False
        else:
            forged["usage"]["physical_attempts"] = []
            forged["usage"]["attempt_count"] = 0
        assert validate_task_analyzer_execution_trace(
            execution_contract=contract(),
            analyzer_trace=forged,
        )[1]


def test_full_chain_exhaustion_uses_terminal_route_identity() -> None:
    trace = analyzer_trace(counts=[1, 1, 1], selected_index=None)

    validated, reasons = validate_task_analyzer_execution_trace(
        execution_contract=contract(),
        analyzer_trace=trace,
    )

    assert reasons == []
    assert validated is not None and validated["exhausted"] is True
    assert trace["model"] == "google/gemini-3.1-pro-preview"
    for wrong_index in (0, 1):
        forged = deepcopy(trace)
        forged["provider"] = ROUTES[wrong_index]["provider"]
        forged["model"] = ROUTES[wrong_index]["model"]
        assert validate_task_analyzer_execution_trace(
            execution_contract=contract(),
            analyzer_trace=forged,
        )[1] == ["wrong_task_analyzer_result_identity"]


def test_chain_exhaustion_rejects_non_fallback_source() -> None:
    trace = analyzer_trace(counts=[1, 1, 1], selected_index=None)
    trace["source"] = "llm_provider"

    assert validate_task_analyzer_execution_trace(
        execution_contract=contract(),
        analyzer_trace=trace,
    )[1] == ["wrong_task_analyzer_exhaustion_result"]


def test_new_explicit_trace_must_declare_repair_zero() -> None:
    trace = analyzer_trace(counts=[1], selected_index=0, repairs=1)
    assert validate_task_analyzer_execution_trace(
        execution_contract=contract(),
        analyzer_trace=trace,
    )[1] == ["wrong_task_analyzer_repair_budget"]


def test_success_trace_rejects_contradictory_result_state() -> None:
    trace = analyzer_trace(counts=[1, 1], selected_index=1)
    trace["source"] = "router_fallback"
    trace["schema_valid"] = False
    trace["fallback_reason"] = "transient"

    assert validate_task_analyzer_execution_trace(
        execution_contract=contract(),
        analyzer_trace=trace,
    )[1] == ["wrong_task_analyzer_success_result"]


def test_new_trace_rejects_malformed_or_unbounded_deadline() -> None:
    for field, value in (
        ("configured_seconds", 61.0),
        ("elapsed_seconds", -1.0),
        ("remaining_seconds", float("nan")),
    ):
        trace = analyzer_trace(counts=[1], selected_index=0)
        trace["chain"]["deadline"][field] = value
        assert validate_task_analyzer_execution_trace(
            execution_contract=contract(),
            analyzer_trace=trace,
        )[1] == ["invalid_task_analyzer_deadline_trace"]


def test_contract_and_route_order_tampering_is_rejected() -> None:
    trace = analyzer_trace(counts=[1, 1], selected_index=1)
    trace["chain"]["configured_routes"].reverse()
    assert validate_task_analyzer_execution_trace(
        execution_contract=contract(),
        analyzer_trace=trace,
    )[1] == ["invalid_task_analyzer_chain_trace"]

    forged = contract()
    forged["source_sha256"] = "0" * 64
    assert validated_task_analyzer_execution_contract(forged) is None


def test_usage_and_response_identity_are_bound_to_physical_attempts() -> None:
    trace = analyzer_trace(counts=[1, 1], selected_index=1)
    mutations = []

    missing_count = deepcopy(trace)
    missing_count["usage"].pop("attempt_count")
    mutations.append(missing_count)

    wrong_response = deepcopy(trace)
    wrong_response["usage"]["physical_attempts"][1]["model"] = ROUTES[0]["model"]
    mutations.append(wrong_response)

    wrong_nested_response = deepcopy(trace)
    wrong_nested_response["usage"]["physical_attempts"][1]["provider_usage"][
        "model"
    ] = ROUTES[0]["model"]
    mutations.append(wrong_nested_response)

    wrong_mirror = deepcopy(trace)
    wrong_mirror["usage"]["physical_attempts"][1]["provider_usage"][
        "physical_attempt_id"
    ] = "f" * 32
    mutations.append(wrong_mirror)

    wrong_aggregate = deepcopy(trace)
    wrong_aggregate["usage"]["output_tokens"] += 1
    mutations.append(wrong_aggregate)

    for forged in mutations:
        validated, reasons = validate_task_analyzer_execution_trace(
            execution_contract=contract(),
            analyzer_trace=forged,
        )
        assert validated is None
        assert reasons
