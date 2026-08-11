"""Shared validation for one DRACO ensemble call and its frozen G1 plan."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Any

from opensquilla.eval.draco_runtime_contract import canonical_json_sha256
from opensquilla.eval.draco_usage_evidence import coerce_metric_int


@dataclass(frozen=True, slots=True)
class EnsembleCallValidationDependencies:
    """Runner-owned pure callbacks used by the shared validation core."""

    models_equivalent: Callable[[Any, Any], bool]
    expanded_proposer_slot_identities: Callable[[object], tuple[str, ...]]
    frozen_proposer_quorum: Callable[[Mapping[str, Any] | None, int], int]
    ensemble_metadata_field_resolved: Callable[[Mapping[str, Any], str], bool]


def g1_registry_contract_reasons_core(
    trace: Mapping[str, Any],
    contract: Mapping[str, Any] | None,
    task_analyzer_execution_contract: Mapping[str, Any] | None = None,
    *,
    dependencies: EnsembleCallValidationDependencies,
) -> list[str]:
    """Fail closed when a G1 call drifts from its frozen registry allowlist."""

    _formal_openrouter_models_equivalent = dependencies.models_equivalent
    expanded_proposer_slot_identities = dependencies.expanded_proposer_slot_identities

    if not isinstance(contract, Mapping):
        return []
    reasons: list[str] = []
    profile_id = str(contract.get("profile_id") or "").strip()
    selection_mode = str(contract.get("selection_mode") or "").strip()
    candidate_scope = str(contract.get("candidate_scope") or "").strip()
    if not candidate_scope:
        candidate_scope = "exact_routes"
    expected_policy = (
        "all_registry_models" if candidate_scope == "registry_all" else "exact_openrouter_routes"
    )
    declared_policy = str(contract.get("policy") or "").strip()
    source_version = str(contract.get("source_registry_snapshot_version") or "").strip()
    expected_hash = str(contract.get("expected_routes_sha256") or "").strip()
    expected_source_registry_hash = str(
        contract.get("expected_source_registry_snapshot_sha256") or ""
    ).strip()
    expected_ranking_schema = str(
        contract.get("expected_ranking_config_schema_version") or ""
    ).strip()
    expected_ranking_version = str(contract.get("expected_ranking_config_version") or "").strip()
    expected_ranking_hash = str(contract.get("expected_ranking_config_sha256") or "").strip()
    expected_proposer_max = coerce_metric_int(contract.get("expected_proposer_count_max"))
    expected_count = coerce_metric_int(contract.get("expected_candidate_count"))
    expected_routes = contract.get("expected_routes")
    if (
        not profile_id
        or selection_mode != "router_dynamic"
        or candidate_scope not in {"registry_all", "exact_routes"}
        or (
            declared_policy != expected_policy
            if "candidate_scope" in contract
            else declared_policy not in {"", expected_policy}
        )
        or contract.get("user_profile_enabled") is not False
        or not source_version
        or len(expected_hash) != 64
        or len(expected_source_registry_hash) != 64
        or not expected_ranking_schema
        or not expected_ranking_version
        or len(expected_ranking_hash) != 64
        or expected_proposer_max <= 0
        or expected_proposer_max > expected_count
        or expected_count <= 0
        or not isinstance(expected_routes, Mapping)
        or len(expected_routes) != expected_count
    ):
        return ["invalid_g1_registry_contract"]
    expected_identities = {f"openrouter:{str(model).strip().lower()}" for model in expected_routes}
    expected_filtered_version = f"{source_version}+{profile_id}+{expected_hash[:12]}"
    executed_plan = trace.get("selection_plan")
    if not isinstance(executed_plan, Mapping):
        return ["missing_g1_selection_plan"]
    analyzer_trace = executed_plan.get("task_analyzer")
    if (
        isinstance(analyzer_trace, Mapping)
        and analyzer_trace.get("source") != "frozen_replay"
        and (
            isinstance(
                executed_plan.get("task_analyzer_execution_contract"),
                Mapping,
            )
            or isinstance(analyzer_trace.get("chain"), Mapping)
        )
    ):
        from opensquilla.eval.draco_task_analyzer_execution import (
            task_analyzer_execution_contract_from_g1_registry,
            validate_task_analyzer_execution_trace,
            validated_task_analyzer_execution_contract,
        )

        expected_execution = validated_task_analyzer_execution_contract(
            task_analyzer_execution_contract
        )
        if expected_execution is None:
            try:
                expected_execution = validated_task_analyzer_execution_contract(
                    task_analyzer_execution_contract_from_g1_registry(contract)
                )
            except (TypeError, ValueError):
                expected_execution = None
        declared_execution = executed_plan.get("task_analyzer_execution_contract")
        chain_trace = analyzer_trace.get("chain")
        new_trace = bool(
            isinstance(chain_trace, Mapping)
            and ("schema_repair_max_retries" in chain_trace or "deadline" in chain_trace)
        )
        if expected_execution is None or (new_trace and declared_execution != expected_execution):
            reasons.append("invalid_g1_task_analyzer_execution_contract")
        else:
            _, execution_reasons = validate_task_analyzer_execution_trace(
                execution_contract=expected_execution,
                analyzer_trace=analyzer_trace,
                models_equivalent=_formal_openrouter_models_equivalent,
            )
            if execution_reasons:
                reasons.append("invalid_g1_task_analyzer_execution_trace")
    if executed_plan.get("analyzer_failure_fallback") is True:
        fallback = contract.get("analyzer_failure_fallback_ensemble")
        proposer_routes = fallback.get("proposers") if isinstance(fallback, Mapping) else None
        aggregator_route = fallback.get("aggregator") if isinstance(fallback, Mapping) else None
        if (
            not isinstance(proposer_routes, list)
            or len(proposer_routes) != 4
            or not isinstance(aggregator_route, Mapping)
            or fallback.get("min_successful_proposers") != 1
            or fallback.get("complete_proposers_only") is not True
            or fallback.get("aggregator_max_recovery_actions") != 1
        ):
            return ["invalid_g1_analyzer_failure_fallback_contract"]
        expected_p = [
            f"{str(route.get('provider') or '')}:{str(route.get('model') or '')}"
            for route in proposer_routes
            if isinstance(route, Mapping)
        ]
        expected_a = (
            f"{str(aggregator_route.get('provider') or '')}:"
            f"{str(aggregator_route.get('model') or '')}"
        )
        if len(expected_p) != 4 or len(set(expected_p)) != 4:
            return ["invalid_g1_analyzer_failure_fallback_contract"]
        if executed_plan.get("user_profile_enabled") is not False:
            reasons.append("wrong_g1_user_profile_enabled")
        if executed_plan.get("analyzer_failure_fallback_schema") != (
            "opensquilla.router-dynamic-analyzer-failure-fallback/v1"
        ):
            reasons.append("wrong_g1_analyzer_failure_fallback_schema")
        if executed_plan.get("selected_P") != expected_p:
            reasons.append("wrong_g1_selected_proposers")
        if executed_plan.get("selected_A") != expected_a:
            reasons.append("wrong_g1_selected_aggregator")
        if (
            executed_plan.get("complete_proposers_only") is not True
            or coerce_metric_int(executed_plan.get("effective_min_successful_proposers")) != 1
            or coerce_metric_int(executed_plan.get("N_min")) != 1
            or coerce_metric_int(executed_plan.get("N_max")) != 4
            or executed_plan.get("aggregator_max_recovery_actions") != 1
        ):
            reasons.append("wrong_g1_analyzer_failure_fallback_quorum")
        analyzer_trace = executed_plan.get("task_analyzer")
        analyzer_chain_trace = (
            analyzer_trace.get("chain") if isinstance(analyzer_trace, Mapping) else None
        )
        if (
            not isinstance(analyzer_trace, Mapping)
            or analyzer_trace.get("schema_valid") is not False
            or not isinstance(analyzer_chain_trace, Mapping)
            or analyzer_chain_trace.get("exhausted") is not True
        ):
            reasons.append("wrong_g1_analyzer_failure_fallback_activation")
        ranking_parameters = executed_plan.get("ranking_parameters")
        if (
            not isinstance(ranking_parameters, Mapping)
            or canonical_json_sha256(ranking_parameters).removeprefix("sha256:")
            != expected_ranking_hash
            or executed_plan.get("ranking_config_schema_version") != expected_ranking_schema
            or executed_plan.get("ranking_config_version") != expected_ranking_version
            or executed_plan.get("ranking_config_hash") != expected_ranking_hash
        ):
            reasons.append("wrong_g1_ranking_config_trace")
        return list(dict.fromkeys(reasons))
    if executed_plan.get("user_profile_enabled") is not False:
        reasons.append("wrong_g1_user_profile_enabled")
    ranking_parameters = executed_plan.get("ranking_parameters")
    ranking_parameters_valid = isinstance(ranking_parameters, Mapping)
    if not ranking_parameters_valid:
        reasons.append("missing_g1_ranking_parameters")
    else:
        try:
            actual_ranking_hash = canonical_json_sha256(ranking_parameters).removeprefix("sha256:")
        except (TypeError, ValueError):
            actual_ranking_hash = ""
        if actual_ranking_hash != expected_ranking_hash:
            reasons.append("wrong_g1_ranking_config_hash")
        if (
            str(ranking_parameters.get("schema_version") or "") != expected_ranking_schema
            or str(ranking_parameters.get("config_version") or "") != expected_ranking_version
        ):
            reasons.append("wrong_g1_ranking_config_identity")
    if (
        str(executed_plan.get("ranking_config_schema_version") or "") != expected_ranking_schema
        or str(executed_plan.get("ranking_config_version") or "") != expected_ranking_version
        or str(executed_plan.get("ranking_config_hash") or "") != expected_ranking_hash
    ):
        reasons.append("wrong_g1_ranking_config_trace")
    allowlist = executed_plan.get("candidate_allowlist")
    if not isinstance(allowlist, Mapping):
        reasons.append("missing_g1_candidate_allowlist")
    else:
        expected_fields = {
            "policy": expected_policy,
            "profile_id": profile_id,
            "source_registry_snapshot_version": source_version,
            "filtered_registry_snapshot_version": expected_filtered_version,
            "expected_routes_sha256": expected_hash,
            "expected_source_registry_snapshot_sha256": (expected_source_registry_hash),
            "expected_candidate_count": expected_count,
            "candidate_count": expected_count,
        }
        if "candidate_scope" in contract:
            expected_fields["candidate_scope"] = candidate_scope
        for field, expected_value in expected_fields.items():
            if allowlist.get(field) != expected_value:
                reasons.append(f"wrong_g1_candidate_allowlist_{field}")
        traced_identities = allowlist.get("expected_identities")
        if (
            not isinstance(traced_identities, list)
            or set(traced_identities) != expected_identities
            or len(traced_identities) != expected_count
        ):
            reasons.append("wrong_g1_candidate_allowlist_identities")
    if coerce_metric_int(executed_plan.get("candidate_pool_size")) != expected_count:
        reasons.append("wrong_g1_candidate_pool_size")
    if executed_plan.get("registry_snapshot_version") != expected_filtered_version:
        reasons.append("wrong_g1_registry_snapshot_version")
    registry_hash = str(executed_plan.get("registry_snapshot_hash") or "")
    if len(registry_hash) != 64 or any(char not in "0123456789abcdef" for char in registry_hash):
        reasons.append("invalid_g1_registry_snapshot_hash")
    candidate_pool = executed_plan.get("candidate_pool")
    candidate_pool_identities = (
        [str(item.get("identity") or "") for item in candidate_pool if isinstance(item, Mapping)]
        if isinstance(candidate_pool, list)
        else []
    )
    if (
        not isinstance(candidate_pool, list)
        or len(candidate_pool) != expected_count
        or len(candidate_pool_identities) != expected_count
        or len(set(candidate_pool_identities)) != expected_count
        or set(candidate_pool_identities) != expected_identities
    ):
        reasons.append("wrong_g1_candidate_pool")
    selected_p = executed_plan.get("selected_P")
    if (
        not isinstance(selected_p, list)
        or not selected_p
        or any(not isinstance(identity, str) for identity in selected_p)
        or len(set(str(identity) for identity in selected_p)) != len(selected_p)
        or any(str(identity) not in expected_identities for identity in selected_p)
    ):
        reasons.append("wrong_g1_selected_proposers")
    selected_a = executed_plan.get("selected_A")
    if not isinstance(selected_a, str) or selected_a not in expected_identities:
        reasons.append("wrong_g1_selected_aggregator")
    task_profile = executed_plan.get("task_profile")
    derived_min = derived_max = 0
    derived_bound_reasons: list[str] = []
    if ranking_parameters_valid and isinstance(task_profile, Mapping):
        try:
            from opensquilla.provider.ranking_router import _proposer_bounds

            derived_min, derived_max, derived_bound_reasons = _proposer_bounds(
                task_profile,
                {},
                ranking_parameters,
            )
        except Exception:  # noqa: BLE001 - malformed trace must fail closed
            reasons.append("invalid_g1_proposer_bound_evidence")
    else:
        reasons.append("missing_g1_task_profile")
    proposer_policy = executed_plan.get("proposer_recovery_policy")
    explicit_quorum = (
        proposer_policy.get("quorum_required") if isinstance(proposer_policy, Mapping) else None
    )
    if (
        isinstance(explicit_quorum, int)
        and not isinstance(explicit_quorum, bool)
        and explicit_quorum > 0
        and (explicit_quorum > derived_min or explicit_quorum > derived_max)
    ):
        derived_min = max(derived_min, explicit_quorum)
        derived_max = max(derived_max, explicit_quorum)
        derived_bound_reasons.append("proposer_recovery_quorum")
    declared_min = coerce_metric_int(executed_plan.get("N_min"))
    declared_max = coerce_metric_int(executed_plan.get("N_max"))
    traced_bound_reasons = executed_plan.get("bound_reasons")
    if (
        derived_min <= 0
        or derived_max < derived_min
        or derived_max > expected_proposer_max
        or declared_min != derived_min
        or declared_max != derived_max
        or not isinstance(traced_bound_reasons, list)
        or traced_bound_reasons != derived_bound_reasons
    ):
        reasons.append("wrong_g1_proposer_bounds")
    selected_count = len(selected_p) if isinstance(selected_p, list) else 0
    expanded_selected_p = expanded_proposer_slot_identities(executed_plan)
    expanded_models = [identity.partition(":")[2] for identity in expanded_selected_p]
    expanded_count = len(expanded_selected_p)
    if not expanded_selected_p:
        reasons.append("invalid_g1_expanded_proposer_roster")
    selected_aggregator_model = selected_a.partition(":")[2] if isinstance(selected_a, str) else ""
    if (
        selected_count < derived_min
        or selected_count > derived_max
        or selected_count > expected_proposer_max
        or coerce_metric_int(executed_plan.get("proposer_count")) != selected_count
        or coerce_metric_int(executed_plan.get("proposer_sample_count")) != expanded_count
        or executed_plan.get("proposer_models") != expanded_models
        or str(executed_plan.get("aggregator_model") or "") != selected_aggregator_model
    ):
        reasons.append("wrong_g1_selected_proposer_count")
    try:
        from opensquilla.provider.ranking_router import (
            ranking_trace_replay_reasons,
        )

        reasons.extend(ranking_trace_replay_reasons(executed_plan))
    except Exception:  # noqa: BLE001 - completion evidence must fail closed
        reasons.append("g1_frozen_ranker_replay_failed")
    replay_contract = contract.get("task_analysis_execution")
    if replay_contract is not None:
        from opensquilla.provider.ranking_router import (
            frozen_task_analysis_plan_reasons,
        )

        reasons.extend(frozen_task_analysis_plan_reasons(executed_plan, replay_contract))
    return list(dict.fromkeys(reasons))


def authorized_dynamic_aggregator_fallback_core(
    trace: Mapping[str, Any],
    *,
    dependencies: EnsembleCallValidationDependencies,
    expected_plan: Mapping[str, Any],
    final_request: Mapping[str, Any],
    usage: Mapping[str, Any] | None,
) -> tuple[str, str, list[str]]:
    """Bind a dynamic aggregator fallback to its frozen roster and physical receipt."""

    _formal_openrouter_models_equivalent = dependencies.models_equivalent

    reasons: list[str] = []
    raw_candidates = expected_plan.get("aggregator_candidates")
    candidate_identities = (
        [str(identity or "").strip() for identity in raw_candidates]
        if isinstance(raw_candidates, list)
        else []
    )
    selected_a = str(expected_plan.get("selected_A") or "").strip()
    if (
        len(candidate_identities) < 2
        or not selected_a
        or candidate_identities[0] != selected_a
        or len(candidate_identities) != len(set(candidate_identities))
        or any(
            identity.count(":") != 1
            or not identity.partition(":")[0]
            or not identity.partition(":")[2]
            for identity in candidate_identities
        )
    ):
        return "", "", ["invalid_aggregator_fallback_roster"]

    recovery = trace.get("aggregator_recovery")
    execution = final_request.get("execution")
    if (
        not isinstance(usage, Mapping)
        or not isinstance(execution, Mapping)
        or not isinstance(recovery, Mapping)
    ):
        return "", "", ["incomplete_aggregator_fallback_identity"]

    # The frozen trace and recovery receipt select the backup identity.  Request
    # fields must bind to it exactly.  Actual identity fields are different:
    # an interrupted stream can legitimately leave all of them absent together
    # with an explicit unknown-usage receipt.  Missing actual identity is then
    # metadata/audit uncertainty, while every non-empty actual value is still
    # checked fail-closed against the frozen backup.
    trace_identity = str(trace.get("executed_A") or "").strip()
    recovery_identity = str(recovery.get("executed_A") or "").strip()
    chosen_identity = (
        trace_identity
        if trace_identity == recovery_identity and trace_identity in candidate_identities[1:]
        else ""
    )
    if not chosen_identity:
        reasons.append("unauthorized_aggregator_fallback_identity")
    chosen_provider, _, chosen_model = chosen_identity.partition(":")

    def provider_matches(value: Any) -> bool:
        return (
            isinstance(value, str)
            and bool(value.strip())
            and value.strip().casefold() == chosen_provider.casefold()
        )

    def model_matches(value: Any) -> bool:
        if not isinstance(value, str) or not value.strip() or not chosen_model:
            return False
        return (
            _formal_openrouter_models_equivalent(value.strip(), chosen_model)
            if chosen_provider.casefold() == "openrouter"
            else value.strip() == chosen_model
        )

    requested_provider_values = (
        usage.get("requested_provider"),
        execution.get("requested_provider"),
        execution.get("provider"),
    )
    requested_model_values = (
        usage.get("requested_model"),
        execution.get("requested_model"),
        execution.get("model"),
    )
    if (
        not chosen_identity
        or not all(provider_matches(value) for value in requested_provider_values)
        or not all(model_matches(value) for value in requested_model_values)
    ):
        reasons.append("unauthorized_aggregator_fallback_identity")

    attempts = recovery.get("attempts") if isinstance(recovery, Mapping) else None
    selected_attempt = recovery.get("selected_attempt") if isinstance(recovery, Mapping) else None
    selected_rows = (
        [
            item
            for item in attempts
            if isinstance(item, Mapping)
            and type(item.get("attempt")) is int
            and item.get("attempt") == selected_attempt
        ]
        if isinstance(attempts, list)
        and isinstance(selected_attempt, int)
        and not isinstance(selected_attempt, bool)
        and selected_attempt > 0
        else []
    )
    selected_row = selected_rows[0] if len(selected_rows) == 1 else None
    raw_physical_id = (
        selected_row.get("physical_attempt_id") if isinstance(selected_row, Mapping) else None
    )
    physical_id = raw_physical_id if isinstance(raw_physical_id, str) else ""
    raw_usage_physical_id = usage.get("physical_attempt_id")
    usage_physical_id = raw_usage_physical_id if isinstance(raw_usage_physical_id, str) else ""
    expected_fallback_index = (
        candidate_identities.index(chosen_identity)
        if chosen_identity in candidate_identities
        else -1
    )
    actual_provider_values = (
        usage.get("provider"),
        execution.get("actual_provider"),
        selected_row.get("actual_provider") if isinstance(selected_row, Mapping) else None,
    )
    actual_model_values = (
        usage.get("model"),
        execution.get("actual_model"),
        selected_row.get("actual_model") if isinstance(selected_row, Mapping) else None,
    )
    present_actual_providers = [
        value for value in actual_provider_values if isinstance(value, str) and value.strip()
    ]
    present_actual_models = [
        value for value in actual_model_values if isinstance(value, str) and value.strip()
    ]
    if (
        any(
            value not in (None, "") and not isinstance(value, str)
            for value in (*actual_provider_values, *actual_model_values)
        )
        or any(not provider_matches(value) for value in present_actual_providers)
        or any(not model_matches(value) for value in present_actual_models)
    ):
        reasons.append("unauthorized_aggregator_fallback_identity")
    if not present_actual_providers:
        reasons.append("missing_actual_aggregator_provider")
    if not present_actual_models:
        reasons.append("missing_actual_aggregator_model")
    degraded_delivery = bool(
        trace.get("execution_outcome") == "degraded_success"
        and trace.get("delivery_outcome") in {"degraded_success", "partial_usable"}
        and isinstance(recovery, Mapping)
        and recovery.get("degraded") is True
        and recovery.get("success") is False
    )
    selected_outcome_valid = bool(
        isinstance(selected_row, Mapping)
        and (
            selected_row.get("outcome") == "succeeded"
            and isinstance(recovery, Mapping)
            and recovery.get("success") is True
            or selected_row.get("outcome") == "failed"
            and degraded_delivery
        )
    )
    if (
        recovery.get("candidate_ids") != candidate_identities
        or recovery.get("executed_A") != chosen_identity
        or trace.get("executed_A") != chosen_identity
        or not isinstance(selected_row, Mapping)
        or selected_row.get("request_started") is not True
        or type(selected_row.get("physical_request_count")) is not int
        or selected_row.get("physical_request_count") != 1
        or len(physical_id) != 32
        or any(character not in "0123456789abcdef" for character in physical_id)
        or len(usage_physical_id) != 32
        or any(character not in "0123456789abcdef" for character in usage_physical_id)
        or usage_physical_id != physical_id
        or not selected_outcome_valid
        or selected_row.get("attempt") != selected_attempt
        or type(selected_row.get("fallback_index")) is not int
        or selected_row.get("fallback_index") != expected_fallback_index
        or type(recovery.get("fallback_index")) is not int
        or recovery.get("fallback_index") != expected_fallback_index
        or str(selected_row.get("requested_provider") or "").strip().casefold()
        != chosen_provider.casefold()
        or not (
            _formal_openrouter_models_equivalent(
                str(selected_row.get("requested_model") or "").strip(),
                chosen_model,
            )
            if chosen_provider.casefold() == "openrouter"
            else str(selected_row.get("requested_model") or "").strip() == chosen_model
        )
    ):
        reasons.append("invalid_aggregator_fallback_physical_evidence")
    return chosen_provider, chosen_model, list(dict.fromkeys(reasons))


def ensemble_call_core_reasons_core(
    trace: Mapping[str, Any],
    *,
    dependencies: EnsembleCallValidationDependencies,
    expected_selection_mode: str = "",
    expected_selection_plan: Mapping[str, Any] | None = None,
    expected_g1_registry_contract: Mapping[str, Any] | None = None,
    expected_task_analyzer_execution_contract: Mapping[str, Any] | None = None,
    final_text: str = "",
    require_output_binding: bool = False,
) -> list[str]:
    """Validate one physical ensemble call without trusting declared counts."""

    authorized_dynamic_aggregator_fallback = partial(
        authorized_dynamic_aggregator_fallback_core,
        dependencies=dependencies,
    )
    ensemble_metadata_field_resolved = dependencies.ensemble_metadata_field_resolved
    expanded_proposer_slot_identities = dependencies.expanded_proposer_slot_identities
    frozen_proposer_quorum = dependencies.frozen_proposer_quorum
    g1_registry_contract_reasons = partial(
        g1_registry_contract_reasons_core,
        dependencies=dependencies,
    )

    reasons: list[str] = []
    if str(trace.get("request_outcome") or "llm_response") != "llm_response":
        reasons.append("aggregator_call_error")
    expected_plan = (
        dict(expected_selection_plan) if isinstance(expected_selection_plan, Mapping) else {}
    )
    executed_plan = trace.get("selection_plan")
    executed_mode = str(
        trace.get("selection_strategy")
        or (executed_plan.get("strategy") if isinstance(executed_plan, Mapping) else "")
        or ""
    )
    dynamic_selection = bool(
        expected_selection_mode == "router_dynamic" or executed_mode == "router_dynamic"
    )
    dynamic_aggregator_fallback = bool(dynamic_selection and trace.get("fallback_used") is True)
    if trace.get("fallback_used") is not False and not dynamic_aggregator_fallback:
        reasons.append("aggregator_fallback_used_or_unknown")
    if str(trace.get("final_request_role") or "") != "aggregator":
        reasons.append("final_request_not_aggregator")

    total = trace.get("total_candidates")
    successful = trace.get("successful_proposers")
    candidate_rows = trace.get("candidates")
    quorum_plan = expected_plan or (
        dict(executed_plan) if isinstance(executed_plan, Mapping) else {}
    )
    complete_only_analyzer_fallback = bool(
        quorum_plan.get("analyzer_failure_fallback") is True
        and quorum_plan.get("complete_proposers_only") is True
        and quorum_plan.get("effective_min_successful_proposers") == 1
    )
    dynamic_usable_contract = bool(
        dynamic_selection
        and not complete_only_analyzer_fallback
        and (
            any(
                field in trace
                for field in (
                    "usable_proposers",
                    "partial_proposers",
                    "execution_quorum_required",
                    "execution_quorum_met",
                )
            )
            or (
                isinstance(candidate_rows, list)
                and any(
                    isinstance(candidate, Mapping)
                    and ("usable_for_aggregation" in candidate or "completion_outcome" in candidate)
                    for candidate in candidate_rows
                )
            )
        )
    )
    declared_quorum = (
        frozen_proposer_quorum(
            expected_plan or (executed_plan if isinstance(executed_plan, Mapping) else None),
            total,
        )
        if isinstance(total, int) and not isinstance(total, bool) and total > 0
        else 0
    )
    declared_counts_valid = bool(
        isinstance(total, int)
        and not isinstance(total, bool)
        and total > 0
        and isinstance(successful, int)
        and not isinstance(successful, bool)
        and 0 <= successful <= total
        and (dynamic_usable_contract or successful >= declared_quorum)
    )
    if not declared_counts_valid:
        reasons.append("insufficient_proposer_quorum")

    expected_total = coerce_metric_int(expected_plan.get("proposer_sample_count"))
    if expected_total <= 0:
        expected_models = expected_plan.get("proposer_models")
        if isinstance(expected_models, list):
            expected_total = len(expected_models)
    if expected_total and total != expected_total:
        reasons.append("wrong_executed_proposer_count")
    if (
        expected_total
        and not dynamic_usable_contract
        and (
            not isinstance(successful, int)
            or isinstance(successful, bool)
            or successful < frozen_proposer_quorum(expected_plan, expected_total)
        )
    ):
        reasons.append("insufficient_configured_proposer_quorum")

    if expected_selection_mode:
        if executed_mode != expected_selection_mode:
            reasons.append("wrong_executed_selection_mode")
    reasons.extend(
        g1_registry_contract_reasons(
            trace,
            expected_g1_registry_contract,
            expected_task_analyzer_execution_contract,
        )
    )
    if expected_plan:
        if not isinstance(executed_plan, Mapping):
            reasons.append("missing_executed_selection_plan")
        else:
            for field in (
                "strategy",
                "selection_mode",
                "profile",
                "proposer_models",
                "selected_P",
                "proposer_sample_count",
                "aggregator_model",
                "selected_A",
                "aggregator_candidates",
                "effective_min_successful_proposers",
                "proposer_recovery_policy",
            ):
                expected_value = expected_plan.get(field)
                if (
                    expected_value not in (None, [], "")
                    and executed_plan.get(field) != expected_value
                ):
                    reasons.append(f"wrong_executed_{field}")

    if not isinstance(candidate_rows, list) or not candidate_rows:
        reasons.append("missing_actual_proposer_candidates")
    else:
        if isinstance(total, int) and not isinstance(total, bool) and len(candidate_rows) != total:
            reasons.append("wrong_actual_proposer_count")
        proven_successes: list[bool] = []
        proven_usable: list[bool] = []
        proven_partials: list[bool] = []
        for candidate in candidate_rows:
            content = candidate.get("content") if isinstance(candidate, Mapping) else None
            content_proven = bool(
                isinstance(candidate, Mapping)
                and candidate.get("request_started") is True
                and isinstance(candidate.get("physical_request_count"), int)
                and not isinstance(candidate.get("physical_request_count"), bool)
                and candidate.get("physical_request_count") > 0
                and isinstance(content, Mapping)
                and coerce_metric_int(content.get("chars")) > 0
                and bool(str(content.get("text") or "").strip())
            )
            strict_proven = bool(
                content_proven
                and isinstance(candidate, Mapping)
                and candidate.get("ok") is True
                and not candidate.get("error")
                and (
                    not complete_only_analyzer_fallback
                    or candidate.get("completion_outcome") == "complete"
                )
            )
            partial_proven = bool(
                dynamic_usable_contract
                and content_proven
                and isinstance(candidate, Mapping)
                and candidate.get("ok") is False
                and candidate.get("usable_for_aggregation") is True
                and candidate.get("completion_outcome") == "partial_usable"
                and bool(str(candidate.get("error") or "").strip())
                and bool(str(candidate.get("error_code") or "").strip())
            )
            usable_proven = strict_proven or partial_proven
            proven_successes.append(strict_proven)
            proven_usable.append(usable_proven)
            proven_partials.append(partial_proven)
            if (
                isinstance(candidate, Mapping)
                and candidate.get("ok") is True
                and not strict_proven
                and not (
                    complete_only_analyzer_fallback
                    and content_proven
                    and candidate.get("completion_outcome") == "partial_usable"
                )
            ):
                reasons.append("invalid_successful_proposer_evidence")
            if dynamic_usable_contract and isinstance(candidate, Mapping):
                expected_outcome = (
                    "complete"
                    if strict_proven
                    else "partial_usable"
                    if partial_proven
                    else "failed"
                )
                if (
                    type(candidate.get("usable_for_aggregation")) is not bool
                    or candidate.get("usable_for_aggregation") is not usable_proven
                    or candidate.get("completion_outcome") != expected_outcome
                ):
                    reasons.append("invalid_usable_proposer_evidence")
                if candidate.get("selected_for_aggregation") is True and not usable_proven:
                    reasons.append("invalid_selected_proposer_evidence")
            if isinstance(candidate, Mapping) and strict_proven:
                if candidate.get(
                    "usage_reported"
                ) is not True and not ensemble_metadata_field_resolved(candidate, "usage"):
                    reasons.append("missing_proposer_usage_metadata")
                if not str(
                    candidate.get("stop_reason") or ""
                ).strip() and not ensemble_metadata_field_resolved(candidate, "stop_reason"):
                    reasons.append("missing_proposer_stop_reason")
        actual_successful = sum(proven_successes)
        if (
            not isinstance(successful, int)
            or isinstance(successful, bool)
            or actual_successful != successful
        ):
            reasons.append("successful_proposer_count_mismatch")
        actual_usable = sum(proven_usable)
        actual_partials = sum(proven_partials)
        quorum_total = expected_total or (
            total if isinstance(total, int) and not isinstance(total, bool) else 0
        )
        configured_quorum = (
            frozen_proposer_quorum(quorum_plan, quorum_total) if quorum_total > 0 else 0
        )
        execution_quorum_required = (
            max(2, configured_quorum)
            if dynamic_usable_contract and actual_partials
            else configured_quorum
        )
        if dynamic_usable_contract:
            selected_count = sum(
                1
                for candidate in candidate_rows
                if isinstance(candidate, Mapping)
                and candidate.get("selected_for_aggregation") is True
            )
            selected_usable_count = sum(
                1
                for index, candidate in enumerate(candidate_rows)
                if isinstance(candidate, Mapping)
                and candidate.get("selected_for_aggregation") is True
                and proven_usable[index]
            )
            if (
                trace.get("usable_proposers") != actual_usable
                or trace.get("partial_proposers") != actual_partials
                or trace.get("selected_candidate_count") != selected_count
                or trace.get("execution_quorum_required") != execution_quorum_required
                or trace.get("execution_quorum_met")
                is not (actual_usable >= execution_quorum_required)
            ):
                reasons.append("invalid_proposer_execution_quorum_evidence")
            if actual_usable < execution_quorum_required:
                reasons.append("insufficient_actual_proposer_quorum")
            if selected_usable_count < execution_quorum_required:
                reasons.append("insufficient_selected_proposer_quorum")
        elif expected_total and actual_successful < configured_quorum:
            reasons.append("insufficient_actual_proposer_quorum")
        raw_expected_selected_p = expected_plan.get("selected_P")
        expected_slot_identities = expanded_proposer_slot_identities(expected_plan)
        if isinstance(raw_expected_selected_p, list):
            if not expected_slot_identities:
                reasons.append("invalid_expected_proposer_slot_roster")
            elif len(candidate_rows) != len(expected_slot_identities):
                reasons.append("wrong_actual_proposer_count")
            else:
                # Recovery may legally replace any initial slot with a member
                # from the frozen backup roster.  Candidate identity is
                # therefore a roster-membership audit, not a positional
                # equality check against the original selected_P array.
                allowed_proposer_identities = set(expected_slot_identities)
                raw_backup_p = expected_plan.get("backup_P")
                if isinstance(raw_backup_p, list):
                    allowed_proposer_identities.update(
                        identity.strip()
                        for identity in raw_backup_p
                        if isinstance(identity, str) and identity.strip() and ":" in identity
                    )
                requested_identity_missing = False
                requested_identity_wrong = False
                actual_identity_missing = False
                actual_identity_wrong = False
                for candidate_index, candidate in enumerate(candidate_rows):
                    execution = (
                        candidate.get("execution") if isinstance(candidate, Mapping) else None
                    )
                    requested_provider = (
                        candidate.get("requested_provider")
                        if isinstance(candidate, Mapping)
                        else None
                    ) or (
                        execution.get("requested_provider") or execution.get("provider")
                        if isinstance(execution, Mapping)
                        else None
                    )
                    requested_model = (
                        candidate.get("requested_model") if isinstance(candidate, Mapping) else None
                    ) or (execution.get("model") if isinstance(execution, Mapping) else None)
                    candidate_provider = (
                        candidate.get("provider") if isinstance(candidate, Mapping) else None
                    )
                    candidate_model = (
                        candidate.get("model") if isinstance(candidate, Mapping) else None
                    )
                    if (
                        not isinstance(requested_provider, str)
                        or not requested_provider.strip()
                        or not isinstance(requested_model, str)
                        or not requested_model.strip()
                    ):
                        requested_identity_missing = True
                        requested_identity = ""
                    else:
                        requested_identity = (
                            requested_provider.strip() + ":" + requested_model.strip()
                        )
                    if requested_identity and requested_identity not in allowed_proposer_identities:
                        requested_identity_wrong = True
                    provider_missing = (
                        not isinstance(candidate_provider, str) or not candidate_provider.strip()
                    )
                    model_missing = (
                        not isinstance(candidate_model, str) or not candidate_model.strip()
                    )
                    if proven_usable[candidate_index] and (
                        (
                            provider_missing
                            and not ensemble_metadata_field_resolved(
                                candidate,
                                "actual_provider",
                            )
                        )
                        or (
                            model_missing
                            and not ensemble_metadata_field_resolved(
                                candidate,
                                "actual_model",
                            )
                        )
                    ):
                        actual_identity_missing = True
                    elif (
                        isinstance(candidate_provider, str)
                        and candidate_provider.strip()
                        and isinstance(candidate_model, str)
                        and candidate_model.strip()
                        and (candidate_provider.strip() + ":" + candidate_model.strip())
                        not in allowed_proposer_identities
                    ) or (
                        requested_identity
                        and isinstance(candidate_provider, str)
                        and candidate_provider.strip()
                        and isinstance(candidate_model, str)
                        and candidate_model.strip()
                        and (
                            candidate_provider.strip() != requested_provider.strip()
                            or candidate_model.strip() != requested_model.strip()
                        )
                    ):
                        actual_identity_wrong = True
                if requested_identity_missing:
                    reasons.append("missing_requested_proposer_identity")
                if requested_identity_wrong:
                    reasons.append("wrong_requested_proposer_identity")
                if actual_identity_missing:
                    reasons.append("missing_actual_proposer_identity")
                if actual_identity_wrong:
                    reasons.append("wrong_actual_proposer_identity")

    final_request = trace.get("final_request")
    if (
        not isinstance(final_request, Mapping)
        or final_request.get("request_started") is not True
        or str(final_request.get("role") or "") != "aggregator"
        or final_request.get("error")
        or trace.get("aggregator_error")
    ):
        reasons.append("aggregator_request_incomplete")
        return list(dict.fromkeys(reasons))

    usage = final_request.get("usage")
    if not isinstance(usage, Mapping) and not ensemble_metadata_field_resolved(
        final_request, "usage"
    ):
        reasons.append("missing_aggregator_usage_metadata")
    if (
        not isinstance(usage, Mapping) or not str(usage.get("stop_reason") or "").strip()
    ) and not ensemble_metadata_field_resolved(final_request, "stop_reason"):
        reasons.append("missing_aggregator_stop_reason")

    fallback_expected_provider = ""
    fallback_expected_model = ""
    if dynamic_aggregator_fallback:
        (
            fallback_expected_provider,
            fallback_expected_model,
            fallback_reasons,
        ) = authorized_dynamic_aggregator_fallback(
            trace,
            expected_plan=expected_plan,
            final_request=final_request,
            usage=usage if isinstance(usage, Mapping) else None,
        )
        reasons.extend(fallback_reasons)

    if expected_plan:
        expected_aggregator_model = (
            fallback_expected_model
            if dynamic_aggregator_fallback
            else expected_plan.get("aggregator_model")
        )
        expected_selected_a = (
            f"{fallback_expected_provider}:{fallback_expected_model}"
            if dynamic_aggregator_fallback and fallback_expected_provider
            else expected_plan.get("selected_A")
        )
        expected_provider, separator, selected_model = (
            expected_selected_a.partition(":")
            if isinstance(expected_selected_a, str)
            else ("", "", "")
        )
        actual_model = usage.get("model") if isinstance(usage, Mapping) else None
        actual_provider = usage.get("provider") if isinstance(usage, Mapping) else None
        requested_model = usage.get("requested_model") if isinstance(usage, Mapping) else None
        requested_provider = usage.get("requested_provider") if isinstance(usage, Mapping) else None
        if not isinstance(expected_aggregator_model, str) or not (
            expected_aggregator_model.strip()
        ):
            reasons.append("wrong_actual_aggregator_model")
        elif (
            not isinstance(actual_model, str) or not actual_model.strip()
        ) and not ensemble_metadata_field_resolved(final_request, "actual_model"):
            reasons.append("missing_actual_aggregator_model")
        elif actual_model.strip() != expected_aggregator_model.strip():
            reasons.append("wrong_actual_aggregator_model")
        if (
            separator != ":"
            or not expected_provider.strip()
            or selected_model.strip()
            != (
                expected_aggregator_model.strip()
                if isinstance(expected_aggregator_model, str)
                else ""
            )
        ):
            reasons.append("wrong_actual_aggregator_provider")
        elif (
            not isinstance(actual_provider, str) or not actual_provider.strip()
        ) and not ensemble_metadata_field_resolved(final_request, "actual_provider"):
            reasons.append("missing_actual_aggregator_provider")
        elif actual_provider.strip() != expected_provider.strip():
            reasons.append("wrong_actual_aggregator_provider")
        if (
            not isinstance(requested_provider, str)
            or not requested_provider.strip()
            or not isinstance(requested_model, str)
            or not requested_model.strip()
        ):
            reasons.append("missing_requested_aggregator_identity")
        elif requested_provider.strip() != expected_provider.strip() or requested_model.strip() != (
            expected_aggregator_model.strip() if isinstance(expected_aggregator_model, str) else ""
        ):
            reasons.append("wrong_requested_aggregator_identity")

    if require_output_binding:
        output = (
            trace.get("assembled_output")
            if trace.get("output_binding_schema") == "opensquilla.ensemble-output-binding/v1"
            else final_request.get("output")
        )
        if not isinstance(output, Mapping):
            reasons.append("missing_aggregator_output_binding")
        else:
            output_text = output.get("text")
            output_chars = coerce_metric_int(output.get("chars"))
            output_truncated = output.get("truncated") is True
            if not isinstance(output_text, str) or not output_text.strip() or output_chars <= 0:
                reasons.append("missing_aggregator_output_binding")
            elif output_chars > len(final_text):
                reasons.append("wrong_aggregator_output_length")
            else:
                final_output_tail = final_text[-output_chars:]
                if (output_truncated and not final_output_tail.startswith(output_text)) or (
                    not output_truncated and output_text != final_output_tail
                ):
                    reasons.append("wrong_aggregator_output_binding")
    return list(dict.fromkeys(reasons))
