"""Shared DRACO task-Analyzer execution-contract and trace validation.

The ranking configuration authenticates ranking behavior.  Formal DRACO G1
experiments may additionally pin an ordered live Analyzer chain without
changing that historical ranking object.  This module keeps those two
contracts separate and gives the runner, resume path, campaign freezer, and
finalizer one fail-closed validator for the resulting physical evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any

TASK_ANALYZER_EXECUTION_CONTRACT_SCHEMA_V3 = (
    "opensquilla.draco.task-analyzer-execution-contract/v3"
)
TASK_ANALYZER_FALLBACK_CHAIN_PROTOCOL = (
    "opensquilla.task-analyzer-fallback-chain/v1"
)

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PUBLIC_REASON = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_ROUTE_FIELDS = frozenset(
    {"provider", "model", "upstream_provider", "max_attempts"}
)
_TRACE_ROUTE_FIELDS = frozenset({"provider", "model", "upstream_provider"})
_OUTCOME_FIELDS = frozenset(
    {
        "candidate_index",
        "provider",
        "model",
        "upstream_provider",
        "outcome",
        "reason",
        "physical_request_count",
    }
)
_CONTRACT_FIELDS = frozenset(
    {
        "schema",
        "route_source",
        "routes",
        "schema_repair_max_retries",
        "total_timeout_seconds",
        "source_sha256",
        "contract_sha256",
    }
)
_DEADLINE_FIELDS = frozenset(
    {
        "configured_seconds",
        "elapsed_seconds",
        "remaining_seconds",
        "expired",
    }
)
_ATTEMPT_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cached_tokens",
    "cache_write_tokens",
)


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalized_route(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or set(value) != _ROUTE_FIELDS:
        return None
    provider = str(value.get("provider") or "").strip().casefold()
    model = str(value.get("model") or "").strip().casefold()
    upstream = str(value.get("upstream_provider") or "").strip().casefold()
    max_attempts = value.get("max_attempts")
    if (
        not provider
        or not model
        or not upstream
        or upstream == "auto"
        or any(character.isspace() for character in provider + model + upstream)
        or isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or max_attempts <= 0
    ):
        return None
    return {
        "provider": provider,
        "model": model,
        "upstream_provider": upstream,
        "max_attempts": max_attempts,
    }


def _normalized_routes(value: Any) -> list[dict[str, Any]] | None:
    if not isinstance(value, list) or not value:
        return None
    routes: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    for raw_route in value:
        route = _normalized_route(raw_route)
        if route is None:
            return None
        identity = (route["provider"], route["model"])
        if identity in identities:
            return None
        identities.add(identity)
        routes.append(route)
    return routes


def build_task_analyzer_execution_contract(
    *,
    routes: Sequence[Mapping[str, Any]],
    schema_repair_max_retries: int,
    total_timeout_seconds: float,
    route_source: str,
    source_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one canonical, self-hashed Analyzer execution contract."""

    normalized_routes = _normalized_routes([dict(route) for route in routes])
    normalized_source = str(route_source or "").strip()
    try:
        timeout = float(total_timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("Analyzer execution timeout must be numeric") from exc
    if (
        normalized_routes is None
        or not normalized_source
        or isinstance(schema_repair_max_retries, bool)
        or schema_repair_max_retries not in {0, 1}
        or not math.isfinite(timeout)
        or timeout <= 0.0
        or not isinstance(source_payload, Mapping)
        or not source_payload
    ):
        raise ValueError("Analyzer execution contract is malformed")
    contract = {
        "schema": TASK_ANALYZER_EXECUTION_CONTRACT_SCHEMA_V3,
        "route_source": normalized_source,
        "routes": normalized_routes,
        "schema_repair_max_retries": schema_repair_max_retries,
        "total_timeout_seconds": timeout,
        "source_sha256": canonical_sha256(source_payload),
    }
    contract["contract_sha256"] = canonical_sha256(contract)
    return contract


def validated_task_analyzer_execution_contract(
    value: Any,
) -> dict[str, Any] | None:
    """Return a detached canonical V3 contract, or ``None`` on any drift."""

    if not isinstance(value, Mapping) or set(value) != _CONTRACT_FIELDS:
        return None
    routes = _normalized_routes(value.get("routes"))
    repairs = value.get("schema_repair_max_retries")
    try:
        timeout = float(value.get("total_timeout_seconds"))
    except (TypeError, ValueError):
        return None
    route_source = str(value.get("route_source") or "").strip()
    source_hash = str(value.get("source_sha256") or "")
    contract_hash = str(value.get("contract_sha256") or "")
    if (
        value.get("schema") != TASK_ANALYZER_EXECUTION_CONTRACT_SCHEMA_V3
        or routes is None
        or not route_source
        or isinstance(repairs, bool)
        or repairs not in {0, 1}
        or not math.isfinite(timeout)
        or timeout <= 0.0
        or _HEX64.fullmatch(source_hash) is None
        or _HEX64.fullmatch(contract_hash) is None
    ):
        return None
    normalized = {
        "schema": TASK_ANALYZER_EXECUTION_CONTRACT_SCHEMA_V3,
        "route_source": route_source,
        "routes": routes,
        "schema_repair_max_retries": repairs,
        "total_timeout_seconds": timeout,
        "source_sha256": source_hash,
    }
    if canonical_sha256(normalized) != contract_hash:
        return None
    normalized["contract_sha256"] = contract_hash
    if canonical_sha256(normalized) != canonical_sha256(value):
        return None
    return normalized


def task_analyzer_execution_contract_from_ranking_config(
    ranking_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze the Analyzer policy genuinely present in a ranking config."""

    analyzer = ranking_config.get("task_analyzer")
    if not isinstance(analyzer, Mapping):
        raise ValueError("ranking config lacks task_analyzer")
    primary = {
        "provider": analyzer.get("provider"),
        "model": analyzer.get("model"),
        "upstream_provider": analyzer.get("upstream_provider"),
    }
    chain_configured = "fallback_chain" in analyzer
    if chain_configured:
        fallback_chain = analyzer.get("fallback_chain")
        if not isinstance(fallback_chain, list):
            raise ValueError("ranking Analyzer fallback chain is malformed")
        routes = [
            {**primary, "max_attempts": 1},
            *[{**dict(route), "max_attempts": 1} for route in fallback_chain],
        ]
        repairs = analyzer.get("schema_repair_max_retries")
        total_timeout = analyzer.get("total_timeout_seconds")
    else:
        max_retries = analyzer.get("max_retries")
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or max_retries < 0
        ):
            raise ValueError("ranking Analyzer max_retries is malformed")
        routes = [{**primary, "max_attempts": max_retries + 1}]
        repairs = 0
        total_timeout = analyzer.get("timeout_seconds")
    return build_task_analyzer_execution_contract(
        routes=routes,
        schema_repair_max_retries=repairs,
        total_timeout_seconds=total_timeout,
        route_source="ranking_config.task_analyzer",
        source_payload={"task_analyzer": deepcopy(dict(analyzer))},
    )


def task_analyzer_execution_contract_from_g1_registry(
    g1_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze a formal G1 live chain separately from its historical ranking."""

    analyzer = g1_contract.get("task_analyzer")
    if not isinstance(analyzer, Mapping):
        raise ValueError("G1 registry contract lacks task_analyzer")
    live_chain = g1_contract.get("live_task_analyzer_chain")
    if not isinstance(live_chain, list) or not live_chain:
        ranking_payload = {
            "task_analyzer": deepcopy(dict(analyzer)),
        }
        return build_task_analyzer_execution_contract(
            routes=[
                {
                    "provider": analyzer.get("provider"),
                    "model": analyzer.get("model"),
                    "upstream_provider": analyzer.get("upstream_provider"),
                    "max_attempts": int(analyzer.get("max_retries")) + 1,
                }
            ],
            schema_repair_max_retries=0,
            total_timeout_seconds=float(analyzer.get("timeout_seconds")),
            route_source="g1_registry_contract.task_analyzer",
            source_payload=ranking_payload,
        )
    routes = [dict(route) for route in live_chain]
    if any(route.get("max_attempts") != 1 for route in routes):
        raise ValueError("formal G1 live Analyzer routes must have max_attempts=1")
    first = routes[0]
    if any(
        str(first.get(field) or "").strip().casefold()
        != str(analyzer.get(field) or "").strip().casefold()
        for field in ("provider", "model", "upstream_provider")
    ):
        raise ValueError("formal G1 Analyzer primary differs from ranking policy")
    per_route_timeout = float(analyzer.get("timeout_seconds"))
    source_payload = {
        "task_analyzer": deepcopy(dict(analyzer)),
        "live_task_analyzer_chain": deepcopy(routes),
    }
    return build_task_analyzer_execution_contract(
        routes=routes,
        schema_repair_max_retries=0,
        total_timeout_seconds=per_route_timeout * len(routes),
        route_source="g1_registry_contract.live_task_analyzer_chain",
        source_payload=source_payload,
    )


def task_analyzer_execution_contract_matches_source(
    execution_contract: Mapping[str, Any],
    task_analyzer_config: Mapping[str, Any],
) -> bool:
    """Bind a formal G1 execution contract to its authenticated source payload."""

    contract = validated_task_analyzer_execution_contract(execution_contract)
    if contract is None or not isinstance(task_analyzer_config, Mapping):
        return False
    routes = contract["routes"]
    primary = routes[0]
    if any(
        str(primary[field]).strip().casefold()
        != str(task_analyzer_config.get(field) or "").strip().casefold()
        for field in ("provider", "model", "upstream_provider")
    ):
        return False
    route_source = contract["route_source"]
    if route_source == "g1_registry_contract.live_task_analyzer_chain":
        source_payload = {
            "task_analyzer": deepcopy(dict(task_analyzer_config)),
            "live_task_analyzer_chain": deepcopy(routes),
        }
    elif route_source == "g1_registry_contract.task_analyzer" and len(routes) == 1:
        source_payload = {
            "task_analyzer": deepcopy(dict(task_analyzer_config)),
        }
    else:
        return False
    return canonical_sha256(source_payload) == contract["source_sha256"]


def _default_models_equivalent(left: Any, right: Any) -> bool:
    return str(left or "").strip().casefold() == str(right or "").strip().casefold()


def _validate_physical_attempt_evidence(
    *,
    attempts: Sequence[Mapping[str, Any]],
    expected_routes: Sequence[Mapping[str, Any]],
    usage: Mapping[str, Any] | None,
    equivalent: Callable[[Any, Any], bool],
) -> str | None:
    """Validate request, response, receipt, and aggregate usage as one ledger."""

    if not isinstance(usage, Mapping):
        return "invalid_task_analyzer_usage_evidence"
    declared_count = usage.get("attempt_count")
    if (
        isinstance(declared_count, bool)
        or not isinstance(declared_count, int)
        or declared_count != len(attempts)
    ):
        return "invalid_task_analyzer_usage_count"
    declared_physical_count = usage.get("physical_request_count")
    if "physical_request_count" in usage and (
        isinstance(declared_physical_count, bool)
        or not isinstance(declared_physical_count, int)
        or declared_physical_count != len(attempts)
    ):
        return "invalid_task_analyzer_usage_count"

    seen_ids: set[str] = set()
    unknown_count = 0
    token_totals = {field: 0 for field in _ATTEMPT_TOKEN_FIELDS}
    cost_total = 0.0
    for ordinal, (attempt, route) in enumerate(
        zip(attempts, expected_routes, strict=True),
        start=1,
    ):
        if not isinstance(attempt, Mapping):
            return "invalid_task_analyzer_physical_attempts"
        attempt_id = str(attempt.get("physical_attempt_id") or "").strip().casefold()
        requested_provider = str(
            attempt.get("requested_provider") or ""
        ).strip().casefold()
        requested_model = attempt.get("requested_model")
        if (
            attempt.get("attempt") != ordinal
            or _HEX32.fullmatch(attempt_id) is None
            or attempt_id in seen_ids
            or requested_provider != route["provider"]
            or not equivalent(requested_model, route["model"])
        ):
            return "invalid_task_analyzer_physical_attempts"
        seen_ids.add(attempt_id)

        provider_usage = attempt.get("provider_usage")
        if not isinstance(provider_usage, Mapping):
            return "invalid_task_analyzer_provider_usage"
        if (
            str(provider_usage.get("physical_attempt_id") or "")
            .strip()
            .casefold()
            != attempt_id
        ):
            return "invalid_task_analyzer_physical_attempt_mirror"
        for reported in (
            attempt.get("reported_physical_attempt_ids"),
            provider_usage.get("reported_physical_attempt_ids"),
        ):
            if reported in (None, []):
                continue
            if not isinstance(reported, list) or [
                str(item).strip().casefold() for item in reported
            ] != [attempt_id]:
                return "conflicting_task_analyzer_physical_attempt_ids"

        for field in _ATTEMPT_TOKEN_FIELDS:
            value = attempt.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return "invalid_task_analyzer_usage_evidence"
            token_totals[field] += value
        billed_cost = attempt.get("billed_cost")
        if (
            isinstance(billed_cost, bool)
            or not isinstance(billed_cost, int | float)
            or not math.isfinite(float(billed_cost))
            or float(billed_cost) < 0.0
        ):
            return "invalid_task_analyzer_usage_evidence"
        cost_total += float(billed_cost)

        usage_unknown = attempt.get("usage_unknown") is True
        actual_provider = str(attempt.get("provider") or "").strip().casefold()
        actual_model = attempt.get("model")
        nested_provider = str(provider_usage.get("provider") or "").strip().casefold()
        nested_model = provider_usage.get("model")
        if usage_unknown:
            unknown_count += 1
            unknown_reason = str(attempt.get("unknown_reason") or "").strip()
            nested_reason = str(provider_usage.get("unknown_reason") or "").strip()
            if (
                actual_provider
                or str(actual_model or "").strip()
                or nested_provider
                or str(nested_model or "").strip()
                or any(attempt[field] != 0 for field in _ATTEMPT_TOKEN_FIELDS)
                or float(billed_cost) != 0.0
                or provider_usage.get("usage_unknown") is not True
                or not unknown_reason
                or nested_reason != unknown_reason
            ):
                return "contradictory_task_analyzer_unknown_usage"
        elif (
            provider_usage.get("usage_unknown") is True
            or actual_provider != route["provider"]
            or not equivalent(actual_model, route["model"])
            or (nested_provider and nested_provider != route["provider"])
            or (nested_model and not equivalent(nested_model, route["model"]))
        ):
            return "wrong_task_analyzer_response_identity"

    declared_unknown_count = usage.get("usage_unknown_count")
    if "usage_unknown_count" in usage and (
        isinstance(declared_unknown_count, bool)
        or not isinstance(declared_unknown_count, int)
        or declared_unknown_count != unknown_count
    ):
        return "invalid_task_analyzer_usage_count"
    for field, total in token_totals.items():
        if field in usage:
            aggregate = usage.get(field)
            if (
                isinstance(aggregate, bool)
                or not isinstance(aggregate, int)
                or aggregate != total
            ):
                return "invalid_task_analyzer_usage_aggregate"
    if "billed_cost" in usage:
        aggregate_cost = usage.get("billed_cost")
        if (
            isinstance(aggregate_cost, bool)
            or not isinstance(aggregate_cost, int | float)
            or not math.isfinite(float(aggregate_cost))
            or not math.isclose(
                float(aggregate_cost),
                cost_total,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            return "invalid_task_analyzer_usage_aggregate"
    return None


def validate_task_analyzer_execution_trace(
    *,
    execution_contract: Mapping[str, Any],
    analyzer_trace: Mapping[str, Any],
    physical_attempts: Sequence[Mapping[str, Any]] | None = None,
    models_equivalent: Callable[[Any, Any], bool] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Authenticate chain, deadline, selected identity, and request prefix."""

    contract = validated_task_analyzer_execution_contract(execution_contract)
    if contract is None or not isinstance(analyzer_trace, Mapping):
        return None, ["invalid_task_analyzer_execution_contract"]
    equivalent = models_equivalent or _default_models_equivalent
    routes = contract["routes"]
    trace_routes = [
        {key: route[key] for key in _TRACE_ROUTE_FIELDS}
        for route in routes
    ]
    chain = analyzer_trace.get("chain")
    usage = analyzer_trace.get("usage")
    raw_usage_attempts = (
        usage.get("physical_attempts") if isinstance(usage, Mapping) else None
    )
    supplied_physical_attempts = physical_attempts
    if physical_attempts is None:
        physical_attempts = (
            raw_usage_attempts if isinstance(raw_usage_attempts, list) else None
        )

    if chain is None:
        if len(routes) != 1:
            return None, ["missing_task_analyzer_chain_trace"]
        attempts = list(physical_attempts or [])
        if physical_attempts is None or len(attempts) > int(routes[0]["max_attempts"]):
            return None, ["invalid_task_analyzer_physical_attempts"]
        expected_attempt_routes = [routes[0]] * len(attempts)
        selected_route = routes[0] if analyzer_trace.get("schema_valid") is True else None
        exhausted = analyzer_trace.get("schema_valid") is not True
        legacy_trace = True
        deadline = None
    else:
        if not isinstance(chain, Mapping):
            return None, ["invalid_task_analyzer_chain_trace"]
        configured_routes = chain.get("configured_routes")
        outcomes = chain.get("attempt_outcomes")
        if (
            chain.get("protocol") != TASK_ANALYZER_FALLBACK_CHAIN_PROTOCOL
            or configured_routes != trace_routes
            or not isinstance(outcomes, list)
            or not outcomes
            or len(outcomes) > len(routes)
        ):
            return None, ["invalid_task_analyzer_chain_trace"]
        has_repair = "schema_repair_max_retries" in chain
        has_deadline = "deadline" in chain
        if has_repair is not has_deadline:
            return None, ["invalid_task_analyzer_chain_trace"]
        legacy_trace = not has_repair
        if has_repair and chain.get("schema_repair_max_retries") != contract[
            "schema_repair_max_retries"
        ]:
            return None, ["wrong_task_analyzer_repair_budget"]
        if has_deadline:
            deadline = chain.get("deadline")
            if not isinstance(deadline, Mapping) or set(deadline) != _DEADLINE_FIELDS:
                return None, ["invalid_task_analyzer_deadline_trace"]
            configured_deadline = deadline.get("configured_seconds")
            elapsed_deadline = deadline.get("elapsed_seconds")
            remaining_deadline = deadline.get("remaining_seconds")
            if any(
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(float(value))
                or float(value) < 0.0
                for value in (
                    configured_deadline,
                    elapsed_deadline,
                    remaining_deadline,
                )
            ):
                return None, ["invalid_task_analyzer_deadline_trace"]
            configured_value = float(configured_deadline)
            elapsed_value = float(elapsed_deadline)
            remaining_value = float(remaining_deadline)
            tolerance = 1e-3
            if (
                configured_value
                > float(contract["total_timeout_seconds"]) + tolerance
                or remaining_value > configured_value + tolerance
                or (
                    deadline.get("expired") is True
                    and (
                        remaining_value != 0.0
                        or elapsed_value + tolerance < configured_value
                    )
                )
                or (
                    deadline.get("expired") is False
                    and abs(
                        configured_value
                        - elapsed_value
                        - remaining_value
                    )
                    > tolerance
                )
                or not isinstance(deadline.get("expired"), bool)
            ):
                return None, ["invalid_task_analyzer_deadline_trace"]
        expected_attempt_routes: list[dict[str, Any]] = []
        success_indexes: list[int] = []
        repairs_used = 0
        for index, outcome in enumerate(outcomes):
            route = routes[index]
            if not isinstance(outcome, Mapping) or set(outcome) != _OUTCOME_FIELDS:
                return None, ["invalid_task_analyzer_chain_trace"]
            count = outcome.get("physical_request_count")
            outcome_kind = outcome.get("outcome")
            reason = str(outcome.get("reason") or "").strip()
            route_matches = bool(
                outcome.get("candidate_index") == index
                and str(outcome.get("provider") or "").strip().casefold()
                == route["provider"]
                and equivalent(outcome.get("model"), route["model"])
                and str(outcome.get("upstream_provider") or "").strip().casefold()
                == route["upstream_provider"]
            )
            if (
                not route_matches
                or outcome_kind not in {"success", "failed"}
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
                or count > int(route["max_attempts"]) + (0 if legacy_trace else 1)
                or (outcome_kind == "success" and (count == 0 or reason))
                or (
                    outcome_kind == "failed"
                    and _PUBLIC_REASON.fullmatch(reason) is None
                )
            ):
                return None, ["invalid_task_analyzer_chain_trace"]
            repairs_used += max(0, count - int(route["max_attempts"]))
            expected_attempt_routes.extend([route] * count)
            if outcome_kind == "success":
                success_indexes.append(index)
        allowed_repairs = 0 if legacy_trace else int(contract["schema_repair_max_retries"])
        if repairs_used > allowed_repairs:
            return None, ["task_analyzer_repair_budget_exceeded"]
        selected_index = chain.get("selected_index")
        exhausted = chain.get("exhausted")
        deadline = chain.get("deadline") if has_deadline else None
        if success_indexes:
            if (
                success_indexes != [len(outcomes) - 1]
                or selected_index != success_indexes[0]
                or exhausted is not False
            ):
                return None, ["invalid_task_analyzer_selection"]
            selected_route = routes[success_indexes[0]]
            if (
                analyzer_trace.get("source") != "llm_provider"
                or analyzer_trace.get("schema_valid") is not True
                or str(analyzer_trace.get("fallback_reason") or "")
            ):
                return None, ["wrong_task_analyzer_success_result"]
        else:
            if selected_index is not None or exhausted is not True:
                return None, ["invalid_task_analyzer_exhaustion"]
            prefix_exhausted = len(outcomes) < len(routes)
            if prefix_exhausted:
                if (
                    legacy_trace
                    or not isinstance(deadline, Mapping)
                    or set(deadline) != _DEADLINE_FIELDS
                    or deadline.get("expired") is not True
                    or outcomes[-1].get("outcome") != "failed"
                ):
                    return None, ["unproven_task_analyzer_deadline_exhaustion"]
            selected_route = None

    attempts = list(physical_attempts or [])
    if (
        physical_attempts is None
        or not isinstance(raw_usage_attempts, list)
        or len(attempts) != len(expected_attempt_routes)
        or len(raw_usage_attempts) != len(expected_attempt_routes)
    ):
        return None, ["invalid_task_analyzer_physical_attempts"]
    raw_usage_reason = _validate_physical_attempt_evidence(
        attempts=raw_usage_attempts,
        expected_routes=expected_attempt_routes,
        usage=usage,
        equivalent=equivalent,
    )
    if raw_usage_reason is not None:
        return None, [raw_usage_reason]
    if supplied_physical_attempts is not None:
        supplied_reason = _validate_physical_attempt_evidence(
            attempts=attempts,
            expected_routes=expected_attempt_routes,
            usage={"attempt_count": len(attempts)},
            equivalent=equivalent,
        )
        if supplied_reason is not None:
            return None, [supplied_reason]
        raw_ids = [
            str(attempt.get("physical_attempt_id") or "").strip().casefold()
            for attempt in raw_usage_attempts
        ]
        supplied_ids = [
            str(attempt.get("physical_attempt_id") or "").strip().casefold()
            for attempt in attempts
        ]
        if supplied_ids != raw_ids:
            return None, ["task_analyzer_physical_attempt_mirror_differs"]

    expected_result_route = (
        selected_route
        or (
            routes[len(chain["attempt_outcomes"]) - 1]
            if isinstance(chain, Mapping)
            else routes[0]
        )
    )
    if chain is None:
        if selected_route is not None and (
            analyzer_trace.get("source") != "llm_provider"
            or analyzer_trace.get("schema_valid") is not True
            or str(analyzer_trace.get("fallback_reason") or "")
        ):
            return None, ["wrong_task_analyzer_success_result"]
        if selected_route is None and (
            analyzer_trace.get("source") != "router_fallback"
            or analyzer_trace.get("schema_valid") is not False
            or not str(analyzer_trace.get("fallback_reason") or "").strip()
        ):
            return None, ["wrong_task_analyzer_exhaustion_result"]
    if (
        str(analyzer_trace.get("provider") or "").strip().casefold()
        != expected_result_route["provider"]
        or not equivalent(analyzer_trace.get("model"), expected_result_route["model"])
    ):
        return None, ["wrong_task_analyzer_result_identity"]
    if selected_route is None and chain is not None:
        outcomes = chain["attempt_outcomes"]
        if (
            analyzer_trace.get("source") != "router_fallback"
            or analyzer_trace.get("schema_valid") is not False
            or str(analyzer_trace.get("fallback_reason") or "").strip()
            != str(outcomes[-1].get("reason") or "").strip()
        ):
            return None, ["wrong_task_analyzer_exhaustion_result"]

    return (
        {
            "physical_routes": deepcopy(expected_attempt_routes),
            "selected_route": deepcopy(selected_route),
            "exhausted": bool(exhausted),
            "legacy_trace": legacy_trace,
            "deadline": deepcopy(deadline),
        },
        [],
    )
