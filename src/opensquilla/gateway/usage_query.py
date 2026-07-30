"""Server-side aggregation for the durable usage ledger.

The legacy ``usage.status`` surface exposes session-lifetime counters.  This
module deliberately does not reuse those rows for time-window queries: every
finite range is built from timestamped ledger events and all dimensions share
one captured ``as_of_ms`` snapshot.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as datetime_time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from opensquilla.provider.fx import canonical_native_per_usd_rates
from opensquilla.session.cost_rollup import rollup_cost_source

_PRESET_DAYS = {
    "today": 1,
    "last_7_calendar_days": 7,
    "last_14_calendar_days": 14,
    "last_30_calendar_days": 30,
}

# These adapters can issue provider-native receipts. TokenRhythm promises a
# settlement state for every physical request, while OpenRouter's receipt is
# expected only when its legacy ``usage.cost`` was accepted as provider billed.
# Other providers may expose billed USD through legacy fields without a native
# receipt, so treating every ``provider_billed`` item as missing would
# incorrectly degrade their coverage.
_NATIVE_RECEIPT_PROVIDER_IDS = frozenset({"openrouter", "tokenrhythm"})


class UsageQueryValidationError(ValueError):
    """Raised when a range or timezone cannot be interpreted safely."""


def _field(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _first(source: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        value = _field(source, name)
        if value is not None:
            return value
    return default


def _integer(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _text(value: Any, default: str = "") -> str:
    return str(value) if value is not None and value != "" else default


def _millis(local: datetime) -> int:
    return int(local.timestamp() * 1000)


@dataclass(frozen=True)
class ResolvedUsageRange:
    preset: str | None
    timezone: str
    from_ms: int | None
    to_ms: int
    as_of_ms: int
    zone: ZoneInfo

    def payload(self) -> dict[str, Any]:
        return {
            "preset": self.preset,
            "timezone": self.timezone,
            "fromMs": self.from_ms,
            "toMs": self.to_ms,
            "endExclusive": True,
        }


def resolve_usage_range(
    params: Mapping[str, Any] | None,
    *,
    now_ms: int | None = None,
) -> ResolvedUsageRange:
    params = params or {}
    schema_version = params.get("schemaVersion", 1)
    if schema_version != 1:
        raise UsageQueryValidationError("usage.query only supports schemaVersion 1")

    timezone_name = _text(params.get("timezone"), "UTC")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise UsageQueryValidationError(f"Unknown IANA timezone: {timezone_name}") from exc

    as_of_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    range_raw = params.get("range") or {"preset": "last_7_calendar_days"}
    if not isinstance(range_raw, Mapping):
        raise UsageQueryValidationError("range must be an object")

    preset_value = range_raw.get("preset")
    preset = _text(preset_value) if preset_value is not None else None
    has_custom = "fromMs" in range_raw or "toMs" in range_raw
    if preset and has_custom:
        raise UsageQueryValidationError("range.preset and custom bounds are mutually exclusive")

    if preset:
        if preset == "all":
            return ResolvedUsageRange(
                preset=preset,
                timezone=timezone_name,
                from_ms=None,
                to_ms=as_of_ms,
                as_of_ms=as_of_ms,
                zone=zone,
            )
        days = _PRESET_DAYS.get(preset)
        if days is None:
            raise UsageQueryValidationError(f"Unknown usage range preset: {preset}")
        local_now = datetime.fromtimestamp(as_of_ms / 1000, tz=zone)
        start_date = local_now.date() - timedelta(days=days - 1)
        start = datetime.combine(start_date, datetime_time.min, tzinfo=zone)
        return ResolvedUsageRange(
            preset=preset,
            timezone=timezone_name,
            from_ms=_millis(start),
            to_ms=as_of_ms,
            as_of_ms=as_of_ms,
            zone=zone,
        )

    from_raw = range_raw.get("fromMs")
    to_raw = range_raw.get("toMs", as_of_ms)
    if from_raw is None:
        raise UsageQueryValidationError("custom range.fromMs is required")
    try:
        from_ms = int(from_raw)
        to_ms = int(to_raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise UsageQueryValidationError(
            "custom range bounds must be integer epoch milliseconds"
        ) from exc
    if from_ms < 0 or to_ms < 0 or from_ms >= to_ms:
        raise UsageQueryValidationError("custom range must satisfy 0 <= fromMs < toMs")
    if to_ms > as_of_ms:
        to_ms = as_of_ms
    if from_ms >= to_ms:
        raise UsageQueryValidationError("custom range starts after the current query snapshot")
    return ResolvedUsageRange(
        preset=None,
        timezone=timezone_name,
        from_ms=from_ms,
        to_ms=to_ms,
        as_of_ms=as_of_ms,
        zone=zone,
    )


_TOKEN_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("inputTokens", ("input_tokens", "inputTokens")),
    ("outputTokens", ("output_tokens", "outputTokens")),
    ("reasoningTokens", ("reasoning_tokens", "reasoningTokens")),
    ("cacheReadTokens", ("cache_read_tokens", "cached_tokens", "cacheReadTokens")),
    ("cacheWriteTokens", ("cache_write_tokens", "cacheWriteTokens")),
)


def _new_totals() -> dict[str, Any]:
    return {
        "inputTokens": 0,
        "outputTokens": 0,
        "reasoningTokens": 0,
        "cacheReadTokens": 0,
        "cacheWriteTokens": 0,
        "totalTokens": 0,
        "costNanos": 0,
        "costUsd": 0.0,
        "billedCostNanos": 0,
        "billedCostUsd": 0.0,
        "estimatedCostNanos": 0,
        "estimatedCostUsd": 0.0,
        "missingCostEntries": 0,
        "eventCount": 0,
        "sessionCount": 0,
        "estimatedEventCount": 0,
        "nativeBilledByCurrency": {},
        "pendingBillingReceiptCount": 0,
        "nativeBillingExpectedReceiptCount": 0,
        "nativeBillingMissingConfirmedReceiptCount": 0,
        "costSource": "none",
        "costSourceCounts": {
            "provider_billed": 0,
            "opensquilla_estimate": 0,
            "mixed": 0,
            "unavailable": 0,
            "none": 0,
        },
    }


def _nanos_to_usd(value: int) -> float:
    return round(value / 1_000_000_000, 9)


def _nanos_decimal(value: int) -> str:
    decimal_value = Decimal(max(0, value)) / Decimal(1_000_000_000)
    rendered = format(decimal_value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _add_billing_receipt(totals: dict[str, Any], receipt: Any) -> None:
    status = _text(_first(receipt, "status"), "")
    if status == "pending":
        totals["pendingBillingReceiptCount"] += 1
        return
    if status != "confirmed":
        return
    currency = _text(_first(receipt, "currency"), "").upper()
    if len(currency) != 3:
        return
    amount_nanos = max(0, _integer(_first(receipt, "amount_nanos", "amountNanos")))
    usd_nanos = max(
        0,
        _integer(
            _first(
                receipt,
                "usd_equivalent_nanos",
                "usdEquivalentNanos",
                "usd_cost_nanos",
            )
        ),
    )
    fx_nanos = max(
        0,
        _integer(
            _first(
                receipt,
                "fx_native_per_usd_nanos",
                "fxNativePerUsdNanos",
            )
        ),
    )
    native = totals["nativeBilledByCurrency"].setdefault(
        currency,
        {
            "amountNanos": 0,
            "usdEquivalentNanos": 0,
            "receiptCount": 0,
            "normalizationRatesNativePerUsd": [],
        },
    )
    native["amountNanos"] = _integer(native.get("amountNanos")) + amount_nanos
    native["usdEquivalentNanos"] = (
        _integer(native.get("usdEquivalentNanos")) + usd_nanos
    )
    native["receiptCount"] = _integer(native.get("receiptCount")) + 1
    if fx_nanos:
        fx = _nanos_decimal(fx_nanos)
        rates = native["normalizationRatesNativePerUsd"]
        if fx not in rates:
            rates.append(fx)


def _add_native_receipt_expectation(
    totals: dict[str, Any],
    source: Any,
    receipt: Any | None,
) -> None:
    """Track physical native-receipt coverage for one usage item."""

    provider = _text(
        _first(source, "provider", "provider_id", "providerId"),
        "",
    ).strip().lower()
    cost_source = _text(_first(source, "cost_source", "costSource"), "none")
    expects_receipt = provider == "tokenrhythm" or (
        provider == "openrouter" and cost_source == "provider_billed"
    )
    if not expects_receipt:
        return
    totals["nativeBillingExpectedReceiptCount"] += 1
    receipt_status = _text(_first(receipt, "status")) if receipt is not None else ""
    if receipt_status not in {"confirmed", "pending"}:
        totals["nativeBillingMissingConfirmedReceiptCount"] += 1


def _merge_native_billing(
    target: dict[str, Any],
    source: Mapping[str, Any],
    *,
    subtract: bool = False,
) -> None:
    direction = -1 if subtract else 1
    source_native = source.get("nativeBilledByCurrency", {})
    if isinstance(source_native, Mapping):
        for currency, raw in source_native.items():
            if not isinstance(raw, Mapping):
                continue
            entry = target["nativeBilledByCurrency"].setdefault(
                str(currency),
                {
                    "amountNanos": 0,
                    "usdEquivalentNanos": 0,
                    "receiptCount": 0,
                    "normalizationRatesNativePerUsd": [],
                },
            )
            for key in ("amountNanos", "usdEquivalentNanos", "receiptCount"):
                entry[key] = max(
                    0,
                    _integer(entry.get(key)) + direction * _integer(raw.get(key)),
                )
            rates = raw.get("normalizationRatesNativePerUsd", [])
            if not subtract and isinstance(rates, Sequence) and not isinstance(rates, str):
                for rate in rates:
                    normalized = _text(rate)
                    if normalized and normalized not in entry["normalizationRatesNativePerUsd"]:
                        entry["normalizationRatesNativePerUsd"].append(normalized)
    target["pendingBillingReceiptCount"] = max(
        0,
        _integer(target.get("pendingBillingReceiptCount"))
        + direction * _integer(source.get("pendingBillingReceiptCount")),
    )
    for key in (
        "nativeBillingExpectedReceiptCount",
        "nativeBillingMissingConfirmedReceiptCount",
    ):
        target[key] = max(
            0,
            _integer(target.get(key)) + direction * _integer(source.get(key)),
        )


def _cost_nanos(source: Any, prefix: str = "") -> int:
    names: tuple[str, ...]
    if prefix == "billed":
        names = ("billed_cost_nanos", "billedCostNanos")
    elif prefix == "estimated":
        names = ("estimated_cost_nanos", "estimatedCostNanos")
    else:
        names = ("cost_nanos", "costNanos", "total_cost_nanos")
    return max(0, _integer(_first(source, *names, default=0)))


def _add_record(
    totals: dict[str, Any],
    source: Any,
    *,
    count_event: bool = True,
) -> None:
    for wire_name, source_names in _TOKEN_FIELDS:
        totals[wire_name] += max(0, _integer(_first(source, *source_names, default=0)))
    totals["costNanos"] += _cost_nanos(source)
    totals["billedCostNanos"] += _cost_nanos(source, "billed")
    totals["estimatedCostNanos"] += _cost_nanos(source, "estimated")
    cost_source = _text(_first(source, "cost_source", "costSource"), "none")
    missing = max(
        0,
        _integer(_first(source, "missing_cost_entries", "missingCostEntries", default=0)),
    )
    status = _text(_first(source, "status", "state"), "finalized")
    if status in {"unknown", "started"} and missing == 0:
        missing = 1
    # Item rows intentionally avoid duplicating envelope coverage columns.
    # Their stable ``unavailable`` source is still enough to attribute one
    # missing price to the correct model instead of rendering it as cost-free.
    if cost_source == "unavailable" and missing == 0:
        missing = 1
    totals["missingCostEntries"] += missing
    if count_event:
        totals["eventCount"] += 1
    if cost_source not in totals["costSourceCounts"]:
        cost_source = "unavailable" if missing else "none"
    totals["costSourceCounts"][cost_source] += 1
    if cost_source in {"opensquilla_estimate", "mixed"}:
        totals["estimatedEventCount"] += 1


def _finish_totals(totals: dict[str, Any], sessions: set[str] | None = None) -> dict[str, Any]:
    totals = {
        **totals,
        "costSourceCounts": dict(totals["costSourceCounts"]),
    }
    totals["totalTokens"] = totals["inputTokens"] + totals["outputTokens"]
    component_cost = totals["billedCostNanos"] + totals["estimatedCostNanos"]
    if totals["costNanos"] != component_cost:
        totals["costNanos"] = component_cost
        totals["missingCostEntries"] += 1
    totals["costUsd"] = _nanos_to_usd(totals["costNanos"])
    totals["billedCostUsd"] = _nanos_to_usd(totals["billedCostNanos"])
    totals["estimatedCostUsd"] = _nanos_to_usd(totals["estimatedCostNanos"])
    native_payload: dict[str, Any] = {}
    native_source = totals.get("nativeBilledByCurrency", {})
    if isinstance(native_source, Mapping):
        for currency, raw in native_source.items():
            if not isinstance(raw, Mapping):
                continue
            amount_nanos = max(0, _integer(raw.get("amountNanos")))
            usd_nanos = max(0, _integer(raw.get("usdEquivalentNanos")))
            rates = raw.get("normalizationRatesNativePerUsd", [])
            native_payload[str(currency)] = {
                "amountNanos": str(amount_nanos),
                "amount": _nanos_decimal(amount_nanos),
                "usdEquivalentNanos": str(usd_nanos),
                "receiptCount": max(0, _integer(raw.get("receiptCount"))),
                "normalizationRatesNativePerUsd": sorted(
                    {
                        _text(rate)
                        for rate in rates
                        if _text(rate)
                    }
                )
                if isinstance(rates, Sequence) and not isinstance(rates, str)
                else [],
            }
    totals["nativeBilledByCurrency"] = native_payload
    totals["pendingBillingReceiptCount"] = max(
        0, _integer(totals.get("pendingBillingReceiptCount"))
    )
    totals["nativeBillingExpectedReceiptCount"] = max(
        0, _integer(totals.get("nativeBillingExpectedReceiptCount"))
    )
    totals["nativeBillingMissingConfirmedReceiptCount"] = max(
        0, _integer(totals.get("nativeBillingMissingConfirmedReceiptCount"))
    )
    if sessions is not None:
        totals["sessionCount"] = len(sessions)
    totals["costSource"] = rollup_cost_source(
        billed_cost_usd=totals["billedCostUsd"],
        estimated_cost_component_usd=totals["estimatedCostUsd"],
        missing_cost_entries=totals["missingCostEntries"],
        provider_billed_entries=_integer(
            totals["costSourceCounts"].get("provider_billed")
        )
        + _integer(totals["costSourceCounts"].get("mixed")),
        estimated_cost_entries=(
            _integer(totals["costSourceCounts"].get("opensquilla_estimate"))
            + _integer(totals["costSourceCounts"].get("mixed"))
        ),
    )
    return totals


def _sum_records(records: Sequence[Any]) -> tuple[dict[str, Any], set[str]]:
    totals = _new_totals()
    sessions: set[str] = set()
    for record in records:
        _add_record(totals, record)
        session_id = _text(_first(record, "session_id", "sessionId"))
        if session_id:
            sessions.add(session_id)
    return _finish_totals(totals, sessions), sessions


def _event_time_ms(event: Any) -> int | None:
    value = _first(
        event,
        "occurred_at_ms",
        "completed_at_ms",
        "occurredAtMs",
        "completedAtMs",
    )
    if value is None:
        return None
    parsed = _integer(value, -1)
    return parsed if parsed >= 0 else None


def _event_origin(event: Any) -> str:
    return _text(_first(event, "origin", "event_source", "source"), "live")


def _is_live_event(event: Any) -> bool:
    return _event_origin(event) in {"live", "live_provider"}


def _bucket_bounds(day: date, zone: ZoneInfo, *, cap_ms: int) -> tuple[int, int]:
    start = datetime.combine(day, datetime_time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), datetime_time.min, tzinfo=zone)
    return _millis(start), min(_millis(end), cap_ms)


def _daily_rows(
    events: Sequence[Any],
    resolved: ResolvedUsageRange,
    items_by_event: Mapping[str, Sequence[Any]],
    receipts_by_event: Mapping[str, Sequence[Any]],
    receipts_by_item: Mapping[tuple[str, int], Any],
) -> list[dict[str, Any]]:
    grouped: dict[date, list[Any]] = defaultdict(list)
    for event in events:
        occurred = _event_time_ms(event)
        if occurred is None:
            continue
        local_day = datetime.fromtimestamp(occurred / 1000, tz=resolved.zone).date()
        grouped[local_day].append(event)

    if resolved.from_ms is not None:
        start_day = datetime.fromtimestamp(resolved.from_ms / 1000, tz=resolved.zone).date()
        end_day = datetime.fromtimestamp((resolved.to_ms - 1) / 1000, tz=resolved.zone).date()
        days: list[date] = []
        cursor = start_day
        while cursor <= end_day:
            days.append(cursor)
            cursor += timedelta(days=1)
    else:
        days = sorted(grouped)

    rows: list[dict[str, Any]] = []
    for day in days:
        day_events = grouped.get(day, [])
        totals, _ = _sum_records(day_events)
        for event in day_events:
            event_id = _text(_first(event, "event_id", "eventId"))
            for receipt in receipts_by_event.get(event_id, ()):
                _add_billing_receipt(totals, receipt)
            physical_sources = items_by_event.get(event_id) or (event,)
            for source in physical_sources:
                ordinal = _integer(_first(source, "ordinal"), -1)
                receipt = receipts_by_item.get((event_id, ordinal)) if ordinal >= 0 else None
                _add_native_receipt_expectation(totals, source, receipt)
        totals = _finish_totals(totals)
        from_ms, to_ms = _bucket_bounds(day, resolved.zone, cap_ms=resolved.to_ms)
        if resolved.from_ms is not None:
            from_ms = max(from_ms, resolved.from_ms)
        rows.append(
            {
                "date": day.isoformat(),
                "fromMs": from_ms,
                "toMs": to_ms,
                "totals": totals,
            }
        )
    return rows


def _group_sessions(
    events: Sequence[Any],
    items_by_event: Mapping[str, Sequence[Any]],
    receipts_by_event: Mapping[str, Sequence[Any]],
    receipts_by_item: Mapping[tuple[str, int], Any],
    session_keys: Mapping[str, str],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for event in events:
        session_id = _text(_first(event, "session_id", "sessionId"), "unknown")
        grouped[session_id].append(event)
    rows: list[dict[str, Any]] = []
    for session_id, session_events in grouped.items():
        totals, _ = _sum_records(session_events)
        for event in session_events:
            event_id = _text(_first(event, "event_id", "eventId"))
            for receipt in receipts_by_event.get(event_id, ()):
                _add_billing_receipt(totals, receipt)
            physical_sources = items_by_event.get(event_id) or (event,)
            for source in physical_sources:
                ordinal = _integer(_first(source, "ordinal"), -1)
                receipt = receipts_by_item.get((event_id, ordinal)) if ordinal >= 0 else None
                _add_native_receipt_expectation(totals, source, receipt)
        totals = _finish_totals(totals)
        timestamps = [ts for event in session_events if (ts := _event_time_ms(event)) is not None]
        rows.append(
            {
                "sessionId": session_id,
                "sessionKey": session_keys.get(session_id),
                "firstUsageAtMs": min(timestamps) if timestamps else None,
                "lastUsageAtMs": max(timestamps) if timestamps else None,
                "totals": totals,
                "modelBreakdown": _group_models(
                    session_events,
                    items_by_event,
                    receipts_by_item,
                ),
            }
        )
    return sorted(rows, key=lambda row: row["lastUsageAtMs"] or 0, reverse=True)


def _index_items_by_event(
    events: Sequence[Any],
    items: Sequence[Any],
) -> dict[str, list[Any]]:
    """Index item rows once for all model and session aggregations.

    An event belongs to exactly one session, so reusing this index makes the
    combined model/session work linear in the number of events and items.  It
    also filters defensive third-party storage results to the queried event
    snapshot before any aggregation occurs.
    """

    event_ids = {
        event_id
        for event in events
        if (event_id := _text(_first(event, "event_id", "eventId")))
    }
    items_by_event: dict[str, list[Any]] = defaultdict(list)
    for item in items:
        event_id = _text(_first(item, "event_id", "eventId"))
        if event_id in event_ids:
            items_by_event[event_id].append(item)
    return dict(items_by_event)


def _index_receipts(
    events: Sequence[Any],
    receipts: Sequence[Any],
) -> tuple[dict[str, list[Any]], dict[tuple[str, int], Any]]:
    event_ids = {
        event_id
        for event in events
        if (event_id := _text(_first(event, "event_id", "eventId")))
    }
    by_event: dict[str, list[Any]] = defaultdict(list)
    by_item: dict[tuple[str, int], Any] = {}
    for receipt in receipts:
        event_id = _text(_first(receipt, "event_id", "eventId"))
        ordinal = _integer(_first(receipt, "ordinal"), -1)
        if event_id not in event_ids or ordinal < 0:
            continue
        by_event[event_id].append(receipt)
        by_item[(event_id, ordinal)] = receipt
    return dict(by_event), by_item


def _group_models(
    events: Sequence[Any],
    items_by_event: Mapping[str, Sequence[Any]],
    receipts_by_item: Mapping[tuple[str, int], Any],
) -> list[dict[str, Any]]:
    event_by_id = {
        _text(_first(event, "event_id", "eventId")): event
        for event in events
        if _text(_first(event, "event_id", "eventId"))
    }

    grouped: dict[tuple[str, str], list[tuple[Any, str, Any | None]]] = defaultdict(list)
    for event_id, event in event_by_id.items():
        event_items = items_by_event.get(event_id)
        if not event_items:
            event_items = [event]
        session_id = _text(_first(event, "session_id", "sessionId"))
        for item in event_items:
            provider = _text(_first(item, "provider", "provider_id", "providerId"))
            model = _text(_first(item, "model", "model_id", "modelId"), "unknown")
            ordinal = _integer(_first(item, "ordinal"), -1)
            receipt = receipts_by_item.get((event_id, ordinal)) if ordinal >= 0 else None
            grouped[(provider, model)].append((item, session_id, receipt))

    rows: list[dict[str, Any]] = []
    for (provider, model), values in grouped.items():
        totals = _new_totals()
        sessions: set[str] = set()
        for item, session_id, receipt in values:
            _add_record(totals, item)
            if receipt is not None:
                _add_billing_receipt(totals, receipt)
            _add_native_receipt_expectation(totals, item, receipt)
            if session_id:
                sessions.add(session_id)
        rows.append(
            {
                "provider": provider,
                "model": model,
                "totals": _finish_totals(totals, sessions),
                "eventCount": len(values),
                "sessionCount": len(sessions),
            }
        )
    return sorted(rows, key=lambda row: row["totals"]["costNanos"], reverse=True)


def _baseline_records(baselines: Sequence[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for baseline in baselines:
        rows.append(
            {
                "input_tokens": _first(baseline, "input_tokens", "inputTokens", default=0),
                "output_tokens": _first(baseline, "output_tokens", "outputTokens", default=0),
                "cache_read_tokens": _first(
                    baseline, "cache_read_tokens", "cacheReadTokens", default=0
                ),
                "cache_write_tokens": _first(
                    baseline, "cache_write_tokens", "cacheWriteTokens", default=0
                ),
                "cost_nanos": _first(baseline, "cost_nanos", "costNanos", default=0),
                "billed_cost_nanos": _first(
                    baseline, "billed_cost_nanos", "billedCostNanos", default=0
                ),
                "estimated_cost_nanos": _first(
                    baseline, "estimated_cost_nanos", "estimatedCostNanos", default=0
                ),
                "missing_cost_entries": _first(
                    baseline, "missing_cost_entries", "missingCostEntries", default=0
                ),
                "cost_source": _first(baseline, "cost_source", "costSource", default="none"),
                "session_id": _first(baseline, "session_id", "sessionId", default=""),
                "session_epoch": _first(
                    baseline, "session_epoch", "sessionEpoch", default=0
                ),
            }
        )
    return rows


# V021 baselines contain no reasoning-token column.  Backfilled reasoning
# tokens are useful attribution, but cannot be compared to an absent legacy
# fact as though the baseline had asserted zero.
_BASELINE_RECONCILED_COMPONENTS = (
    "inputTokens",
    "outputTokens",
    "cacheReadTokens",
    "cacheWriteTokens",
    "costNanos",
    "billedCostNanos",
    "estimatedCostNanos",
    "missingCostEntries",
)


def _session_epoch_key(source: Any) -> tuple[str, int]:
    return (
        _text(_first(source, "session_id", "sessionId")),
        max(0, _integer(_first(source, "session_epoch", "sessionEpoch", default=0))),
    )


def _group_epoch_totals(records: Sequence[Any]) -> dict[tuple[str, int], dict[str, Any]]:
    grouped: dict[tuple[str, int], list[Any]] = defaultdict(list)
    for record in records:
        grouped[_session_epoch_key(record)].append(record)
    return {key: _sum_records(values)[0] for key, values in grouped.items()}


def _cost_components_are_consistent(totals: Mapping[str, Any]) -> bool:
    return _integer(totals.get("costNanos")) == (
        _integer(totals.get("billedCostNanos"))
        + _integer(totals.get("estimatedCostNanos"))
    )


def _reconcile_historical_events(
    baselines: Sequence[Any],
    historical: Sequence[Any],
) -> tuple[
    list[Any],
    dict[str, Any],
    dict[str, Any],
    set[tuple[str, int]],
    int,
]:
    """Trust history only when every known component fits its session epoch."""

    baseline_records = _baseline_records(baselines)
    baseline_by_epoch = _group_epoch_totals(baseline_records)
    historical_by_epoch = _group_epoch_totals(historical)
    events_by_epoch: dict[tuple[str, int], list[Any]] = defaultdict(list)
    for event in historical:
        events_by_epoch[_session_epoch_key(event)].append(event)

    trusted: list[Any] = []
    trusted_totals_by_epoch: dict[tuple[str, int], dict[str, Any]] = {}
    conflict_count = 0
    for key, history_totals in historical_by_epoch.items():
        baseline_totals = baseline_by_epoch.get(key)
        conflict = baseline_totals is None or any(
            not _text(_first(event, "event_id", "eventId"))
            for event in events_by_epoch[key]
        )
        if baseline_totals is not None:
            conflict = (
                not _cost_components_are_consistent(baseline_totals)
                or not _cost_components_are_consistent(history_totals)
                or any(
                    _integer(history_totals.get(field))
                    > _integer(baseline_totals.get(field))
                    for field in _BASELINE_RECONCILED_COMPONENTS
                )
            )
        if conflict:
            conflict_count += 1
            continue
        trusted.extend(events_by_epoch[key])
        trusted_totals_by_epoch[key] = history_totals

    residual = _finish_totals(_new_totals())
    residual_session_epochs: set[tuple[str, int]] = set()
    for key, baseline_totals in baseline_by_epoch.items():
        attributed = trusted_totals_by_epoch.get(key, _finish_totals(_new_totals()))
        epoch_residual = _subtract_totals(baseline_totals, attributed)
        residual = _add_totals(residual, epoch_residual)
        if key[0] and _has_positive_accounting(epoch_residual):
            residual_session_epochs.add(key)
    residual["sessionCount"] = len(residual_session_epochs)
    baseline_totals, _ = _sum_records(baseline_records)
    return (
        trusted,
        residual,
        baseline_totals,
        residual_session_epochs,
        conflict_count,
    )


def _subtract_totals(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    result = _new_totals()
    numeric_fields = (
        "inputTokens",
        "outputTokens",
        "reasoningTokens",
        "cacheReadTokens",
        "cacheWriteTokens",
        "costNanos",
        "billedCostNanos",
        "estimatedCostNanos",
        "missingCostEntries",
    )
    for field in numeric_fields:
        result[field] = max(0, _integer(left.get(field)) - _integer(right.get(field)))
    _merge_native_billing(result, left)
    _merge_native_billing(result, right, subtract=True)
    return _finish_totals(result)


def _add_totals(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    result = _new_totals()
    numeric_fields = (
        "inputTokens",
        "outputTokens",
        "reasoningTokens",
        "cacheReadTokens",
        "cacheWriteTokens",
        "costNanos",
        "billedCostNanos",
        "estimatedCostNanos",
        "missingCostEntries",
        "eventCount",
        "estimatedEventCount",
    )
    for field in numeric_fields:
        result[field] = _integer(left.get(field)) + _integer(right.get(field))
    result["sessionCount"] = max(
        _integer(left.get("sessionCount")), _integer(right.get("sessionCount"))
    )
    left_counts = left.get("costSourceCounts", {})
    right_counts = right.get("costSourceCounts", {})
    if isinstance(left_counts, Mapping) and isinstance(right_counts, Mapping):
        for source in result["costSourceCounts"]:
            result["costSourceCounts"][source] = _integer(
                left_counts.get(source)
            ) + _integer(right_counts.get(source))
    _merge_native_billing(result, left)
    _merge_native_billing(result, right)
    return _finish_totals(result)


def _has_positive_accounting(totals: Mapping[str, Any]) -> bool:
    return any(
        _integer(totals.get(field)) > 0
        for field in (
            "inputTokens",
            "outputTokens",
            "reasoningTokens",
            "cacheReadTokens",
            "cacheWriteTokens",
            "costNanos",
            "missingCostEntries",
        )
    )


async def query_usage_ledger(
    storage: Any,
    params: Mapping[str, Any] | None,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Execute one consistent usage query against a SessionStorage ledger API."""

    resolved = resolve_usage_range(params, now_ms=now_ms)
    include = params.get("include", {}) if isinstance(params, Mapping) else {}
    if not isinstance(include, Mapping):
        raise UsageQueryValidationError("include must be an object")

    state = await storage.get_usage_ledger_state()
    if state is None:
        state = await storage.initialize_usage_ledger(now_ms=resolved.as_of_ms)
    ledger_started_at = _integer(
        _first(state, "ledger_started_at_ms", "ledgerStartedAtMs", default=resolved.as_of_ms)
    )

    events = await storage.query_usage_events(
        resolved.from_ms,
        resolved.to_ms,
        statuses=("finalized", "unknown"),
    )
    # Partition by status in one pass.  Equality-based list membership here is
    # quadratic for the common all-finalized case and becomes visible at only
    # a few thousand ledger rows.
    finalized: list[Any] = []
    unknown: list[Any] = []
    for event in events:
        if _text(_first(event, "status", "state"), "finalized") == "finalized":
            finalized.append(event)
        else:
            unknown.append(event)
    attributed_events = [*finalized, *unknown]

    legacy_unattributed = _finish_totals(_new_totals())
    include_legacy = resolved.preset == "all"
    baseline_totals = _finish_totals(_new_totals())
    residual_session_epochs: set[tuple[str, int]] = set()
    historical_component_conflicts = 0
    crosses_cutover = resolved.from_ms is None or resolved.from_ms < ledger_started_at
    if crosses_cutover:
        baselines = await storage.list_usage_legacy_baselines()
        if include_legacy:
            historical_candidates = [
                event for event in finalized if not _is_live_event(event)
            ]
        else:
            complete_history = await storage.query_usage_events(
                None,
                ledger_started_at,
                statuses=("finalized",),
            )
            historical_candidates = [
                event for event in complete_history if not _is_live_event(event)
            ]
        (
            trusted_historical,
            reconciled_residual,
            baseline_totals,
            residual_session_epochs,
            historical_component_conflicts,
        ) = _reconcile_historical_events(baselines, historical_candidates)
        trusted_ids = {
            event_id
            for event in trusted_historical
            if (event_id := _text(_first(event, "event_id", "eventId")))
        }
        attributed_events = [
            event
            for event in attributed_events
            if _is_live_event(event)
            or _text(_first(event, "event_id", "eventId")) in trusted_ids
        ]
        finalized = [
            event
            for event in finalized
            if _is_live_event(event)
            or _text(_first(event, "event_id", "eventId")) in trusted_ids
        ]
        unknown = [event for event in unknown if _is_live_event(event)]
        legacy_unattributed = reconciled_residual

    attributed_totals, _ = _sum_records(attributed_events)
    attributed_session_epochs = {
        key
        for event in attributed_events
        if (key := _session_epoch_key(event))[0]
    }
    totals = (
        _add_totals(attributed_totals, legacy_unattributed)
        if include_legacy
        else dict(attributed_totals)
    )
    if include_legacy:
        totals["sessionCount"] = len(
            attributed_session_epochs | residual_session_epochs
        )
    if not _cost_components_are_consistent(totals):
        # Storage constraints and reconciliation should make this unreachable;
        # preserve the component facts and the public accounting invariant if a
        # third-party storage adapter supplies inconsistent data.
        totals["costNanos"] = (
            _integer(totals.get("billedCostNanos"))
            + _integer(totals.get("estimatedCostNanos"))
        )
        totals = _finish_totals(totals)

    event_ids = [
        event_id
        for event in attributed_events
        if (event_id := _text(_first(event, "event_id", "eventId")))
    ]
    items = await storage.query_usage_event_items(event_ids) if event_ids else []
    items_by_event = _index_items_by_event(attributed_events, items)
    query_receipts = getattr(storage, "query_usage_item_billing_receipts", None)
    receipts = (
        await query_receipts(event_ids)
        if event_ids and callable(query_receipts)
        else []
    )
    receipts_by_event, receipts_by_item = _index_receipts(
        attributed_events,
        receipts,
    )
    for receipt in receipts:
        _add_billing_receipt(attributed_totals, receipt)
    for event in attributed_events:
        event_id = _text(_first(event, "event_id", "eventId"))
        physical_sources = items_by_event.get(event_id) or (event,)
        for source in physical_sources:
            ordinal = _integer(_first(source, "ordinal"), -1)
            receipt = receipts_by_item.get((event_id, ordinal)) if ordinal >= 0 else None
            _add_native_receipt_expectation(attributed_totals, source, receipt)
    attributed_totals = _finish_totals(attributed_totals)
    totals = (
        _add_totals(attributed_totals, legacy_unattributed)
        if include_legacy
        else dict(attributed_totals)
    )
    if include_legacy:
        totals["sessionCount"] = len(
            attributed_session_epochs | residual_session_epochs
        )
    totals = _finish_totals(totals)

    session_ids = list(
        dict.fromkeys(
            session_id
            for event in attributed_events
            if (session_id := _text(_first(event, "session_id", "sessionId")))
        )
    )
    session_keys: dict[str, str] = {}
    resolve_session_keys = getattr(storage, "resolve_usage_session_keys", None)
    if callable(resolve_session_keys) and session_ids:
        try:
            session_keys = await resolve_session_keys(session_ids)
        except Exception:  # noqa: BLE001 - optional navigation metadata only.
            session_keys = {}

    backfill_status = _text(
        _first(state, "backfill_status", "backfillStatus"), "pending"
    )
    anomaly_count = max(
        0,
        _integer(_first(state, "anomaly_count", "anomalyCount", default=0)),
    )
    backfill_complete = backfill_status in {"complete", "completed"}
    all_historical_attributed = (
        backfill_complete
        and anomaly_count == 0
        and historical_component_conflicts == 0
        and not _has_positive_accounting(legacy_unattributed)
    )
    time_attribution_partial = crosses_cutover and not all_historical_attributed
    reasons: list[str] = []
    if time_attribution_partial:
        reasons.append("window_before_ledger")
    if not backfill_complete and crosses_cutover:
        reasons.append(f"backfill_{backfill_status}")
    if _has_positive_accounting(legacy_unattributed):
        reasons.append("legacy_unattributed")
    if attributed_totals["missingCostEntries"]:
        reasons.append("usage_unavailable")
    if anomaly_count:
        reasons.append("backfill_anomaly")
    if historical_component_conflicts:
        reasons.append("backfill_component_conflict")
        reasons.append("backfill_exceeds_baseline")
    status = "complete" if not reasons else "partial"

    get_receipt_state = getattr(storage, "get_usage_billing_receipt_state", None)
    receipt_state = await get_receipt_state() if callable(get_receipt_state) else None
    native_exact_from_ms = (
        _integer(
            _first(
                receipt_state,
                "tracking_started_at_ms",
                "trackingStartedAtMs",
                default=resolved.as_of_ms,
            )
        )
        if receipt_state is not None
        else None
    )
    pending_receipt_count = sum(
        1 for receipt in receipts if _text(_first(receipt, "status")) == "pending"
    )
    missing_confirmed_receipt_count = 0
    if native_exact_from_ms is not None:
        attributed_by_id = {
            _text(_first(event, "event_id", "eventId")): event
            for event in attributed_events
        }
        item_event_ids: set[str] = set()
        for item in items:
            event_id = _text(_first(item, "event_id", "eventId"))
            if event_id:
                item_event_ids.add(event_id)
            event = attributed_by_id.get(event_id)
            occurred_at = _event_time_ms(event) if event is not None else None
            if occurred_at is None or occurred_at < native_exact_from_ms:
                continue
            provider = _text(
                _first(item, "provider", "provider_id", "providerId"),
                "",
            ).strip().lower()
            if provider not in _NATIVE_RECEIPT_PROVIDER_IDS:
                continue
            source = _text(_first(item, "cost_source", "costSource"), "none")
            if provider == "openrouter" and source != "provider_billed":
                continue
            ordinal = _integer(_first(item, "ordinal"), -1)
            receipt = receipts_by_item.get((event_id, ordinal))
            receipt_status = _text(_first(receipt, "status")) if receipt is not None else ""
            # Pending receipts are disclosed separately and must not also be
            # reported as missing. Missing/invalid TokenRhythm receipts can
            # legitimately leave the item estimated or unavailable.
            if receipt_status not in {"confirmed", "pending"}:
                missing_confirmed_receipt_count += 1
        # A post-cutover finalized native-provider envelope without any
        # physical item cannot own a receipt because of the receipt table's
        # composite foreign key. Surface that upgrade/race anomaly instead of
        # silently claiming exact native coverage.
        for event_id, event in attributed_by_id.items():
            if event_id in item_event_ids:
                continue
            if _text(_first(event, "status", "state"), "finalized") != "finalized":
                continue
            occurred_at = _event_time_ms(event)
            if occurred_at is None or occurred_at < native_exact_from_ms:
                continue
            provider = _text(
                _first(event, "provider", "provider_id", "providerId"),
                "",
            ).strip().lower()
            source = _text(_first(event, "cost_source", "costSource"), "none")
            if provider == "tokenrhythm" or (
                provider == "openrouter" and source == "provider_billed"
            ):
                missing_confirmed_receipt_count += 1

    native_reason_codes: list[str] = []
    if native_exact_from_ms is None:
        native_reason_codes.append("native_billing_unavailable")
    elif resolved.from_ms is None or resolved.from_ms < native_exact_from_ms:
        native_reason_codes.append("window_before_native_billing_receipts")
    if missing_confirmed_receipt_count:
        native_reason_codes.append("missing_confirmed_billing_receipt")
    if pending_receipt_count:
        native_reason_codes.append("pending_billing_receipt")
    native_status = (
        "unavailable"
        if native_exact_from_ms is None
        else "complete"
        if not native_reason_codes
        else "partial"
    )

    payload: dict[str, Any] = {
        "schemaVersion": 1,
        "source": "usage_ledger",
        "asOfMs": resolved.as_of_ms,
        "range": resolved.payload(),
        # Additive: the canonical native-per-USD display rates the billing
        # adapters normalize receipts with.  Clients that render CNY from
        # canonical USD must use this instead of a hardcoded rate so their
        # figures agree with receipt-exact amounts; older clients ignore it.
        "fxRatesNativePerUsd": canonical_native_per_usd_rates(),
        "totals": totals,
        "attributedTotals": attributed_totals,
        "coverage": {
            "status": status,
            "timeAttribution": "partial" if time_attribution_partial else "complete",
            "pricing": (
                "partial" if totals["missingCostEntries"] else "complete"
            ),
            "exactFromMs": ledger_started_at,
            "backfill": backfill_status,
            "reasonCodes": reasons,
            "anomalyCount": anomaly_count + historical_component_conflicts,
            "legacyUnattributed": {
                "knownBeforeMs": ledger_started_at,
                "includedInTotals": include_legacy,
                "totals": legacy_unattributed,
            },
            "nativeBilling": {
                "status": native_status,
                "exactFromMs": native_exact_from_ms,
                "reasonCodes": native_reason_codes,
                "missingConfirmedReceiptCount": missing_confirmed_receipt_count,
                "pendingReceiptCount": pending_receipt_count,
            },
        },
        "legacyUnattributed": {
            "knownBeforeMs": ledger_started_at,
            "includedInTotals": include_legacy,
            "totals": legacy_unattributed,
        },
        "missingCostEntries": totals["missingCostEntries"],
        "eventCount": attributed_totals["eventCount"],
        "sessionCount": totals["sessionCount"],
    }
    if include.get("days", True):
        payload["days"] = _daily_rows(
            attributed_events,
            resolved,
            items_by_event,
            receipts_by_event,
            receipts_by_item,
        )
    else:
        payload["days"] = []
    if include.get("models", True):
        payload["models"] = _group_models(
            attributed_events,
            items_by_event,
            receipts_by_item,
        )
    else:
        payload["models"] = []
    if include.get("sessions", True):
        payload["sessions"] = _group_sessions(
            attributed_events,
            items_by_event,
            receipts_by_event,
            receipts_by_item,
            session_keys,
        )
    else:
        payload["sessions"] = []
    return payload


__all__ = [
    "ResolvedUsageRange",
    "UsageQueryValidationError",
    "query_usage_ledger",
    "resolve_usage_range",
]
