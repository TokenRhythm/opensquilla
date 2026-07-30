"""Usage domain RPC handlers — wired to session manager."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from opensquilla.gateway.rpc import (
    RpcContext,
    RpcHandlerError,
    RpcUnavailableError,
    get_dispatcher,
)
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.gateway.usage_query import UsageQueryValidationError, query_usage_ledger
from opensquilla.provider.model_catalog import (
    resolve_effective_context_window,
    shared_catalog,
)
from opensquilla.session.cost_rollup import rollup_cost_source
from opensquilla.session.tokenizer import estimate_tokens

_d = get_dispatcher()
_CONTEXT_WARNING_RATIO = 0.85


def _now_ms() -> int:
    return int(time.time() * 1000)


def _field(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _first_field(source: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        value = _field(source, name)
        if value is not None:
            return value
    return default


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _transcript_entry_tokens(entry: Any) -> int:
    token_count = _positive_int(_field(entry, "token_count"))
    if token_count is not None:
        return token_count
    total = 0
    for name in ("content", "reasoning_content"):
        value = _field(entry, name)
        if value:
            total += estimate_tokens(str(value))
    tool_calls = _field(entry, "tool_calls")
    if tool_calls:
        total += estimate_tokens(str(tool_calls))
    return total


# Shared-resolver sources -> this RPC's wire labels; "catalog"/"default"
# keep the per-catalog labels below (unchanged wire behavior).
_WINDOW_SOURCE_LABELS = {"override": "model_override", "config": "config"}


def _resolve_context_window(model: str | None, ctx: RpcContext) -> tuple[int | None, str]:
    config = ctx.config
    provider = str(getattr(getattr(config, "llm", None), "provider", "") or "")
    runtime_catalog = getattr(getattr(ctx, "turn_runner", None), "_model_catalog", None)
    global_window = None
    for owner in (config, getattr(config, "llm", None)):
        global_window = _positive_int(getattr(owner, "context_window_tokens", None))
        if global_window is not None:
            break
    if model:
        # One shared-resolver call per catalog, runtime first: the resolver
        # owns the "[models.*] override > global config window > catalog >
        # default" precedence (same rule as the engine budgeting path).
        for catalog, catalog_label in (
            (runtime_catalog, "runtime_model_catalog"),
            (shared_catalog(), "static_model_catalog"),
        ):
            if catalog is None or not hasattr(catalog, "resolve_context_window"):
                continue
            window, source = resolve_effective_context_window(
                catalog, model, provider=provider, global_override=global_window or 0
            )
            resolved = _positive_int(window)
            if resolved is None:
                continue
            return resolved, _WINDOW_SOURCE_LABELS.get(source, catalog_label)
    if global_window is not None:
        return global_window, "config"
    return None, "unavailable"


async def _context_status(
    source: Any,
    *,
    ctx: RpcContext,
    session_key: str,
    model: str | None,
    allow_transcript_estimate: bool = False,
) -> dict[str, Any] | None:
    persisted_context_tokens = _positive_int(
        _first_field(source, "context_tokens", "current_context_tokens")
    )
    compaction_count = _positive_int(_field(source, "compaction_count")) or 0
    context_tokens = persisted_context_tokens
    token_source = "session_context_tokens" if context_tokens is not None else "unavailable"
    should_estimate_transcript = (
        allow_transcript_estimate
        and ctx.session_manager is not None
        and (context_tokens is None or compaction_count > 0)
    )
    if should_estimate_transcript:
        get_transcript = getattr(ctx.session_manager, "get_transcript", None)
        if callable(get_transcript):
            try:
                transcript = await get_transcript(session_key)
            except (KeyError, AttributeError, NotImplementedError):
                transcript = None
            if transcript is not None:
                context_tokens = sum(_transcript_entry_tokens(entry) for entry in transcript)
                token_source = "transcript_estimate"
    if context_tokens is None:
        return None

    context_window, window_source = _resolve_context_window(model, ctx)
    if context_window is None:
        return None

    threshold = int(context_window * _CONTEXT_WARNING_RATIO)
    pressure = min(1.0, context_tokens / context_window) if context_window > 0 else 0.0
    return {
        "contextTokens": context_tokens,
        "contextWindowTokens": context_window,
        "thresholdTokens": threshold,
        "pressure": round(pressure, 6),
        "warningRatio": _CONTEXT_WARNING_RATIO,
        "compactionCount": compaction_count,
        "tokenSource": token_source,
        "windowSource": window_source,
        "context_tokens": context_tokens,
        "context_window_tokens": context_window,
        "threshold_tokens": threshold,
        "warning_ratio": _CONTEXT_WARNING_RATIO,
        "compaction_count": compaction_count,
        "token_source": token_source,
        "window_source": window_source,
    }


def _resolved_session_cost_fields(
    source: Any,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    ephemeral: bool = False,
) -> dict[str, Any]:
    legacy_total = _field(source, "estimated_cost_usd")
    total_cost = _field(source, "total_cost_usd")

    billed_cost = _field(source, "billed_cost_usd")
    if billed_cost is None:
        billed_cost = _field(source, "billed_cost", 0.0) or 0.0

    estimated_component = _field(source, "estimated_cost_component_usd")
    if estimated_component is None:
        source_name = _field(source, "cost_source")
        estimated_component = (
            float(total_cost or 0.0)
            if source_name in {None, "", "none", "opensquilla_estimate"}
            and not billed_cost
            else 0.0
        )

    missing_entries = _field(source, "missing_cost_entries", 0) or 0
    cost_source = _field(source, "cost_source")
    estimate_basis = _first_field(source, "estimate_basis", "estimateBasis")
    price_source = _first_field(source, "price_source", "priceSource")
    if total_cost is None:
        total_cost = legacy_total
    if (
        legacy_total
        and not billed_cost
        and not estimated_component
        and not missing_entries
        and cost_source in {None, "", "none", "opensquilla_estimate"}
    ):
        if not total_cost:
            total_cost = legacy_total
        estimated_component = legacy_total
    if total_cost is None:
        total_cost = 0.0

    if not cost_source or cost_source == "none":
        if billed_cost or estimated_component or missing_entries:
            cost_source = rollup_cost_source(
                billed_cost_usd=float(billed_cost or 0.0),
                estimated_cost_component_usd=float(estimated_component or 0.0),
                missing_cost_entries=int(missing_entries or 0),
            )
        elif input_tokens or output_tokens or cache_read_tokens or cache_write_tokens:
            cost_source = "unavailable"
        else:
            cost_source = "none"

    return {
        "cost_usd": float(total_cost or 0.0),
        "billed_cost_usd": float(billed_cost or 0.0),
        "estimated_cost_usd": float(estimated_component or 0.0),
        "cost_source": cost_source,
        "missing_cost_entries": int(missing_entries or 0),
        "cost_ephemeral": bool(ephemeral),
        # Additive provenance keys, sourced from the session record when
        # present; ``None`` when the persisted row predates them or never
        # carried per-model breakdown provenance. ``_usage_row`` fans these
        # snake_case values out to both key cases, matching the other cost
        # fields returned here.
        "estimate_basis": estimate_basis,
        "price_source": price_source,
    }


def _usage_row(
    *,
    session_key: str,
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
    billed_cost_usd: float = 0.0,
    estimated_cost_usd: float = 0.0,
    cost_source: str = "none",
    missing_cost_entries: int = 0,
    cost_ephemeral: bool = False,
    estimate_basis: str | None = None,
    price_source: str | None = None,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    created_at: int | None = None,
    updated_at: int | None = None,
    started_at: int | None = None,
    ended_at: int | None = None,
    context_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cost = round(cost_usd, 6)
    billed_cost = round(billed_cost_usd, 6)
    estimated_cost = round(estimated_cost_usd, 6)
    return {
        # Canonical keys used by newer RPC consumers.
        "sessionKey": session_key,
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "costUsd": cost,
        "billedCostUsd": billed_cost,
        "estimatedCostUsd": estimated_cost,
        "costSource": cost_source,
        "missingCostEntries": missing_cost_entries,
        "costEphemeral": cost_ephemeral,
        # Additive provenance keys — None when the source has no per-model
        # breakdown to derive them from (see _resolved_session_cost_fields).
        "estimateBasis": estimate_basis,
        "priceSource": price_source,
        "cacheReadTokens": cache_read_tokens,
        "cacheWriteTokens": cache_write_tokens,
        "createdAt": created_at,
        "updatedAt": updated_at,
        "startedAt": started_at,
        "endedAt": ended_at,
        "model": model,
        "contextStatus": context_status,
        # Compatibility aliases used by the shipped web UI.
        "session": session_key,
        "key": session_key,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost,
        "billed_cost_usd": billed_cost,
        "estimated_cost_usd": estimated_cost,
        "cost_source": cost_source,
        "missing_cost_entries": missing_cost_entries,
        "cost_ephemeral": cost_ephemeral,
        "estimate_basis": estimate_basis,
        "price_source": price_source,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "created_at": created_at,
        "updated_at": updated_at,
        "started_at": started_at,
        "ended_at": ended_at,
        "context_status": context_status,
    }


def _tracker_rows(ctx: RpcContext, *, now_ms: int) -> list[dict[str, Any]]:
    if ctx.usage_tracker is None:
        return []
    all_sessions = ctx.usage_tracker.all_sessions()
    if not all_sessions:
        return []

    config_model = getattr(ctx.config, "llm", None) and ctx.config.llm.model or None
    rows = []
    for session_key, usage in all_sessions.items():
        cost_fields = _resolved_session_cost_fields(
            usage,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=getattr(usage, "cache_read_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_write_tokens", 0) or 0,
            ephemeral=True,
        )
        # Read aggregate billed/cost_source from SessionUsage so the row
        # matches the per-model breakdown items. Without this, a
        # tracker-only row would always show cost_source=opensquilla_estimate
        # while individual breakdown chips could be provider_billed — a
        # visible self-contradiction in the UI ("row says estimated but
        # every model says actual").
        usage_billed = float(getattr(usage, "billed_cost", 0.0) or 0.0)
        usage_estimate = float(usage.cost or 0.0)
        # ``total_cost`` mixes per-model billed (where available) with
        # estimates (where not), so a "mixed" session row matches the
        # breakdown sum instead of under-reporting the unbilled portion.
        usage_total = float(getattr(usage, "total_cost", usage_estimate) or 0.0)
        usage_cost_source = str(
            getattr(usage, "cost_source", "opensquilla_estimate") or "opensquilla_estimate"
        )
        provider_billed_entries = max(
            0,
            int(getattr(usage, "provider_billed_entries", 0) or 0),
        )
        has_billed_receipt = (
            provider_billed_entries > 0
            or usage_cost_source in {"provider_billed", "mixed"}
        )
        if has_billed_receipt:
            # Real billed available — surface the mixed total (billed +
            # estimate-fallback for any unbilled model) as the row's
            # canonical cost so it matches the breakdown sum exactly.
            cost_fields["cost_usd"] = usage_total
            cost_fields["billed_cost_usd"] = usage_billed
            # The estimated component is the UNBILLED remainder, not the full
            # list-price estimate — otherwise cost_usd != billed + estimated and
            # fully-billed rows falsely show a large "estimated" figure.
            cost_fields["estimated_cost_usd"] = max(0.0, usage_total - usage_billed)
            cost_fields["cost_source"] = usage_cost_source  # provider_billed or mixed
        else:
            cost_fields["cost_usd"] = usage_estimate
            cost_fields["estimated_cost_usd"] = usage_estimate
            cost_fields["cost_source"] = "opensquilla_estimate"
        row = _usage_row(
            session_key=session_key,
            model=usage.model_id or config_model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            **cost_fields,
            cache_read_tokens=getattr(usage, "cache_read_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_write_tokens", 0) or 0,
            created_at=now_ms,
            updated_at=now_ms,
        )
        row["modelBreakdown"] = getattr(usage, "model_breakdown", [])
        row["deploymentBreakdown"] = getattr(usage, "deployment_breakdown", [])
        rows.append(row)
    return rows


_BILLED_COST_SOURCES = frozenset({"provider_billed", "mixed"})


def _row_has_usage(row: Mapping[str, Any]) -> bool:
    return any(
        float(row.get(name) or 0.0) > 0.0
        for name in (
            "input_tokens",
            "output_tokens",
            "cost_usd",
            "billed_cost_usd",
            "estimated_cost_usd",
            "cache_read_tokens",
            "cache_write_tokens",
        )
    )


def _row_can_overlay_tracker_totals(row: Mapping[str, Any], tracker_row: Mapping[str, Any]) -> bool:
    """Allow tracker totals to cover the tiny done→persistence status race.

    ``usage.status`` normally prefers persisted rows because they are durable.
    During a just-finished turn, however, the in-memory tracker can already
    contain the final provider-billed totals while the session row is still at
    its zero defaults. In that narrow window, surfacing the tracker row avoids a
    visibly wrong "0 tokens / $0" status without overriding real persisted
    totals once they exist.
    """

    source = str(row.get("cost_source") or row.get("costSource") or "none")
    return source in {"none", "unavailable"} and not _row_has_usage(row) and _row_has_usage(
        tracker_row
    )


def _overlay_tracker_totals(row: dict[str, Any], tracker_row: Mapping[str, Any]) -> None:
    for snake, camel in (
        ("input_tokens", "inputTokens"),
        ("output_tokens", "outputTokens"),
        ("cost_usd", "costUsd"),
        ("billed_cost_usd", "billedCostUsd"),
        ("estimated_cost_usd", "estimatedCostUsd"),
        ("cost_source", "costSource"),
        ("missing_cost_entries", "missingCostEntries"),
        ("cache_read_tokens", "cacheReadTokens"),
        ("cache_write_tokens", "cacheWriteTokens"),
    ):
        value = tracker_row.get(snake, tracker_row.get(camel))
        row[snake] = value
        row[camel] = value
    row["cost_ephemeral"] = True
    row["costEphemeral"] = True
    if not row.get("model"):
        row["model"] = tracker_row.get("model")


def _reconcile_breakdown_to_row(row: dict[str, Any]) -> None:
    """Make per-model breakdown costs sum to the row's displayed total.

    **Fallback path**: when the in-memory tracker has captured real per-call
    ``billed_cost`` per model, the breakdown items already carry provider-billed
    figures and their sum already equals ``row.cost_usd`` by construction
    (since the row's ``billed_cost_usd`` and the per-model billed totals
    are accumulated from the same ``ProviderDoneEvent.billed_cost`` source
    values). In that case this function is a no-op — see
    the early-return guard below.

    The pro-rate path below remains for **disk-loaded sessions**: after a
    gateway restart, the in-memory ``UsageTracker._per_model`` is empty;
    only the aggregate ``billed_cost_usd`` survives on the persisted
    session row. If we still want to render *some* per-model breakdown
    (e.g. via tracker re-population from a future turn), and that
    breakdown happens to come back as estimate-only, the row total
    (billed) and the breakdown items (estimate) will drift due to the
    cache-blind pricing-table estimate (no cache_read pricing field in
    ``engine.pricing.ModelPrice``; see ``pricing.py:175-178``). Pro-rate
    each item's cost so the breakdown sums to the row's billed total
    while preserving the relative share implied by the estimates, and
    mark each item with ``cost_source: provider_billed_prorated`` so the
    UI can disclose that the figure is a computed split, not a per-model
    billing receipt.

    No-op when:
    - breakdown has 0 or 1 items (single-item rows already match by construction
      via the cost rollup path);
    - row is estimate-only (sums equal by construction);
    - row cost is 0 (no billed total to spread);
    - **all items already carry ``provider_billed`` and their sum matches the
      row total within 0.001**.
    """
    breakdown = row.get("modelBreakdown")
    if not isinstance(breakdown, list) or len(breakdown) <= 1:
        return
    cost_source = str(row.get("cost_source") or row.get("costSource") or "none")
    if cost_source not in _BILLED_COST_SOURCES:
        return
    row_cost = float(row.get("cost_usd") or row.get("costUsd") or 0.0)
    if row_cost <= 0.0:
        return

    def _item_cost(item: Mapping[str, Any]) -> float:
        return float(item.get("costUsd") or item.get("cost_usd") or 0.0)

    # Pro-rating exists only to correct drift between row.cost_usd and the
    # breakdown sum. If they already agree within 1/10th of a cent, there is no
    # drift to correct — no matter the per-item source. This covers two cases:
    #   - Pure billed: every item is provider_billed and matches by construction.
    #   - Mixed: some items provider_billed, others opensquilla_estimate; row
    #     uses SessionUsage.total_cost which sums billed-where-available plus
    #     estimate-where-not, so the breakdown sum matches and rebadging would
    #     be misleading.
    # Without this guard, mixed rows would falsely rebadge every item as
    # ``provider_billed_prorated`` and trigger the "split is estimated"
    # disclosure even though each item's individual source is already the
    # correct truth.
    items_for_check = [item for item in breakdown if isinstance(item, Mapping)]
    if items_for_check:
        items_sum = sum(_item_cost(item) for item in items_for_check)
        # 0.001 ≈ 1/10th of a cent; chosen because cost rollup rounds to 6
        # decimals (1e-6) and accumulating ~hundreds of float operations
        # rarely overshoots 1e-3. Tighter than that risks false-positives
        # under benign rounding; looser would let real drift slip through.
        if abs(items_sum - row_cost) <= 0.001:
            return

    estimated_sum = sum(_item_cost(item) for item in breakdown if isinstance(item, Mapping))
    dict_items = [item for item in breakdown if isinstance(item, dict)]

    def _stamp(item: dict, cost: float, estimate: float | None) -> None:
        item["costUsd"] = cost
        item["cost_usd"] = cost
        item["billedCostUsd"] = cost
        item["billed_cost_usd"] = cost
        if estimate is not None:
            item["estimatedCostUsd"] = round(estimate, 6)
            item["estimated_cost_usd"] = round(estimate, 6)
        item["costSource"] = "provider_billed_prorated"
        item["cost_source"] = "provider_billed_prorated"

    if estimated_sum <= 0.0:
        # All-zero estimates: split row cost equally. Absorb rounding loss
        # into the last item so the breakdown sums exactly to row_cost.
        n = len(dict_items)
        if n == 0:
            return
        equal = round(row_cost / float(n), 6)
        running = 0.0
        for idx, item in enumerate(dict_items):
            share = round(row_cost - running, 6) if idx == n - 1 else equal
            _stamp(item, share, None)
            running += share
        return

    scale = row_cost / estimated_sum
    running = 0.0
    last_idx = len(dict_items) - 1
    for idx, item in enumerate(dict_items):
        original = _item_cost(item)
        if idx == last_idx:
            # Last item absorbs the rounding remainder so sum == row_cost exactly.
            prorated = round(row_cost - running, 6)
        else:
            prorated = round(original * scale, 6)
        _stamp(item, prorated, original)
        running += prorated


def _append_tracker_only_rows(
    rows: list[dict[str, Any]],
    tracker_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge tracker rows into disk-loaded rows.

    Disk persistence (cost-rollup pipeline) records the final per-session model
    but no per-model breakdown. The in-memory tracker accumulates breakdown via
    ``SessionUsage._per_model`` while the session is alive. Without this merge,
    sessions that hit the billing path lose their breakdown on the next status
    fetch — the "auto · N models" UI never surfaces for auto-routed sessions
    even though the data is still in memory.

    Also reconciles per-model cost figures to the row total via
    ``_reconcile_breakdown_to_row`` so the expand-row sum equals the visible
    session cost for billed sessions (otherwise cache-discount blind estimates
    inflate the breakdown).
    """
    tracker_by_key = {tr["session"]: tr for tr in tracker_rows}
    seen = set()
    for row in rows:
        seen.add(row["session"])
        tracker_row = tracker_by_key.get(row["session"])
        if (
            tracker_row
            and tracker_row.get("modelBreakdown")
            and not row.get("modelBreakdown")
        ):
            row["modelBreakdown"] = tracker_row["modelBreakdown"]
        if (
            tracker_row
            and tracker_row.get("deploymentBreakdown")
            and not row.get("deploymentBreakdown")
        ):
            row["deploymentBreakdown"] = tracker_row["deploymentBreakdown"]
        if tracker_row and _row_can_overlay_tracker_totals(row, tracker_row):
            _overlay_tracker_totals(row, tracker_row)
        _reconcile_breakdown_to_row(row)
    return rows + [row for row in tracker_rows if row["session"] not in seen]


