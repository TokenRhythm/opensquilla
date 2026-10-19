"""Read-time replacement of item valuations; revisions never add new calls."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from opensquilla.session.cost_rollup import rollup_cost_source


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return dict(vars(value))


def _nanos(amount: Any) -> int | None:
    if amount is None:
        return None
    return int((Decimal(str(amount)) * 1_000_000_000).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _unpriced_custom_free(row: Mapping[str, Any]) -> bool:
    """Only the old custom fallback proves that a free label lacked a quote."""
    return (
        row.get("price_source") == "custom_free"
        and row.get("cost_source") == "free"
        and all(
            int(row.get(name) or 0) == 0
            for name in ("cost_nanos", "billed_cost_nanos", "estimated_cost_nanos")
        )
    )


def _mark_unpriced(row: dict[str, Any]) -> None:
    row.update(
        cost_source="unavailable",
        estimate_basis=None,
        missing_cost_entries=max(1, int(row.get("missing_cost_entries") or 0)),
    )


def apply_current_settlements(
    events: Sequence[Any],
    items_by_event: Mapping[str, Sequence[Any]],
    receipts: Sequence[Any],
    settlements: Sequence[Mapping[str, Any]],
) -> tuple[list[Any], dict[str, Sequence[Any]], list[Any]]:
    """Keep the physical event and token measurements, replace only valuation."""
    current = {(str(row["event_id"]), int(row["ordinal"])): row for row in settlements}
    native = {}
    confirmed_keys = set()
    for receipt in receipts:
        row = _mapping(receipt)
        key = (row["event_id"], row["ordinal"])
        native[key] = receipt
        if row.get("status") == "confirmed":
            confirmed_keys.add(key)
    protected_event_ids = {key[0] for key in confirmed_keys | current.keys()}
    updated_items: dict[str, Sequence[Any]] = dict(items_by_event)
    updated_events: list[Any] = []
    for original in events:
        event = _mapping(original)
        event_id = str(event["event_id"])
        rows = [_mapping(item) for item in items_by_event.get(event_id, ())]
        unpriced_ordinals = {
            int(item["ordinal"])
            for item in rows
            if _unpriced_custom_free(item)
            and (event_id, int(item["ordinal"])) not in confirmed_keys
            and (event_id, int(item["ordinal"])) not in current
        }
        if not rows and _unpriced_custom_free(event) and event_id not in protected_event_ids:
            _mark_unpriced(event)
            updated_events.append(event)
            continue
        if not unpriced_ordinals and not any(
            (event_id, int(item["ordinal"])) in current for item in rows
        ):
            updated_events.append(original)
            continue
        original_item_missing = sum(
            max(
                int(item.get("missing_cost_entries") or 0),
                int(item.get("cost_source") == "unavailable"),
            )
            for item in rows
        )
        # Missing ensemble usage can exist only on the envelope, without an
        # item to settle. Pricing the known items cannot erase that evidence.
        missing_usage_residual = max(
            0, int(event.get("missing_cost_entries") or 0) - original_item_missing
        )
        replacements = []
        for item in rows:
            key = (event_id, int(item["ordinal"]))
            revision = current.get(key)
            if revision is not None:
                usd = revision.get("usd_amount")
                exact = Decimal(str(usd)) if usd is not None else Decimal(0)
                billed = revision["authority"] == "provider_confirmed"
                item["cost_usd_exact"] = format(exact, "f")
                item["billed_cost_usd_exact"] = format(exact if billed else Decimal(0), "f")
                item["estimated_cost_usd_exact"] = format(Decimal(0) if billed else exact, "f")
                item.update(
                    {
                        name: revision[name]
                        for name in (
                            "cost_nanos",
                            "billed_cost_nanos",
                            "estimated_cost_nanos",
                            "cost_source",
                            "missing_cost_entries",
                        )
                    }
                )
                old_receipt = native.pop(key, None)
                if revision["authority"] == "provider_confirmed" and revision["status"] == "final":
                    # Preserve a recorded FX rate; absent normalization evidence
                    # remains absent instead of inventing one from rounded USD.
                    native[key] = {
                        "event_id": event_id,
                        "ordinal": key[1],
                        "status": "confirmed",
                        "currency": revision["currency"],
                        "amount_nanos": _nanos(revision.get("native_amount")),
                        "usd_equivalent_nanos": _nanos(revision.get("usd_amount")),
                        "amount_exact": revision.get("native_amount"),
                        "usd_equivalent_exact": revision.get("usd_amount"),
                        "fx_native_per_usd": revision.get("fx_native_per_usd"),
                        "fx_source": revision.get("fx_source"),
                        "fx_source_revision": revision.get("fx_source_revision"),
                        "fx_native_per_usd_nanos": (
                            _nanos(revision["fx_native_per_usd"])
                            if revision.get("fx_native_per_usd") is not None
                            else _mapping(old_receipt).get("fx_native_per_usd_nanos")
                            if old_receipt is not None
                            else 1_000_000_000
                            if revision["currency"] == "USD"
                            else None
                        ),
                    }
                elif revision["status"] == "pending":
                    native[key] = {
                        "event_id": event_id,
                        "ordinal": key[1],
                        "status": "pending",
                        "currency": revision["currency"],
                    }
            elif key[1] in unpriced_ordinals:
                _mark_unpriced(item)
            replacements.append(item)
        updated_items[event_id] = replacements
        for name in ("cost_nanos", "billed_cost_nanos", "estimated_cost_nanos"):
            event[name] = sum(int(item.get(name) or 0) for item in replacements)
            exact_name = name.replace("_nanos", "_usd_exact")
            event[exact_name] = format(
                sum(
                    (
                        Decimal(str(item[exact_name]))
                        if exact_name in item
                        else Decimal(int(item.get(name) or 0)) / 1_000_000_000
                        for item in replacements
                    ),
                    Decimal(0),
                ),
                "f",
            )
        event["missing_cost_entries"] = missing_usage_residual + sum(
            max(
                int(item.get("missing_cost_entries") or 0),
                int(item.get("cost_source") == "unavailable"),
            )
            for item in replacements
        )
        event["cost_source"] = rollup_cost_source(
            billed_cost_usd=event["billed_cost_nanos"] / 1_000_000_000,
            estimated_cost_component_usd=event["estimated_cost_nanos"] / 1_000_000_000,
            missing_cost_entries=event["missing_cost_entries"],
            provider_billed_entries=sum(
                item.get("cost_source") in {"provider_billed", "mixed"} for item in replacements
            ),
            estimated_cost_entries=sum(
                item.get("cost_source") in {"opensquilla_estimate", "mixed"}
                for item in replacements
            ),
        )
        if unpriced_ordinals and event.get("estimate_basis") == "free":
            event["estimate_basis"] = None
        updated_events.append(event)
    return updated_events, updated_items, list(native.values())


def summarize_metering(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Quantities are comparable only within the same metric/unit/dimensions."""
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        for meter in row.get("meters", ()):
            # Provider request IDs identify receipts, not a meter dimension
            # suitable for aggregation or a user-facing capability label.
            dimensions = {
                str(k): str(v)
                for k, v in meter.get("dimensions", {}).items()
                if k != "provider_request_id"
            }
            key = (str(meter["metric"]), str(meter["unit"]), tuple(sorted(dimensions.items())))
            bucket = grouped.setdefault(
                key,
                {
                    "metric": key[0],
                    "unit": key[1],
                    "dimensions": dimensions,
                    "quantity": Decimal(0),
                    "unknownCount": 0,
                    "measurementCount": 0,
                },
            )
            bucket["measurementCount"] += 1
            if meter.get("known") and meter.get("quantity") is not None:
                bucket["quantity"] += Decimal(str(meter["quantity"]))
            else:
                bucket["unknownCount"] += 1
    return [{**bucket, "quantity": format(bucket["quantity"], "f")} for bucket in grouped.values()]
