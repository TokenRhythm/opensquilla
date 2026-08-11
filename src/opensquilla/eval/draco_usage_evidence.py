"""Pure DRACO helpers for merging and deduplicating usage receipts.

The experiment and resume runners deliberately re-export these names for
backward compatibility.  Keep this module independent from either script so
both execution paths use exactly the same receipt semantics.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from opensquilla.usage_evidence import is_missing_usage_placeholder

STABLE_RECEIPT_EVIDENCE_KEY = "stable_receipt_evidence"


def _coerce_metric_int(value: Any) -> int:
    """Mirror the runners' nonnegative metric coercion without importing them."""

    if isinstance(value, bool):
        return 0
    if isinstance(value, int | float):
        return max(0, int(value))
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def usage_row_is_missing_placeholder(row: Mapping[str, Any]) -> bool:
    return is_missing_usage_placeholder(row)


def usage_row_response_ids(row: Mapping[str, Any]) -> frozenset[str]:
    values: list[Any] = []
    direct = row.get("response_id")
    if direct is not None:
        values.append(direct)
    provider_usage = row.get("provider_usage")
    if isinstance(provider_usage, Mapping):
        response_ids = provider_usage.get("response_ids")
        if isinstance(response_ids, (list, tuple, set, frozenset)):
            values.extend(response_ids)
        elif response_ids is not None:
            values.append(response_ids)
        response_id = provider_usage.get("response_id")
        if response_id is not None:
            values.append(response_id)
    return frozenset(str(value).strip() for value in values if str(value).strip())


def usage_receipt_fingerprint(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(row.get("provider") or "").strip(),
        str(row.get("model") or "").strip(),
        _coerce_metric_int(row.get("input_tokens")),
        _coerce_metric_int(row.get("output_tokens")),
        _coerce_metric_int(row.get("reasoning_tokens")),
        _coerce_metric_int(row.get("cached_tokens")),
        _coerce_metric_int(row.get("cache_write_tokens")),
        float(row.get("billed_cost") or 0.0),
        str(row.get("cost_source") or "none").strip().casefold(),
    )