def _usage_totals(rows: list[dict[str, Any]]) -> dict[str, int | float]:
    total_in = sum(int(row["input_tokens"] or 0) for row in rows)
    total_out = sum(int(row["output_tokens"] or 0) for row in rows)
    total_cost = sum(float(row["cost_usd"] or 0.0) for row in rows)
    return {
        "input": total_in,
        "output": total_out,
        "cost": total_cost,
        "cache_read": sum(int(row["cache_read_tokens"] or 0) for row in rows),
        "cache_write": sum(int(row["cache_write_tokens"] or 0) for row in rows),
    }


@_d.method("usage.status", scope="operator.read")
async def _handle_usage_status(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    now_ms = _now_ms()
    tracker_rows = _tracker_rows(ctx, now_ms=now_ms)

    if ctx.session_manager is None:
        totals = _usage_totals(tracker_rows)
        return {
            "totalSessions": len(tracker_rows),
            "activeSessions": len(tracker_rows),
            "totalInputTokens": totals["input"],
            "totalOutputTokens": totals["output"],
            "totalTokens": totals["input"] + totals["output"],
            "totalCostUsd": round(float(totals["cost"]), 6),
            "totalCacheReadTokens": totals["cache_read"],
            "totalCacheWriteTokens": totals["cache_write"],
            "sessions": tracker_rows,
        }

    try:
        requested_session_key = None
        if isinstance(params, Mapping):
            requested_session_key = (
                params.get("sessionKey") or params.get("session_key") or params.get("key")
            )
        sessions = await ctx.session_manager.list_sessions()
        rows = []
        active = sum(1 for s in sessions if _field(s, "status", "") == "running")
        for s in sessions:
            input_tokens = _first_field(s, "input_tokens", "total_input_tokens", default=0) or 0
            output_tokens = _first_field(s, "output_tokens", "total_output_tokens", default=0) or 0
            cache_read = _field(s, "cache_read", 0) or 0
            cache_write = _field(s, "cache_write", 0) or 0
            cost_fields = _resolved_session_cost_fields(
                s,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            )

            # Resolve model: session record > model_override > config default
            session_model = _field(s, "model") or _field(s, "model_override")
            if not session_model and ctx.config:
                session_model = getattr(ctx.config.llm, "model", None)
            session_key = _field(s, "session_key", "unknown")
            context_status = await _context_status(
                s,
                ctx=ctx,
                session_key=session_key,
                model=session_model,
                allow_transcript_estimate=requested_session_key == session_key,
            )
            rows.append(
                _usage_row(
                    session_key=session_key,
                    model=session_model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    **cost_fields,
                    cache_read_tokens=cache_read,
                    cache_write_tokens=cache_write,
                    created_at=_field(s, "created_at"),
                    updated_at=_field(s, "updated_at"),
                    started_at=_field(s, "started_at"),
                    ended_at=_field(s, "ended_at"),
                    context_status=context_status,
                )
            )
        rows = _append_tracker_only_rows(rows, tracker_rows)
        totals = _usage_totals(rows)
        tracker_only_count = len(rows) - len(sessions)
        return {
            "totalSessions": len(rows),
            "activeSessions": active + tracker_only_count,
            "totalInputTokens": totals["input"],
            "totalOutputTokens": totals["output"],
            "totalTokens": totals["input"] + totals["output"],
            "totalCostUsd": round(float(totals["cost"]), 6),
            "totalCacheReadTokens": totals["cache_read"],
            "totalCacheWriteTokens": totals["cache_write"],
            "sessions": rows,
        }
    except (AttributeError, NotImplementedError):
        totals = _usage_totals(tracker_rows)
        return {
            "totalSessions": len(tracker_rows),
            "activeSessions": len(tracker_rows),
            "totalInputTokens": totals["input"],
            "totalOutputTokens": totals["output"],
            "totalTokens": totals["input"] + totals["output"],
            "totalCostUsd": round(float(totals["cost"]), 6),
            "totalCacheReadTokens": totals["cache_read"],
            "totalCacheWriteTokens": totals["cache_write"],
            "sessions": tracker_rows,
        }


@_d.method("usage.query", scope="operator.read")
async def _handle_usage_query(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    """Aggregate timestamped usage events for one explicit calendar range.

    This is intentionally additive.  ``usage.status`` and ``usage.cost`` keep
    their session-lifetime wire contracts for older clients while new clients
    opt in after discovering this method in the WebSocket hello payload.
    """

    if ctx.session_manager is None:
        raise RpcUnavailableError("Durable usage accounting is not configured")
    storage = get_session_storage(ctx.session_manager)
    if storage is None or not hasattr(storage, "query_usage_events"):
        raise RpcUnavailableError("Durable usage accounting is not available")
    try:
        return await query_usage_ledger(storage, params)
    except UsageQueryValidationError as exc:
        raise RpcHandlerError("INVALID_REQUEST", str(exc)) from exc


@_d.method("usage.cost", scope="operator.read")
async def _handle_usage_cost(params: dict | None, ctx: RpcContext) -> dict[str, Any]:
    now_ms = _now_ms()
    tracker_rows = _tracker_rows(ctx, now_ms=now_ms)

    if ctx.session_manager is None:
        return {
            "breakdown": tracker_rows,
            "totalCostUsd": round(float(_usage_totals(tracker_rows)["cost"]), 6),
        }
    try:
        sessions = await ctx.session_manager.list_sessions()
        breakdown = []
        for s in sessions:
            input_tokens = _first_field(s, "input_tokens", "total_input_tokens", default=0) or 0
            output_tokens = _first_field(s, "output_tokens", "total_output_tokens", default=0) or 0
            cache_read = _field(s, "cache_read", 0) or 0
            cache_write = _field(s, "cache_write", 0) or 0
            cost_fields = _resolved_session_cost_fields(
                s,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            )
            breakdown.append(
                _usage_row(
                    session_key=_field(s, "session_key", "unknown"),
                    model=_field(s, "model", "unknown"),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    **cost_fields,
                    cache_read_tokens=cache_read,
                    cache_write_tokens=cache_write,
                    created_at=_field(s, "created_at"),
                    updated_at=_field(s, "updated_at"),
                    started_at=_field(s, "started_at"),
                    ended_at=_field(s, "ended_at"),
                )
            )
        breakdown = _append_tracker_only_rows(breakdown, tracker_rows)
        return {
            "breakdown": breakdown,
            "totalCostUsd": round(float(_usage_totals(breakdown)["cost"]), 6),
        }
    except (AttributeError, NotImplementedError):
        return {
            "breakdown": tracker_rows,
            "totalCostUsd": round(float(_usage_totals(tracker_rows)["cost"]), 6),
        }