def usage_row_match_priority(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> int | None:
    if usage_row_is_missing_placeholder(left) != usage_row_is_missing_placeholder(right):
        return None
    left_ids = usage_row_response_ids(left)
    right_ids = usage_row_response_ids(right)
    if left_ids and right_ids:
        return 0 if left_ids & right_ids else None
    if usage_receipt_fingerprint(left) != usage_receipt_fingerprint(right):
        return None
    return 1 if not left_ids and not right_ids else 2


def build_stable_receipt_evidence(
    *rows: Mapping[str, Any],
) -> dict[str, Any]:
    providers: set[str] = set()
    models: set[str] = set()
    cost_usd_nanos: set[int] = set()
    usage_is_byok_values: set[bool] = set()
    router_is_byok_values: set[bool] = set()
    token_values: dict[str, set[int]] = {
        key: set()
        for key in (
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cached_tokens",
            "cache_write_tokens",
        )
    }
    inherited_conflicts: set[str] = set()

    def _add_bool(value: Any, target: set[bool]) -> None:
        if value is True or value is False:
            target.add(value)

    def _add_cost(value: Any) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            return
        cost_usd_nanos.add(int(round(float(value) * 1_000_000_000)))

    for row in rows:
        provider = str(row.get("provider") or "").strip().casefold()
        if provider:
            providers.add(provider)
        model = str(row.get("model") or "").strip()
        if model:
            models.add(model)
        _add_cost(row.get("billed_cost"))
        for key, values in token_values.items():
            raw_value = row.get(key)
            if isinstance(raw_value, int) and not isinstance(raw_value, bool) and raw_value >= 0:
                values.add(raw_value)
        billing_receipt = row.get("billing_receipt")
        if isinstance(billing_receipt, Mapping):
            receipt_nanos = billing_receipt.get("usd_equivalent_nanos")
            if (
                isinstance(receipt_nanos, int)
                and not isinstance(receipt_nanos, bool)
                and receipt_nanos >= 0
            ):
                cost_usd_nanos.add(receipt_nanos)

        provider_usage = row.get("provider_usage")
        if not isinstance(provider_usage, Mapping):
            continue
        _add_bool(provider_usage.get("is_byok"), usage_is_byok_values)
        _add_cost(provider_usage.get("provider_reported_cost"))
        router_metadata = provider_usage.get("router_metadata")
        if isinstance(router_metadata, Mapping):
            _add_bool(router_metadata.get("is_byok"), router_is_byok_values)
        inherited = provider_usage.get(STABLE_RECEIPT_EVIDENCE_KEY)
        if not isinstance(inherited, Mapping):
            continue
        providers.update(
            str(value).strip().casefold()
            for value in inherited.get("providers") or []
            if str(value).strip()
        )
        models.update(
            str(value).strip() for value in inherited.get("models") or [] if str(value).strip()
        )
        for value in inherited.get("cost_usd_nanos") or []:
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                cost_usd_nanos.add(value)
        for value in inherited.get("usage_is_byok_values") or []:
            _add_bool(value, usage_is_byok_values)
        for value in inherited.get("router_is_byok_values") or []:
            _add_bool(value, router_is_byok_values)
        inherited_token_values = inherited.get("token_values")
        if isinstance(inherited_token_values, Mapping):
            for key, values in token_values.items():
                for value in inherited_token_values.get(key) or []:
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        values.add(value)
        inherited_conflicts.update(
            str(value).strip()
            for value in inherited.get("conflict_fields") or []
            if str(value).strip()
        )

    conflict_fields = set(inherited_conflicts)
    if len(providers) > 1:
        conflict_fields.add("provider")
    if len(models) > 1:
        conflict_fields.add("model")
    if len(cost_usd_nanos) > 1:
        conflict_fields.add("cost_usd_nanos")
    if len(usage_is_byok_values | router_is_byok_values) > 1:
        conflict_fields.add("is_byok")
    for key, values in token_values.items():
        if len(values) > 1:
            conflict_fields.add(key)

    return {
        "providers": sorted(providers),
        "models": sorted(models),
        "cost_usd_nanos": sorted(cost_usd_nanos),
        "usage_is_byok_values": sorted(usage_is_byok_values),
        "router_is_byok_values": sorted(router_is_byok_values),
        "token_values": {key: sorted(values) for key, values in token_values.items() if values},
        "conflict_fields": sorted(conflict_fields),
        "receipt_conflict": bool(conflict_fields),
    }


def merge_usage_receipt_provenance(
    target: dict[str, Any],
    source: Mapping[str, Any],
) -> None:
    target_ids = usage_row_response_ids(target)
    source_ids = usage_row_response_ids(source)
    stable_id_match = bool(target_ids and source_ids and target_ids & source_ids)
    stable_receipt_evidence = (
        build_stable_receipt_evidence(target, source) if stable_id_match else None
    )
    if stable_id_match:
        for key in (
            "provider",
            "model",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cached_tokens",
            "cache_write_tokens",
            "billed_cost",
            "cost_source",
            "billing_receipt",
        ):
            if key in source:
                target[key] = source[key]
    for key in (
        "provider",
        "model",
        "requested_provider",
        "requested_model",
        "response_id",
        "billing_receipt",
    ):
        if not target.get(key) and source.get(key):
            target[key] = source[key]
    source_usage = source.get("provider_usage")
    source_usage = source_usage if isinstance(source_usage, Mapping) else {}
    target_usage = (
        dict(target.get("provider_usage"))
        if isinstance(target.get("provider_usage"), Mapping)
        else {}
    )
    for key, value in source_usage.items():
        if key == STABLE_RECEIPT_EVIDENCE_KEY:
            continue
        if key == "response_ids":
            existing = target_usage.get(key)
            existing_values = (
                list(existing)
                if isinstance(existing, (list, tuple, set, frozenset))
                else [existing]
                if existing is not None
                else []
            )
            source_values = (
                list(value) if isinstance(value, (list, tuple, set, frozenset)) else [value]
            )
            target_usage[key] = sorted(
                {
                    str(item).strip()
                    for item in [*existing_values, *source_values]
                    if str(item).strip()
                }
            )
        elif stable_id_match or not target_usage.get(key):
            target_usage[key] = value
    if stable_receipt_evidence is not None:
        target_usage[STABLE_RECEIPT_EVIDENCE_KEY] = stable_receipt_evidence
    if target_usage:
        target["provider_usage"] = target_usage


def deduplicate_stable_usage_receipts(
    units: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse repeated physical receipts only when a stable response ID proves it."""

    deduplicated: list[dict[str, Any]] = []
    for unit in units:
        row = dict(unit)
        response_ids = usage_row_response_ids(row)
        if not response_ids:
            # Similar token/cost values are not proof of the same physical call.
            deduplicated.append(row)
            continue
        matching_indexes = [
            index
            for index, existing in enumerate(deduplicated)
            if response_ids & usage_row_response_ids(existing)
        ]
        if not matching_indexes:
            deduplicated.append(row)
            continue
        target = deduplicated[matching_indexes[0]]
        merge_usage_receipt_provenance(target, row)
        # A later receipt can bridge two previously separate response-id sets.
        for index in reversed(matching_indexes[1:]):
            merge_usage_receipt_provenance(target, deduplicated.pop(index))
    return deduplicated


__all__ = [
    "STABLE_RECEIPT_EVIDENCE_KEY",
    "build_stable_receipt_evidence",
    "deduplicate_stable_usage_receipts",
    "merge_usage_receipt_provenance",
    "usage_receipt_fingerprint",
    "usage_row_is_missing_placeholder",
    "usage_row_match_priority",
    "usage_row_response_ids",
]
